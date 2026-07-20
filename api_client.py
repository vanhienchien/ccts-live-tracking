import asyncio
import json
import requests
from playwright.async_api import async_playwright


class CCTSClient:
    """Client đăng nhập & gọi API hệ thống CCTS.

    Dùng Playwright ASYNC API (không phải Sync API) vì toàn bộ ứng dụng
    (FastAPI/uvicorn) chạy trong 1 event loop asyncio đang hoạt động liên
    tục. Playwright Sync API vốn được thiết kế cho script chạy độc lập,
    không có event loop nào khác đang chạy cùng lúc - khi dùng chung với
    ASGI app (đặc biệt trên Windows, do khác biệt policy event loop), nó có
    thể xung đột dù được gọi trong thread riêng. Async API mới là cách dùng
    đúng và ổn định trong ngữ cảnh này.
    """

    def __init__(self, username="esmanager", password="Ccts123.", base_url="https://cloud.cnpowercore.com:8091"):
        self.username = username
        self.password = password
        self.base_url = base_url
        self.session = requests.Session()
        self.token = None
        self.ssoticket = None
        self.base_headers = {
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'en-US',
            'content-type': 'application/json;charset=UTF-8',
            'origin': 'https://console.cnpowercore.com',
            'priority': 'u=1, i',
            'referer': 'https://console.cnpowercore.com/',
            'sec-ch-ua': '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36'
        }

    async def login(self):
        """Đăng nhập bằng Playwright Async API, lấy token + cookie ssoticket."""
        print("[+] Đang khởi động Playwright để đăng nhập...")

        token_found = False

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 720})
            page = await context.new_page()

            async def handle_request_interception(route):
                nonlocal token_found
                request = route.request

                if "findCCTSTicket" in request.url and request.method == "POST":
                    try:
                        post_data = request.post_data
                        if post_data:
                            payload = json.loads(post_data)
                            if "token" in payload:
                                self.token = payload["token"]
                                token_found = True
                    except Exception:
                        pass

                await route.continue_()

            await page.route("**/findCCTSTicket**", handle_request_interception)

            try:
                await page.goto("https://console.cnpowercore.com/", timeout=90000)
                await page.wait_for_load_state("domcontentloaded")

                await page.fill("input[placeholder*='username or email']", self.username)
                await page.fill("input[placeholder*='Password']", self.password)
                await page.wait_for_timeout(1000)
                await page.click("button:has-text('Log in'), button[type='submit']")

                for _ in range(20):
                    if token_found:
                        break
                    await page.wait_for_timeout(500)

                cookies = await context.cookies()
                for c in cookies:
                    if c['name'] == 'ssoticket':
                        self.ssoticket = c['value']

            except Exception as e:
                print(f"[-] Lỗi Playwright: {e}")
            finally:
                await browser.close()

        if not self.token or not self.ssoticket:
            raise Exception("[-] Đăng nhập thất bại, không lấy được Token hoặc Cookie ssoticket.")

        self.session.headers.update(self.base_headers)
        self.session.cookies.set('ssoticket', self.ssoticket, domain='cloud.cnpowercore.com')
        print(f"[✓] Đăng nhập thành công! Token: {self.token[:15]}...")

    async def _post(self, endpoint, payload):
        """POST kèm tự động gắn Token, tự re-login nếu Token hết hạn.

        requests.post() bản thân là hàm đồng bộ (blocking) - không xung đột
        kiểu asyncio như Playwright Sync API, nhưng vẫn nên chạy qua
        asyncio.to_thread() để không "đứng hình" event loop trong lúc chờ
        mạng, đặc biệt khi có nhiều client WebSocket khác đang kết nối."""
        url = f"{self.base_url}{endpoint}"
        payload['token'] = self.token

        res = await asyncio.to_thread(self.session.post, url, json=payload)
        res_data = res.json()

        if res_data.get("code") in ["401", "403", "50001"] or not res_data.get("success", True):
            if "token" in str(res_data.get("message", "")).lower() or res_data.get("code") in ["401", "50001"]:
                print("[!] Token nội bộ đã hết hạn. Đang gọi Playwright Re-login...")
                await self.login()
                payload['token'] = self.token
                res = await asyncio.to_thread(self.session.post, url, json=payload)
                res_data = res.json()

        return res_data