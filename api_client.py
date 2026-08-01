import asyncio
import base64
import hashlib
import io
import json
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import serialization


class CCTSClient:
    """Client đăng nhập & gọi API hệ thống CCTS (Pure API Version - Không Playwright)."""

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
        self.session.headers.update(self.base_headers)

    def _clean_and_load_public_key(self, pub_key_raw: str):
        """Làm sạch Public Key bị obfuscate 'power'."""
        clean_key = pub_key_raw.strip().replace("\r", "").replace("\n", "").replace("power", "").replace("POWER", "")
        clean_key = "".join(c for c in clean_key if c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
        
        missing = len(clean_key) % 4
        if missing:
            clean_key += "=" * (4 - missing)
        
        pem_key = f"-----BEGIN PUBLIC KEY-----\n{clean_key}\n-----END PUBLIC KEY-----"
        
        try:
            return serialization.load_pem_public_key(pem_key.encode('utf-8'))
        except Exception as e:
            print(f"[-] Load Public Key error: {e}")
            raise

    def _encrypt_password(self, pub_key_raw: str, plaintext_password: str) -> str:
        """Mã hóa theo đúng logic frontend: MD5 → RSA PKCS1v15"""
        # Bước 1: MD5
        md5_hash = hashlib.md5(plaintext_password.encode('utf-8')).hexdigest()
        
        # Bước 2: RSA
        public_key = self._clean_and_load_public_key(pub_key_raw)
        encrypted_bytes = public_key.encrypt(
            md5_hash.encode('utf-8'),
            padding.PKCS1v15()
        )
        return base64.b64encode(encrypted_bytes).decode('utf-8')

    async def login(self):
        """Đăng nhập thuần API."""
      
        def _fetch_key():
            return self.session.get(f"{self.base_url}/authen/index/getPublicKey")
        
        res_key = await asyncio.to_thread(_fetch_key)
        key_data = res_key.json()

        if str(key_data.get("code")) != "200":
            raise Exception(f"[-] Không lấy được Public Key: {key_data}")
            
        pub_key_raw = key_data.get("data")
        
        encrypted_pw = self._encrypt_password(pub_key_raw, self.password)

        payload = {
            "account": self.username,
            "password": encrypted_pw
        }
        
        def _post_login():
            return self.session.post(f"{self.base_url}/authen/login/validate", json=payload)
        
        res_login = await asyncio.to_thread(_post_login)
        login_data = res_login.json()

        if str(login_data.get("code")) != "200" or not login_data.get("success"):
            raise Exception(f"[-] Đăng nhập thất bại: {login_data}")

        self.token = login_data.get("token") or login_data.get("data", {}).get("token")
        self.ssoticket = self.session.cookies.get('ssoticket')

        if not self.token:
            raise Exception("[-] Không tìm thấy token trong response.")
            
        print(f"[✓] Đăng nhập thành công! Token: {self.token[:30]}...")

    async def _post(self, endpoint, payload=None):
        """POST helper - Tương thích hoàn toàn với ccts_data.py"""
        if payload is None:
            payload = {}
        
        # Đảm bảo có token
        if isinstance(payload, dict):
            payload = dict(payload)  # copy
            if "token" not in payload and self.token:
                payload["token"] = self.token

        url = f"{self.base_url}{endpoint}"

        def _execute():
            return self.session.post(url, json=payload, headers=self.base_headers)

        res = await asyncio.to_thread(_execute)
        
        try:
            res_data = res.json()
        except:
            res_data = {"code": "500", "message": "Invalid JSON", "success": False}

        # Tự động re-login nếu token hết hạn
        if res_data.get("code") in ["401", "403", "50001"] or not res_data.get("success", True):
            if "token" in str(res_data.get("message", "")).lower() or str(res_data.get("code")) in ["401", "50001"]:
                print("[!] Token hết hạn. Đang re-login...")
                await self.login()
                # Thử lại lần nữa
                if isinstance(payload, dict):
                    payload["token"] = self.token
                res = await asyncio.to_thread(_execute)
                res_data = res.json()

        return res_data

    # ------------------------------------------------------------------
    # Export ticket → Excel (dùng cho thống kê / visualization)
    # ------------------------------------------------------------------
    async def create_export_task(self, start_time, end_time, ticket_status=None, sla_timeout=None, offset=420):
        """
        Gửi yêu cầu xuất danh sách ticket sang Excel.
        start_time / end_time nhận giờ Việt Nam (YYYY-MM-DD HH:MM:SS),
        tự trừ 7 tiếng để khớp backend UTC.
        """
        try:
            start_dt = datetime.strptime(str(start_time).strip(), "%Y-%m-%d %H:%M:%S") - timedelta(hours=7)
            start_time_payload = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            start_time_payload = start_time

        try:
            end_dt = datetime.strptime(str(end_time).strip(), "%Y-%m-%d %H:%M:%S") - timedelta(hours=7)
            end_time_payload = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            end_time_payload = end_time

        request_data = {
            "createStartTime": start_time_payload,
            "createStopTime": end_time_payload,
        }
        if ticket_status:
            request_data["ticketStatus"] = ticket_status
        if sla_timeout is not None:
            request_data["slaTimeout"] = str(sla_timeout)

        payload = {
            "requestParam": json.dumps(request_data),
            "offset": offset,
        }
        print(f"[+] Gửi yêu cầu xuất (Status={ticket_status}, UTC start={start_time_payload})...")
        return await self._post("/ocpp/exportTask/addTicket", payload)

    async def get_export_tasks(self, page_num=1, page_size=10):
        """Lấy danh sách nhiệm vụ xuất dữ liệu."""
        payload = {"page": {"pageNum": page_num, "pageSize": page_size}}
        return await self._post("/ocpp/exportTask/list", payload)

    async def export_and_download_tickets(
        self,
        start_time,
        end_time,
        ticket_status=None,
        sla_timeout=None,
        offset=420,
        check_interval=5,
        timeout=180,
    ):
        """
        Quy trình đầy đủ: tạo task xuất → poll đến khi sẵn sàng → tải Excel
        trực tiếp vào RAM → trả về dict[str, DataFrame] các sheet.

        Sheets trả về (luôn có đủ key, sheet thiếu sẽ là DataFrame rỗng):
            Ticket Information, Appointment, Events Record,
            Solutions, Spare Parts Record, Additional information
        """
        res_export = await self.create_export_task(
            start_time, end_time,
            ticket_status=ticket_status,
            sla_timeout=sla_timeout,
            offset=offset,
        )
        if not res_export.get("success") and str(res_export.get("code")) not in ("200", "0"):
            print(f"[-] Thất bại khi gửi yêu cầu xuất: {res_export.get('message')}")
            return None

        print("[+] Đã gửi yêu cầu xuất. Đang chờ file sẵn sàng...")
        start_poll = time.time()
        download_url = None
        current_interval = check_interval
        status = "n/a"

        while time.time() - start_poll < timeout:
            res_tasks = await self.get_export_tasks(page_num=1, page_size=5)
            data = res_tasks.get("data", {})
            tasks = data.get("list", []) if isinstance(data, dict) else []
            if not isinstance(tasks, list):
                tasks = data.get("records", [])

            if tasks:
                latest = tasks[0]
                download_url = (
                    latest.get("fileUrl")
                    or latest.get("downloadUrl")
                    or latest.get("fileLocation")
                    or latest.get("accessLocation")
                )
                status = str(latest.get("status"))
                if status == "2" and download_url:
                    print(f"[✓] File Excel sẵn sàng: {download_url}")
                    break
                if latest.get("errorMsg"):
                    print(f"[-] Task xuất lỗi từ server: {latest.get('errorMsg')}")
                    return None

            print(f"[*] File chưa sẵn sàng (status={status}). Đợi {current_interval}s...")
            await asyncio.sleep(current_interval)
            current_interval = min(current_interval + 5, 20)

        if not download_url:
            print("[-] Timeout: file Excel chưa được tạo xong.")
            return None

        print("[+] Đang tải Excel vào RAM...")

        def _download():
            return self.session.get(download_url, timeout=120)

        try:
            res_file = await asyncio.to_thread(_download)
            if res_file.status_code != 200:
                print(f"[-] Lỗi tải file HTTP {res_file.status_code}")
                return None

            dfs = pd.read_excel(io.BytesIO(res_file.content), sheet_name=None)
            required = [
                "Ticket Information",
                "Appointment",
                "Events Record",
                "Solutions",
                "Spare Parts Record",
                "Additional information",
            ]
            for sheet in required:
                if sheet not in dfs:
                    dfs[sheet] = pd.DataFrame()

            print("[✓] Đọc Excel từ RAM thành công!")
            return dfs
        except Exception as e:
            print(f"[-] Lỗi xử lý Excel trong RAM: {e}")
            return None