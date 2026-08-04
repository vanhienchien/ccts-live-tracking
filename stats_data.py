"""
stats_data.py — chỉ chịu trách nhiệm đăng nhập + cào + chuẩn hóa ticket.

- Cào CỐ ĐỊNH 2 tài khoản esmanager + itsmanagermt (ccts_shared.STATS_SCRAPE_
  ACCOUNTS) — KHÔNG phụ thuộc config.CCTS_ACCOUNTS, vì lịch cào (0h / khởi
  động) rơi vào giờ không ai dùng tài khoản tổng, nên cố định luôn cho an
  toàn & dễ kiểm soát. Gộp + khử trùng theo Ticket ID.
- Nếu 1 tài khoản chưa cào được ngay, tự động THỬ LẠI theo vòng lặp (tối đa
  MAX_SCRAPE_ROUNDS lần, cách nhau SCRAPE_RETRY_DELAY_SECONDS giây) cho đến
  khi có dữ liệu hoặc hết số vòng cho phép. Nếu sau cùng vẫn KHÔNG cào được
  ticket nào (từ cả 2 tài khoản), GIỮ NGUYÊN cache thống kê cũ (ngày hôm
  qua) thay vì ghi đè bằng dữ liệu rỗng.
- Cửa sổ: 45 ngày kết thúc tại 0h hôm nay (KHÔNG gồm ngày hiện tại).
- Lọc BSS.No2, map Region/Tech, phân loại EV/BSS.
- Ghi cache thô (danh sách ticket đã chuẩn hóa) cho các module biểu đồ dùng.
- Dùng CCTS_API_LOCK dùng chung với ccts_data.py (qua ccts_shared) để 2
  module không bao giờ gọi API CCTS cùng lúc; và STATS_REFRESH_LOCK (single-
  flight) để gộp nhiều lượt cào thống kê gọi gần nhau (khởi động / 0h /
  admin bấm tay) thành 1 lượt duy nhất.

Không vẽ chart ở đây.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from ccts_shared import VN_TZ, CCTS_API_LOCK, STATS_REFRESH_LOCK, STATS_SCRAPE_ACCOUNTS, ClientPool

SCRAPE_LOOKBACK_DAYS = 45
STATS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "stats_daily_cache.json")
SAMPLE_XLSX = os.path.join(os.path.dirname(__file__), "Tickets_esmanager_20260728_201743.xlsx")

# Cào tối đa 10 vòng, chờ 20s giữa các vòng cho tài khoản còn thất bại.
MAX_SCRAPE_ROUNDS = 10
SCRAPE_RETRY_DELAY_SECONDS = 20

# Công ty đã RÚT KHỎI khu vực HCM (08/2026) -> HCM không còn nằm trong danh
# sách khu vực được quản lý; xem thêm ccts_shared.DEPRECATED_REGIONS (nơi
# region_map gốc cũng được tự động chuẩn hoá HCM -> "KV không quản lý").
ALLOWED_REGIONS = ("DNA-QNA", "DNI-VTU", "LDO-BTH", "EC", "Mtay")
_ALLOWED_SET = set(ALLOWED_REGIONS)

EXCLUDED_TECH_NAMES = {
    "unassigned",
    "bình dương",
    "binh duong",
}

_REGION_PREFIX_RULES: list[tuple[str, str]] = [
    # B.HCM đã bị loại: công ty rút khỏi khu vực HCM, xem ccts_shared.DEPRECATED_REGIONS.
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
        from ccts_shared import load_static_data_filtered
        _, tech_map, region_map, _, tech_by_region = load_static_data_filtered()
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
    """Chỉ 5 KV được quản lý — loại 'KV không quản lý' (gồm cả HCM, đã rút
    khỏi từ 08/2026) và mọi region lạ."""
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


_stats_pool = ClientPool()  # pool phiên đăng nhập RIÊNG của stats_data.py


async def _export_one_account(username, password, start_time, end_time) -> dict | None:
    """Export Excel cho 1 tài khoản (tự relogin 1 lần nếu bị đá session, qua
    ClientPool dùng chung). Trả dict các sheet DataFrame (đã gắn
    _source_account), hoặc None nếu thất bại."""

    async def _action(client):
        dfs = await client.export_and_download_tickets(
            start_time=start_time,
            end_time=end_time,
            ticket_status=None,
        )
        if not dfs:
            raise RuntimeError("export_and_download_tickets trả về rỗng")
        return dfs

    dfs, ok = await _stats_pool.call_with_retry(username, password, _action)
    if not ok or not dfs:
        return None

    out = {}
    for name, df in dfs.items():
        if df is None or (hasattr(df, "empty") and df.empty):
            continue
        part = df.copy()
        part["_source_account"] = username
        out[name] = part
    return out if out else None


def _finalize_from_raw(raw, events_raw, spare_raw, appt_raw, meta_extra: dict):
    """Phần xử lý THUẦN (không gọi mạng): chuẩn hóa Ticket Information +
    trích xuất ticket đã đóng 30 ngày. Dùng chung cho cả dữ liệu cào trực
    tiếp lẫn dữ liệu fallback từ file mẫu."""
    end_date_ex = meta_extra["end_date_exclusive"]

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

    meta = {
        **meta_extra,
        "tech_by_region": tech_by_region,
        "row_count": int(len(processed)),
        "closed_count": int(len(closed_enriched)),
    }
    return processed, meta, closed_enriched


def _load_sample_bundle():
    """Đọc file Excel mẫu làm phao cứu sinh CUỐI CÙNG — chỉ dùng khi cào
    thất bại hoàn toàn VÀ không hề có cache cũ nào (ví dụ lần deploy đầu
    tiên). Trả (raw, events_raw, spare_raw, appt_raw) hoặc None nếu không có
    file mẫu."""
    if not os.path.exists(SAMPLE_XLSX):
        return None
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
    return raw, events_raw, spare_raw, appt_raw


async def fetch_all_accounts_tickets(start_time=None, end_time=None):
    """
    Cào 2 tài khoản cố định (ccts_shared.STATS_SCRAPE_ACCOUNTS), gộp + khử
    trùng Ticket Information / Events Record / Spare Parts / Appointment.

    Cào theo VÒNG LẶP: mỗi vòng thử các tài khoản CHƯA thành công; tài khoản
    nào đã có dữ liệu ở vòng trước thì không cào lại. Lặp tối đa
    MAX_SCRAPE_ROUNDS vòng, nghỉ SCRAPE_RETRY_DELAY_SECONDS giây giữa các
    vòng (KHÔNG giữ CCTS_API_LOCK trong lúc nghỉ, để không chặn ccts_data.py
    cào bản đồ realtime mỗi 15').

    Trả (processed_df, source, meta, closed_enriched_df) nếu có ít nhất 1
    tài khoản cào được; trả None nếu SAU TỐI ĐA số vòng vẫn không lấy được
    ticket nào từ bất kỳ tài khoản nào (để refresh_stats_cache() giữ nguyên
    cache cũ thay vì ghi đè dữ liệu rỗng).
    """
    start_time, end_time, end_date_ex = scrape_time_range()
    print(f"[stats] Khoảng cào [start, end): {start_time} → {end_time} (không gồm {end_date_ex})")

    pending = {
        acc["username"]: acc
        for acc in STATS_SCRAPE_ACCOUNTS
        if acc.get("username")
    }
    bundles: dict[str, dict] = {}

    round_no = 0
    while pending and round_no < MAX_SCRAPE_ROUNDS:
        round_no += 1
        print(
            f"[stats] Vòng cào {round_no}/{MAX_SCRAPE_ROUNDS} — "
            f"còn {len(pending)} tài khoản chưa xong: {list(pending)}"
        )
        async with CCTS_API_LOCK:
            for username, acc in list(pending.items()):
                password = acc.get("password") or ""
                print(f"[stats] Đang cào account: {username} ...")
                try:
                    bundle = await _export_one_account(username, password, start_time, end_time)
                except Exception as e:
                    bundle = None
                    print(f"[stats]   → lỗi {username}: {e}")

                if bundle and "Ticket Information" in bundle:
                    bundles[username] = bundle
                    pending.pop(username, None)
                    print(
                        f"[stats]   → OK {username}: TI={len(bundle['Ticket Information'])}, "
                        f"EV={len(bundle.get('Events Record', []))}, "
                        f"SP={len(bundle.get('Spare Parts Record', []))}, "
                        f"AP={len(bundle.get('Appointment', []))}"
                    )
                else:
                    print(f"[stats]   → chưa được: {username} (sẽ thử lại ở vòng sau)")

        if pending and round_no < MAX_SCRAPE_ROUNDS:
            print(f"[stats] Đợi {SCRAPE_RETRY_DELAY_SECONDS}s trước vòng kế tiếp...")
            await asyncio.sleep(SCRAPE_RETRY_DELAY_SECONDS)

    ok_accounts = list(bundles.keys())
    fail_accounts = list(pending.keys())
    if fail_accounts:
        print(f"[stats] Sau {round_no} vòng vẫn chưa cào được: {fail_accounts}")

    meta_extra = {
        "start_time": start_time,
        "end_time": end_time,
        "end_date_exclusive": end_date_ex,
        "accounts_ok": ok_accounts,
        "accounts_fail": fail_accounts,
        "rounds_used": round_no,
        "lookback_days": SCRAPE_LOOKBACK_DAYS,
    }

    if not bundles:
        # Không có tài khoản nào cào được sau tối đa số vòng cho phép.
        # KHÔNG dùng sample fallback ở đây - để refresh_stats_cache() tự
        # quyết định giữ cache cũ (ưu tiên) hoặc mới dùng sample (chót).
        print(f"[stats] Cào thất bại toàn bộ sau {round_no} vòng — không có ticket nào.")
        return None

    ti_frames = [b["Ticket Information"] for b in bundles.values()]
    ev_frames = [b["Events Record"] for b in bundles.values() if "Events Record" in b]
    sp_frames = [b["Spare Parts Record"] for b in bundles.values() if "Spare Parts Record" in b]
    ap_frames = [b["Appointment"] for b in bundles.values() if "Appointment" in b]

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

    source = "live" if not fail_accounts else "live_partial"
    processed, meta, closed_enriched = _finalize_from_raw(raw, events_raw, spare_raw, appt_raw, meta_extra)
    meta["source"] = source
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


def _build_payload(df, source, meta, closed_df) -> dict:
    tech_by_region = meta.pop("tech_by_region", {}) or {}
    if not tech_by_region:
        _, _, tech_by_region = _get_static_maps()
    return {
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


async def _refresh_stats_cache_impl() -> dict:
    """Phần thực thi thật sự (chạy dưới STATS_REFRESH_LOCK)."""
    result = await fetch_all_accounts_tickets()

    if result is None:
        # Cào thất bại HOÀN TOÀN sau tối đa MAX_SCRAPE_ROUNDS vòng.
        old = load_stats_cache()
        if old:
            print(
                "[stats] Cào thất bại toàn bộ — GIỮ NGUYÊN cache thống kê cũ "
                f"(generated_at={old.get('meta', {}).get('generated_at', '?')})."
            )
            return old

        # Chưa từng có cache nào (ví dụ lần deploy đầu tiên) → dùng file mẫu
        # làm phao cứu sinh CUỐI CÙNG để trang /stats không trống trơn.
        sample = _load_sample_bundle()
        if sample is None:
            raise RuntimeError(
                "Không cào được ticket từ bất kỳ tài khoản nào, cũng không có "
                "cache cũ hay file mẫu để hiển thị tạm."
            )
        raw, events_raw, spare_raw, appt_raw = sample
        _, end_time, end_date_ex = scrape_time_range()
        meta_extra = {
            "start_time": None,
            "end_time": end_time,
            "end_date_exclusive": end_date_ex,
            "accounts_ok": [],
            "accounts_fail": [a["username"] for a in STATS_SCRAPE_ACCOUNTS],
            "rounds_used": MAX_SCRAPE_ROUNDS,
            "lookback_days": SCRAPE_LOOKBACK_DAYS,
        }
        processed, meta, closed_df = _finalize_from_raw(raw, events_raw, spare_raw, appt_raw, meta_extra)
        payload = _build_payload(processed, "sample", meta, closed_df)
        save_stats_cache(payload)
        print(f"[stats] Cache v2 (sample fallback): TI={meta.get('row_count', 0)}")
        return payload

    df, source, meta, closed_df = result
    payload = _build_payload(df, source, meta, closed_df)
    save_stats_cache(payload)
    print(
        f"[stats] Cache v2: TI={meta.get('row_count', 0)}, "
        f"closed30d={meta.get('closed_count', 0)}, source={source}"
    )
    return payload


async def refresh_stats_cache(**_kwargs) -> dict:
    """Cào 2 tài khoản cố định, 45 ngày (không gồm hôm nay) → cache v2
    (tickets + closed). Dùng STATS_REFRESH_LOCK kiểu "single-flight": nếu đã
    có 1 lượt cào khác đang chạy (khởi động / lịch 0h / admin bấm tay xảy ra
    gần nhau), lượt gọi này sẽ ĐỢI lượt kia xong rồi dùng luôn kết quả đó,
    thay vì cào 2 lần chồng nhau."""
    if STATS_REFRESH_LOCK.locked():
        print("[stats] Đã có 1 lượt cào thống kê khác đang chạy — đợi kết quả đó, không cào lặp lại.")
        async with STATS_REFRESH_LOCK:
            pass
        return load_stats_cache() or {}

    async with STATS_REFRESH_LOCK:
        return await _refresh_stats_cache_impl()


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
    async def _main():
        p = await refresh_stats_cache()
        print("tickets:", len(p.get("tickets") or []))
        print("meta:", p.get("meta"))

    asyncio.run(_main())