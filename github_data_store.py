"""
Đọc dữ liệu tĩnh TRỰC TIẾP từ GitHub qua GitHub Contents API tại runtime.

Nguồn chính trạm + phân công (1 file CSV):
- StationData.csv
  Cột bắt buộc (tiếng Anh):
    station_code, lat, lng, technician, region
  Cột tùy chọn (giữ cho nâng cấp sau):
    old_district, old_province, new_district, new_province
    (chấp nhận alias "long" → lng nếu CSV cũ còn dùng long)

Nguồn JSON còn lại:
- ChargePointModels.json  : {charge_point_model: name}
- EngineerCoords.json     : {engineer_name: {"lat","lng"}} hoặc team lead lồng

Lợi ích: sửa CSV/JSON trên máy → push GitHub → app tự tải lại (cache TTL),
không cần deploy lại Render.

Khuyến nghị: repo GitHub RIÊNG chỉ chứa data (không gắn Auto-Deploy Render).
"""

import json
import math
import time
from io import StringIO

import pandas as pd
import requests

from config import (
    GITHUB_DATA_REPO,
    GITHUB_DATA_BRANCH,
    GITHUB_TOKEN,
    GITHUB_CP_MODEL_JSON_PATH,
)

try:
    from config import GITHUB_STATION_DATA_CSV_PATH  # type: ignore
except Exception:
    GITHUB_STATION_DATA_CSV_PATH = "StationData.csv"

try:
    from config import GITHUB_ENGINEER_COORDS_JSON_PATH  # type: ignore
except Exception:
    GITHUB_ENGINEER_COORDS_JSON_PATH = "EngineerCoords.json"

from utils import extract_core_station_code

CACHE_TTL_SECONDS = 300  # 5 phút

_cache = {
    "data": None,
    "ts": 0.0,
    "engineer_homes": {},
    "engineer_rollup": {},
}

_TEAM_LEAD_NAMES = {"Nguyễn Hải Nguyên"}


def _github_headers():
    headers = {"Accept": "application/vnd.github.v3.raw"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _fetch_github_raw(path: str) -> bytes:
    if not GITHUB_DATA_REPO:
        raise RuntimeError(
            "Chưa cấu hình biến môi trường GITHUB_DATA_REPO "
            "(dạng 'ten-tai-khoan/ten-repo')."
        )
    url = f"https://api.github.com/repos/{GITHUB_DATA_REPO}/contents/{path}"
    params = {"ref": GITHUB_DATA_BRANCH} if GITHUB_DATA_BRANCH else {}
    res = requests.get(url, headers=_github_headers(), params=params, timeout=20)
    res.raise_for_status()
    return res.content


def _fetch_github_json(path: str):
    return json.loads(_fetch_github_raw(path).decode("utf-8"))


def _fetch_github_csv(path: str) -> pd.DataFrame:
    raw = _fetch_github_raw(path).decode("utf-8-sig")
    return pd.read_csv(StringIO(raw), dtype=str).fillna("")


def _load_station_data_csv(path: str):
    """
    StationData.csv → (coords_map, tech_map, region_map, tech_by_region)

    coords_map: {core_code: {"lat": float, "lng": float}}
    tech_map:   {core_code: technician_name}
    region_map: {core_code: region}
    tech_by_region: {region: [tech, ...]}  (sorted)
    """
    df = _fetch_github_csv(path)
    # Chuẩn hoá tên cột (lowercase, strip) để chịu CSV viết hoa/thường lẫn
    col_map = {str(c).strip().lower(): c for c in df.columns}
    def col(*names):
        for n in names:
            if n.lower() in col_map:
                return col_map[n.lower()]
        return None

    c_code = col("station_code")
    c_lat = col("lat")
    c_lng = col("lng", "long")  # alias "long"
    c_tech = col("technician", "tech", "kỹ thuật phụ trách")
    c_region = col("region", "khu vực")

    if not c_code:
        raise RuntimeError("StationData.csv thiếu cột station_code")

    coords_map, tech_map, region_map = {}, {}, {}
    tech_by_region_set: dict[str, set] = {}

    for _, r in df.iterrows():
        code = str(r.get(c_code) or "").strip()
        if not code:
            continue
        core_code = extract_core_station_code(code)

        if c_lat and c_lng:
            try:
                lat = float(r.get(c_lat))
                lng = float(r.get(c_lng))
                if math.isfinite(lat) and math.isfinite(lng):
                    coords_map[core_code] = {"lat": lat, "lng": lng}
            except (ValueError, TypeError):
                pass

        tech = str(r.get(c_tech) or "").strip() if c_tech else ""
        region = str(r.get(c_region) or "").strip() if c_region else ""

        if tech:
            tech_map[core_code] = tech
            if region:
                tech_by_region_set.setdefault(region, set()).add(tech)
        if region:
            region_map[core_code] = region

    tech_by_region = {r: sorted(v) for r, v in tech_by_region_set.items()}
    return coords_map, tech_map, region_map, tech_by_region


def _parse_engineer_coords(raw) -> tuple[dict, dict]:
    """Parse EngineerCoords.json → (homes, rollup).

    homes:  {tên KT: {"lat": float, "lng": float}}
    rollup: {tên thành viên: tên team lead}
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


def load_static_data(force: bool = False):
    """Trả về (coords_map, tech_map, region_map, cp_model_map, tech_by_region).

    Có cache TTL để không gọi GitHub API liên tục.
    """
    now = time.time()
    if (
        not force
        and _cache["data"] is not None
        and (now - _cache["ts"]) < CACHE_TTL_SECONDS
    ):
        return _cache["data"]

    coords_map: dict = {}
    tech_map: dict = {}
    region_map: dict = {}
    cp_model_map: dict = {}
    tech_by_region: dict = {}

    # --- 1) StationData.csv (coords + tech + region) ---
    try:
        coords_map, tech_map, region_map, tech_by_region = _load_station_data_csv(
            GITHUB_STATION_DATA_CSV_PATH
        )
        print(
            f"[static] StationData.csv: {len(coords_map)} coords, "
            f"{len(tech_map)} tech map, {len(region_map)} region, "
            f"{len(tech_by_region)} KV"
        )
    except Exception as e:
        print(f"⚠️ Lỗi tải/đọc '{GITHUB_STATION_DATA_CSV_PATH}' từ GitHub: {e}")

    # --- 2) ChargePointModels.json ---
    try:
        cp_model_map = _fetch_github_json(GITHUB_CP_MODEL_JSON_PATH)
        if not isinstance(cp_model_map, dict):
            cp_model_map = {}
    except Exception as e:
        print(f"⚠️ Lỗi tải/đọc '{GITHUB_CP_MODEL_JSON_PATH}' từ GitHub: {e}")

    # --- 3) EngineerCoords.json ---
    engineer_homes: dict = {}
    engineer_rollup: dict = {}
    try:
        eng_raw = _fetch_github_json(GITHUB_ENGINEER_COORDS_JSON_PATH)
        engineer_homes, engineer_rollup = _parse_engineer_coords(eng_raw)
        print(
            f"[static] EngineerCoords: {len(engineer_homes)} nhà KT"
            + (
                f", rollup {len(engineer_rollup)} thành viên → lead"
                if engineer_rollup
                else ""
            )
        )
    except Exception as e:
        print(f"⚠️ Lỗi tải/đọc '{GITHUB_ENGINEER_COORDS_JSON_PATH}' từ GitHub: {e}")

    data = (coords_map, tech_map, region_map, cp_model_map, tech_by_region)

    # Chỉ cache nếu tải được ít nhất 1 phần — tránh 1 lần lỗi mạng xoá sạch cache
    if coords_map or tech_map or cp_model_map or engineer_homes or _cache["data"] is None:
        _cache["data"] = data
        _cache["ts"] = now
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
      rollup = {thành viên: team_lead}
    """
    load_static_data(force=force)
    return (
        dict(_cache.get("engineer_homes") or {}),
        dict(_cache.get("engineer_rollup") or {}),
    )