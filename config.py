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
# Toạ độ trạm / phân công kỹ thuật viên / model trụ sạc được đọc TRỰC TIẾP từ
# GitHub qua API tại runtime (không đóng gói cùng code khi deploy) - để bạn
# chỉ cần sửa file & push GitHub là app tự nhận dữ liệu mới ở lần làm mới kế
# tiếp, KHÔNG CẦN deploy lại toàn bộ project trên Render.
#
# GITHUB_DATA_REPO: dạng "ten-tai-khoan/ten-repo"
# Khuyến nghị: để repo này (chứa 3 file data) TÁCH RIÊNG khỏi repo code app -
# như vậy push data sẽ không bao giờ vô tình kích hoạt Render tự deploy lại.
GITHUB_DATA_REPO = os.environ.get("GITHUB_DATA_REPO", "").strip()
GITHUB_DATA_BRANCH = os.environ.get("GITHUB_DATA_BRANCH", "main").strip()
# Chỉ cần nếu repo data là PRIVATE - tạo Personal Access Token (Settings > Developer
# settings > Fine-grained tokens), quyền "Contents: Read-only" trên đúng repo đó.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

GITHUB_STATIONS_JSON_PATH = os.environ.get("GITHUB_STATIONS_JSON_PATH", "StationAssignments.json").strip()
GITHUB_COORDS_JSON_PATH = os.environ.get("GITHUB_COORDS_JSON_PATH", "StationCoords.json").strip()
GITHUB_CP_MODEL_JSON_PATH = os.environ.get("GITHUB_CP_MODEL_JSON_PATH", "ChargePointModels.json").strip()

# ==================== CCTS ACCOUNTS ====================
CCTS_ACCOUNTS = [
    {
        "username": os.environ.get("CCTS_USERNAME_ES", "esmanager"),
        "password": os.environ.get("CCTS_PASSWORD", "Ccts123."),
        "role": "esmanager"   # để phân biệt nếu cần
    },
    # {
    #     "username": os.environ.get("CCTS_USERNAME_ITS", "its_frontdesk 04"),
    #     "password": os.environ.get("CCTS_PASSWORD_its", "Duynam123."),
    #     "role": "itsmanagerfrondesk"
    # },
    # {
    #     "username": os.environ.get("CCTS_USERNAME_ES1", "esmanager_2"),
    #     "password": os.environ.get("CCTS_PASSWORD1", "Hienchien123."),
    #     "role": "esmanager"   # để phân biệt nếu cần
    # },
]
SESSION_COOKIE_NAME = "session_token"

# Chuỗi bí mật tự đặt để bảo vệ endpoint /api/traccar (app Traccar Client di
# động gửi vị trí vào đây) - tránh người lạ gửi toạ độ giả vào hệ thống.
# Để trống thì endpoint không yêu cầu token (KHÔNG khuyến khích khi deploy thật).
TRACCAR_TOKEN = os.environ.get("TRACCAR_TOKEN", "").strip()

# Số giây làm mới dữ liệu ticket từ CCTS (mặc định 15 phút)
TICKET_REFRESH_SECONDS = int(os.environ.get("TICKET_REFRESH_SECONDS", 600))

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