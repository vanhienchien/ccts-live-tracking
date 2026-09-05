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

# ==================== NGUỒN DỮ LIỆU TĨNH (GitHub) ====================
# Đọc TRỰC TIẾP từ GitHub qua API tại runtime (không đóng gói khi deploy):
#   - StationData.csv          : toạ độ + KT phụ trách + khu vực
#   - ChargePointModels.json   : map model trụ sạc
#   - EngineerCoords.json      : toạ độ nhà KT / team lead
# Chỉ cần sửa file & push GitHub là app tự nhận dữ liệu mới (cache TTL),
# KHÔNG CẦN deploy lại Render.
#
# GITHUB_DATA_REPO: dạng "ten-tai-khoan/ten-repo"
# Khuyến nghị: repo data TÁCH RIÊNG khỏi repo code app (tránh Auto-Deploy).
GITHUB_DATA_REPO = os.environ.get("GITHUB_DATA_REPO", "").strip()
GITHUB_DATA_BRANCH = os.environ.get("GITHUB_DATA_BRANCH", "main").strip()
# Chỉ cần nếu repo data là PRIVATE - PAT quyền "Contents: Read-only".
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

GITHUB_STATION_DATA_CSV_PATH = os.environ.get(
    "GITHUB_STATION_DATA_CSV_PATH", "StationData.csv"
).strip()
GITHUB_CP_MODEL_JSON_PATH = os.environ.get(
    "GITHUB_CP_MODEL_JSON_PATH", "ChargePointModels.json"
).strip()
GITHUB_ENGINEER_COORDS_JSON_PATH = os.environ.get(
    "GITHUB_ENGINEER_COORDS_JSON_PATH", "EngineerCoords.json"
).strip()

# ==================== CCTS ACCOUNTS ====================
CCTS_ACCOUNTS = [
    {
        "username": os.environ.get("CCTS_USERNAME_ES", "esmanager"),
        "password": os.environ.get("CCTS_PASSWORD", "Ccts123."),
        "role": "esmanager"   # để phân biệt nếu cần
    },
    {
        "username": os.environ.get("CCTS_USERNAME_ITS", "its_frontdesk 04"),
        "password": os.environ.get("CCTS_PASSWORD_its", "Duynam123."),
        "role": "itsmanagerfrondesk"
    },
    # {
    #     "username": os.environ.get("CCTS_USERNAME_ES1", "esmanager_2"),
    #     "password": os.environ.get("CCTS_PASSWORD1", "Hienchien123."),
    #     "role": "esmanager"   # để phân biệt nếu cần
    # },
]
SESSION_COOKIE_NAME = "session_token"

# Chuỗi bí mật ký JWT (cookie web + token mobile). BẮT BUỘC đặt trên Render —
# thiếu thì mỗi lần restart sinh khoá ngẫu nhiên mới, khiến MỌI session cũ
# (web + mobile) bị đăng xuất hàng loạt.
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY", "").strip()

# Số giây làm mới dữ liệu ticket từ CCTS (mặc định 15 phút)
TICKET_REFRESH_SECONDS = int(os.environ.get("TICKET_REFRESH_SECONDS", 600))

# ==================== CẢNH BÁO TELEGRAM (SLA sắp vỡ hạn) ====================
# Đặt 2 biến này trên Render > Environment để bật cảnh báo tự động cho ticket
# sắp vỡ SLA 48h (còn ~0-3h). Thiếu 1 trong 2 -> tính năng tự tắt (chỉ log ra
# console), không ảnh hưởng phần còn lại của app.
#   TELEGRAM_BOT_TOKEN : token bot lấy từ @BotFather trên Telegram
#   TELEGRAM_CHAT_ID   : chat ID nhận tin (cá nhân hoặc group)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

_gc = None
_spreadsheet = None  # cache Spreadsheet object - open_by_url() gọi fetch_sheet_metadata()
                      # (1 API call riêng) mỗi lần, nên KHÔNG mở lại mỗi request.


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


def get_spreadsheet(force_reopen: bool = False):
    """Mở Spreadsheet (cache lại sau lần đầu).

    force_reopen=True: bỏ qua cache, mở lại từ đầu - dùng khi lần gọi trước
    lỗi vì token/cache phía gspread có thể đã hỏng, không chỉ do mạng/API
    Google tạm gián đoạn."""
    global _spreadsheet
    if not SPREADSHEET_URL:
        raise RuntimeError("Chưa cấu hình biến môi trường SPREADSHEET_URL.")
    if _spreadsheet is not None and not force_reopen:
        return _spreadsheet
    gc = get_gspread_client()
    _spreadsheet = gc.open_by_url(SPREADSHEET_URL)
    return _spreadsheet