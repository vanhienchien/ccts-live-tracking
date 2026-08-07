"""
stats_charts_volume.py — biểu đồ đường số ticket theo ngày.

Đọc cache tickets từ stats_data, phân tích theo:
- khu vực (ALLOWED_REGIONS)
- tổng cộng tất cả khu vực (1 đường, kèm TB/ngày, đỉnh, đáy — total_series)
- kỹ thuật viên trong từng khu vực (ma trận tech × ngày, frontend vẽ heatmap)
- khối lượng công việc THỰC TẾ theo KT — số ticket kết hợp quãng đường di
  chuyển ước tính giữa các trạm (dựa toạ độ StationCoords.json), vì 2 KT
  cùng số ticket nhưng 1 người ở thành phố (trạm gần nhau) và 1 người ở
  vùng núi (trạm cách xa) thì khối lượng thực tế rất khác nhau
  (tech_workload — frontend vẽ biểu đồ bubble)
- bộ lọc EV / BSS / all

Không cào API.
"""

from __future__ import annotations

import json
import os
import time
import math
import urllib.parse
import urllib.request
from typing import Any

import pandas as pd

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from stats_data import (
    ALLOWED_REGIONS,
    _ALLOWED_SET,
    _extract_core_station_code,
    _parse_create_time,
    get_coords_map,
    is_excluded_tech,
    is_managed_region,
    records_to_tickets_df,
)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
# Module này chỉ lấy 30 ngày gần nhất trong data đã cào (45 ngày).
CHART_LOOKBACK_DAYS = 30
EARTH_RADIUS_KM = 6371.0



# ~km mỗi độ vĩ độ (đường kính Trái Đất / 360). Dùng cho xấp xỉ
# equirectangular — fallback khi OSRM không trả được; vẫn đủ tốt để so sánh.
_DEG_TO_KM = EARTH_RADIUS_KM * math.pi / 180.0

# OSRM (Open Source Routing Machine) — khoảng cách ĐƯỜNG ĐI thực tế, miễn phí.
# Mặc định dùng demo public (router.project-osrm.org). Job 0h/ngày + cache đĩa
# nên quota demo là chấp nhận được; production nên self-host:
#   docker run -t -i -p 5000:5000 -v $PWD:/data osrm/osrm-backend \
#     osrm-routed --algorithm mld /data/vietnam-latest.osrm
# rồi set OSRM_BASE_URL=http://127.0.0.1:5000
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
OSRM_ENABLED = os.environ.get("OSRM_ENABLED", "1").strip() not in {"0", "false", "False", "no"}
OSRM_TIMEOUT_SEC = float(os.environ.get("OSRM_TIMEOUT_SEC", "12"))
OSRM_PAUSE_SEC = float(os.environ.get("OSRM_PAUSE_SEC", "0.15"))  # lịch sự với public server
ROAD_DIST_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "road_dist_cache.json"
)


def _haversine_km(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Khoảng cách đường chim bay (Haversine) giữa 2 toạ độ (lat, lng), km."""
    lat1, lon1 = p1
    lat2, lon2 = p2
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _fast_km(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Xấp xỉ equirectangular — fallback khi không có đường đi OSRM."""
    lat1, lon1 = p1
    lat2, lon2 = p2
    if lat1 == lat2 and lon1 == lon2:
        return 0.0
    x = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) * 0.5))
    y = lat2 - lat1
    return _DEG_TO_KM * math.sqrt(x * x + y * y)


def _max_pairwise_km(points: list[tuple[float, float]]) -> float:
    """Bán kính phục vụ (max pairwise) — dùng chim bay, chỉ để tham khảo."""
    best = 0.0
    n = len(points)
    for i in range(n):
        pi = points[i]
        for j in range(i + 1, n):
            d = _fast_km(pi, points[j])
            if d > best:
                best = d
    return best


def _load_road_cache() -> dict[str, float]:
    try:
        if os.path.exists(ROAD_DIST_CACHE_FILE):
            with open(ROAD_DIST_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # key "coreA|coreB" → km
                return {str(k): float(v) for k, v in data.items() if v is not None}
    except Exception as e:
        print(f"[stats] Không đọc road_dist_cache: {e}")
    return {}


def _save_road_cache(cache: dict[str, float]) -> None:
    try:
        with open(ROAD_DIST_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        print(f"[stats] Không ghi road_dist_cache: {e}")


def _pair_key(a: str, b: str) -> str:
    return f"{a}|{b}" if a < b else f"{b}|{a}"


def _osrm_route_km(p1: tuple[float, float], p2: tuple[float, float]) -> float | None:
    """Gọi OSRM /route/v1/driving → khoảng cách đường đi (km).
    Toạ độ OSRM: lon,lat. Trả None nếu lỗi / không route được."""
    # lon,lat ; lon,lat
    coords = f"{p1[1]},{p1[0]};{p2[1]},{p2[0]}"
    url = (
        f"{OSRM_BASE_URL}/route/v1/driving/{coords}"
        f"?overview=false&alternatives=false&steps=false"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ccts-stats/1.0"})
        with urllib.request.urlopen(req, timeout=OSRM_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("code") != "Ok":
            return None
        routes = payload.get("routes") or []
        if not routes:
            return None
        meters = routes[0].get("distance")
        if meters is None:
            return None
        return float(meters) / 1000.0
    except Exception as e:
        print(f"[stats] OSRM lỗi {p1}->{p2}: {e}")
        return None


class _DistCache:
    """Khoảng cách giữa 2 mã trạm.

    Ưu tiên: cache đĩa (road) → OSRM road → equirectangular fallback.
    Mỗi cặp chỉ gọi OSRM tối đa 1 lần / process; kết quả ghi đĩa để job
    0h các ngày sau tái sử dụng (tập trạm ít đổi).
    """

    __slots__ = ("_pts", "_mem", "_disk", "_osrm_ok", "_osrm_fail", "_fallback")

    def __init__(self, pts_by_core: dict[str, tuple[float, float]], disk: dict[str, float] | None = None):
        self._pts = pts_by_core
        self._mem: dict[str, float] = {}
        self._disk = disk if disk is not None else _load_road_cache()
        self._osrm_ok = 0
        self._osrm_fail = 0
        self._fallback = 0

    def leg(self, a: str, b: str) -> float:
        if a == b:
            return 0.0
        key = _pair_key(a, b)
        if key in self._mem:
            return self._mem[key]
        if key in self._disk:
            d = self._disk[key]
            self._mem[key] = d
            return d

        pa = self._pts.get(a)
        pb = self._pts.get(b)
        if pa is None or pb is None:
            self._mem[key] = float("inf")
            return float("inf")

        d: float | None = None
        if OSRM_ENABLED:
            d = _osrm_route_km(pa, pb)
            if d is not None:
                self._osrm_ok += 1
                # lịch sự với public server
                if OSRM_PAUSE_SEC > 0:
                    time.sleep(OSRM_PAUSE_SEC)
            else:
                self._osrm_fail += 1

        if d is None:
            d = _fast_km(pa, pb)
            self._fallback += 1
        else:
            # chỉ ghi đĩa khi là road distance thật
            self._disk[key] = round(d, 3)

        self._mem[key] = d
        return d

    def flush_disk(self) -> None:
        if self._disk is not None:
            _save_road_cache(self._disk)

    def stats(self) -> dict:
        return {
            "osrm_ok": self._osrm_ok,
            "osrm_fail": self._osrm_fail,
            "fallback_bird": self._fallback,
            "disk_entries": len(self._disk),
            "osrm_enabled": OSRM_ENABLED,
            "osrm_base": OSRM_BASE_URL,
        }


def _filter_last_n_days(df: pd.DataFrame, n_days: int = CHART_LOOKBACK_DAYS) -> pd.DataFrame:
    """Create Date trong n ngày gần nhất, không gồm ngày hiện tại (đã cắt ở stats_data)."""
    if df is None or df.empty or "Create Date" not in df.columns:
        return df
    today_0h = datetime.now(VN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    # Ngày cuối cùng có thể có data = hôm qua
    end = (today_0h - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (today_0h - timedelta(days=n_days)).strftime("%Y-%m-%d")
    return df[(df["Create Date"] >= start) & (df["Create Date"] <= end)].copy()


def _filter_cp(df: pd.DataFrame, cp_type: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if cp_type == "all":
        return df
    if "cp_type" not in df.columns:
        return df
    return df[df["cp_type"] == cp_type].copy()


def aggregate_daily_by_region(df: pd.DataFrame) -> dict[str, Any]:
    empty = {
        "labels": [],
        "regions": list(ALLOWED_REGIONS),
        "datasets": {r: [] for r in ALLOWED_REGIONS},
        "total_tickets": 0,
        "date_range": {"from": None, "to": None},
    }
    if df is None or df.empty:
        return empty

    df = df[df["Region"].isin(_ALLOWED_SET)].copy()
    if df.empty:
        return empty

    pivot = (
        df.groupby(["Create Date", "Region"])
        .size()
        .unstack(fill_value=0)
        .sort_index()
    )
    for r in ALLOWED_REGIONS:
        if r not in pivot.columns:
            pivot[r] = 0
    pivot = pivot[list(ALLOWED_REGIONS)]

    labels = list(pivot.index.astype(str))
    datasets = {r: [int(x) for x in pivot[r].tolist()] for r in ALLOWED_REGIONS}

    return {
        "labels": labels,
        "regions": list(ALLOWED_REGIONS),
        "datasets": datasets,
        "total_tickets": int(len(df)),
        "date_range": {
            "from": labels[0] if labels else None,
            "to": labels[-1] if labels else None,
        },
    }


def aggregate_daily_by_tech_per_region(
    df: pd.DataFrame,
    tech_by_region: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if df is None:
        df = pd.DataFrame()

    df = df[df["Region"].isin(_ALLOWED_SET)].copy() if not df.empty else df
    all_dates = sorted(df["Create Date"].unique().tolist()) if not df.empty else []

    for region in ALLOWED_REGIONS:
        sub = df[df["Region"] == region] if not df.empty else df
        known = list((tech_by_region or {}).get(region, []))
        actual = sorted(sub["Tech"].dropna().unique().tolist()) if not sub.empty else []
        techs = []
        seen = set()
        for t in known + actual:
            t = str(t).strip()
            if not t or t in seen or is_excluded_tech(t):
                continue
            seen.add(t)
            techs.append(t)

        if sub.empty or not all_dates:
            result[region] = {
                "labels": all_dates,
                "techs": techs,
                "datasets": {t: [0] * len(all_dates) for t in techs},
            }
            continue

        pivot = (
            sub.groupby(["Create Date", "Tech"])
            .size()
            .unstack(fill_value=0)
            .reindex(all_dates, fill_value=0)
        )
        datasets = {}
        for t in techs:
            datasets[t] = (
                [int(x) for x in pivot[t].tolist()] if t in pivot.columns
                else [0] * len(all_dates)
            )
        result[region] = {"labels": all_dates, "techs": techs, "datasets": datasets}

    return result


def aggregate_daily_total(df: pd.DataFrame) -> dict[str, Any]:
    """Chuỗi TỔNG (gộp tất cả khu vực) theo ngày — dùng cho biểu đồ 1 đường
    "Tổng cộng". Kèm các chỉ số phụ (TB/ngày, đỉnh, đáy, chênh lệch ngày kề
    trước) để frontend vẽ thêm nhãn số liệu / điểm nhấn trực quan trên từng
    ngày thay vì chỉ có 1 đường trơn."""
    empty = {
        "labels": [],
        "total": [],
        "deltas": [],
        "avg": 0,
        "max_value": 0,
        "max_date": None,
        "min_value": 0,
        "min_date": None,
        "date_range": {"from": None, "to": None},
    }
    if df is None or df.empty:
        return empty

    df = df[df["Region"].isin(_ALLOWED_SET)].copy()
    if df.empty:
        return empty

    counts = df.groupby("Create Date").size().sort_index()
    labels = list(counts.index.astype(str))
    values = [int(x) for x in counts.tolist()]
    if not values:
        return empty

    avg = round(sum(values) / len(values), 2)
    max_value = max(values)
    min_value = min(values)
    max_date = labels[values.index(max_value)]
    min_date = labels[values.index(min_value)]
    deltas = [None] + [values[i] - values[i - 1] for i in range(1, len(values))]

    return {
        "labels": labels,
        "total": values,
        "deltas": deltas,
        "avg": avg,
        "max_value": max_value,
        "max_date": max_date,
        "min_value": min_value,
        "min_date": min_date,
        "date_range": {"from": labels[0], "to": labels[-1]},
    }


# Khoảng cách đường chim bay > ngưỡng này giữa 2 trạm kề nhau (trong cùng
# 1 ngày của 1 KT) được coi là NHIỄU toạ độ — KT chỉ phụ trách 1 tỉnh/KV,
# nên 2 trạm hợp lệ không thể cách nhau hơn ~200 km (một số tỉnh rất rộng). Cặp nhiễu bị bỏ khỏi
# tổng km và khỏi tập trạm dùng tính bán kính / TB.
MAX_LEGIT_LEG_KM = 200.0


def _filter_outlier_stations(
    pts_by_core: dict[str, tuple[float, float]],
    max_km: float = MAX_LEGIT_LEG_KM,
) -> dict[str, tuple[float, float]]:
    """Loại trạm toạ độ sai: giữ trạm nằm trong bán kính max_km quanh tâm
    (trung bình lat/lng) của chính tập trạm KT. 1 vòng lọc là đủ để loại
    outlier đơn lẻ (nhầm tỉnh / null-island)."""
    if len(pts_by_core) <= 1:
        return pts_by_core
    n = len(pts_by_core)
    lat_sum = 0.0
    lng_sum = 0.0
    for lat, lng in pts_by_core.values():
        lat_sum += lat
        lng_sum += lng
    cen = (lat_sum / n, lng_sum / n)
    kept = {
        core: pt
        for core, pt in pts_by_core.items()
        if _fast_km(cen, pt) <= max_km
    }
    return kept if kept else pts_by_core


def _empty_tech_row(tech: str, region: str, ticket_count: int) -> dict:
    return {
        "tech": tech,
        "region": region,
        "ticket_count": ticket_count,
        "coord_ticket_count": 0,
        "unique_stations": 0,
        "total_km": 0.0,
        "avg_leg_km": 0.0,
        "km_per_ticket": 0.0,
        "service_radius_km": 0.0,
        "leg_count": 0,
        "noise_legs": 0,
    }


def aggregate_tech_travel_workload(df: pd.DataFrame, coords_map: dict | None) -> dict[str, Any]:
    """So sánh khối lượng công việc THỰC TẾ của từng KT — không chỉ đếm số
    ticket mà còn ước tính QUÃNG ĐƯỜNG di chuyển giữa các trạm trong từng
    ngày (dựa toạ độ StationCoords.json qua stats_data.get_coords_map()).

    Ý tưởng: 2 KT cùng số ticket trong 30 ngày, nhưng 1 người ở thành phố
    (trạm sát nhau) và 1 người ở miền núi (trạm cách 40–50 km) thì người
    miền núi vất vả hơn — metric "avg_leg_km" (TB km mỗi chặng hợp lệ)
    thể hiện rõ điều đó.

    Cách tính (đã lọc nhiễu + tối ưu):
      1. Lấy ticket có toạ độ; map core → (lat, lng) 1 lần.
      2. Unique station → loại outlier (> 200 km khỏi tâm tập trạm KT).
      3. Với từng ngày: sắp theo Create Time, cộng chặng kề nhau ≤ 200 km
         (cặp > 200 km = nhiễu, bỏ). Cùng trạm = 0 km, không gọi công thức.
      4. Khoảng cách cache theo cặp mã trạm (_DistCache) — mỗi cặp chỉ
         tính 1 lần dù lặp lại nhiều ngày.
      5. Khoảng cách ưu tiên OSRM đường đi thực tế (miễn phí) + cache đĩa
         road_dist_cache.json; fallback equirectangular nếu OSRM lỗi.
      6. avg_leg_km / unique_stations / service_radius_km như trước.

    Job 0h/ngày: OSRM chỉ gọi cho cặp trạm CHƯA có trong cache → ngày
    sau gần như chỉ đọc đĩa. Trả {"techs": [...], "coverage": {...}}.
    """
    empty = {
        "techs": [],
        "coverage": {
            "tickets_total": 0,
            "tickets_with_coords": 0,
            "pct": 0,
            "noise_legs_dropped": 0,
            "max_legit_km": MAX_LEGIT_LEG_KM,
        },
    }
    if df is None or df.empty or not coords_map:
        return empty
    if "Station Code" not in df.columns or "Tech" not in df.columns:
        return empty

    d = df[df["Region"].isin(_ALLOWED_SET)].copy()
    d = d[~d["Tech"].apply(is_excluded_tech)]
    if d.empty:
        return empty

    # Resolve core + coords 1 lần cho toàn bộ frame (tránh apply lặp trong loop)
    cores = d["Station Code"].map(_extract_core_station_code)
    # coords_map value = {"lat": .., "lng": ..} → tuple; None nếu thiếu
    def _to_pt(core):
        if not core:
            return None
        c = coords_map.get(core)
        if not c:
            return None
        try:
            return (float(c["lat"]), float(c["lng"]))
        except (KeyError, TypeError, ValueError):
            return None

    pts_series = cores.map(_to_pt)
    d = d.assign(_core=cores, _pt=pts_series)
    d["_dt"] = d["Create Time"].map(_parse_create_time)

    tickets_total = int(len(d))
    has_pt = d["_pt"].notna()
    tickets_with_coords = int(has_pt.sum())
    noise_legs_total = 0
    road_disk = _load_road_cache()
    dist_stats_acc = {"osrm_ok": 0, "osrm_fail": 0, "fallback_bird": 0}
    last_dist: _DistCache | None = None

    results = []
    for (tech, region), g in d.groupby(["Tech", "Region"], sort=False):
        ticket_count = int(len(g))
        g_coord = g.loc[g["_pt"].notna()]
        if g_coord.empty:
            results.append(_empty_tech_row(tech, region, ticket_count))
            continue

        # Unique station → (lat, lng); lọc outlier
        pts_by_core: dict[str, tuple[float, float]] = {}
        for core, pt in zip(g_coord["_core"].to_numpy(), g_coord["_pt"].to_numpy()):
            if core and core not in pts_by_core and pt is not None:
                pts_by_core[core] = pt
        pts_by_core = _filter_outlier_stations(pts_by_core)
        if not pts_by_core:
            results.append(_empty_tech_row(tech, region, ticket_count))
            continue

        unique_stations = len(pts_by_core)
        dist = _DistCache(pts_by_core, disk=road_disk)
        last_dist = dist

        # Ticket thuộc trạm hợp lệ + có Create Date / time
        mask_valid = g_coord["_core"].isin(pts_by_core.keys())
        g_valid = g_coord.loc[mask_valid]
        valid_coord_count = int(len(g_valid))

        total_km = 0.0
        leg_count = 0
        noise_legs = 0

        if valid_coord_count and "Create Date" in g_valid.columns:
            # Sắp 1 lần theo ngày + thời gian, rồi duyệt sequential theo ngày
            ordered = g_valid.dropna(subset=["_dt"]).sort_values(["Create Date", "_dt"])
            # groupby Create Date trên frame đã sort → thứ tự trong nhóm giữ nguyên
            for _, day_g in ordered.groupby("Create Date", sort=False):
                cores_day = day_g["_core"].to_numpy()
                prev = None
                for core in cores_day:
                    if prev is None:
                        prev = core
                        continue
                    leg = dist.leg(prev, core)
                    prev = core
                    if leg > MAX_LEGIT_LEG_KM:
                        noise_legs += 1
                        continue
                    total_km += leg
                    leg_count += 1

        noise_legs_total += noise_legs
        st = dist.stats()
        dist_stats_acc["osrm_ok"] += st["osrm_ok"]
        dist_stats_acc["osrm_fail"] += st["osrm_fail"]
        dist_stats_acc["fallback_bird"] += st["fallback_bird"]
        avg_leg = round(total_km / leg_count, 2) if leg_count else 0.0
        radius_km = (
            _max_pairwise_km(list(pts_by_core.values()))
            if unique_stations > 1
            else 0.0
        )

        results.append({
            "tech": tech,
            "region": region,
            "ticket_count": ticket_count,
            "coord_ticket_count": valid_coord_count,
            "unique_stations": unique_stations,
            "total_km": round(total_km, 1),
            "avg_leg_km": avg_leg,
            "km_per_ticket": round(total_km / valid_coord_count, 2) if valid_coord_count else 0.0,
            "service_radius_km": round(radius_km, 1),
            "leg_count": leg_count,
            "noise_legs": noise_legs,
        })

    if last_dist is not None:
        last_dist.flush_disk()
    dist_stats_acc["disk_entries"] = len(road_disk)
    dist_stats_acc["osrm_base"] = OSRM_BASE_URL
    print(
        f"[stats] Road dist: OSRM ok={dist_stats_acc['osrm_ok']} "
        f"fail={dist_stats_acc['osrm_fail']} "
        f"fallback_bird={dist_stats_acc['fallback_bird']} "
        f"disk={dist_stats_acc['disk_entries']} base={OSRM_BASE_URL}"
    )

    results.sort(key=lambda r: (r["avg_leg_km"], r["total_km"]), reverse=True)
    return {
        "techs": results,
        "coverage": {
            "tickets_total": tickets_total,
            "tickets_with_coords": tickets_with_coords,
            "pct": round(100 * tickets_with_coords / tickets_total, 1) if tickets_total else 0,
            "noise_legs_dropped": noise_legs_total,
            "max_legit_km": MAX_LEGIT_LEG_KM,
            "distance_mode": "road_osrm" if OSRM_ENABLED else "bird_flight",
            "osrm": {
                "ok": dist_stats_acc.get("osrm_ok", 0),
                "fail": dist_stats_acc.get("osrm_fail", 0),
                "fallback_bird": dist_stats_acc.get("fallback_bird", 0),
                "disk_entries": dist_stats_acc.get("disk_entries", 0),
                "base": dist_stats_acc.get("osrm_base", OSRM_BASE_URL),
            },
        },
    }


def _payload_for_df(df: pd.DataFrame, tech_by_region: dict | None) -> dict[str, Any]:
    agg = aggregate_daily_by_region(df)
    agg["by_region"] = aggregate_daily_by_tech_per_region(df, tech_by_region=tech_by_region or {})
    agg["total_series"] = aggregate_daily_total(df)
    return agg


def build_volume_payload_from_cache(cache: dict[str, Any]) -> dict[str, Any]:
    """Xây payload Chart.js từ cache v2 (tickets + meta)."""
    meta = cache.get("meta") or {}
    tech_by_region = meta.get("tech_by_region") or {}
    df = records_to_tickets_df(cache.get("tickets") or [])
    if not df.empty and "Region" in df.columns:
        df = df[df["Region"].apply(is_managed_region)].copy()
    df = _filter_last_n_days(df, CHART_LOOKBACK_DAYS)

    if df.empty:
        empty = _payload_for_df(df, tech_by_region)
        return {
            "cp_type": "all",
            "by_cp_type": {"all": empty, "ev": empty, "bss": empty},
            **empty,
            "tech_workload": aggregate_tech_travel_workload(df, None),
            "scrape_days": meta.get("lookback_days"),
            "chart_days": CHART_LOOKBACK_DAYS,
            "source": meta.get("source", "unknown"),
            "generated_at": meta.get("generated_at"),
            "counts": {"all": 0, "ev": 0, "bss": 0},
            "meta": meta,
        }

    df_all = df
    df_ev = _filter_cp(df, "ev")
    df_bss = _filter_cp(df, "bss")

    by_cp = {
        "all": _payload_for_df(df_all, tech_by_region),
        "ev": _payload_for_df(df_ev, tech_by_region),
        "bss": _payload_for_df(df_bss, tech_by_region),
    }
    root = dict(by_cp["all"])
    root["by_cp_type"] = by_cp
    root["cp_type"] = "all"
    # Quãng đường tính 1 LẦN trên toàn bộ ticket (không tách EV/BSS) vì 1 KT
    # có thể xử lý cả 2 loại trong cùng 1 chuyến đi — tách theo cp_type sẽ
    # làm quãng đường mỗi loại bị ước lượng sai (thấp hơn thực tế).
    root["tech_workload"] = aggregate_tech_travel_workload(df_all, get_coords_map())
    root["scrape_days"] = meta.get("lookback_days")
    root["chart_days"] = CHART_LOOKBACK_DAYS
    root["source"] = meta.get("source", "unknown")
    root["generated_at"] = meta.get("generated_at")
    root["counts"] = {
        "all": int(len(df_all)),
        "ev": int(len(df_ev)),
        "bss": int(len(df_bss)),
    }
    root["meta"] = {
        "start_time": meta.get("start_time"),
        "end_time": meta.get("end_time"),
        "end_date_exclusive": meta.get("end_date_exclusive"),
        "accounts_ok": meta.get("accounts_ok"),
        "accounts_fail": meta.get("accounts_fail"),
    }
    return root