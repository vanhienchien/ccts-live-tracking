"""
Cấu hình chung cho toàn bộ ứng dụng - đọc từ biến môi trường (.env), KHÔNG
hard-code thông tin nhạy cảm trong code (khác với bản demo main.py ban đầu).

Các biến môi trường cần thiết:
- SPREADSHEET_URL              : Link Google Sheet chứa tab "Users"
- GOOGLE_SERVICE_ACCOUNT_JSON  : Nội dung JSON của Service Account (dán nguyên
                                  văn dạng chuỗi 1 dòng) - dùng khi deploy (Render/
                                  Railway...) vì các nền tảng này thường không cho
                                  upload file, chỉ cho set biến môi trường.
  HOẶC
- GOOGLE_SERVICE_ACCOUNT_FILE  : Đường dẫn tới file service_account.json (dùng
                                  khi chạy local hoặc VPS có thể upload file).
- CCTS_USERNAME / CCTS_PASSWORD: Tài khoản đăng nhập hệ thống CCTS để cào ticket.
- SESSION_SECRET (tuỳ chọn)    : Không bắt buộc ở bản đơn giản này vì ta chỉ dùng
                                  session token ngẫu nhiên lưu trong bộ nhớ server.

Có thể tạo file `.env` ở thư mục gốc (dùng cùng python-dotenv) khi chạy local
để không phải export biến môi trường thủ công mỗi lần.
"""

import os
import json

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv là tuỳ chọn, không bắt buộc khi deploy (đã set env vars sẵn)

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_URL = os.environ.get("SPREADSHEET_URL", "").strip()

# Thông tin đăng nhập CCTS - nên set qua biến môi trường khi deploy thật,
# giá trị mặc định dưới đây chỉ để tiện chạy thử demo cục bộ.
CCTS_USER = os.environ.get("CCTS_USERNAME", "esmanager")
CCTS_PASS = os.environ.get("CCTS_PASSWORD", "Ccts123.")

SESSION_COOKIE_NAME = "session_token"

# Chuỗi bí mật tự đặt để bảo vệ endpoint /api/traccar (app Traccar Client di
# động gửi vị trí vào đây) - tránh người lạ gửi toạ độ giả vào hệ thống.
# Để trống thì endpoint không yêu cầu token (KHÔNG khuyến khích khi deploy thật).
TRACCAR_TOKEN = os.environ.get("TRACCAR_TOKEN", "").strip()

# Số giây làm mới dữ liệu ticket từ CCTS (mặc định 15 phút)
TICKET_REFRESH_SECONDS = int(os.environ.get("TICKET_REFRESH_SECONDS", 900))

_gc = None


def get_gspread_client():
    """Trả về gspread client đã xác thực (cache lại, chỉ khởi tạo 1 lần)."""
    global _gc
    if _gc is not None:
        return _gc

    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        if not os.path.exists(creds_path):
            raise RuntimeError(
                "Không tìm thấy thông tin Service Account. Hãy set biến môi trường "
                "GOOGLE_SERVICE_ACCOUNT_JSON (dán nguyên nội dung file json) hoặc "
                f"đặt file tại '{creds_path}' (GOOGLE_SERVICE_ACCOUNT_FILE)."
            )
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)

    _gc = gspread.authorize(creds)
    return _gc


def get_spreadsheet():
    if not SPREADSHEET_URL:
        raise RuntimeError("Chưa cấu hình biến môi trường SPREADSHEET_URL.")
    gc = get_gspread_client()
    return gc.open_by_url(SPREADSHEET_URL)