"""
Module xác thực đơn giản - đọc trực tiếp từ Google Sheets, KHÔNG mã hoá mật
khẩu (theo yêu cầu đơn giản hoá). Không có giao diện quản lý tài khoản trong
web - Admin tự thêm/sửa/xoá tài khoản trực tiếp trên Google Sheets.

Cấu trúc sheet "Users" - đúng 5 cột theo thứ tự:
    full_name | username | password | role | region

Ví dụ:
    Nguyễn Văn A | vana    | 123456  | Kỹ thuật | Miền Nam
    Trần Thị B   | tranb   | abcdef  | Điều phối khu vực | Miền Nam
    ...

Cột "role" (chức vụ) phải là 1 trong 5 giá trị sau (không phân biệt hoa/thường,
tự động bỏ khoảng trắng thừa): Kỹ thuật, Điều phối khu vực, Điều hành,
Giám đốc, Admin.

Phân quyền xem vị trí:
- Admin: xem được TOÀN BỘ (kể cả Admin khác).
- Các vai trò khác: chỉ xem được các cấp bậc THẤP HƠN mình (không phân biệt
  khu vực), cộng với vị trí của chính mình.
"""

import time

import gspread
import pandas as pd
from gspread_dataframe import get_as_dataframe

from config import get_spreadsheet

# Mã lỗi HTTP coi là TẠM THỜI phía Google Sheets API (đơ/quá tải vài giây,
# không phải do app sai) - đáng để thử lại thay vì trả lỗi 500 ngay cho
# người dùng đang đăng nhập. Lỗi khác (401/403 sai quyền, 404 sheet không
# tồn tại...) thì raise ngay, retry không giải quyết được gì.
_TRANSIENT_API_STATUS = {429, 500, 502, 503, 504}

USERS_SHEET = "Users"
USERS_COLUMNS = ["full_name", "username", "password", "role", "region"]

ROLE_LEVELS = {
    "kỹ thuật": 1,
    "điều phối khu vực": 2,
    "điều hành": 3,
    "giám đốc": 4,
    "admin": 5,
}

# Nhãn hiển thị đúng chuẩn (chữ hoa/thường) - dùng cho dropdown chọn vai trò
# trong bảng cấp quyền Admin, và để LUÔN GHI ĐÚNG 1 CÁCH VIẾT thống nhất
# xuống Sheet dù người dùng gõ hoa/thường lẫn lộn.
ROLE_LABELS = ["Kỹ thuật", "Điều phối khu vực", "Điều hành", "Giám đốc", "Admin"]
_ROLE_LABEL_BY_LOWER = {r.lower(): r for r in ROLE_LABELS}

CACHE_TTL_SECONDS = 60  # cache danh sách user để tránh gọi Google Sheets liên tục

_cache = {"df": None, "ts": 0.0}


def _role_level(role):
    return ROLE_LEVELS.get(str(role or "").strip().lower(), 0)


def _get_worksheet(force_reopen=False):
    sh = get_spreadsheet(force_reopen=force_reopen)
    return sh.worksheet(USERS_SHEET)


def _fetch_users_df_with_retry(retries=2, base_delay=0.6):
    """Đọc sheet Users, tự thử lại vài lần nếu Google Sheets API báo lỗi
    TẠM THỜI (503 Service Unavailable, quá tải, timeout...). Đây chính là
    nguyên nhân từng gặp: /login lỗi 500 dù app vẫn chạy bình thường - do
    Google API đơ đúng lúc gọi, không retry nên bung lỗi thẳng lên người
    dùng ngay lần đầu."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            ws = _get_worksheet(force_reopen=(attempt > 0))
            return get_as_dataframe(ws, evaluate_formulas=True)
        except gspread.exceptions.APIError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            last_err = e
            if status not in _TRANSIENT_API_STATUS or attempt == retries:
                raise
            print(f"[users_store] Google Sheets API lỗi tạm thời ({status}), thử lại lần {attempt + 1}/{retries}...")
            time.sleep(base_delay * (attempt + 1))
    raise last_err


def _read_users_df(force=False):
    now = time.time()
    if not force and _cache["df"] is not None and (now - _cache["ts"]) < CACHE_TTL_SECONDS:
        return _cache["df"]

    df = _fetch_users_df_with_retry()

    if df is None or df.empty:
        df = pd.DataFrame(columns=USERS_COLUMNS)
    else:
        df = df.dropna(how="all")
        for col in USERS_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[USERS_COLUMNS].fillna("")
        for col in USERS_COLUMNS:
            df[col] = df[col].astype(str).str.strip()
        df = df[df["username"] != ""].reset_index(drop=True)

    _cache["df"] = df
    _cache["ts"] = now
    return df


def verify_login(username, password):
    """Luôn đọc dữ liệu MỚI NHẤT (force=True) khi đăng nhập để chắc chắn nhận
    diện đúng ngay cả khi bạn vừa sửa Sheet vài giây trước."""
    username = (username or "").strip()
    password = (password or "").strip()
    if not username or not password:
        return None

    df = _read_users_df(force=True)
    match = df[df["username"].str.lower() == username.lower()]
    if match.empty:
        return None

    row = match.iloc[0]
    if row["password"] != password:
        return None

    return {
        "username": row["username"],
        "full_name": row["full_name"] or row["username"],
        "role": row["role"],
        "region": row["region"],
    }


def list_users_public():
    """Toàn bộ danh sách tài khoản, KHÔNG bao gồm cột mật khẩu - dùng để build
    tag lọc kỹ thuật viên (đối chiếu tên hiển thị trong list_Stations.json
    với username thật để hiển thị chấm online/offline)."""
    df = _read_users_df()
    return df.drop(columns=["password"]).to_dict("records")


def get_user_info(username):
    """Tra cứu role/region hiện tại của 1 username (dùng cache, để không gọi
    Google Sheets liên tục khi có nhiều vị trí được gửi lên mỗi giây)."""
    df = _read_users_df()
    match = df[df["username"].str.lower() == (username or "").strip().lower()]
    if match.empty:
        return None
    row = match.iloc[0]
    return {
        "username": row["username"],
        "full_name": row["full_name"] or row["username"],
        "role": row["role"],
        "region": row["region"],
    }


def can_view(viewer_role, target_role):
    """Admin xem được tất cả; các vai trò khác chỉ xem được cấp thấp hơn mình."""
    if str(viewer_role or "").strip().lower() == "admin":
        return True
    return _role_level(target_role) < _role_level(viewer_role)


