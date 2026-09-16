import asyncio
import base64
import gc
import hashlib
import io
import json
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import serialization

# ===== AN TOÀN KHI BỊ "ĐÁ" (code 512 — đăng nhập nơi khác) =====
# Đồng bộ với bản vá 2026-09-15 của scripts/api_client.py (client dùng cho
# các script tự động hoá chạy trên máy Windows). CCTS chỉ cho 1 phiên/tài
# khoản trên web này — nếu web app (Render) và ai đó đăng nhập tay trên
# console.cnpowercore.com dùng CHUNG account thì CHẮC CHẮN đá nhau.
#
# 2 phát hiện quan trọng khi đối chiếu lại file NÀY với bản đã vá:
#   1) BUG "success" dạng CHUỖI: server có lúc trả success="false" (chuỗi,
#      không phải bool) — code cũ dùng `not res_data.get("success", True)`
#      để phát hiện lỗi, nhưng `not "false"` = False trong Python (chuỗi
#      không rỗng luôn truthy) -> điều kiện này KHÔNG BAO GIỜ bắt được lỗi
#      qua nhánh "success" nếu code không nằm sẵn trong ["401","403",
#      "50001"]. Hệ quả: bị đá (code 512, không nằm trong danh sách đó) từ
#      TRƯỚC ĐẾN NAY không hề được phát hiện/relogin ở file này — client sẽ
#      lặng lẽ trả dữ liệu rỗng vô thời hạn cho tới khi tiến trình Render bị
#      restart. Sửa bằng _is_success() chuẩn hoá cả 2 dạng bool/chuỗi.
#   2) Tự động relogin NGAY LẬP TỨC khi bị đá là hành vi RỦI RO (dù bug #1
#      khiến nó chưa từng thực sự chạy tới cho code 512): việc đăng nhập lại
#      đó lại đá ngược phiên người vừa đăng nhập tay ra. Sau khi sửa bug #1,
#      nếu giữ nguyên hành vi relogin-ngay sẽ kích hoạt đúng vòng lặp đá qua
#      đá lại đó. Vì vậy: chỉ tự relogin NGAY cho lỗi hết hạn tự nhiên
#      (không có ai tranh chấp phiên); riêng code 512 (bị đá thật) thì KHÔNG
#      tự relogin trong _post() nữa — export_and_download_tickets() tự xử
#      lý an toàn (đợi rồi relogin, xem _wait_and_relogin_after_kick()); các
#      hàm khác (search_ticket, get_ticket_follow_records...) sẽ trả thẳng
#      response 512 về — nơi gọi (ccts_data.py, qua ClientPool) coi đó là 1
#      lượt cào rỗng và tự thử lại ở chu kỳ 15 phút kế tiếp như bình thường.
SESSION_INVALID_CODES = {"401", "403", "50001", "512"}
SESSION_INVALID_KEYWORDS = ("token", "logged in elsewhere", "please log in again")
EXPORT_KICK_WAIT_SECONDS = 30
MAX_EXPORT_RELOGIN_ATTEMPTS = 5


def _is_success(res_data: dict) -> bool:
    """Chuẩn hoá field 'success' — server có lúc trả CHUỖI ("true"/"false")
    thay vì boolean JSON thật (xác nhận qua logs/api_anomalies_*.log của
    scripts/api_client.py, chữ ký lỗi 512 ngày 2026-09-12: chuỗi "false").
    So sánh truthy trực tiếp trên chuỗi là SAI vì "false" (chuỗi không rỗng)
    vẫn truthy trong Python."""
    val = res_data.get("success", True)
    if isinstance(val, str):
        return val.strip().lower() == "true"
    return bool(val)


def _is_session_invalidated(res_data: dict) -> bool:
    if _is_success(res_data):
        return False
    if str(res_data.get("code")) in SESSION_INVALID_CODES:
        return True
    message = str(res_data.get("message", "")).lower()
    return any(kw in message for kw in SESSION_INVALID_KEYWORDS)


def _extract_task_pk(res_data: dict) -> Optional[str]:
    """Cố lấy taskPk của export task vừa tạo trực tiếp từ response của
    createExportTask (nếu server trả về). Trả None nếu không tìm thấy — nơi
    gọi sẽ tự chụp nhanh bằng cách gọi lại exportTask/list ngay sau đó."""
    data = res_data.get("data")
    if isinstance(data, dict):
        for key in ("taskPk", "taskPK", "id", "pk", "exportTaskPk"):
            val = data.get(key)
            if val is not None:
                return str(val)
    elif isinstance(data, (str, int)):
        return str(data)
    return None


class CCTSClient:
    """Client đăng nhập & gọi API hệ thống CCTS (Pure API Version - Không Playwright)."""

    # (connect_timeout, read_timeout) áp cho MỌI request tới CCTS. Không có
    # timeout kết hợp với CCTS_API_LOCK toàn cục (ccts_shared.py) từng có
    # nghĩa là 1 kết nối treo sẽ đơ luôn cả bản đồ lẫn thống kê tới khi
    # Render kill tiến trình.
    REQUEST_TIMEOUT = (5, 30)

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
            return self.session.get(
                f"{self.base_url}/authen/index/getPublicKey", timeout=self.REQUEST_TIMEOUT
            )
        
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
            return self.session.post(
                f"{self.base_url}/authen/login/validate", json=payload, timeout=self.REQUEST_TIMEOUT
            )
        
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
        """POST helper - Tương thích hoàn toàn với ccts_data.py.

        AN TOÀN KHI BỊ ĐÁ (2026-09-15, đồng bộ scripts/api_client.py): chỉ
        tự động đăng nhập lại NGAY khi phiên hết hạn TỰ NHIÊN (401/403/
        50001 hoặc message chứa "token"/"please log in again") — trường hợp
        này không có ai tranh chấp phiên nên an toàn để tự phục hồi ngay.
        Khi bị "đá" thật sự (code 512 — đăng nhập nơi khác), KHÔNG tự đăng
        nhập lại ở đây nữa: tự relogin ngay lập tức chính là hành vi đá
        ngược lại phiên người vừa đăng nhập, gây vòng lặp đá qua đá lại rất
        rủi ro cho 1 web app chạy nền liên tục như thế này. Trả thẳng
        response 512 về cho nơi gọi (export_and_download_tickets() tự xử lý
        an toàn qua _wait_and_relogin_after_kick(); các hàm khác coi đây là
        1 lượt gọi rỗng, tự thử lại ở chu kỳ sau)."""
        if payload is None:
            payload = {}

        # Đảm bảo có token
        if isinstance(payload, dict):
            payload = dict(payload)  # copy
            if "token" not in payload and self.token:
                payload["token"] = self.token

        url = f"{self.base_url}{endpoint}"

        def _execute():
            return self.session.post(
                url, json=payload, headers=self.base_headers, timeout=self.REQUEST_TIMEOUT
            )

        res = await asyncio.to_thread(_execute)

        try:
            res_data = res.json()
        except (ValueError, json.JSONDecodeError) as e:
            print(f"[-] Phản hồi không phải JSON hợp lệ từ {endpoint}: {e!r}")
            res_data = {"code": "500", "message": "Invalid JSON", "success": False}

        if _is_session_invalidated(res_data):
            code_str = str(res_data.get("code"))
            was_kicked = code_str == "512"

            if was_kicked:
                print(f"[!] Tài khoản '{self.username}' bị đăng nhập nơi khác (code 512) khi gọi "
                      f"{endpoint}. KHÔNG tự đăng nhập lại ở đây (tránh đá ngược phiên vừa đăng "
                      f"nhập) — trả kết quả 512 về cho nơi gọi tự xử lý.")
                return res_data

            print(f"[!] Phiên đăng nhập hết hạn tự nhiên (code={res_data.get('code')!r}, "
                  f"message={res_data.get('message')!r}) cho tài khoản [{self.username}]. "
                  f"Đang re-login...")
            await self.login()
            # Thử lại lần nữa
            if isinstance(payload, dict):
                payload["token"] = self.token
            res = await asyncio.to_thread(_execute)
            try:
                res_data = res.json()
            except (ValueError, json.JSONDecodeError) as e:
                print(f"[-] Phản hồi không phải JSON hợp lệ từ {endpoint} (sau re-login): {e!r}")
                res_data = {"code": "500", "message": "Invalid JSON", "success": False}

        return res_data

    async def _wait_and_relogin_after_kick(self, wait_seconds: int = EXPORT_KICK_WAIT_SECONDS) -> bool:
        """Chờ `wait_seconds` giây rồi đăng nhập lại "êm" đúng 1 lần sau khi
        bị đá (code 512). KHÔNG dùng lại kiểu đăng nhập lại ngay lập tức —
        đó chính là hành vi rủi ro đã bỏ khỏi _post(). Trả về True nếu
        đăng nhập lại thành công, False nếu vẫn thất bại."""
        print(f"[i] Bị đá — đợi {wait_seconds}s rồi đăng nhập lại (tránh đá ngược lại phiên vừa "
              f"đăng nhập)...")
        await asyncio.sleep(wait_seconds)
        try:
            await self.login()
            return True
        except Exception as e:
            print(f"[!] Đăng nhập lại thất bại sau khi bị đá: {e}")
            return False

    @staticmethod
    def _pick_own_export_task(tasks: list, task_pk=None, file_name: str = None) -> Optional[dict]:
        """Tìm ĐÚNG task export của mình trong danh sách server trả về —
        KHÔNG BAO GIỜ lấy đại tasks[0]. Danh sách export task là CHUNG cho
        account, sắp mới nhất lên đầu; nếu trong lúc mình đang chờ/bị đá mà
        có người khác (vd đăng nhập tay trên console.cnpowercore.com) export
        1 file mới, file đó sẽ nhảy lên đầu danh sách -> lấy tasks[0] sẽ trả
        NHẦM FILE của người khác thay vì file mình vừa yêu cầu (bug thực tế
        đã xảy ra ở scripts/api_client.py, vá ngày 2026-09-15). `taskPk` là
        định danh duy nhất mỗi task — xác nhận từ chính source code frontend
        thật (component/dialog/exportDialog.js dùng đúng field này để khớp
        task khi nhận cập nhật qua WebSocket)."""
        if task_pk is not None:
            for t in tasks:
                if str(t.get("taskPk")) == str(task_pk):
                    return t
        if file_name:
            for t in tasks:
                if t.get("fileName") == file_name:
                    return t
        return None

    # ------------------------------------------------------------------
    # Tra cứu chi tiết ticket (search + lịch sử trạng thái) — dùng để làm
    # giàu dữ liệu cho các ticket "Open" đang overdue trên bản đồ.
    # ------------------------------------------------------------------
    async def get_ticket_follow_records(self, ticket_pk, page_num=1, page_size=10):
        """Lấy lịch sử xử lý (follow record) thô của 1 ticket.
        Endpoint thật: POST /ccts/cctsTicketHistory/list
        Payload: {"cctsTicketPk": ..., "page": {"pageNum":1,"pageSize":10}, "token": ...}
        ("token" được _post tự gắn vào, không cần truyền tay)."""
        payload = {
            "cctsTicketPk": ticket_pk,
            "page": {"pageNum": page_num, "pageSize": page_size},
        }
        return await self._post("/ccts/cctsTicketHistory/list", payload)

    async def get_ticket_timeline(self, ticket_pk):
        """
        Lấy danh sách lịch sử xử lý của Ticket và parse nội dung JSON trong 'content'
        thành cấu trúc: followRecordStatus, followRecordContent, createTime.
        """
        res = await self.get_ticket_follow_records(ticket_pk)
        data = res.get("data", {})
        records = data.get("list", []) if isinstance(data, dict) else []
        if not isinstance(records, list):
            records = data.get("records", []) if isinstance(data, dict) else []

        timeline = []
        for item in records:
            create_time = item.get("createTime", "")
            raw_content = item.get("content", "")
            status = None
            content_text = ""
            if raw_content:
                try:
                    content_dict = json.loads(raw_content)
                    status = content_dict.get("followRecordStatus")
                    content_text = content_dict.get("followRecordContent", "")
                except (json.JSONDecodeError, TypeError):
                    pass
            # Nếu không tìm thấy followRecordStatus (hoặc content rỗng), đây là bản ghi tạo ticket ban đầu
            if not status:
                timeline.append({
                    "followRecordStatus": "Open",
                    "createTime": create_time,
                })
            else:
                entry = {
                    "followRecordStatus": status,
                    "createTime": create_time,
                }
                if content_text:
                    entry["followRecordContent"] = content_text
                timeline.append(entry)
        return timeline

    async def search_ticket(self, ticket_name_or_id):
        """
        Tìm kiếm ticket theo ID hoặc Name, sau đó tự động lấy danh sách
        lịch sử trạng thái (timeline) đã được bóc tách dữ liệu.
        """
        endpoint = "/ccts/cctsTicket/findCCTSTicket"
        payload_by_id = {
            "page": {"pageNum": 1, "pageSize": 10},
            "cctsTicketId": ticket_name_or_id,
            "timezoneOffset": 420,
        }
        res_id = await self._post(endpoint, payload_by_id)
        data_id = res_id.get("data", {})
        list_id = data_id.get("list", []) if isinstance(data_id, dict) else data_id
        if not isinstance(list_id, list):
            list_id = data_id.get("records", [])

        ticket_info = None
        if list_id:
            ticket_info = list_id[0]
        else:
            payload_by_name = {
                "page": {"pageNum": 1, "pageSize": 10},
                "cctsTicketName": ticket_name_or_id,
                "timezoneOffset": 420,
            }
            res_name = await self._post(endpoint, payload_by_name)
            data_name = res_name.get("data", {})
            list_name = data_name.get("list", []) if isinstance(data_name, dict) else data_name
            if not isinstance(list_name, list):
                list_name = data_name.get("records", [])
            if list_name:
                ticket_info = list_name[0]

        if not ticket_info:
            return None

        # Trích xuất cctsTicketPk để gọi API lấy danh sách lịch sử
        ccts_ticket_pk = ticket_info.get("cctsTicketPk")
        timeline = await self.get_ticket_timeline(ccts_ticket_pk) if ccts_ticket_pk else []
        return {
            "ticket": ticket_info,
            "timeline": timeline,
        }

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
        usecols_map: dict[str, list[str]] | None = None,
    ):
        """
        Quy trình đầy đủ: tạo task xuất → poll đến khi sẵn sàng → tải Excel
        trực tiếp vào RAM → trả về dict[str, DataFrame] các sheet.

        Sheets trả về (luôn có đủ key, sheet thiếu sẽ là DataFrame rỗng):
            Ticket Information, Appointment, Events Record,
            Solutions, Spare Parts Record, Additional information

        `usecols_map`: dict tùy chọn {tên_sheet: [tên_cột,...]} — nếu có,
        MỖI SHEET sẽ được parse riêng lẻ (không dùng sheet_name=None) và chỉ
        giữ lại các cột khớp whitelist NGAY KHI PARSE, thay vì parse full-
        width rồi mới cắt sau. Đây là chỗ tốn RAM nhất khi file Excel lớn
        (60 ngày dữ liệu): trước đây `pd.read_excel(sheet_name=None)` parse
        CẢ 6 sheet cùng lúc và giữ full-width đồng thời trong RAM. Parse
        tuần tự + trim ngay từng sheet giúp không bao giờ có quá 1 sheet
        full-width sống cùng lúc.
        """
        res_export = await self.create_export_task(
            start_time, end_time,
            ticket_status=ticket_status,
            sla_timeout=sla_timeout,
            offset=offset,
        )

        kick_recoveries = 0
        task_pk = None
        file_name = None

        # AN TOÀN KHI BỊ ĐÁ NGAY LÚC GỬI YÊU CẦU (2026-09-15, đồng bộ
        # scripts/api_client.py): _post() không còn tự relogin cho code 512
        # nữa (xem _post()) -> tự xử lý ở đây bằng cách đợi rồi gửi lại. An
        # toàn vì CHƯA có task nào được server ghi nhận, không có rủi ro
        # trùng file.
        while _is_session_invalidated(res_export) and str(res_export.get("code")) == "512":
            kick_recoveries += 1
            if kick_recoveries > MAX_EXPORT_RELOGIN_ATTEMPTS:
                print(f"[!] Bị đá liên tục {MAX_EXPORT_RELOGIN_ATTEMPTS} lần ngay lúc gửi yêu cầu "
                      f"export cho [{self.username}]. Dừng lại — chưa có export nào được tạo trên "
                      f"server nên không mất dữ liệu.")
                return None
            await self._wait_and_relogin_after_kick()
            res_export = await self.create_export_task(
                start_time, end_time,
                ticket_status=ticket_status,
                sla_timeout=sla_timeout,
                offset=offset,
            )

        if not _is_success(res_export) and str(res_export.get("code")) not in ("200", "0"):
            print(f"[-] Thất bại khi gửi yêu cầu xuất: {res_export.get('message')}")
            return None

        # Cố lấy taskPk NGAY từ response tạo task (nếu server trả về) — cách
        # chính xác nhất, không có khoảng hở đua tranh với export của người
        # khác. Nếu không có, chụp nhanh NGAY LẬP TỨC bản ghi mới nhất ngay
        # sau khi tạo (khoảng hở đua tranh chỉ còn đúng 1 round-trip này).
        task_pk = _extract_task_pk(res_export)
        if not task_pk:
            snap = await self.get_export_tasks(page_num=1, page_size=5)
            if _is_success(snap):
                snap_data = snap.get("data", {})
                snap_tasks = snap_data.get("list", []) if isinstance(snap_data, dict) else []
                if not isinstance(snap_tasks, list):
                    snap_tasks = snap_data.get("records", [])
                if snap_tasks:
                    task_pk = snap_tasks[0].get("taskPk")
                    file_name = snap_tasks[0].get("fileName")

        if not task_pk and not file_name:
            print(f"[!] CẢNH BÁO: không xác định được taskPk/fileName của export vừa tạo cho "
                  f"[{self.username}] — nếu có người khác export file khác trong lúc chờ, có thể "
                  f"lấy nhầm file.")

        print(f"[+] Đã gửi yêu cầu xuất (taskPk={task_pk}). Đang chờ file sẵn sàng...")
        start_poll = time.time()
        download_url = None
        current_interval = check_interval
        status = "n/a"

        while time.time() - start_poll < timeout:
            res_tasks = await self.get_export_tasks(page_num=1, page_size=20)

            if _is_session_invalidated(res_tasks) and str(res_tasks.get("code")) == "512":
                kick_recoveries += 1
                if kick_recoveries > MAX_EXPORT_RELOGIN_ATTEMPTS:
                    print(f"[!] Bị đá liên tục {MAX_EXPORT_RELOGIN_ATTEMPTS} lần khi đang chờ file "
                          f"export cho [{self.username}]. Dừng lại.")
                    return None
                # Task đã được server ghi nhận & vẫn xử lý nền dù phiên bị đá hay
                # không -> KHÔNG gửi lại createExportTask, chỉ đăng nhập lại rồi hỏi lại.
                await self._wait_and_relogin_after_kick()
                continue

            data = res_tasks.get("data", {})
            tasks = data.get("list", []) if isinstance(data, dict) else []
            if not isinstance(tasks, list):
                tasks = data.get("records", [])

            if not tasks:
                await asyncio.sleep(current_interval)
                continue

            # QUAN TRỌNG: KHÔNG lấy đại tasks[0] — có thể là task của người khác vừa
            # export trong lúc mình đang chờ/bị đá (xem _pick_own_export_task()).
            latest = self._pick_own_export_task(tasks, task_pk=task_pk, file_name=file_name)

            if latest is None:
                if task_pk or file_name:
                    print(f"[*] Chưa thấy task export của mình (taskPk={task_pk}) trong "
                          f"{len(tasks)} task gần nhất. Đợi {current_interval}s...")
                    await asyncio.sleep(current_interval)
                    current_interval = min(current_interval + 5, 20)
                    continue
                # Không có định danh nào để khớp -> fallback tasks[0] kèm cảnh báo.
                print(f"[!] CẢNH BÁO: không có taskPk/fileName để khớp chính xác cho "
                      f"[{self.username}] — dùng tạm task mới nhất trong danh sách.")
                latest = tasks[0]

            download_url = (
                latest.get("fileUrl")
                or latest.get("downloadUrl")
                or latest.get("fileLocation")
                or latest.get("accessLocation")
            )
            status = str(latest.get("status"))
            if status == "2" and download_url:
                print(f"[✓] File Excel sẵn sàng (taskPk={latest.get('taskPk')}): {download_url}")
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

        required = [
            "Ticket Information",
            "Appointment",
            "Events Record",
            "Solutions",
            "Spare Parts Record",
            "Additional information",
        ]

        try:
            res_file = await asyncio.to_thread(_download)
            if res_file.status_code != 200:
                print(f"[-] Lỗi tải file HTTP {res_file.status_code}")
                return None

            file_bytes = res_file.content
            # Giải phóng response object ngay (nó còn giữ 1 bản cache nội bộ
            # của cùng bytes) — chỉ giữ lại `file_bytes` cần dùng.
            del res_file
            bio = io.BytesIO(file_bytes)
            del file_bytes

            excel_file = pd.ExcelFile(bio, engine="openpyxl")

            dfs: dict[str, pd.DataFrame] = {}
            for sheet in required:
                if sheet not in excel_file.sheet_names:
                    dfs[sheet] = pd.DataFrame()
                    continue

                sheet_df = excel_file.parse(sheet_name=sheet)

                keep = usecols_map.get(sheet) if usecols_map else None
                if keep:
                    cols_present = [c for c in sheet_df.columns if str(c).strip() in keep]
                    if cols_present:
                        trimmed = sheet_df.loc[:, cols_present].copy()
                        del sheet_df
                        sheet_df = trimmed

                dfs[sheet] = sheet_df

            excel_file.close()
            del excel_file, bio
            gc.collect()

            print("[✓] Đọc Excel từ RAM thành công!")
            return dfs
        except Exception as e:
            print(f"[-] Lỗi xử lý Excel trong RAM: {e}")
            return None