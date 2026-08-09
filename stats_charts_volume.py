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

import os
import json
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
# Module này chỉ lấy 30 ngày gần nhất trong data 60 ngày.
CHART_LOOKBACK_DAYS = 30
# Tây Nguyên do nhà thầu quản lý — không đưa vào biểu đồ khối lượng KT.
WORKLOAD_EXCLUDED_REGIONS = frozenset({
    "Tây Nguyên", "Tay Nguyen", "TÂY NGUYÊN", "tay nguyen",
})
EARTH_RADIUS_KM = 6371.0



# ~km mỗi độ vĩ độ. Equirectangular đủ chính xác ở cự ly tỉnh VN.
_DEG_TO_KM = EARTH_RADIUS_KM * math.pi / 180.0

# Hệ số ước lượng đường đi ≈ đường chim bay × ROAD_FACTOR.
# Thực tế đường bộ/xe máy thường dài hơn chim bay ~20–40% (đường vòng,
# địa hình). 1.30 là mức trung bình hợp lý cho so sánh tương đối giữa KT;
# có thể chỉnh bằng env ROAD_DISTANCE_FACTOR.

ROAD_FACTOR = float(os.environ.get("ROAD_DISTANCE_FACTOR", "1.30"))


def load_engineer_homes() -> tuple[dict[str, tuple[float, float]], dict[str, str]]:
    """Lấy toạ độ nhà KT từ GitHub (EngineerCoords.json) qua github_data_store.

    Trả:
      homes: {tên KT: (lat, lng)}
      rollup: {thành viên: team lead}
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


def _home_station_km(
    home: tuple[float, float],
    station: tuple[float, float],
) -> float:
    """Ước lượng km nhà → trạm (chim bay × ROAD_FACTOR)."""
    return _fast_km(home, station) * ROAD_FACTOR


def _nearest_home_km(
    station: tuple[float, float],
    candidate_homes: list[tuple[float, float]],
) -> float:
    """Khoảng cách tới nhà gần nhất trong danh sách (đã × ROAD_FACTOR)."""
    if not candidate_homes:
        return 0.0
    best_bird = min(_fast_km(h, station) for h in candidate_homes)
    return best_bird * ROAD_FACTOR



def _haversine_km(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Khoảng cách đường chim bay (Haversine), km."""
    lat1, lon1 = p1
    lat2, lon2 = p2
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _fast_km(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Xấp xỉ equirectangular (chim bay), km. Cùng trạm → 0."""
    lat1, lon1 = p1
    lat2, lon2 = p2
    if lat1 == lat2 and lon1 == lon2:
        return 0.0
    x = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) * 0.5))
    y = lat2 - lat1
    return _DEG_TO_KM * math.sqrt(x * x + y * y)


def _road_km(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Ước lượng quãng đường đi ≈ chim bay × ROAD_FACTOR."""
    return _fast_km(p1, p2) * ROAD_FACTOR


def _max_pairwise_km(points: list[tuple[float, float]]) -> float:
    """Bán kính phục vụ (max pairwise chim bay) — tham khảo địa bàn rộng/hẹp."""
    best = 0.0
    n = len(points)
    for i in range(n):
        pi = points[i]
        for j in range(i + 1, n):
            d = _fast_km(pi, points[j])
            if d > best:
                best = d
    return best


class _DistCache:
    """Cache khoảng cách ước lượng theo cặp mã trạm (core).

    Mỗi cặp chỉ tính 1 lần trong process. Dùng _road_km (chim bay × hệ số).
    """

    __slots__ = ("_pts", "_cache")

    def __init__(self, pts_by_core: dict[str, tuple[float, float]]):
        self._pts = pts_by_core
        self._cache: dict[tuple[str, str], float] = {}

    def leg(self, a: str, b: str) -> float:
        if a == b:
            return 0.0
        key = (a, b) if a < b else (b, a)
        d = self._cache.get(key)
        if d is not None:
            return d
        pa = self._pts.get(a)
        pb = self._pts.get(b)
        if pa is None or pb is None:
            d = float("inf")
        else:
            d = _road_km(pa, pb)
        self._cache[key] = d
        return d


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


def _empty_tech_row(tech: str, region: str, ticket_count: int, has_home: bool = False) -> dict:
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
        "has_home": bool(has_home),
        "nearest_station": None,
        "nearest_km": None,
        "farthest_station": None,
        "farthest_km": None,
    }


def aggregate_tech_travel_workload(df: pd.DataFrame, coords_map: dict | None) -> dict[str, Any]:
    """Khối lượng di chuyển theo KT: khoảng cách NHÀ → TRẠM.

    Thay vì cộng chặng giữa các trạm kề nhau trong ngày (phụ thuộc thứ tự
    ticket), lấy toạ độ nhà riêng (EngineerCoords.json) làm tâm:
      - Mỗi ticket Tại trạm có toạ độ: km = nhà(KT) → trạm × ROAD_FACTOR
      - Loại nhiễu: chim bay > MAX_LEGIT_LEG_KM (200 km)
      - avg_leg_km = TB km nhà→trạm; total_km = tổng các chặng hợp lệ

    Đội Lâm Đồng (team lead "Nguyễn Hải Nguyên"):
      - JSON lồng danh sách thành viên + toạ độ nhà từng người
      - Mỗi trạm đo tới nhà THÀNH VIÊN GẦN NHẤT
      - Ticket/metric của mọi thành viên GỘP về lead "Nguyễn Hải Nguyên"

    Chỉ ticket handling_type=onsite (Từ xa bỏ). Fallback: nếu không có nhà
    KT thì giữ logic cũ (chặng giữa trạm) cho KT đó.
    """
    empty = {
        "techs": [],
        "coverage": {
            "tickets_total": 0,
            "tickets_with_coords": 0,
            "pct": 0,
            "noise_legs_dropped": 0,
            "max_legit_km": MAX_LEGIT_LEG_KM,
            "mode": "home_to_station",
        },
    }
    if df is None or df.empty or not coords_map:
        return empty
    if "Station Code" not in df.columns or "Tech" not in df.columns:
        return empty

    homes, rollup = load_engineer_homes()
    # Tập nhà theo team lead (để đo nearest)
    team_homes: dict[str, list[tuple[float, float]]] = {}
    for member, lead in rollup.items():
        if member in homes:
            team_homes.setdefault(lead, []).append(homes[member])

    d = df[df["Region"].isin(_ALLOWED_SET)].copy()
    d = d[~d["Region"].astype(str).isin(WORKLOAD_EXCLUDED_REGIONS)].copy()
    d = d[~d["Tech"].apply(is_excluded_tech)]
    if "handling_type" in d.columns:
        before = len(d)
        is_remote = d["handling_type"].astype(str).str.lower().eq("remote")
        d = d.loc[~is_remote].copy()
        dropped_remote = before - len(d)
        if dropped_remote:
            print(f"[stats] Workload: loại {dropped_remote} ticket Từ xa (remote)")
    if d.empty:
        return empty

    # Rollup tên KT thành viên đội → team lead
    def _credit_tech(name: str) -> str:
        n = str(name or "").strip()
        return rollup.get(n, n)

    d = d.copy()
    d["Tech"] = d["Tech"].map(_credit_tech)

    cores = d["Station Code"].map(_extract_core_station_code)

    def _to_pt(core):
        if not core:
            return None
        cobj = coords_map.get(core)
        if not cobj:
            return None
        try:
            return (float(cobj["lat"]), float(cobj["lng"]))
        except (KeyError, TypeError, ValueError):
            return None

    pts_series = cores.map(_to_pt)
    d = d.assign(_core=cores, _pt=pts_series)

    tickets_total = int(len(d))
    has_pt = d["_pt"].notna()
    tickets_with_coords = int(has_pt.sum())
    noise_legs_total = 0

    results = []
    for (tech, region), g in d.groupby(["Tech", "Region"], sort=False):
        ticket_count = int(len(g))
        g_coord = g.loc[g["_pt"].notna()]

        # Tâm đo: nhà KT hoặc cụm nhà đội (xác định sớm để flag has_home)
        is_team_lead = tech in team_homes and len(team_homes[tech]) > 0
        home_pt = homes.get(tech)
        candidate_homes = team_homes.get(tech) if is_team_lead else ([home_pt] if home_pt else [])
        has_home = bool(candidate_homes)

        if g_coord.empty:
            results.append(_empty_tech_row(tech, region, ticket_count, has_home=has_home))
            continue

        # Unique station points
        pts_by_core: dict[str, tuple[float, float]] = {}
        for core, pt in zip(g_coord["_core"].to_numpy(), g_coord["_pt"].to_numpy()):
            if core and core not in pts_by_core and pt is not None:
                pts_by_core[str(core)] = pt

        # Lọc nhiễu: trạm > 200km chim bay khỏi tâm (nhà / nhà gần nhất trong đội)
        if candidate_homes:
            filtered: dict[str, tuple[float, float]] = {}
            for core, pt in pts_by_core.items():
                bird = min(_fast_km(h, pt) for h in candidate_homes)
                if bird > MAX_LEGIT_LEG_KM:
                    noise_legs_total += 1
                    continue
                filtered[core] = pt
            pts_by_core = filtered
        else:
            pts_by_core = _filter_outlier_stations(pts_by_core)

        if not pts_by_core:
            results.append(_empty_tech_row(tech, region, ticket_count, has_home=has_home))
            continue

        unique_stations = len(pts_by_core)
        mask_valid = g_coord["_core"].isin(pts_by_core.keys())
        g_valid = g_coord.loc[mask_valid]
        valid_coord_count = int(len(g_valid))

        total_km = 0.0
        leg_count = 0
        nearest_station = None
        nearest_km = None
        farthest_station = None
        farthest_km = None

        def _track_ext(core, leg_km):
            nonlocal nearest_station, nearest_km, farthest_station, farthest_km
            if nearest_km is None or leg_km < nearest_km:
                nearest_km = leg_km
                nearest_station = core
            if farthest_km is None or leg_km > farthest_km:
                farthest_km = leg_km
                farthest_station = core

        if candidate_homes and valid_coord_count:
            # Mỗi ticket hợp lệ: nhà (gần nhất) → trạm
            for core, pt in zip(g_valid["_core"].to_numpy(), g_valid["_pt"].to_numpy()):
                if pt is None:
                    continue
                if is_team_lead:
                    leg = _nearest_home_km(pt, candidate_homes)
                else:
                    leg = _home_station_km(home_pt, pt)
                bird = leg / ROAD_FACTOR if ROAD_FACTOR else leg
                if bird > MAX_LEGIT_LEG_KM:
                    noise_legs_total += 1
                    continue
                total_km += leg
                leg_count += 1
            # Trạm gần / xa nhất theo unique station (nhà → trạm)
            for core, pt in pts_by_core.items():
                if is_team_lead:
                    leg = _nearest_home_km(pt, candidate_homes)
                else:
                    leg = _home_station_km(home_pt, pt)
                _track_ext(core, leg)
        elif valid_coord_count:
            # Fallback: chặng kề trong ngày (không có toạ độ nhà)
            dist = _DistCache(pts_by_core)
            if "Create Date" in g_valid.columns:
                g_valid = g_valid.copy()
                g_valid["_dt"] = g_valid["Create Time"].map(_parse_create_time)
                ordered = g_valid.dropna(subset=["_dt"]).sort_values(["Create Date", "_dt"])
                for _, day_g in ordered.groupby("Create Date", sort=False):
                    cores_day = day_g["_core"].to_numpy()
                    prev = None
                    for core in cores_day:
                        if prev is None:
                            prev = core
                            continue
                        leg = dist.leg(prev, core)
                        bird = leg / ROAD_FACTOR if ROAD_FACTOR else leg
                        prev = core
                        if bird > MAX_LEGIT_LEG_KM:
                            noise_legs_total += 1
                            continue
                        total_km += leg
                        leg_count += 1
            # nearest/farthest pairwise rough: min/max leg from first station
            cores_list = list(pts_by_core.keys())
            if len(cores_list) >= 2:
                for i, ca in enumerate(cores_list):
                    for cb in cores_list[i + 1:]:
                        leg = dist.leg(ca, cb)
                        bird = leg / ROAD_FACTOR if ROAD_FACTOR else leg
                        if bird > MAX_LEGIT_LEG_KM:
                            continue
                        # track as station pair extremes for display
                        if nearest_km is None or leg < nearest_km:
                            nearest_km = leg
                            nearest_station = f"{ca} ↔ {cb}"
                        if farthest_km is None or leg > farthest_km:
                            farthest_km = leg
                            farthest_station = f"{ca} ↔ {cb}"

        avg_leg = round(total_km / leg_count, 2) if leg_count else 0.0
        if candidate_homes and pts_by_core:
            radius_km = max(
                min(_fast_km(h, pt) for h in candidate_homes) * ROAD_FACTOR
                for pt in pts_by_core.values()
            )
        else:
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
            "noise_legs": 0,
            "home_based": bool(candidate_homes),
            "has_home": bool(candidate_homes),
            "team_lead": is_team_lead,
            "nearest_station": nearest_station,
            "nearest_km": round(nearest_km, 1) if nearest_km is not None else None,
            "farthest_station": farthest_station,
            "farthest_km": round(farthest_km, 1) if farthest_km is not None else None,
        })

    results.sort(key=lambda r: (-r["avg_leg_km"], -r["ticket_count"], r["tech"]))
    pct = round(tickets_with_coords / tickets_total * 100, 1) if tickets_total else 0.0
    return {
        "techs": results,
        "coverage": {
            "tickets_total": tickets_total,
            "tickets_with_coords": tickets_with_coords,
            "pct": pct,
            "noise_legs_dropped": noise_legs_total,
            "max_legit_km": MAX_LEGIT_LEG_KM,
            "mode": "home_to_station",
            "homes_loaded": len(homes),
            "team_rollup": len(rollup),
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