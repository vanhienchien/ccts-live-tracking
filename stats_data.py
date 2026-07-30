"""
stats_data.py — chỉ chịu trách nhiệm đăng nhập + cào + chuẩn hóa ticket.

- Cào tất cả tài khoản trong config.CCTS_ACCOUNTS, gộp + khử trùng Ticket ID.
- Cửa sổ: 45 ngày kết thúc tại 0h hôm nay (KHÔNG gồm ngày hiện tại).
- Lọc BSS.No2, map Region/Tech, phân loại EV/BSS.
- Ghi cache thô (danh sách ticket đã chuẩn hóa) cho các module biểu đồ dùng.

Không vẽ chart ở đây.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
SCRAPE_LOOKBACK_DAYS = 45
STATS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "stats_daily_cache.json")
SAMPLE_XLSX = os.path.join(os.path.dirname(__file__), "Tickets_esmanager_20260728_201743.xlsx")

ALLOWED_REGIONS = ("DNA-QNA", "DNI-VTU", "LDO-BTH", "EC", "Mtay", "HCM")
_ALLOWED_SET = set(ALLOWED_REGIONS)

EXCLUDED_TECH_NAMES = {
    "unassigned",
    "bình dương",
    "binh duong",
}

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

_memory_cache: dict[str, Any] | None = None


def _extract_core_station_code(station_code: str | None) -> str | None:
    try:
        from utils import extract_core_station_code
        return extract_core_station_code(station_code)
    except Exception:
        if not station_code or (isinstance(station_code, float) and pd.isna(station_code)):
            return None
        return str(station_code).strip().upper()


def _get_static_maps() -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    try:
        from github_data_store import load_static_data
        _, tech_map, region_map, _, tech_by_region = load_static_data()
        return region_map or {}, tech_map or {}, tech_by_region or {}
    except Exception as e:
        print(f"[stats] Không tải StationAssignments: {e}")
        return {}, {}, {}


def _infer_region_prefix(station_code: str | None) -> str | None:
    if not station_code or (isinstance(station_code, float) and pd.isna(station_code)):
        return None
    code = str(station_code).strip().upper()
    for prefix, region in _REGION_PREFIX_RULES:
        if code.startswith(prefix.upper()):
            return region
    return None


def is_managed_region(region: str | None) -> bool:
    """Chỉ 6 KV được quản lý — loại 'KV không quản lý' và mọi region lạ."""
    if not region:
        return False
    r = str(region).strip()
    if r in _ALLOWED_SET:
        return True
    # chuẩn hóa nhẹ
    if r.lower() in {"kv không quản lý", "kv khong quan ly", "không quản lý", "unmanaged"}:
        return False
    return False


def resolve_region(station_code: str | None, region_map: dict[str, str] | None = None) -> str | None:
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
    return None


def classify_cp_type(cp_id) -> str:
    if cp_id is None or (isinstance(cp_id, float) and pd.isna(cp_id)):
        return "ev"
    s = str(cp_id).strip().upper()
    return "bss" if s.startswith("BSS") else "ev"


def is_excluded_tech(name: str) -> bool:
    n = (name or "").strip().lower()
    if not n or n in EXCLUDED_TECH_NAMES:
        return True
    if "unassigned" in n:
        return True
    if "bình dương" in n or "binh duong" in n:
        return True
    return False


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


def scrape_time_range() -> tuple[str, str, str]:
    """
    [start, end) giờ VN:
    - end = 0h hôm nay (KHÔNG gồm ngày hiện tại)
    - start = end - 45 ngày
    """
    now = datetime.now(VN_TZ)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=SCRAPE_LOOKBACK_DAYS)
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d"),
    )


def _pick_accounts() -> list:
    try:
        from config import CCTS_ACCOUNTS
        if CCTS_ACCOUNTS:
            return list(CCTS_ACCOUNTS)
    except Exception as e:
        print(f"[stats] Không đọc CCTS_ACCOUNTS: {e}")
    return [{"username": "esmanager", "password": "Ccts123.", "role": "esmanager"}]


def process_ticket_information(
    df: pd.DataFrame,
    region_map=None,
    tech_map=None,
    end_date_exclusive: str | None = None,
) -> pd.DataFrame:
    cols_out = [
        "Ticket ID", "Station Code", "Charge Point ID", "Create Time", "Create Date",
        "Region", "Tech", "cp_type", "SLA Status", "Severity", "Problem Description", "Address",
        "Error Code",
    ]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols_out)

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    for col in (
        "Ticket ID", "Station Code", "Create Time", "SLA Status", "Severity",
        "Charge Point ID", "Problem Description", "Address", "Error Code",
    ):
        if col not in df.columns:
            df[col] = None

    pd_col = df["Problem Description"].astype(str).str.strip()
    before = len(df)
    df = df[~pd_col.str.startswith("BSS.No2")].copy()
    dropped = before - len(df)
    if dropped:
        print(f"[stats] Loại {dropped} ticket BSS.No2")

    if region_map is None or tech_map is None:
        rm, tm, _ = _get_static_maps()
        region_map = region_map if region_map is not None else rm
        tech_map = tech_map if tech_map is not None else tm

    def _tech_for(station_code):
        core = _extract_core_station_code(station_code)
        if core and tech_map and core in tech_map:
            name = str(tech_map[core] or "").strip()
            return name if name else "Unassigned"
        return "Unassigned"

    df["Region"] = df["Station Code"].apply(lambda s: resolve_region(s, region_map))
    df = df[df["Region"].apply(is_managed_region)].copy()
    df["Tech"] = df["Station Code"].apply(_tech_for)
    df["cp_type"] = df["Charge Point ID"].apply(classify_cp_type)

    df["_dt"] = df["Create Time"].apply(_parse_create_time)
    df = df.dropna(subset=["_dt"]).copy()
    df["Create Date"] = df["_dt"].dt.strftime("%Y-%m-%d")
    df = df.drop(columns=["_dt"])

    if end_date_exclusive:
        df = df[df["Create Date"] < end_date_exclusive].copy()

    if "Ticket ID" in df.columns:
        df["Ticket ID"] = df["Ticket ID"].astype(str).str.strip()
        df = df.drop_duplicates(subset=["Ticket ID"])

    keep = [c for c in cols_out if c in df.columns]
    return df[keep].reset_index(drop=True)


OPEN_STATUSES = {
    "open",
    "appointment",
    "pending for asp close",
    "pending for spare parts close",
}
CLOSED_STATUS = "pending for local team close"


def _norm_status(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).strip().lower()


def extract_closed_tickets_from_events(
    events_df: pd.DataFrame,
    end_date_exclusive: str,
    lookback_days: int = 30,
) -> pd.DataFrame:
    """
    Duyệt Events Record (mới → cũ) theo từng Ticket ID:
    - Gặp trạng thái mở → ticket đang mở, bỏ.
    - Gặp 'Pending for local team close' → đã đóng, Close Time = Create Time event đó.
    - Dừng khi Create Time event < (end - lookback_days).
    Chỉ giữ ticket đóng có Close Time trong [end-lookback, end).
    """
    cols = ["Ticket ID", "Close Time", "Close Date"]
    if events_df is None or events_df.empty:
        return pd.DataFrame(columns=cols)

    df = events_df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    if "Ticket ID" not in df.columns or "Ticket Status" not in df.columns or "Create Time" not in df.columns:
        return pd.DataFrame(columns=cols)

    df["Ticket ID"] = df["Ticket ID"].astype(str).str.strip()
    df["_dt"] = df["Create Time"].apply(_parse_create_time)
    df = df.dropna(subset=["_dt"])
    df["_status"] = df["Ticket Status"].apply(_norm_status)

    end_dt = datetime.strptime(end_date_exclusive, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=lookback_days)

    closed_rows = []
    for tid, g in df.groupby("Ticket ID", sort=False):
        g = g.sort_values("_dt", ascending=False)
        result = None  # None | ("open",) | ("closed", close_dt)
        for _, row in g.iterrows():
            evt_dt = row["_dt"]
            if evt_dt is None:
                continue
            # Dừng khi quá cửa sổ 30 ngày (event cũ hơn start)
            if evt_dt < start_dt:
                break
            st = row["_status"]
            if st in OPEN_STATUSES:
                result = ("open", None)
                break
            if st == CLOSED_STATUS:
                result = ("closed", evt_dt)
                break
        if result and result[0] == "closed" and result[1] is not None:
            close_dt = result[1]
            if start_dt <= close_dt < end_dt:
                closed_rows.append({
                    "Ticket ID": tid,
                    "Close Time": close_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "Close Date": close_dt.strftime("%Y-%m-%d"),
                })

    return pd.DataFrame(closed_rows) if closed_rows else pd.DataFrame(columns=cols)


def enrich_closed_with_ticket_info(
    closed_df: pd.DataFrame,
    ticket_info_df: pd.DataFrame,
    spare_ids: set,
    appointment_ids: set,
    region_map,
    tech_map,
) -> pd.DataFrame:
    """Ghép Station/Region/Tech/cp_type/SLA + cờ chờ VT / hẹn khách."""
    if closed_df is None or closed_df.empty:
        return pd.DataFrame()

    info = ticket_info_df.copy() if ticket_info_df is not None else pd.DataFrame()
    if not info.empty:
        info.columns = [str(c).strip() for c in info.columns]
        if "Ticket ID" in info.columns:
            info["Ticket ID"] = info["Ticket ID"].astype(str).str.strip()
            info = info.drop_duplicates(subset=["Ticket ID"])

    def _tech_for(station_code):
        core = _extract_core_station_code(station_code)
        if core and tech_map and core in tech_map:
            name = str(tech_map[core] or "").strip()
            return name if name else "Unassigned"
        return "Unassigned"

    rows = []
    info_by_id = {}
    if not info.empty and "Ticket ID" in info.columns:
        info_by_id = {str(r["Ticket ID"]): r for _, r in info.iterrows()}

    for _, c in closed_df.iterrows():
        tid = str(c["Ticket ID"])
        info_row = info_by_id.get(tid)
        station = info_row.get("Station Code") if info_row is not None else None
        cp_id = info_row.get("Charge Point ID") if info_row is not None else None
        sla = info_row.get("SLA Status") if info_row is not None else None
        severity = info_row.get("Severity") if info_row is not None else None
        region = resolve_region(station, region_map)
        if not is_managed_region(region):
            continue
        tech = _tech_for(station)
        sla_s = str(sla or "").strip().lower()
        has_spare = tid in spare_ids
        has_appt = tid in appointment_ids
        is_od = sla_s == "overdue"
        rows.append({
            "Ticket ID": tid,
            "Close Time": c.get("Close Time"),
            "Close Date": c.get("Close Date"),
            "Station Code": station,
            "Region": region,
            "Tech": tech,
            "cp_type": classify_cp_type(cp_id),
            "SLA Status": sla,
            "is_overdue": is_od,
            "has_spare_wait": has_spare,
            "has_appointment": has_appt,
            # Overdue + (chờ VT hoặc hẹn khách) — đánh dấu trên biểu đồ
            "is_overdue_excuse": bool(is_od and (has_spare or has_appt)),
            "Severity": severity,
        })
    return pd.DataFrame(rows)


async def _export_one_account(username, password, start_time, end_time) -> dict | None:
    """Trả dict các sheet DataFrame (đã gắn _source_account)."""
    from api_client import CCTSClient

    client = CCTSClient(username=username, password=password)
    await client.login()
    dfs = await client.export_and_download_tickets(
        start_time=start_time,
        end_time=end_time,
        ticket_status=None,
    )
    if not dfs:
        return None
    out = {}
    for name, df in dfs.items():
        if df is None or (hasattr(df, "empty") and df.empty):
            continue
        part = df.copy()
        part["_source_account"] = username
        out[name] = part
    return out if out else None


async def fetch_all_accounts_tickets(
    start_time=None,
    end_time=None,
    use_sample_fallback=True,
):
    """
    Cào multi-account → gộp Ticket Information + Events Record + Spare Parts.
    Trả (processed_open_window_df, source, meta, closed_enriched_df).
    """
    start_time, end_time, end_date_ex = scrape_time_range()
    print(f"[stats] Khoảng cào [start, end): {start_time} → {end_time} (không gồm {end_date_ex})")

    accounts = _pick_accounts()
    ti_frames = []
    ev_frames = []
    sp_frames = []
    ap_frames = []
    ok_accounts = []
    fail_accounts = []

    try:
        from ccts_gate import CCTS_API_LOCK
    except Exception:
        CCTS_API_LOCK = None

    async def _run_exports():
        for acc in accounts:
            username = acc.get("username") or ""
            password = acc.get("password") or ""
            if not username:
                continue
            try:
                print(f"[stats] Đang cào account: {username} ...")
                bundle = await _export_one_account(username, password, start_time, end_time)
                if not bundle or "Ticket Information" not in bundle:
                    fail_accounts.append(username)
                    print(f"[stats]   → thiếu Ticket Information: {username}")
                    continue
                ti_frames.append(bundle["Ticket Information"])
                if "Events Record" in bundle:
                    ev_frames.append(bundle["Events Record"])
                if "Spare Parts Record" in bundle:
                    sp_frames.append(bundle["Spare Parts Record"])
                if "Appointment" in bundle:
                    ap_frames.append(bundle["Appointment"])
                ok_accounts.append(username)
                print(
                    f"[stats]   → TI={len(bundle['Ticket Information'])}, "
                    f"EV={len(bundle.get('Events Record', []))}, "
                    f"SP={len(bundle.get('Spare Parts Record', []))}, "
                    f"AP={len(bundle.get('Appointment', []))}"
                )
            except Exception as e:
                fail_accounts.append(username)
                print(f"[stats]   → lỗi {username}: {e}")

    if CCTS_API_LOCK is not None:
        async with CCTS_API_LOCK:
            print("[stats] Giữ CCTS_API_LOCK")
            await _run_exports()
            print("[stats] Nhả CCTS_API_LOCK")
    else:
        await _run_exports()

    meta = {
        "start_time": start_time,
        "end_time": end_time,
        "end_date_exclusive": end_date_ex,
        "accounts_ok": ok_accounts,
        "accounts_fail": fail_accounts,
        "lookback_days": SCRAPE_LOOKBACK_DAYS,
    }

    source = "live"
    if not ti_frames:
        if use_sample_fallback and os.path.exists(SAMPLE_XLSX):
            print(f"[stats] Fallback file mẫu: {SAMPLE_XLSX}")
            raw = pd.read_excel(SAMPLE_XLSX, sheet_name="Ticket Information")
            events_raw = pd.read_excel(SAMPLE_XLSX, sheet_name="Events Record")
            try:
                spare_raw = pd.read_excel(SAMPLE_XLSX, sheet_name="Spare Parts Record")
            except Exception:
                spare_raw = pd.DataFrame()
            try:
                appt_raw = pd.read_excel(SAMPLE_XLSX, sheet_name="Appointment")
            except Exception:
                appt_raw = pd.DataFrame()
            source = "sample"
        else:
            raise RuntimeError("Không cào được ticket từ bất kỳ account nào")
    else:
        raw = pd.concat(ti_frames, ignore_index=True)
        if "Ticket ID" in raw.columns:
            raw["Ticket ID"] = raw["Ticket ID"].astype(str).str.strip()
            before = len(raw)
            raw = raw.drop_duplicates(subset=["Ticket ID"])
            print(f"[stats] TI gộp {before} → {len(raw)}")
        events_raw = pd.concat(ev_frames, ignore_index=True) if ev_frames else pd.DataFrame()
        spare_raw = pd.concat(sp_frames, ignore_index=True) if sp_frames else pd.DataFrame()
        appt_raw = pd.concat(ap_frames, ignore_index=True) if ap_frames else pd.DataFrame()
        if not events_raw.empty and "Ticket ID" in events_raw.columns:
            events_raw["Ticket ID"] = events_raw["Ticket ID"].astype(str).str.strip()

    spare_ids = set()
    if spare_raw is not None and not spare_raw.empty and "Ticket ID" in spare_raw.columns:
        spare_ids = set(spare_raw["Ticket ID"].astype(str).str.strip().tolist())
    appointment_ids = set()
    if appt_raw is not None and not appt_raw.empty and "Ticket ID" in appt_raw.columns:
        appointment_ids = set(appt_raw["Ticket ID"].astype(str).str.strip().tolist())

    region_map, tech_map, tech_by_region = _get_static_maps()
    processed = process_ticket_information(
        raw,
        region_map=region_map,
        tech_map=tech_map,
        end_date_exclusive=end_date_ex,
    )

    closed_basic = extract_closed_tickets_from_events(
        events_raw, end_date_exclusive=end_date_ex, lookback_days=30
    )
    closed_enriched = enrich_closed_with_ticket_info(
        closed_basic, raw, spare_ids, appointment_ids, region_map, tech_map
    )
    print(
        f"[stats] Closed 30d: {len(closed_enriched)} "
        f"(overdue={int(closed_enriched['is_overdue'].sum()) if len(closed_enriched) else 0}, "
        f"spare={int(closed_enriched['has_spare_wait'].sum()) if len(closed_enriched) else 0}, "
        f"appt={int(closed_enriched['has_appointment'].sum()) if len(closed_enriched) else 0})"
    )

    meta["tech_by_region"] = tech_by_region
    meta["row_count"] = int(len(processed))
    meta["closed_count"] = int(len(closed_enriched))
    print(f"[stats] Sau chuẩn hóa TI: {len(processed)} ticket (source={source})")
    return processed, source, meta, closed_enriched


def tickets_df_to_records(df: pd.DataFrame) -> list:
    if df is None or df.empty:
        return []
    out = df.where(pd.notna(df), None)
    return out.to_dict(orient="records")


def records_to_tickets_df(records) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)


def save_stats_cache(payload: dict) -> None:
    global _memory_cache
    _memory_cache = payload
    try:
        with open(STATS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        print(f"[stats] Đã lưu cache → {STATS_CACHE_FILE}")
    except Exception as e:
        print(f"[stats] Lỗi ghi cache: {e}")


def load_stats_cache():
    global _memory_cache
    if _memory_cache is not None:
        return _memory_cache
    try:
        if os.path.exists(STATS_CACHE_FILE):
            with open(STATS_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and ("tickets" in data or "labels" in data):
                _memory_cache = data
                return data
    except Exception as e:
        print(f"[stats] Lỗi đọc cache: {e}")
    return None


def get_cached_tickets_df() -> pd.DataFrame:
    cache = load_stats_cache()
    if not cache:
        return pd.DataFrame()
    if "tickets" in cache:
        return records_to_tickets_df(cache.get("tickets") or [])
    return pd.DataFrame()


def get_cached_meta() -> dict:
    cache = load_stats_cache() or {}
    return cache.get("meta") or {}


async def refresh_stats_cache(**_kwargs) -> dict:
    """Cào multi-account 45 ngày, không gồm hôm nay → cache v2 (tickets + closed)."""
    df, source, meta, closed_df = await fetch_all_accounts_tickets()
    tech_by_region = meta.pop("tech_by_region", {}) or {}
    if not tech_by_region:
        _, _, tech_by_region = _get_static_maps()

    payload = {
        "version": 2,
        "tickets": tickets_df_to_records(df),
        "closed_tickets": tickets_df_to_records(closed_df),
        "meta": {
            **meta,
            "source": source,
            "generated_at": datetime.now(VN_TZ).isoformat(timespec="seconds"),
            "tech_by_region": tech_by_region,
            "allowed_regions": list(ALLOWED_REGIONS),
        },
    }
    save_stats_cache(payload)
    print(
        f"[stats] Cache v2: TI={meta.get('row_count', 0)}, "
        f"closed30d={meta.get('closed_count', 0)}, source={source}"
    )
    return payload


def get_cached_daily_volume():
    cache = load_stats_cache()
    if not cache:
        return None
    if cache.get("version") == 2 or "tickets" in cache:
        try:
            from stats_charts_volume import build_volume_payload_from_cache
            return build_volume_payload_from_cache(cache)
        except Exception as e:
            print(f"[stats] Lỗi build volume từ cache: {e}")
            return None
    return cache


async def get_daily_volume_stats(force_refresh: bool = False, **kwargs):
    if not force_refresh:
        cached = get_cached_daily_volume()
        if cached is not None:
            return cached
    await refresh_stats_cache(**kwargs)
    out = get_cached_daily_volume()
    if out is None:
        raise RuntimeError("Không xây được payload thống kê sau khi cào")
    return out


if __name__ == "__main__":
    import asyncio

    async def _main():
        p = await refresh_stats_cache()
        print("tickets:", len(p.get("tickets") or []))
        print("meta:", p.get("meta"))

    asyncio.run(_main())
