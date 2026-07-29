"""
Module thống kê & trực quan dữ liệu ticket CCTS.

- Cào lịch sử qua api_client.export_and_download_tickets (fallback file mẫu)
- Map Station Code → Region qua StationAssignments.json (github_data_store)
- Aggregate số ticket theo ngày (Create Time) × khu vực
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
SAMPLE_XLSX = os.path.join(os.path.dirname(__file__), "Tickets_esmanager_20260728_201743.xlsx")

# Chỉ hiển thị các khu vực chuẩn trong StationAssignments.json
ALLOWED_REGIONS = ("DNA-QNA", "DNI-VTU", "LDO-BTH", "EC", "Mtay", "HCM")
_ALLOWED_SET = set(ALLOWED_REGIONS)

# Không phải kỹ thuật viên / không thuộc quản lý → loại khỏi biểu đồ KT
EXCLUDED_TECH_NAMES = {
    "unassigned",
    "bình dương",
    "binh duong",
}

# Prefix fallback → mã khu vực chuẩn (khi station chưa có trong StationAssignments)
_REGION_PREFIX_RULES: list[tuple[str, str]] = [
    ("B.HCM", "HCM"),
    ("B.DNI", "DNI-VTU"),
    ("B.VTU", "DNI-VTU"),
    ("B.DNA", "DNA-QNA"),
    ("B.QNA", "DNA-QNA"),
    ("B.LDO", "LDO-BTH"),
    ("B.BTH", "LDO-BTH"),
    ("B.STR", "Mtay"),
    ("B.VLO", "Mtay"),
    ("B.AGG", "Mtay"),
    ("B.CTH", "Mtay"),
    ("B.KG", "Mtay"),
    ("B.CMU", "Mtay"),
    ("C.NTH", "EC"),
    ("C.QNA", "DNA-QNA"),
    ("C.DNG", "EC"),
    ("C.PYE", "EC"),
    ("C.KHA", "EC"),
]


def _extract_core_station_code(station_code: str | None) -> str | None:
    try:
        from utils import extract_core_station_code
        return extract_core_station_code(station_code)
    except Exception:
        if not station_code or (isinstance(station_code, float) and pd.isna(station_code)):
            return None
        return str(station_code).strip().upper()


def _infer_region_prefix(station_code: str | None) -> str | None:
    if not station_code or (isinstance(station_code, float) and pd.isna(station_code)):
        return None
    code = str(station_code).strip().upper()
    for prefix, region in _REGION_PREFIX_RULES:
        if code.startswith(prefix.upper()):
            return region
    return None


def _get_static_maps() -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    """
    Trả (region_map, tech_map, tech_by_region) từ StationAssignments.json.
    tech_by_region: {region: [engineer_name, ...]}
    """
    try:
        from github_data_store import load_static_data
        # (coords_map, tech_map, region_map, cp_model_map, tech_by_region)
        _, tech_map, region_map, _, tech_by_region = load_static_data()
        return region_map or {}, tech_map or {}, tech_by_region or {}
    except Exception as e:
        print(f"[stats] Không tải được StationAssignments từ GitHub: {e}")
        return {}, {}, {}


def _get_region_map() -> dict[str, str]:
    region_map, _, _ = _get_static_maps()
    return region_map


def resolve_region(station_code: str | None, region_map: dict[str, str] | None = None) -> str | None:
    """
    Ưu tiên region_map (StationAssignments), fallback prefix.
    Chỉ trả về khu vực trong ALLOWED_REGIONS; bỏ 'KV không quản lý' và vùng khác.
    """
    region = None
    core = _extract_core_station_code(station_code)
    if region_map and core and core in region_map:
        raw = str(region_map[core] or "").strip()
        if raw and raw != "KV không quản lý":
            region = raw
    if not region:
        region = _infer_region_prefix(station_code)

    if region in _ALLOWED_SET:
        return region
    return None  # ngoài danh sách cho phép → loại


def _parse_create_time(val) -> datetime | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def process_ticket_information(
    df: pd.DataFrame,
    region_map: dict[str, str] | None = None,
    tech_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Chuẩn hóa Ticket Information → Ticket ID, Station Code, Region, Tech, Create Date."""
    if df is None or df.empty:
        return pd.DataFrame(
            columns=["Ticket ID", "Station Code", "Create Time", "Region", "Tech", "Create Date"]
        )

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    for col in ("Ticket ID", "Station Code", "Create Time", "SLA Status", "Severity"):
        if col not in df.columns:
            df[col] = None

    if region_map is None or tech_map is None:
        rm, tm, _ = _get_static_maps()
        if region_map is None:
            region_map = rm
        if tech_map is None:
            tech_map = tm

    def _tech_for(station_code):
        core = _extract_core_station_code(station_code)
        if core and tech_map and core in tech_map:
            name = str(tech_map[core] or "").strip()
            return name if name else "Unassigned"
        return "Unassigned"

    df["Region"] = df["Station Code"].apply(lambda s: resolve_region(s, region_map))
    df["Tech"] = df["Station Code"].apply(_tech_for)
    # Chỉ giữ ticket thuộc 6 khu vực được phép
    df = df[df["Region"].notna()].copy()

    df["_dt"] = df["Create Time"].apply(_parse_create_time)
    df = df.dropna(subset=["_dt"]).copy()
    df["Create Date"] = df["_dt"].dt.strftime("%Y-%m-%d")
    df = df.drop(columns=["_dt"])

    if "Ticket ID" in df.columns:
        df["Ticket ID"] = df["Ticket ID"].astype(str).str.strip()
        df = df.drop_duplicates(subset=["Ticket ID"])

    return df


def aggregate_daily_by_region(df: pd.DataFrame) -> dict[str, Any]:
    """Pivot ngày × khu vực → payload Chart.js (chỉ ALLOWED_REGIONS)."""
    if df is None or df.empty:
        return {
            "labels": [],
            "regions": [],
            "datasets": {},
            "total_tickets": 0,
            "date_range": {"from": None, "to": None},
        }

    # Đảm bảo chỉ còn region hợp lệ
    df = df[df["Region"].isin(_ALLOWED_SET)].copy()
    if df.empty:
        return {
            "labels": [],
            "regions": [],
            "datasets": {},
            "total_tickets": 0,
            "date_range": {"from": None, "to": None},
        }

    pivot = (
        df.groupby(["Create Date", "Region"])
        .size()
        .unstack(fill_value=0)
        .sort_index()
    )

    # Cột theo thứ tự cố định; region không có data trong kỳ vẫn hiện = 0
    for r in ALLOWED_REGIONS:
        if r not in pivot.columns:
            pivot[r] = 0
    pivot = pivot[list(ALLOWED_REGIONS)]

    labels = list(pivot.index.astype(str))
    regions = list(ALLOWED_REGIONS)
    datasets: dict[str, list[int]] = {r: [int(x) for x in pivot[r].tolist()] for r in regions}
    # Không đưa Total toàn công ty vào datasets (che khu vực nhỏ)

    return {
        "labels": labels,
        "regions": regions,
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
    """
    Với mỗi khu vực: pivot Create Date × Tech → datasets (không có Total).
    {
      "HCM": {"labels": [...], "techs": [...], "datasets": {"KT A": [..], ...}},
      ...
    }
    """
    result: dict[str, Any] = {}
    if df is None or df.empty:
        for r in ALLOWED_REGIONS:
            result[r] = {"labels": [], "techs": [], "datasets": {}}
        return result

    df = df[df["Region"].isin(_ALLOWED_SET)].copy()
    all_dates = sorted(df["Create Date"].unique().tolist()) if not df.empty else []

    for region in ALLOWED_REGIONS:
        sub = df[df["Region"] == region]
        # Danh sách KT ưu tiên từ StationAssignments, bổ sung KT có ticket thực tế
        known_techs = list(tech_by_region.get(region, [])) if tech_by_region else []
        actual_techs = sorted(sub["Tech"].dropna().unique().tolist()) if not sub.empty else []
        def _is_excluded_tech(name: str) -> bool:
            n = name.strip().lower()
            if n in EXCLUDED_TECH_NAMES:
                return True
            # khớp gần: chứa "unassigned" / "bình dương"
            if "unassigned" in n:
                return True
            if "bình dương" in n or "binh duong" in n:
                return True
            return False

        techs = []
        seen = set()
        for t in known_techs + actual_techs:
            t = str(t).strip()
            if not t or t in seen:
                continue
            if _is_excluded_tech(t):
                continue
            seen.add(t)
            techs.append(t)

        if sub.empty or not all_dates:
            result[region] = {"labels": all_dates, "techs": techs, "datasets": {t: [0] * len(all_dates) for t in techs}}
            continue

        pivot = (
            sub.groupby(["Create Date", "Tech"])
            .size()
            .unstack(fill_value=0)
            .reindex(all_dates, fill_value=0)
        )
        datasets: dict[str, list[int]] = {}
        for t in techs:
            if t in pivot.columns:
                datasets[t] = [int(x) for x in pivot[t].tolist()]
            else:
                datasets[t] = [0] * len(all_dates)

        result[region] = {
            "labels": all_dates,
            "techs": techs,
            "datasets": datasets,
        }

    return result


def _pick_ccts_account() -> tuple[str, str]:
    """Lấy account đầu tiên từ config.CCTS_ACCOUNTS."""
    try:
        from config import CCTS_ACCOUNTS
        if CCTS_ACCOUNTS:
            acc = CCTS_ACCOUNTS[0]
            return acc["username"], acc["password"]
    except Exception:
        pass
    return "esmanager", "Ccts123."


# Cào 1 lần ~2 tháng; biểu đồ đường chỉ lấy 10 ngày gần nhất trong đó
SCRAPE_LOOKBACK_DAYS = 62  # ~2 tháng
CHART_LOOKBACK_DAYS = 10


def default_stats_time_range(*, until_now: bool = False) -> tuple[str, str]:
    """
    Khoảng cào (giờ VN) — 2 tháng gần nhất:
    - until_now=False (job 0h): → 0h hôm nay
    - until_now=True (restart): → hiện tại
    """
    now = datetime.now(VN_TZ)
    today_0h = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_0h = today_0h - timedelta(days=SCRAPE_LOOKBACK_DAYS)
    end = now if until_now else today_0h
    return (
        start_0h.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


async def fetch_stats_tickets(
    start_time: str | None = None,
    end_time: str | None = None,
    use_sample_fallback: bool = True,
    until_now: bool = False,
) -> tuple[pd.DataFrame, str]:
    """
    Cào ticket [start_time, end_time] (giờ VN).
    Dùng CCTS_API_LOCK để không chạy song song với ccts_data.
    """
    if start_time is None or end_time is None:
        d_start, d_end = default_stats_time_range(until_now=until_now)
        if start_time is None:
            start_time = d_start
        if end_time is None:
            end_time = d_end
    print(f"[stats] Khoảng cào: {start_time} → {end_time} (VN)")

    source = "live"
    username, password = _pick_ccts_account()

    try:
        from api_client import CCTSClient
        from ccts_gate import CCTS_API_LOCK

        async with CCTS_API_LOCK:
            print("[stats] Đã giữ CCTS_API_LOCK — bắt đầu export...")
            client = CCTSClient(username=username, password=password)
            await client.login()
            dfs = await client.export_and_download_tickets(
                start_time=start_time,
                end_time=end_time,
                ticket_status=None,
            )
            print("[stats] Nhả CCTS_API_LOCK.")

        if not dfs or "Ticket Information" not in dfs or dfs["Ticket Information"].empty:
            raise RuntimeError("Export trả về rỗng")
        raw = dfs["Ticket Information"]
        print(f"[stats] Cào live OK: {len(raw)} dòng (account={username})")
    except Exception as e:
        print(f"[stats] Cào live thất bại: {e}")
        if not use_sample_fallback or not os.path.exists(SAMPLE_XLSX):
            raise
        print(f"[stats] Fallback → file mẫu: {SAMPLE_XLSX}")
        raw = pd.read_excel(SAMPLE_XLSX, sheet_name="Ticket Information")
        source = "sample"

    region_map, tech_map, tech_by_region = _get_static_maps()
    processed = process_ticket_information(raw, region_map=region_map, tech_map=tech_map)
    processed.attrs["tech_by_region"] = tech_by_region
    return processed, source


def _filter_last_n_days(df: pd.DataFrame, n_days: int = CHART_LOOKBACK_DAYS) -> pd.DataFrame:
    """Lọc ticket có Create Date trong n ngày gần nhất (tính từ 0h hôm nay lùi lại)."""
    if df is None or df.empty or "Create Date" not in df.columns:
        return df
    today_0h = datetime.now(VN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start = (today_0h - timedelta(days=n_days - 1)).strftime("%Y-%m-%d")
    end = today_0h.strftime("%Y-%m-%d")
    return df[(df["Create Date"] >= start) & (df["Create Date"] <= end)].copy()


def _get_coords_map() -> dict:
    try:
        from github_data_store import load_static_data
        coords_map, _, _, _, _ = load_static_data()
        return coords_map or {}
    except Exception as e:
        print(f"[stats] Không tải StationCoords: {e}")
        return {}


def _parse_coords_from_address(address) -> tuple[float, float] | None:
    """Parse lat;lng từ Address dạng '...|10.947188;106.60611'."""
    if address is None or (isinstance(address, float) and pd.isna(address)):
        return None
    text = str(address)
    # ưu tiên đoạn sau | nếu có
    if "|" in text:
        text = text.split("|")[-1]
    import re
    m = re.search(r"(-?\d{1,3}\.\d+)\s*[;,]\s*(-?\d{1,3}\.\d+)", text)
    if not m:
        return None
    try:
        lat, lng = float(m.group(1)), float(m.group(2))
        if abs(lat) <= 90 and abs(lng) <= 180:
            return lat, lng
    except ValueError:
        pass
    return None


def build_overdue_heatmap(df: pd.DataFrame, coords_map: dict | None = None) -> dict[str, Any]:
    """
    Ticket SLA Status = Overdue → aggregate theo trạm + tọa độ.
    points: [{station_code, lat, lng, overdue_count, major_count, region}, ...]
    """
    empty = {"points": [], "max_overdue": 0, "total_overdue": 0, "stations_with_coords": 0, "stations_missing_coords": 0}
    if df is None or df.empty:
        return empty

    if coords_map is None:
        coords_map = _get_coords_map()

    work = df.copy()
    if "SLA Status" not in work.columns:
        return empty
    work["_sla"] = work["SLA Status"].astype(str).str.strip().str.lower()
    overdue = work[work["_sla"] == "overdue"].copy()
    if overdue.empty:
        return empty

    if "Severity" not in overdue.columns:
        overdue["Severity"] = ""
    overdue["_major"] = overdue["Severity"].astype(str).str.strip().str.lower().eq("major")

    # group by station
    rows = []
    missing = 0
    for station_code, g in overdue.groupby("Station Code", dropna=False):
        core = _extract_core_station_code(station_code)
        lat = lng = None
        if core and coords_map and core in coords_map:
            c = coords_map[core]
            try:
                lat, lng = float(c["lat"]), float(c["lng"])
            except Exception:
                lat = lng = None
        if lat is None and "Address" in g.columns:
            parsed = _parse_coords_from_address(g.iloc[0].get("Address"))
            if parsed:
                lat, lng = parsed
        if lat is None or lng is None:
            missing += 1
            continue
        region = ""
        if "Region" in g.columns and g["Region"].notna().any():
            region = str(g["Region"].dropna().iloc[0])
        rows.append({
            "station_code": str(station_code or core or ""),
            "core_code": core or "",
            "lat": lat,
            "lng": lng,
            "overdue_count": int(len(g)),
            "major_count": int(g["_major"].sum()),
            "region": region,
        })

    max_od = max((r["overdue_count"] for r in rows), default=0)
    return {
        "points": rows,
        "max_overdue": int(max_od),
        "total_overdue": int(sum(r["overdue_count"] for r in rows)),
        "stations_with_coords": len(rows),
        "stations_missing_coords": missing,
    }


def build_daily_volume_payload(
    df: pd.DataFrame,
    source: str = "unknown",
    tech_by_region: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """
    - Biểu đồ đường / KT: chỉ 10 ngày gần nhất trong dữ liệu đã cào (~2 tháng).
    - Heatmap overdue: toàn bộ ticket Overdue trong khoảng cào 2 tháng.
    """
    if tech_by_region is None:
        tech_by_region = df.attrs.get("tech_by_region") if hasattr(df, "attrs") else None
        if tech_by_region is None:
            _, _, tech_by_region = _get_static_maps()

    df_chart = _filter_last_n_days(df, CHART_LOOKBACK_DAYS)
    agg = aggregate_daily_by_region(df_chart)
    agg["by_region"] = aggregate_daily_by_tech_per_region(df_chart, tech_by_region=tech_by_region or {})
    agg["heatmap"] = build_overdue_heatmap(df)  # full scrape window
    agg["scrape_days"] = SCRAPE_LOOKBACK_DAYS
    agg["chart_days"] = CHART_LOOKBACK_DAYS
    agg["source"] = source
    agg["generated_at"] = datetime.now(VN_TZ).isoformat(timespec="seconds")
    return agg


# =====================================================================
# Cache file — cào 1 lần/ngày lúc 0h, trang /stats chỉ đọc cache
# =====================================================================
STATS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "stats_daily_cache.json")

_memory_cache: dict[str, Any] | None = None


def save_stats_cache(payload: dict[str, Any]) -> None:
    global _memory_cache
    _memory_cache = payload
    try:
        import json
        with open(STATS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        print(f"[stats] Đã lưu cache → {STATS_CACHE_FILE}")
    except Exception as e:
        print(f"[stats] Lỗi ghi cache: {e}")


def load_stats_cache() -> dict[str, Any] | None:
    """Đọc cache bộ nhớ → file. Trả None nếu chưa có."""
    global _memory_cache
    if _memory_cache is not None:
        return _memory_cache
    try:
        import json
        if os.path.exists(STATS_CACHE_FILE):
            with open(STATS_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "labels" in data:
                _memory_cache = data
                return data
    except Exception as e:
        print(f"[stats] Lỗi đọc cache: {e}")
    return None


def get_cached_daily_volume() -> dict[str, Any] | None:
    """API dùng hàm này — KHÔNG cào, chỉ đọc cache."""
    return load_stats_cache()


async def refresh_stats_cache(
    start_time: str | None = None,
    end_time: str | None = None,
    until_now: bool = False,
) -> dict[str, Any]:
    """
    Cào dữ liệu 10 ngày, aggregate, ghi cache.
    - until_now=True: restart / admin test (đến thời điểm hiện tại).
    - until_now=False: job 0h (đến 0h hôm nay).
    """
    df, source = await fetch_stats_tickets(
        start_time=start_time, end_time=end_time, until_now=until_now,
    )
    payload = build_daily_volume_payload(df, source)
    save_stats_cache(payload)
    print(
        f"[stats] Cache cập nhật: {payload.get('total_tickets', 0)} ticket, "
        f"source={source}, range={payload.get('date_range')}"
    )
    return payload


# Tương thích ngược: nếu ai đó còn gọi get_daily_volume_stats → ưu tiên cache
async def get_daily_volume_stats(
    start_time: str | None = None,
    end_time: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    if not force_refresh:
        cached = load_stats_cache()
        if cached is not None:
            return cached
    return await refresh_stats_cache(start_time=start_time, end_time=end_time)


if __name__ == "__main__":
    import asyncio
    import json

    async def _main():
        payload = await refresh_stats_cache()
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    asyncio.run(_main())
