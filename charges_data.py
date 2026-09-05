"""
charges_data.py — Đọc total_charges.xlsx (TOÀN BỘ trụ sạc EV + BSS của MỌI
trạm, kể cả trạm hiện không có sự cố) từ GitHub Contents API, gom thành
từng "trạm sạc" cho trang Bản đồ tổng hợp (/all-stations).

Khác với ccts_data.py (chỉ hiện trạm ĐANG có ticket mở, gom theo Station
Code lấy từ CCTS) - module này hiện TOÀN BỘ hệ thống, gom theo TOẠ ĐỘ VẬT
LÝ (làm tròn ~11m) chứ KHÔNG theo Mã trạm. Lý do: kiểm tra dữ liệu thật cho
thấy 1 vị trí vật lý có thể mang 2 Mã trạm khác nhau - 1 mã bắt đầu bằng
"B." cho trụ đổi pin (SN bắt đầu "BSS-") và 1 mã bắt đầu bằng "C." cho trụ
sạc EV riêng - nếu gom theo Mã trạm, 2 marker sẽ chồng lên nhau tại cùng 1
điểm thay vì gộp thành 1 trạm duy nhất như thực tế người dùng nhìn thấy.

Quy tắc ưu tiên: 1 trạm có cả trụ EV lẫn BSS -> "type" = "ev" (icon EV được
ưu tiên hiển thị), chỉ khi trạm HOÀN TOÀN không có trụ EV mới là "bss".

Cache TTL giống github_data_store.py (5 phút) - sửa/thêm dòng trong file
Excel trên GitHub, push xong app tự nạp lại ở lần gọi API kế tiếp, không
cần deploy lại.
"""
from __future__ import annotations

import io
import math
import time
from datetime import datetime

import pandas as pd

from config import GITHUB_TOTAL_CHARGES_XLSX_PATH
from github_data_store import fetch_github_raw
from ccts_shared import VN_TZ, load_static_data_filtered
from utils import extract_core_station_code

UNASSIGNED_TECH = "Chưa phân công"

CACHE_TTL_SECONDS = 300  # 5 phút, giống github_data_store.py

# Toạ độ làm tròn 4 chữ số thập phân (~11m) coi là "cùng 1 trạm vật lý".
# Đối chiếu dữ liệu thật: có 471 cặp toạ độ trùng ở mức làm tròn này giữa 1
# Mã trạm BSS và 1 Mã trạm EV khác nhau - đúng ngưỡng "cùng 1 site".
COORD_ROUND_DP = 4

_cache = {"data": None, "ts": 0.0}


def _to_float(val) -> float | None:
    """Ép kiểu lat/lng về float, dọn vài lỗi nhập liệu thật gặp trong file
    (dấu phẩy thay dấu chấm, dấu nháy/dấu phẩy thừa đầu-cuối, vd "'11.51229",
    "15,,240278"). Trả None nếu vẫn không parse được - dòng đó bị bỏ qua,
    không làm hỏng cả file."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if math.isfinite(val) else None
    s = str(val).strip().strip("'").strip(",")
    if not s:
        return None
    # "15,,240278" -> "15.240278" (nhiều dấu phẩy liên tiếp coi là 1 dấu chấm)
    s = s.replace(",,", ",").replace(",", ".")
    try:
        f = float(s)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _to_str(val) -> str:
    """str() an toàn cho ô Excel trống - pandas để NaN (float) cho ô trống
    dù đã ép dtype=str lúc đọc, str(nan) sẽ ra chữ "nan" sai nếu không lọc."""
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return ""
    return str(val).strip()


def _load_charges_df() -> pd.DataFrame:
    raw = fetch_github_raw(GITHUB_TOTAL_CHARGES_XLSX_PATH)
    df = pd.read_excel(io.BytesIO(raw), sheet_name=0, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _build_stations(df: pd.DataFrame) -> tuple[list[dict], dict, int]:
    """Gom từng dòng (1 dòng = 1 trụ sạc) theo toạ độ vật lý đã làm tròn, kèm
    quy đổi Mã trạm -> kỹ thuật viên/khu vực (dùng chung tech_map/region_map
    từ StationData.csv, y hệt bản đồ sự cố) để phục vụ bảng "số trụ mỗi kỹ
    thuật viên quản lý". Trả (list trạm, tech_summary theo khu vực, số dòng
    bị bỏ qua vì thiếu/sai toạ độ).

    tech_summary: {region: {tech_name: {"ev_count", "bss_count", "total_count"}}}
    - đếm theo TỪNG TRỤ (không theo trạm gộp toạ độ), vì 1 trạm vật lý có
    thể có trụ EV/BSS do 2 kỹ thuật viên khác nhau phụ trách."""
    _, tech_map, region_map, _, _ = load_static_data_filtered()

    groups: dict[tuple[float, float], dict] = {}
    tech_summary: dict[str, dict[str, dict]] = {}
    missing_coord: list[dict] = []

    # iterrows() (không phải itertuples()) - itertuples() sanitize tên cột
    # có khoảng trắng/ký tự đặc biệt (vd "Vĩ độ (lat)") thành _1, _2... nên
    # tra theo tên gốc sẽ luôn ra None. iterrows() trả Series giữ nguyên tên
    # cột gốc - chậm hơn nhưng ~14000 dòng vẫn dưới 1 giây, không đáng lo.
    for _, row_d in df.iterrows():
        lat = _to_float(row_d.get("Vĩ độ (lat)"))
        lng = _to_float(row_d.get("Kinh độ (long)"))
        if lat is None or lng is None:
            missing_coord.append({
                "sn": _to_str(row_d.get("SN")),
                "station_code": _to_str(row_d.get("Mã trạm/Code Station")),
                "name": _to_str(row_d.get("Tên trạm/ Name Station")),
            })
            continue

        sn = _to_str(row_d.get("SN"))
        is_bss = sn.upper().startswith("BSS-")
        code = _to_str(row_d.get("Mã trạm/Code Station"))
        name = _to_str(row_d.get("Tên trạm/ Name Station"))
        district = _to_str(row_d.get("Quận Huyện/ District"))
        province = _to_str(row_d.get("Tỉnh thành/Province"))

        core_code = extract_core_station_code(code)
        tech_name = tech_map.get(core_code) or UNASSIGNED_TECH
        region = region_map.get(core_code) or "KV không quản lý"

        key = (round(lat, COORD_ROUND_DP), round(lng, COORD_ROUND_DP))
        g = groups.get(key)
        if g is None:
            g = {
                "lat": lat, "lng": lng,
                "codes": set(), "name": name,
                "district": district, "province": province,
                "ev_count": 0, "bss_count": 0, "techs": set(),
            }
            groups[key] = g

        if code:
            g["codes"].add(code)
        if tech_name:
            g["techs"].add(tech_name)
        if is_bss:
            g["bss_count"] += 1
        else:
            g["ev_count"] += 1
        if not g["name"] and name:
            g["name"] = name

        region_bucket = tech_summary.setdefault(region, {})
        tstat = region_bucket.setdefault(tech_name, {"ev_count": 0, "bss_count": 0})
        if is_bss:
            tstat["bss_count"] += 1
        else:
            tstat["ev_count"] += 1

    stations = []
    for g in groups.values():
        stations.append({
            "lat": g["lat"],
            "lng": g["lng"],
            "station_codes": sorted(g["codes"]),
            "name": g["name"],
            "district": g["district"],
            "province": g["province"],
            "ev_count": g["ev_count"],
            "bss_count": g["bss_count"],
            "total_count": g["ev_count"] + g["bss_count"],
            # Ưu tiên EV: chỉ "bss" khi HOÀN TOÀN không có trụ EV nào.
            "type": "bss" if g["ev_count"] == 0 else "ev",
            "techs": sorted(g["techs"]),
        })

    # Sắp xếp mỗi khu vực theo tổng số trụ giảm dần - dễ so sánh "ai quản lý
    # nhiều nhất" ngay khi mở bảng, không cần tự cộng.
    tech_summary_out = {}
    for region, techs in tech_summary.items():
        rows = [
            {
                "tech_name": tech_name,
                "ev_count": t["ev_count"],
                "bss_count": t["bss_count"],
                "total_count": t["ev_count"] + t["bss_count"],
            }
            for tech_name, t in techs.items()
        ]
        rows.sort(key=lambda r: r["total_count"], reverse=True)
        tech_summary_out[region] = rows

    return stations, tech_summary_out, missing_coord


def refresh_charges_cache(force: bool = False) -> dict:
    """Trả payload {stations, total_poles, total_stations, skipped_rows,
    generated_at}. Cache TTL 5 phút; lỗi tải/parse thì GIỮ NGUYÊN cache cũ
    (nếu có) thay vì xoá sạch, giống triết lý cache toàn bộ chương trình."""
    now = time.time()
    if not force and _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL_SECONDS:
        return _cache["data"]

    try:
        df = _load_charges_df()
        stations, tech_summary, missing_coord = _build_stations(df)
        payload = {
            "stations": stations,
            "tech_summary": tech_summary,
            "total_poles": int(len(df)),
            "total_stations": len(stations),
            "skipped_rows": len(missing_coord),
            "missing_coord_items": missing_coord,
            "generated_at": datetime.now(VN_TZ).isoformat(timespec="seconds"),
        }
        _cache["data"] = payload
        _cache["ts"] = now
        print(
            f"[charges_data] Đã nạp {len(df)} trụ sạc -> gộp thành {len(stations)} trạm "
            f"({len(missing_coord)} dòng bỏ qua vì thiếu/sai toạ độ)."
        )
        return payload
    except Exception as e:
        print(f"⚠️ [charges_data] Lỗi tải/parse total_charges.xlsx: {e!r}")
        if _cache["data"] is not None:
            return _cache["data"]
        raise


def get_cached_payload() -> dict | None:
    return _cache["data"]
