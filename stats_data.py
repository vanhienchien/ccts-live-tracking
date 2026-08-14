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
- Cửa sổ: 60 ngày kết thúc tại 0h hôm nay (KHÔNG gồm ngày hiện tại).
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

SCRAPE_LOOKBACK_DAYS = 60
STATS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "stats_daily_cache.json")
SAMPLE_XLSX = os.path.join(os.path.dirname(__file__), "Tickets_esmanager_20260728_201743.xlsx")

# Cào tối đa 10 vòng, chờ 20s giữa các vòng cho tài khoản còn thất bại.
MAX_SCRAPE_ROUNDS = 10
SCRAPE_RETRY_DELAY_SECONDS = 20

# Giới hạn thời gian chờ CCTS xử lý xong 1 lượt export/tài khoản. Nếu CCTS
# không bao giờ trả status "sẵn sàng" (dữ liệu quá lớn / lỗi phía CCTS),
# vòng chờ bên trong client.export_and_download_tickets() có thể chạy vô
# hạn — mà lệnh gọi đó nằm TRONG CCTS_API_LOCK, nên nếu treo sẽ chặn luôn
# ccts_data.py cào bản đồ realtime mỗi 15'. Bọc bằng asyncio.wait_for để tự
# bỏ cuộc sau EXPORT_TIMEOUT_SECONDS, nhả khoá, rồi để cơ chế vòng lặp
# round-based (MAX_SCRAPE_ROUNDS) thử lại bình thường thay vì treo cả hệ
# thống.
EXPORT_TIMEOUT_SECONDS = int(os.environ.get("EXPORT_TIMEOUT_SECONDS", "180"))

# Công ty đã RÚT KHỎI khu vực HCM (08/2026) -> HCM không còn nằm trong danh
# sách khu vực được quản lý; xem thêm ccts_shared.DEPRECATED_REGIONS (nơi
# region_map gốc cũng được tự động chuẩn hoá HCM -> "KV không quản lý").
ALLOWED_REGIONS = ("DNA-QNA", "DNI-BPH", "LDO-BTH", "Tây Nguyên", "Mtay")
_ALLOWED_SET = set(ALLOWED_REGIONS)

EXCLUDED_TECH_NAMES = {
    "unassigned",
    "bình dương",
    "binh duong",
}

_REGION_PREFIX_RULES: list[tuple[str, str]] = [
    # B.HCM đã bị loại: công ty rút khỏi khu vực HCM, xem ccts_shared.DEPRECATED_REGIONS.
    ("B.DNI", "DNI-BPH"),
    ("B.BPH", "DNI-BPH"),
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
    ("C.NTH", "LDO-BTH"),
    ("C.QNA", "DNA-QNA")
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


def get_coords_map() -> dict[str, dict]:
    """Toạ độ trạm — ước tính quãng đường di chuyển KT."""
    try:
        from ccts_shared import load_static_data_filtered
        coords_map, _, _, _, _ = load_static_data_filtered()
        return coords_map or {}
    except Exception as e:
        print(f"[stats] Không tải StationCoords: {e}")
        return {}


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


ONSITE_HANDLING_ALIASES = {
    "tại trạm", "tai tram", "onsite", "on-site", "on site", "at station", "tại chỗ", "tai cho",
}
REMOTE_HANDLING_ALIASES = {
    "từ xa", "tu xa", "remote", "remotely", "from remote",
}


def normalize_handling_type(val) -> str | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip().lower()
    if not s:
        return None
    if s in ONSITE_HANDLING_ALIASES or any(a in s for a in ("tại trạm", "tai tram", "onsite", "on-site")):
        return "onsite"
    if s in REMOTE_HANDLING_ALIASES or any(a in s for a in ("từ xa", "tu xa", "remote")):
        return "remote"
    return None


def extract_handling_type_map(additional_df: pd.DataFrame | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if additional_df is None or additional_df.empty:
        return out
    df = additional_df.copy()
    df.columns = [str(col).strip() for col in df.columns]
    tid_col = ht_col = None
    for col in df.columns:
        cl = col.lower().replace("_", " ")
        if tid_col is None and cl in {"ticket id", "ticketid", "ticket"}:
            tid_col = col
        if ht_col is None and ("handling" in cl or cl in {"handling type", "handle type"}):
            ht_col = col
    if tid_col is None or ht_col is None:
        print(f"[stats] Additional information thiếu Ticket ID/Handling type (cols={list(df.columns)[:12]})")
        return out
    for _, row in df.iterrows():
        tid = str(row.get(tid_col) or "").strip()
        if not tid or tid.lower() == "nan":
            continue
        norm = normalize_handling_type(row.get(ht_col))
        if norm:
            out[tid] = norm
    print(
        f"[stats] Handling type: {len(out)} ticket "
        f"(onsite={sum(1 for v in out.values() if v == 'onsite')}, "
        f"remote={sum(1 for v in out.values() if v == 'remote')})"
    )
    return out




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
    - start = end - 60 ngày
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
    handling_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    cols_out = [
        "Ticket ID", "Station Code", "Charge Point ID", "Create Time", "Create Date",
        "Region", "Tech", "cp_type", "SLA Status", "Severity", "Problem Description", "Address",
        "Error Code", "handling_type",
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

    if handling_map:
        df["handling_type"] = df["Ticket ID"].map(
            lambda tid: handling_map.get(str(tid).strip()) if tid is not None else None
        )
    else:
        df["handling_type"] = None

    keep = [col for col in cols_out if col in df.columns]
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
    handling_map: dict[str, str] | None = None,
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
        problem = None
        if info_row is not None:
            for pk in ("Problem Description", "problem description", "ProblemDescription"):
                if pk in info_row.index if hasattr(info_row, "index") else pk in info_row:
                    problem = info_row.get(pk)
                    break
                try:
                    problem = info_row.get(pk)
                    if problem is not None and str(problem).strip():
                        break
                except Exception:
                    pass
        region = resolve_region(station, region_map)
        if not is_managed_region(region):
            continue
        tech = _tech_for(station)
        sla_s = str(sla or "").strip().lower()
        has_spare = tid in spare_ids
        has_appt = tid in appointment_ids
        is_od = sla_s == "overdue"
        create_time = info_row.get("Create Time") if info_row is not None else None
        close_time = c.get("Close Time")
        duration_hours = None
        ct = _parse_create_time(create_time)
        clt = _parse_create_time(close_time)
        if ct is not None and clt is not None and clt >= ct:
            duration_hours = round((clt - ct).total_seconds() / 3600.0, 2)

        ht = None
        if handling_map:
            ht = handling_map.get(tid)
        if ht is None and info_row is not None:
            try:
                ht = info_row.get("handling_type")
            except Exception:
                ht = None

        rows.append({
            "Ticket ID": tid,
            "Create Time": create_time.strftime("%Y-%m-%d %H:%M:%S") if hasattr(create_time, "strftime") else create_time,
            "Close Time": close_time,
            "Close Date": c.get("Close Date"),
            "duration_hours": duration_hours,
            "Station Code": station,
            "Charge Point ID": cp_id,
            "Region": region,
            "Tech": tech,
            "cp_type": classify_cp_type(cp_id),
            "SLA Status": sla,
            "is_overdue": is_od,
            "has_spare_wait": has_spare,
            "has_appointment": has_appt,
            "is_overdue_excuse": bool(is_od and (has_spare or has_appt)),
            "Severity": severity,
            "Problem Description": (str(problem).strip() if problem is not None and str(problem).strip() not in ("", "nan", "None") else ""),
            "handling_type": ht,
        })
    return pd.DataFrame(rows)


_stats_pool = ClientPool()  # pool phiên đăng nhập RIÊNG của stats_data.py


async def _export_one_account(username, password, start_time, end_time) -> dict | None:
    """Export Excel cho 1 tài khoản (tự relogin 1 lần nếu bị đá session, qua
    ClientPool dùng chung). Trả dict các sheet DataFrame (đã gắn
    _source_account), hoặc None nếu thất bại."""

    async def _action(client):
        try:
            dfs = await asyncio.wait_for(
                client.export_and_download_tickets(
                    start_time=start_time,
                    end_time=end_time,
                    ticket_status=None,
                ),
                timeout=EXPORT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"export_and_download_tickets quá {EXPORT_TIMEOUT_SECONDS}s "
                "(CCTS không trả về file sẵn sàng) — bỏ cuộc để nhả khoá."
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


def _finalize_from_raw(raw, events_raw, spare_raw, appt_raw, meta_extra: dict, additional_raw=None):
    """Chuẩn hóa TI + closed 30 ngày. additional_raw → handling_type."""
    end_date_ex = meta_extra["end_date_exclusive"]

    spare_ids = set()
    if spare_raw is not None and not spare_raw.empty and "Ticket ID" in spare_raw.columns:
        spare_ids = set(spare_raw["Ticket ID"].astype(str).str.strip().tolist())
    appointment_ids = set()
    if appt_raw is not None and not appt_raw.empty and "Ticket ID" in appt_raw.columns:
        appointment_ids = set(appt_raw["Ticket ID"].astype(str).str.strip().tolist())

    handling_map = extract_handling_type_map(additional_raw)

    region_map, tech_map, tech_by_region = _get_static_maps()
    processed = process_ticket_information(
        raw,
        region_map=region_map,
        tech_map=tech_map,
        end_date_exclusive=end_date_ex,
        handling_map=handling_map,
    )

    closed_basic = extract_closed_tickets_from_events(
        events_raw, end_date_exclusive=end_date_ex, lookback_days=30
    )
    closed_enriched = enrich_closed_with_ticket_info(
        closed_basic, raw, spare_ids, appointment_ids, region_map, tech_map,
        handling_map=handling_map,
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
    try:
        additional_raw = pd.read_excel(SAMPLE_XLSX, sheet_name="Additional information")
    except Exception:
        additional_raw = pd.DataFrame()
    return raw, events_raw, spare_raw, appt_raw, additional_raw


async def fetch_all_accounts_tickets(start_time=None, end_time=None):
    """
    Cào 2 tài khoản cố định (ccts_shared.STATS_SCRAPE_ACCOUNTS), gộp + khử
    trùng Ticket Information / Events Record / Spare Parts / Appointment.

    Cào theo VÒNG LẶP: mỗi vòng thử các tài khoản CHƯA thành công; tài khoản
    nào đã có dữ liệu ở vòng trước thì không cào lại. Lặp tối đa
    MAX_SCRAPE_ROUNDS vòng, nghỉ SCRAPE_RETRY_DELAY_SECONDS giây giữa các
    vòng (KHÔNG giữ CCTS_API_LOCK trong lúc nghỉ, để không chặn ccts_data.py
    cào bản đồ realtime mỗi 15').

    Trả (processed_df, source, meta, closed_enriched_df, bundles) nếu có ít
    nhất 1 tài khoản cào được; trả None nếu SAU TỐI ĐA số vòng vẫn không lấy
    được ticket nào từ bất kỳ tài khoản nào (để refresh_stats_cache() giữ
    nguyên cache cũ thay vì ghi đè dữ liệu rỗng).

    bundles = {username: {sheet_name: DataFrame}} — dùng lại cho report
    hằng ngày (không cào lần 2).
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
    ai_frames = [b["Additional information"] for b in bundles.values() if "Additional information" in b]

    raw = pd.concat(ti_frames, ignore_index=True)
    if "Ticket ID" in raw.columns:
        raw["Ticket ID"] = raw["Ticket ID"].astype(str).str.strip()
        before = len(raw)
        raw = raw.drop_duplicates(subset=["Ticket ID"])
        print(f"[stats] TI gộp {before} → {len(raw)}")
    events_raw = pd.concat(ev_frames, ignore_index=True) if ev_frames else pd.DataFrame()
    spare_raw = pd.concat(sp_frames, ignore_index=True) if sp_frames else pd.DataFrame()
    appt_raw = pd.concat(ap_frames, ignore_index=True) if ap_frames else pd.DataFrame()
    additional_raw = pd.concat(ai_frames, ignore_index=True) if ai_frames else pd.DataFrame()
    if not events_raw.empty and "Ticket ID" in events_raw.columns:
        events_raw["Ticket ID"] = events_raw["Ticket ID"].astype(str).str.strip()

    source = "live" if not fail_accounts else "live_partial"
    processed, meta, closed_enriched = _finalize_from_raw(
        raw, events_raw, spare_raw, appt_raw, meta_extra, additional_raw=additional_raw
    )
    meta["source"] = source
    print(f"[stats] Sau chuẩn hóa TI: {len(processed)} ticket (source={source})")
    return processed, source, meta, closed_enriched, bundles


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
        from cache_store import save_stats_cache_file
        save_stats_cache_file(payload, STATS_CACHE_FILE)
        print(f"[stats] Đã lưu cache → {STATS_CACHE_FILE} (+ S3 nếu bật)")
    except Exception as e:
        try:
            with open(STATS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            print(f"[stats] Đã lưu cache local → {STATS_CACHE_FILE}")
        except Exception as e2:
            print(f"[stats] Lỗi ghi cache: {e2}")
        print(f"[stats] cache_store save: {e}")


def load_stats_cache():
    global _memory_cache
    if _memory_cache is not None:
        return _memory_cache
    try:
        from cache_store import load_stats_cache_file
        data = load_stats_cache_file(STATS_CACHE_FILE)
        if isinstance(data, dict) and ("tickets" in data or "labels" in data or "charts" in data):
            _memory_cache = data
            return data
    except Exception as e:
        print(f"[stats] cache_store load: {e}")
        try:
            if os.path.exists(STATS_CACHE_FILE):
                with open(STATS_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and ("tickets" in data or "labels" in data):
                    _memory_cache = data
                    return data
        except Exception as e2:
            print(f"[stats] Lỗi đọc cache: {e2}")
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


_REPORT_TIMEOUT_SECONDS = int(os.environ.get("REPORT_TIMEOUT_SECONDS", "600"))  # 10 phút


async def _run_daily_report_safe(bundles: dict) -> None:
    """Xuất Excel + upload Drive từ bundles vừa cào. Lỗi report không làm
    hỏng cache thống kê.

    QUAN TRỌNG: run_daily_report_from_bundles() là hàm ĐỒNG BỘ (blocking) —
    tạo file Excel + gọi Google Drive API. Nếu gọi thẳng nó trong 1 hàm
    async như trước đây, nó sẽ CHIẾM DỤNG event loop chính của toàn bộ
    server: trong lúc nó chạy (và đặc biệt nếu nó bị TREO, vd do OAuth
    refresh token lỗi), server sẽ ngưng phản hồi MỌI request (HTTP,
    WebSocket...) chứ không chỉ riêng phần báo cáo — khiến cả app "đơ" tới
    khi Render tự restart, và lần cào thống kê kế tiếp sẽ lặp lại y hệt.

    Vì vậy: chạy nó trong 1 thread riêng (asyncio.to_thread) để KHÔNG chặn
    event loop, và giới hạn thời gian bằng asyncio.wait_for — nếu quá
    _REPORT_TIMEOUT_SECONDS thì tự huỷ chờ (thread nền có thể vẫn chạy dở,
    nhưng server không còn bị treo theo nó nữa) và log rõ ràng để biết mà
    kiểm tra Google Drive/token thay vì im lặng."""
    if not bundles:
        return
    try:
        from report import run_daily_report_from_bundles
        print("[stats] Bắt đầu tạo báo cáo Excel hằng ngày từ dữ liệu vừa cào...")
        await asyncio.wait_for(
            asyncio.to_thread(run_daily_report_from_bundles, bundles),
            timeout=_REPORT_TIMEOUT_SECONDS,
        )
        print("[stats] Báo cáo Excel + Drive hoàn tất.")
    except asyncio.TimeoutError:
        print(
            f"[stats] ⚠️ Tạo báo cáo Excel quá {_REPORT_TIMEOUT_SECONDS}s — đã HUỶ CHỜ để "
            "không treo server (thread nền có thể vẫn chạy dở). Rất có thể do Google Drive/"
            "OAuth token bị kẹt — kiểm tra lại GOOGLE_TOKEN_JSON."
        )
    except Exception as e:
        print(f"[stats] ⚠️ Lỗi khi tạo báo cáo Excel (cache stats vẫn giữ): {e!r}")


async def _refresh_stats_cache_impl() -> dict:
    """Phần thực thi thật sự (chạy dưới STATS_REFRESH_LOCK).

    Luồng thành công:
      1) Cào 2 account (60 ngày) → bundles thô
      2) Chuẩn hoá → cache JSON + charts (trang /stats)
      3) Dùng lại bundles → Excel báo cáo + upload Drive
    """
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
        # (Không chạy report từ sample — tránh Excel sai ngày.)
        sample = _load_sample_bundle()
        if sample is None:
            raise RuntimeError(
                "Không cào được ticket từ bất kỳ tài khoản nào, cũng không có "
                "cache cũ hay file mẫu để hiển thị tạm."
            )
        raw, events_raw, spare_raw, appt_raw, additional_raw = sample
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
        processed, meta, closed_df = _finalize_from_raw(
            raw, events_raw, spare_raw, appt_raw, meta_extra, additional_raw=additional_raw
        )
        payload = _build_payload(processed, "sample", meta, closed_df)
        payload = _prebuild_chart_payloads(payload)
        save_stats_cache(payload)
        print(f"[stats] Cache v2 (sample fallback): TI={meta.get('row_count', 0)}")
        return payload

    df, source, meta, closed_df, bundles = result
    payload = _build_payload(df, source, meta, closed_df)
    payload = _prebuild_chart_payloads(payload)
    save_stats_cache(payload)
    print(
        f"[stats] Cache v2: TI={meta.get('row_count', 0)}, "
        f"closed30d={meta.get('closed_count', 0)}, source={source}"
    )

    # Báo cáo Excel hằng ngày — dùng chung dữ liệu vừa cào (không cào lại).
    await _run_daily_report_safe(bundles)

    return payload


async def refresh_stats_cache(**_kwargs) -> dict:
    """Cào 2 tài khoản cố định, 60 ngày (không gồm hôm nay) → cache v2
    (tickets + closed) + báo cáo Excel/Drive.

    Dùng STATS_REFRESH_LOCK kiểu "single-flight": nếu đã có 1 lượt cào khác
    đang chạy (khởi động / lịch 0h / admin bấm tay xảy ra gần nhau), lượt
    gọi này sẽ ĐỢI lượt kia xong rồi dùng luôn kết quả đó, thay vì cào 2
    lần chồng nhau."""
    if STATS_REFRESH_LOCK.locked():
        print("[stats] Đã có 1 lượt cào thống kê khác đang chạy — đợi kết quả đó, không cào lặp lại.")
        async with STATS_REFRESH_LOCK:
            pass
        return load_stats_cache() or {}

    async with STATS_REFRESH_LOCK:
        return await _refresh_stats_cache_impl()



def _prebuild_chart_payloads(cache_payload: dict) -> dict:
    """Nướng sẵn JSON từng biểu đồ ngay sau khi cào (model 0h xong hết).

    Route /api/stats/* chỉ trả cache["charts"][...], không tính lại trên request
    (trừ khi cache cũ thiếu field — khi đó build 1 lần rồi ghi lại).
    """
    charts: dict[str, Any] = dict(cache_payload.get("charts") or {})

    builders = [
        ("daily_volume", "stats_charts_volume", "build_volume_payload_from_cache"),
        ("overdue_rate", "stats_charts_overdue_rate", "build_overdue_rate_payload_from_cache"),
        ("heatmap", "stats_charts_heatmap", "build_heatmap_payload_from_cache"),
        ("error_codes", "stats_charts_error_codes", "build_error_codes_payload_from_cache"),
    ]
    for key, mod_name, fn_name in builders:
        try:
            mod = __import__(mod_name, fromlist=[fn_name])
            fn = getattr(mod, fn_name)
            charts[key] = fn(cache_payload)
            print(f"[stats] Pre-build chart OK: {key}")
        except ModuleNotFoundError:
            print(f"[stats] Bỏ qua pre-build {key}: chưa có module {mod_name}")
        except Exception as e:
            print(f"[stats] Lỗi pre-build {key}: {e!r}")

    cache_payload["charts"] = charts
    return cache_payload


def ensure_chart_in_cache(chart_key: str):
    """Lấy chart đã nướng; nếu cache cũ thiếu thì build 1 lần và ghi lại."""
    cache = load_stats_cache()
    if not cache:
        return None
    charts = cache.get("charts")
    if not isinstance(charts, dict):
        charts = {}
    if chart_key in charts and charts[chart_key] is not None:
        return charts[chart_key]

    builders = {
        "daily_volume": ("stats_charts_volume", "build_volume_payload_from_cache"),
        "overdue_rate": ("stats_charts_overdue_rate", "build_overdue_rate_payload_from_cache"),
        "heatmap": ("stats_charts_heatmap", "build_heatmap_payload_from_cache"),
        "error_codes": ("stats_charts_error_codes", "build_error_codes_payload_from_cache"),
    }
    spec = builders.get(chart_key)
    if not spec:
        return None
    mod_name, fn_name = spec
    try:
        mod = __import__(mod_name, fromlist=[fn_name])
        payload = getattr(mod, fn_name)(cache)
    except Exception as e:
        print(f"[stats] ensure_chart {chart_key} lỗi: {e!r}")
        return None
    charts[chart_key] = payload
    cache["charts"] = charts
    save_stats_cache(cache)
    print(f"[stats] ensure_chart: đã bổ sung {chart_key} vào cache")
    return payload


def get_cached_daily_volume():
    """Ưu tiên chart đã nướng lúc 0h; fallback build 1 lần nếu cache cũ."""
    return ensure_chart_in_cache("daily_volume")


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