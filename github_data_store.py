"""
Đọc dữ liệu tĩnh (toạ độ trạm, phân công kỹ thuật viên theo khu vực, tên
model trụ sạc) TRỰC TIẾP từ GitHub qua GitHub Contents API tại runtime -
thay vì đóng gói file cùng code khi deploy, và thay vì Google Sheets (chậm
hơn nhiều do gspread phải gọi qua Google Sheets API).

Cả 3 file đều là JSON (đổi từ Excel/json cũ sang JSON thuần để xử lý nhanh
hơn):

- StationAssignments.json : {region: {engineer_name: [station_code, ...]}}
- StationCoords.json      : {station_code: {"lat": float, "lng": float}}
- ChargePointModels.json  : {charge_point_model: name}

Lợi ích: bạn chỉ cần sửa file trên máy, push lên GitHub - app sẽ tự tải lại
dữ liệu mới ở lần làm mới kế tiếp (có cache TTL, xem CACHE_TTL_SECONDS bên
dưới), KHÔNG CẦN deploy lại toàn bộ project trên Render.

QUAN TRỌNG: nếu 3 file này nằm CHUNG repo với code app đang deploy trên
Render, việc push commit vẫn có thể kích hoạt Render tự động deploy lại (tuỳ
cấu hình Auto-Deploy của bạn) - dù bản thân module này không cần điều đó. Để
tránh hẳn deploy không cần thiết, khuyến nghị: tạo 1 repo GitHub RIÊNG chỉ
chứa 3 file data này (không kết nối với Render), rồi trỏ GITHUB_DATA_REPO
sang repo đó.
"""

import json
import math
import time

import requests

from config import (
    GITHUB_DATA_REPO, GITHUB_DATA_BRANCH, GITHUB_TOKEN,
    GITHUB_STATIONS_JSON_PATH, GITHUB_COORDS_JSON_PATH, GITHUB_CP_MODEL_JSON_PATH,
)
from utils import extract_core_station_code

CACHE_TTL_SECONDS = 300  # 5 phút - đủ nhanh để nhận thay đổi, không gọi GitHub API quá dày

_cache = {"data": None, "ts": 0.0}


def _github_headers():
    headers = {}
    # Nếu repo là private, bạn vẫn cần truyền Token để có quyền đọc file
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _fetch_github_json(path):
    if not GITHUB_DATA_REPO:
        raise RuntimeError("Chưa cấu hình biến môi trường GITHUB_DATA_REPO (dạng 'ten-tai-khoan/ten-repo').")

    # Chuyển sang dùng Raw URL thay vì gọi qua GitHub Contents API để tránh rate limit
    branch = GITHUB_DATA_BRANCH if GITHUB_DATA_BRANCH else "main"
    url = f"https://raw.githubusercontent.com/{GITHUB_DATA_REPO}/{branch}/{path}"
    
    # Bỏ params={"ref": ...} vì nhánh đã được chỉ định thẳng vào URL
    res = requests.get(url, headers=_github_headers(), timeout=20)
    res.raise_for_status()
    return json.loads(res.content.decode("utf-8"))


def load_static_data(force=False):
    """Trả về (coords_map, tech_map, region_map, cp_model_map, tech_by_region).
    Có cache TTL để không gọi GitHub API liên tục."""
    now = time.time()
    if not force and _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL_SECONDS:
        return _cache["data"]

    coords_map = {}
    tech_map = {}
    region_map = {}
    cp_model_map = {}
    tech_by_region = {}

    try:
        coords_data = _fetch_github_json(GITHUB_COORDS_JSON_PATH)
        for station_code, c in coords_data.items():
            if not isinstance(c, dict):
                continue
            core_code = extract_core_station_code(station_code)
            try:
                coords_map[core_code] = {"lat": float(c["lat"]), "lng": float(c["lng"])}
            except (KeyError, TypeError, ValueError):
                continue
    except Exception as e:
        print(f"⚠️ Lỗi tải/đọc '{GITHUB_COORDS_JSON_PATH}' từ GitHub: {e}")

    try:
        list_stations = _fetch_github_json(GITHUB_STATIONS_JSON_PATH)
        tech_by_region_set = {}
        for region, engs in list_stations.items():
            tech_by_region_set.setdefault(region, set())
            for eng, station_list in engs.items():
                tech_by_region_set[region].add(eng)
                for st in station_list:
                    core_code = extract_core_station_code(st)
                    tech_map[core_code] = eng
                    region_map[core_code] = region
        tech_by_region = {r: sorted(v) for r, v in tech_by_region_set.items()}
    except Exception as e:
        print(f"⚠️ Lỗi tải/đọc '{GITHUB_STATIONS_JSON_PATH}' từ GitHub: {e}")

    try:
        cp_model_map = _fetch_github_json(GITHUB_CP_MODEL_JSON_PATH)
        if not isinstance(cp_model_map, dict):
            cp_model_map = {}
    except Exception as e:
        print(f"⚠️ Lỗi tải/đọc '{GITHUB_CP_MODEL_JSON_PATH}' từ GitHub: {e}")

    data = (coords_map, tech_map, region_map, cp_model_map, tech_by_region)

    # Chỉ cache lại nếu tải được ít nhất 1 phần dữ liệu - tránh việc 1 lần lỗi
    # mạng tạm thời (rate limit, mất mạng...) xoá sạch dữ liệu đang có trong cache
    if coords_map or tech_map or cp_model_map or _cache["data"] is None:
        _cache["data"] = data
        _cache["ts"] = now
        return data

    print("⚠️ Không tải được dữ liệu mới từ GitHub - dùng lại dữ liệu tĩnh lần trước.")
    return _cache["data"]


def reload_static_data():
    return load_static_data(force=True)