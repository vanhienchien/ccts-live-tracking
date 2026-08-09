"""
stats_charts_heatmap.py — bản đồ nhiệt (heatmap) số lượng ticket & ticket
Overdue theo vị trí trạm, tách riêng theo 5 khu vực quản lý.

- Nguồn: cache "tickets" (mọi ticket trong cửa sổ cào, đã lọc managed
  region) — lấy 30 ngày gần nhất theo Create Date (đồng bộ với
  stats_charts_volume.py) để các biểu đồ trong trang cùng 1 mốc thời gian.
- overdue = SLA Status (chuẩn hoá lower) == "overdue", lấy TRỰC TIẾP trên
  cùng tập ticket ở trên. Khác với stats_charts_overdue_rate.py (module đó
  đo tỷ lệ Overdue LÚC ĐÓNG ticket, dựa trên closed_tickets 30 ngày Close
  Time) — module này đo Overdue HIỆN TẠI theo vị trí, không quan tâm ticket
  đã đóng hay chưa, đúng theo cột SLA Status của ticket.
- Gộp theo mã trạm (core station code, qua StationCoords.json) trong từng
  khu vực → điểm {station, lat, lng, count} — count = trọng số (weight) để
  frontend vẽ heat layer (leaflet.heat) chuyển màu mượt kiểu bản đồ mưa,
  KHÔNG phải marker rời rạc từng ticket.
- bounds/center mỗi khu vực TỰ TÍNH từ toạ độ các trạm CÓ ticket trong khu
  vực đó (không hard-code ranh giới tỉnh) → luôn khớp đúng phạm vi trạm
  thực tế, có đệm biên ~18% để không bị cắt sát mép bản đồ. Chỉ dùng toạ độ
  trung tâm dự phòng khi 1 khu vực không có bất kỳ trạm nào có toạ độ hợp
  lệ trong dữ liệu hiện tại (hiếm, ~99.5% trạm có toạ độ).
- region_boundaries: ranh giới tỉnh CŨ (trước sáp nhập 2025) gộp theo 5 khu
  vực, để frontend cắt bản đồ nhiệt đúng phạm vi (xem get_region_boundaries).
- engineers: toạ độ nhà kỹ thuật viên (EngineerCoords.json qua GitHub), gán
  vào đúng khu vực bằng point-in-polygon trên region_boundaries, để điều
  phối biết khu vực nào đủ/thiếu KT xử lý sự cố (xem build_engineer_payload).

Không cào API, không vẽ chart — chỉ chuẩn bị payload JSON cho frontend.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from stats_data import (
    ALLOWED_REGIONS,
    _extract_core_station_code,
    get_coords_map,
    is_managed_region,
    records_to_tickets_df,
)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Ranh giới hành chính (đã gộp theo tỉnh CŨ, trước sáp nhập 1/7/2025) của 5
# khu vực quản lý — sinh 1 LẦN bằng scripts/build_region_boundaries.py
# (union polygon các tỉnh, xem REGION_PROVINCES trong file đó để biết/chỉnh
# tỉnh nào thuộc khu vực nào), KHÔNG tính lại lúc runtime. Dùng để frontend
# "cắt" bản đồ nhiệt chỉ hiện đúng phạm vi tỉnh đã phân, giống kiểu bản đồ
# thời tiết theo vùng.
# Lưu ý: sau 1/7/2025 hành chính VN chỉ còn 34 tỉnh/thành (gộp từ 63 tỉnh
# cũ) — OpenStreetMap/dữ liệu ranh giới "hiện hành" bây giờ là theo ranh
# giới MỚI, không còn khớp với cách bạn phân theo tỉnh cũ (vd "Lâm Đồng cũ"
# giờ đã gộp vào tỉnh Lâm Đồng mới cùng Bình Thuận, Đắk Nông...). Vì vậy
# ranh giới ở đây lấy từ dữ liệu tỉnh CŨ (nguồn: repo GIS công khai
# nguyenduy1133/Free-GIS-Data, bản "Pre-2025"), không lấy trực tiếp từ OSM.
_REGION_BOUNDARIES_PATH = Path(__file__).parent / "region_boundaries.geojson"
_region_boundaries_cache: dict[str, dict] | None = None


def get_region_boundaries() -> dict[str, dict]:
    """Trả về {region: geojson_geometry} đã nạp sẵn (đọc file 1 lần, cache)."""
    global _region_boundaries_cache
    if _region_boundaries_cache is not None:
        return _region_boundaries_cache
    result: dict[str, dict] = {}
    try:
        with open(_REGION_BOUNDARIES_PATH, encoding="utf-8") as f:
            fc = json.load(f)
        for feat in fc.get("features", []):
            region = (feat.get("properties") or {}).get("region")
            if region:
                result[region] = feat["geometry"]
    except (OSError, json.JSONDecodeError, KeyError):
        # Thiếu file/hỏng dữ liệu: bỏ qua, frontend tự fallback về cách cũ
        # (fitBounds theo trạm, không cắt bản đồ) — không chặn cả trang.
        result = {}
    _region_boundaries_cache = result
    return result


# ─────────────────────────────────────────────────────────────────────────
# Kỹ thuật viên (KT): toạ độ nhà KT (EngineerCoords.json qua GitHub) → gán
# vào đúng khu vực bằng point-in-polygon trên region_boundaries, để điều
# phối biết khu vực nào đang thiếu KT. Độc lập với ticket/cp_type/ngày —
# chỉ phụ thuộc danh sách KT hiện tại, nên tính 1 lần mỗi lần build payload
# (không phải theo từng "all/ev/bss" như _payload_for_df, tránh trùng lặp).
# ─────────────────────────────────────────────────────────────────────────

def load_engineer_homes() -> tuple[dict[str, tuple[float, float]], dict[str, str]]:
    """Lấy toạ độ nhà KT từ GitHub (EngineerCoords.json) qua github_data_store.

    Trả:
      homes: {tên KT: (lat, lng)}
      rollup: {thành viên: team lead}  — vd nhóm "Nguyễn Hải Nguyên" (Lâm Đồng)
      gồm nhiều KT khác, get_engineer_homes() đã tự làm phẳng cấu trúc lồng
      trong EngineerCoords.json và trả rollup tương ứng.
    """
    homes: dict[str, tuple[float, float]] = {}
    rollup: dict[str, str] = {}
    try:
        from github_data_store import get_engineer_homes
        raw_homes, raw_rollup = get_engineer_homes()
    except Exception as e:
        print(f"[stats] Không tải EngineerCoords từ GitHub: {e}")
        return homes, rollup

    for name, coord in (raw_homes or {}).items():
        if not isinstance(coord, dict):
            continue
        try:
            homes[str(name).strip()] = (float(coord["lat"]), float(coord["lng"]))
        except (KeyError, TypeError, ValueError):
            continue
    for member, lead in (raw_rollup or {}).items():
        rollup[str(member).strip()] = str(lead).strip()
    return homes, rollup


def _point_in_ring(lat: float, lng: float, ring: list[list[float]]) -> bool:
    """Ray casting cổ điển — ring là list [lng, lat] theo đúng thứ tự GeoJSON."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _point_in_polygon_coords(lat: float, lng: float, polygon_coords: list) -> bool:
    """polygon_coords theo GeoJSON Polygon: [outer_ring, hole1, hole2, ...]."""
    if not polygon_coords:
        return False
    if not _point_in_ring(lat, lng, polygon_coords[0]):
        return False
    for hole in polygon_coords[1:]:
        if _point_in_ring(lat, lng, hole):
            return False
    return True


def _point_in_geometry(lat: float, lng: float, geometry: dict) -> bool:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        return _point_in_polygon_coords(lat, lng, coords)
    if gtype == "MultiPolygon":
        return any(_point_in_polygon_coords(lat, lng, poly) for poly in coords)
    return False


def _geometry_bbox_center(geometry: dict) -> tuple[float, float] | None:
    """Tâm bbox (lat, lng) của geometry — dùng để tìm khu vực GẦN NHẤT khi
    1 điểm rơi ngoài cả 5 khu vực (vd KT ở TP.HCM/Bình Dương, ngoài phạm vi
    5 khu vực đã phân) — heuristic đơn giản, đủ dùng cho việc gợi ý điều
    phối, không cần chính xác tuyệt đối khoảng cách địa lý."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    pts: list[list[float]] = []

    def collect(rings):
        for ring in rings:
            pts.extend(ring)

    if gtype == "Polygon":
        collect(coords)
    elif gtype == "MultiPolygon":
        for poly in coords:
            collect(poly)
    if not pts:
        return None
    lngs = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return (sum(lats) / len(lats), sum(lngs) / len(lngs))


def _assign_region_for_point(lat: float, lng: float, boundaries: dict[str, dict]) -> str | None:
    for region, geom in boundaries.items():
        if _point_in_geometry(lat, lng, geom):
            return region
    return None


def _nearest_region_for_point(lat: float, lng: float, boundaries: dict[str, dict]) -> str | None:
    best_region, best_dist = None, None
    for region, geom in boundaries.items():
        center = _geometry_bbox_center(geom)
        if not center:
            continue
        d = (center[0] - lat) ** 2 + (center[1] - lng) ** 2
        if best_dist is None or d < best_dist:
            best_dist, best_region = d, region
    return best_region


def build_engineer_payload(boundaries: dict[str, dict]) -> dict[str, Any]:
    """Gán từng KT vào đúng khu vực (point-in-polygon theo ranh giới tỉnh
    cũ) để điều phối biết khu vực nào có/thiếu nhân lực xử lý sự cố.

    KT rơi ngoài cả 5 khu vực (vd nhà ở TP.HCM/Bình Dương — ngoài phạm vi
    quản lý) vẫn được gán vào khu vực GẦN NHẤT nhưng đánh dấu approx=True,
    để điều phối biết đây là ước lượng, không phải KT thường trực tại đó.
    """
    homes, rollup = load_engineer_homes()
    leads = set(rollup.values())
    by_region: dict[str, list[dict]] = {r: [] for r in ALLOWED_REGIONS}

    for name, (lat, lng) in homes.items():
        lead = rollup.get(name)
        entry = {
            "name": name,
            "lat": lat,
            "lng": lng,
            "role": "lead" if name in leads else ("member" if lead else "solo"),
            "team_lead": lead if lead and lead != name else None,
        }
        region = _assign_region_for_point(lat, lng, boundaries)
        if region is None:
            region = _nearest_region_for_point(lat, lng, boundaries)
            entry["approx"] = True
        if region in by_region:
            by_region[region].append(entry)

    for lst in by_region.values():
        lst.sort(key=lambda e: (e.get("approx", False), e["name"]))

    return {
        "by_region": by_region,
        "counts": {r: len(v) for r, v in by_region.items()},
    }
# Module này chỉ lấy 30 ngày gần nhất trong data 60 ngày
# stats_charts_volume.py, để đồng bộ mốc thời gian với các biểu đồ khác.
CHART_LOOKBACK_DAYS = 30
TOP_STATIONS_LIMIT = 12

# Toạ độ trung tâm dự phòng — CHỈ dùng khi 1 khu vực không có bất kỳ trạm
# nào có toạ độ hợp lệ trong dữ liệu hiện tại (hiếm, ~99.5% trạm có toạ độ
# trong StationCoords.json). Không dùng để định hình bounds khi có dữ liệu
# thật — bounds thật luôn ưu tiên.
_REGION_FALLBACK_CENTER: dict[str, tuple[float, float]] = {
    "DNA-QNA": (16.02, 108.22),     # Đà Nẵng
    "DNI-BPH": (10.94, 106.82),     # Biên Hoà, Đồng Nai
    "LDO-BTH": (11.66, 108.44),     # Đà Lạt, Lâm Đồng cũ
    "Tây Nguyên": (13.06, 108.87),  # Buôn Ma Thuột
    "Mtay": (10.03, 105.78),        # Cần Thơ
}
_REGION_FALLBACK_SPAN_DEG = 0.9  # ~100km khung nhìn dự phòng


def _filter_last_n_days(df: pd.DataFrame, n_days: int = CHART_LOOKBACK_DAYS) -> pd.DataFrame:
    """Create Date trong n ngày gần nhất, không gồm ngày hiện tại (đã cắt ở stats_data)."""
    if df is None or df.empty or "Create Date" not in df.columns:
        return df
    today_0h = datetime.now(VN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (today_0h - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (today_0h - timedelta(days=n_days)).strftime("%Y-%m-%d")
    return df[(df["Create Date"] >= start) & (df["Create Date"] <= end)].copy()


def _filter_cp(df: pd.DataFrame, cp_type: str) -> pd.DataFrame:
    if df is None or df.empty or cp_type == "all" or "cp_type" not in df.columns:
        return df
    return df[df["cp_type"] == cp_type].copy()


def _is_overdue_series(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty or "SLA Status" not in df.columns:
        return pd.Series([], dtype=bool)
    return df["SLA Status"].astype(str).str.strip().str.lower().eq("overdue")


def _region_bounds(points: list[dict]) -> dict[str, float]:
    lats = [p["lat"] for p in points]
    lngs = [p["lng"] for p in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)
    # đệm biên tối thiểu để cụm trạm sát nhau không bị zoom quá gắt
    lat_span = max(max_lat - min_lat, 0.05)
    lng_span = max(max_lng - min_lng, 0.05)
    pad_lat = lat_span * 0.18
    pad_lng = lng_span * 0.18
    return {
        "min_lat": round(min_lat - pad_lat, 5),
        "max_lat": round(max_lat + pad_lat, 5),
        "min_lng": round(min_lng - pad_lng, 5),
        "max_lng": round(max_lng + pad_lng, 5),
    }


def _fallback_bounds(region: str) -> dict[str, float]:
    lat, lng = _REGION_FALLBACK_CENTER.get(region, (16.0, 108.0))
    half = _REGION_FALLBACK_SPAN_DEG / 2
    return {
        "min_lat": round(lat - half, 5), "max_lat": round(lat + half, 5),
        "min_lng": round(lng - half, 5), "max_lng": round(lng + half, 5),
    }


def _points_for(df_region: pd.DataFrame, coords_map: dict) -> tuple[list[dict], int, int]:
    """Gộp ticket theo core station code → [{station,lat,lng,count}] (sort desc),
    + (số trạm có toạ độ hợp lệ, tổng số trạm có ticket trong df_region)."""
    if df_region is None or df_region.empty or "Station Code" not in df_region.columns:
        return [], 0, 0

    d = df_region.copy()
    d["_core"] = d["Station Code"].map(_extract_core_station_code)
    d = d.dropna(subset=["_core"])
    if d.empty:
        return [], 0, 0

    counts = d.groupby("_core").size()
    stations_total = int(len(counts))

    points: list[dict] = []
    with_coords = 0
    for core, cnt in counts.items():
        cobj = coords_map.get(core)
        if not cobj:
            continue
        try:
            lat, lng = float(cobj["lat"]), float(cobj["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        with_coords += 1
        points.append({"station": core, "lat": lat, "lng": lng, "count": int(cnt)})

    points.sort(key=lambda p: -p["count"])
    return points, with_coords, stations_total


def _pack_layer(points: list[dict], region: str) -> dict[str, Any]:
    total = sum(p["count"] for p in points)
    bounds = _region_bounds(points) if points else _fallback_bounds(region)
    if points:
        center = {
            "lat": round((bounds["min_lat"] + bounds["max_lat"]) / 2, 5),
            "lng": round((bounds["min_lng"] + bounds["max_lng"]) / 2, 5),
        }
    else:
        lat, lng = _REGION_FALLBACK_CENTER.get(region, (16.0, 108.0))
        center = {"lat": lat, "lng": lng}
    return {
        "total": total,
        "max_count": max((p["count"] for p in points), default=0),
        "bounds": bounds,
        "center": center,
        "top_stations": points[:TOP_STATIONS_LIMIT],
        # [lat, lng, weight] — định dạng leaflet.heat ăn thẳng, weight=count
        "heat_points": [[p["lat"], p["lng"], p["count"]] for p in points],
    }


def _payload_for_df(df: pd.DataFrame, coords_map: dict) -> dict[str, Any]:
    regions: dict[str, Any] = {}
    stations_with_coords_total = 0
    stations_total_all = 0

    for region in ALLOWED_REGIONS:
        sub = df[df["Region"] == region] if not df.empty else df
        overdue_mask = _is_overdue_series(sub)
        overdue_sub = sub[overdue_mask] if sub is not None and not sub.empty else sub

        vol_points, vol_coords, vol_stations = _points_for(sub, coords_map)
        od_points, _od_coords, _od_stations = _points_for(overdue_sub, coords_map)

        stations_with_coords_total += vol_coords
        stations_total_all += vol_stations

        regions[region] = {
            "volume": _pack_layer(vol_points, region),
            "overdue": _pack_layer(od_points, region),
            "stations_with_coords": vol_coords,
            "stations_total": vol_stations,
            "coord_coverage_pct": (
                round(vol_coords / vol_stations * 100, 1) if vol_stations else 0.0
            ),
        }

    total_tickets = int(len(df)) if df is not None else 0
    total_overdue = int(_is_overdue_series(df).sum()) if df is not None and not df.empty else 0
    return {
        "regions": regions,
        "total_tickets": total_tickets,
        "total_overdue": total_overdue,
        "overdue_pct": round(total_overdue / total_tickets * 100, 1) if total_tickets else 0.0,
        "stations_with_coords": stations_with_coords_total,
        "stations_total": stations_total_all,
    }


def _safe_build_engineer_payload(boundaries: dict[str, dict]) -> dict[str, Any]:
    try:
        return build_engineer_payload(boundaries)
    except Exception as e:  # không để lỗi tải KT làm sập cả bản đồ nhiệt
        print(f"[stats] Không gán được KT vào khu vực: {e}")
        return {"by_region": {r: [] for r in ALLOWED_REGIONS}, "counts": {r: 0 for r in ALLOWED_REGIONS}}


def build_heatmap_payload_from_cache(cache: dict[str, Any]) -> dict[str, Any]:
    """Xây payload heatmap (volume + overdue, theo vị trí trạm) từ cache v2."""
    meta = cache.get("meta") or {}
    df = records_to_tickets_df(cache.get("tickets") or [])
    if not df.empty and "Region" in df.columns:
        df = df[df["Region"].apply(is_managed_region)].copy()
    df = _filter_last_n_days(df, CHART_LOOKBACK_DAYS)
    coords_map = get_coords_map()
    boundaries = get_region_boundaries()

    if df.empty:
        empty = _payload_for_df(df, coords_map)
        return {
            "chart": "heatmap",
            "cp_type": "all",
            "by_cp_type": {"all": empty, "ev": empty, "bss": empty},
            **empty,
            "chart_days": CHART_LOOKBACK_DAYS,
            "source": meta.get("source", "unknown"),
            "generated_at": meta.get("generated_at"),
            "counts": {"all": 0, "ev": 0, "bss": 0},
            "meta": meta,
            "region_boundaries": boundaries,
            "engineers": _safe_build_engineer_payload(boundaries),
        }

    df_all = df
    df_ev = _filter_cp(df, "ev")
    df_bss = _filter_cp(df, "bss")

    by_cp = {
        "all": _payload_for_df(df_all, coords_map),
        "ev": _payload_for_df(df_ev, coords_map),
        "bss": _payload_for_df(df_bss, coords_map),
    }
    root = dict(by_cp["all"])
    root["chart"] = "heatmap"
    root["by_cp_type"] = by_cp
    root["cp_type"] = "all"
    root["chart_days"] = CHART_LOOKBACK_DAYS
    root["source"] = meta.get("source", "unknown")
    root["generated_at"] = meta.get("generated_at")
    root["counts"] = {
        "all": int(len(df_all)),
        "ev": int(len(df_ev)),
        "bss": int(len(df_bss)),
    }
    root["meta"] = {
        "end_date_exclusive": meta.get("end_date_exclusive"),
        "accounts_ok": meta.get("accounts_ok"),
    }
    root["region_boundaries"] = boundaries
    root["engineers"] = _safe_build_engineer_payload(boundaries)
    return root