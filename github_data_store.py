"""
Đọc dữ liệu tĩnh (toạ độ trạm, phân công kỹ thuật viên theo khu vực, tên
model trụ sạc) TRỰC TIẾP từ GitHub qua GitHub Contents API tại runtime -
thay vì đóng gói file cùng code khi deploy, và thay vì Google Sheets (chậm
hơn nhiều do gspread phải gọi qua Google Sheets API).

Các file JSON tĩnh (đổi từ Excel/json cũ sang JSON thuần để xử lý nhanh hơn):

- StationAssignments.json : {region: {engineer_name: [station_code, ...]}}
- StationCoords.json      : {station_code: {"lat": float, "lng": float}}
- ChargePointModels.json  : {charge_point_model: name}
- EngineerCoords.json     : {engineer_name: {"lat", "lng"}} hoặc team lead
                            lồng {member: {"lat","lng"}} (vd. đội Lâm Đồng)

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

# Đường dẫn EngineerCoords trên repo data — có thể khai báo trong config;
# mặc định "EngineerCoords.json" (cùng thư mục với 3 file kia).
try:
    from config import GITHUB_ENGINEER_COORDS_JSON_PATH  # type: ignore
except Exception:
    GITHUB_ENGINEER_COORDS_JSON_PATH = "EngineerCoords.json"
from utils import extract_core_station_code

CACHE_TTL_SECONDS = 300  # 5 phút - đủ nhanh để nhận thay đổi, không gọi GitHub API quá dày

_cache = {"data": None, "ts": 0.0, "engineer_homes": {}, "engineer_rollup": {}}


def _github_headers():
    # Accept: raw -> GitHub trả thẳng nội dung file gốc (bytes), không cần tự
    # giải mã base64.
    headers = {"Accept": "application/vnd.github.v3.raw"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _fetch_github_json(path):
    if not GITHUB_DATA_REPO:
        raise RuntimeError("Chưa cấu hình biến môi trường GITHUB_DATA_REPO (dạng 'ten-tai-khoan/ten-repo').")

    url = f"https://api.github.com/repos/{GITHUB_DATA_REPO}/contents/{path}"
    params = {"ref": GITHUB_DATA_BRANCH} if GITHUB_DATA_BRANCH else {}
    res = requests.get(url, headers=_github_headers(), params=params, timeout=20)
    res.raise_for_status()
    return json.loads(res.content.decode("utf-8"))



# Team lead có cấu trúc lồng trong EngineerCoords (đội Lâm Đồng).
_TEAM_LEAD_NAMES = {"Nguyễn Hải Nguyên"}


def _parse_engineer_coords(raw) -> tuple[dict, dict]:
    """Parse EngineerCoords.json → (homes, rollup).

    homes:  {tên KT: {"lat": float, "lng": float}}
    rollup: {tên thành viên: tên team lead}  — metric gộp về lead
    """
    homes: dict = {}
    rollup: dict = {}
    if not isinstance(raw, dict):
        return homes, rollup
    for name, val in raw.items():
        name = str(name).strip()
        if not name or not isinstance(val, dict):
            continue
        # Team lead: value = {member: {lat, lng}}
        if "lat" not in val and "lng" not in val:
            for member, coord in val.items():
                m = str(member).strip()
                if not m or not isinstance(coord, dict):
                    continue
                try:
                    lat, lng = float(coord["lat"]), float(coord["lng"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not (math.isfinite(lat) and math.isfinite(lng)):
                    continue
                homes[m] = {"lat": lat, "lng": lng}
                if name in _TEAM_LEAD_NAMES:
                    rollup[m] = name
            continue
        try:
            lat, lng = float(val["lat"]), float(val["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lng)):
            continue
        homes[name] = {"lat": lat, "lng": lng}
    return homes, rollup


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
                lat = float(c["lat"])
                lng = float(c["lng"])
            except (KeyError, TypeError, ValueError):
                continue
            # float("nan")/float("inf") KHÔNG raise exception ở trên (Python chấp
            # nhận chuỗi "NaN"/"Infinity" khi ép kiểu) nhưng JSON response của
            # FastAPI/Starlette lại KHÔNG chấp nhận các giá trị này (allow_nan=False)
            # -> phải tự kiểm tra isfinite() và loại bỏ tại đây, tránh cả app crash sau này.
            if not (math.isfinite(lat) and math.isfinite(lng)):
                print(f"⚠️ Bỏ qua toạ độ không hợp lệ (NaN/Infinity) cho trạm '{station_code}'.")
                continue
            coords_map[core_code] = {"lat": lat, "lng": lng}
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

    engineer_homes: dict = {}
    engineer_rollup: dict = {}
    try:
        eng_raw = _fetch_github_json(GITHUB_ENGINEER_COORDS_JSON_PATH)
        engineer_homes, engineer_rollup = _parse_engineer_coords(eng_raw)
        print(
            f"[static] EngineerCoords: {len(engineer_homes)} nhà KT"
            + (f", rollup {len(engineer_rollup)} thành viên → lead" if engineer_rollup else "")
        )
    except Exception as e:
        print(f"⚠️ Lỗi tải/đọc '{GITHUB_ENGINEER_COORDS_JSON_PATH}' từ GitHub: {e}")

    data = (coords_map, tech_map, region_map, cp_model_map, tech_by_region)

    # Chỉ cache lại nếu tải được ít nhất 1 phần dữ liệu - tránh việc 1 lần lỗi
    # mạng tạm thời (rate limit, mất mạng...) xoá sạch dữ liệu đang có trong cache
    if coords_map or tech_map or cp_model_map or engineer_homes or _cache["data"] is None:
        _cache["data"] = data
        _cache["ts"] = now
        # Engineer homes luôn cập nhật khi fetch thành công; nếu lỗi giữ bản cũ
        if engineer_homes or _cache.get("engineer_homes") is None:
            _cache["engineer_homes"] = engineer_homes
            _cache["engineer_rollup"] = engineer_rollup
        return data

    print("⚠️ Không tải được dữ liệu mới từ GitHub - dùng lại dữ liệu tĩnh lần trước.")
    return _cache["data"]


def reload_static_data():
    return load_static_data(force=True)


def get_engineer_homes(force: bool = False) -> tuple[dict, dict]:
    """Toạ độ nhà KT + rollup đội lead.

    Trả (homes, rollup):
      homes  = {tên: {"lat": float, "lng": float}}
      rollup = {thành viên: team_lead}  (vd. Nguyễn Văn Mười → Nguyễn Hải Nguyên)

    Tự gọi load_static_data() để dùng chung cache TTL / GitHub fetch.
    """
    load_static_data(force=force)
    return (
        dict(_cache.get("engineer_homes") or {}),
        dict(_cache.get("engineer_rollup") or {}),
    )