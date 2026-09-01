"""
stats_data.py — chỉ chịu trách nhiệm CHUẨN HÓA ticket + GIỮ CACHE cho các
module nhỏ khác dùng (stats_charts_*.py). KHÔNG tự lo việc lấy dữ liệu thô
(đăng nhập/cào CCTS hay đọc file Excel) — việc đó nằm ở stats_source.py;
stats_data.py chỉ gọi stats_source.get_raw_bundle(...) và nhận về raw
sheets theo 1 hình dạng cố định, không cần biết dữ liệu đến từ đâu.

- Chuẩn hoá: lọc BSS.No2, map Region/Tech, phân loại EV/BSS, gắn
  is_overdue/has_spare_wait/has_appointment/is_overdue_excuse cho MỌI
  ticket (kể cả đang mở), để stats_charts_overdue_rate.py tính tỷ lệ
  Overdue theo cửa sổ NGÀY TẠO (30 ngày gần nhất, mọi trạng thái) — tách
  biệt với closed_tickets (ticket ĐÃ ĐÓNG trong 30 ngày qua Events) dùng
  riêng để tính hiệu suất công việc (top hiệu quả/khối lượng, boxplot thời
  gian xử lý).
- Nếu nguồn dữ liệu (CCTS live hoặc thư mục Excel local) không trả được
  ticket nào, GIỮ NGUYÊN cache thống kê cũ thay vì ghi đè bằng dữ liệu
  rỗng; nếu chưa từng có cache nào, dùng file mẫu (SAMPLE_XLSX) làm phao
  cứu sinh cuối cùng.
- Ghi cache thô (danh sách ticket đã chuẩn hóa) + charts đã "nướng" sẵn
  (_prebuild_chart_payloads, gọi các module stats_charts_*.py) cho route
  /api/stats/* dùng.
- STATS_REFRESH_LOCK (single-flight, qua ccts_shared) để gộp nhiều lượt
  làm mới gọi gần nhau (khởi động / 0h / admin bấm tay) thành 1 lượt.

Không vẽ chart ở đây (xem stats_charts_*.py). Không tự cào CCTS ở đây (xem
stats_source.py).
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from ccts_shared import VN_TZ, STATS_REFRESH_LOCK
import stats_source

STATS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "stats_daily_cache.json")
SAMPLE_XLSX = os.path.join(os.path.dirname(__file__), "Tickets_esmanager_20260728_201743.xlsx")

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


def process_ticket_information(
    df: pd.DataFrame,
    region_map=None,
    tech_map=None,
    end_date_exclusive: str | None = None,
    handling_map: dict[str, str] | None = None,
    spare_ids: set | None = None,
    appointment_ids: set | None = None,
) -> pd.DataFrame:
    """
    spare_ids / appointment_ids: dùng để gắn cờ has_spare_wait / has_appointment
    cho TOÀN BỘ ticket (không riêng ticket đã đóng) — phục vụ tính overdue
    theo cửa sổ NGÀY TẠO (xem stats_charts_overdue_rate.py), song song với
    is_overdue lấy trực tiếp từ SLA Status hiện tại của ticket.
    """
    cols_out = [
        "Ticket ID", "Station Code", "Charge Point ID", "Create Time", "Create Date",
        "Region", "Tech", "cp_type", "SLA Status", "Severity", "Problem Description", "Address",
        "Error Code", "Ticket Status", "handling_type", "is_overdue", "has_spare_wait",
        "has_appointment", "is_overdue_excuse",
    ]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols_out)

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    for col in (
        "Ticket ID", "Station Code", "Create Time", "SLA Status", "Severity",
        "Charge Point ID", "Problem Description", "Address", "Error Code", "Ticket Status",
    ):
        if col not in df.columns:
            df[col] = None

    pd_col = df["Problem Description"].astype(str).str.strip()
    before = len(df)
    df = df[~pd_col.str.startswith("BSS.No")].copy()
    dropped = before - len(df)
    if dropped:
        print(f"[stats] Loại {dropped} ticket BSS.No")

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

    # SLA hiện tại của ticket (áp dụng cho CẢ ticket mở lẫn đã đóng) — dùng
    # để tính overdue theo cửa sổ ngày TẠO, khác với is_overdue của
    # enrich_closed_with_ticket_info (chỉ tính trên ticket đã đóng).
    sla_l = (
        df["SLA Status"].astype(str).str.strip().str.lower()
        if "SLA Status" in df.columns
        else pd.Series([""] * len(df), index=df.index)
    )
    df["is_overdue"] = sla_l.eq("overdue")

    # Cờ chờ VT / hẹn khách: ưu tiên sheet Spare Parts / Appointment,
    # đồng thời nhận diện theo Ticket Status hiện tại (ticket đang mở ở
    # trạng thái Appointment / Pending for spare parts vẫn được coi là có
    # lý do khách quan dù sheet có thể thiếu record).
    status_l = (
        df["Ticket Status"].astype(str).str.strip().str.lower()
        if "Ticket Status" in df.columns
        else pd.Series([""] * len(df), index=df.index)
    )
    from_sheet_spare = df["Ticket ID"].isin(spare_ids) if spare_ids else False
    from_sheet_appt = df["Ticket ID"].isin(appointment_ids) if appointment_ids else False
    from_status_spare = status_l.str.contains("spare parts", na=False)
    from_status_appt = status_l.eq("appointment")

    df["has_spare_wait"] = from_sheet_spare | from_status_spare
    df["has_appointment"] = from_sheet_appt | from_status_appt
    df["is_overdue_excuse"] = df["is_overdue"] & (df["has_spare_wait"] | df["has_appointment"])

    keep = [col for col in cols_out if col in df.columns]
    return df[keep].reset_index(drop=True)


OPEN_STATUSES = {
    "open",
    "appointment",
    "pending for asp close",
    "pending for spare parts",
    "pending for spare parts close",  # backward-compat
}
CLOSED_STATUS = "pending for local team close"
REOPEN_HINT_STATUSES = {
    "pending for local team close",
    "pending for voms confirm",
}


def _norm_status(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).strip().lower()


def _is_empty_detail(val) -> bool:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return True
    s = str(val).strip()
    return (not s) or s in {"----", "-", "nan", "None", "null"}


def _format_duration(create_dt: datetime | None, now_dt: datetime | None = None) -> tuple[float | None, str]:
    """Trả (hours_float, human string)."""
    if create_dt is None:
        return None, "—"
    if now_dt is None:
        now_dt = datetime.now(VN_TZ).replace(tzinfo=None)
    if getattr(create_dt, "tzinfo", None) is not None:
        create_dt = create_dt.replace(tzinfo=None)
    if getattr(now_dt, "tzinfo", None) is not None:
        now_dt = now_dt.replace(tzinfo=None)
    if now_dt < create_dt:
        return 0.0, "0h"
    delta = now_dt - create_dt
    hours = round(delta.total_seconds() / 3600.0, 1)
    days = delta.days
    rem_h = (delta.seconds // 3600)
    if days > 0:
        human = f"{days}d {rem_h}h"
    else:
        human = f"{rem_h}h" if rem_h else f"{delta.seconds // 60}m"
    return hours, human


def build_open_long_tickets(
    ticket_info_df: pd.DataFrame,
    events_df: pd.DataFrame | None,
    appt_df: pd.DataFrame | None,
    solutions_df: pd.DataFrame | None,
    top_n: int = 20,
    now_dt: datetime | None = None,
) -> list[dict]:
    """
    Top N ticket đang mở lâu nhất (theo Create Time cũ nhất).

    Trạng thái mở: Open / Appointment / Pending for ASP close / Pending for spare parts
    (xem OPEN_STATUSES).

    Quy tắc lấy ghi chú (detail_note):
    - Open:
        * Nếu Events từng có 'Pending for local team close' hoặc 'Pending for VOMS confirm'
          → is_reopened=True, detail_note='Ticket mở lại'
        * Ngược lại → is_reopened=False, detail_note='Chưa có thông tin xử lý'
    - Pending for spare parts:
        * Lấy Record Detail mới nhất của event status 'Pending for spare parts'
        * Nếu rỗng/---- → fallback Solution Description
    - Appointment:
        * Lấy Detail từ sheet Appointment (mới nhất)
        * Nếu rỗng → fallback Solution Description
    - Pending for ASP close:
        * Chỉ lấy Solution Description
    """
    if ticket_info_df is None or ticket_info_df.empty:
        return []

    ti = ticket_info_df.copy()
    ti.columns = [str(c).strip() for c in ti.columns]
    if "Ticket ID" not in ti.columns or "Ticket Status" not in ti.columns:
        return []

    ti["Ticket ID"] = ti["Ticket ID"].astype(str).str.strip()
    ti["_status"] = ti["Ticket Status"].apply(_norm_status)
    open_mask = ti["_status"].isin(OPEN_STATUSES)
    open_mask = open_mask | ti["_status"].str.contains("spare parts", na=False)
    open_df = ti[open_mask].copy()
    if open_df.empty:
        return []

    open_df["_create_dt"] = open_df["Create Time"].apply(_parse_create_time)
    open_df = open_df.dropna(subset=["_create_dt"])
    # open_df = open_df.sort_values("_create_dt", ascending=True).head(top_n * 3)

    events_by_tid: dict[str, pd.DataFrame] = {}
    if events_df is not None and not events_df.empty:
        ev = events_df.copy()
        ev.columns = [str(c).strip() for c in ev.columns]
        if "Ticket ID" in ev.columns:
            ev["Ticket ID"] = ev["Ticket ID"].astype(str).str.strip()
            for tid, g in ev.groupby("Ticket ID"):
                events_by_tid[tid] = g

    appt_by_tid: dict[str, list] = {}
    if appt_df is not None and not appt_df.empty:
        ap = appt_df.copy()
        ap.columns = [str(c).strip() for c in ap.columns]
        if "Ticket ID" in ap.columns:
            ap["Ticket ID"] = ap["Ticket ID"].astype(str).str.strip()
            for tid, g in ap.groupby("Ticket ID"):
                if "Create Time" in g.columns:
                    g = g.copy()
                    g["_dt"] = g["Create Time"].apply(_parse_create_time)
                    g = g.sort_values("_dt", ascending=False, na_position="last")
                appt_by_tid[tid] = g.to_dict(orient="records")

    sol_by_tid: dict[str, list] = {}
    if solutions_df is not None and not solutions_df.empty:
        sol = solutions_df.copy()
        sol.columns = [str(c).strip() for c in sol.columns]
        if "Ticket ID" in sol.columns:
            sol["Ticket ID"] = sol["Ticket ID"].astype(str).str.strip()
            for tid, g in sol.groupby("Ticket ID"):
                if "Create Time" in g.columns:
                    g = g.copy()
                    g["_dt"] = g["Create Time"].apply(_parse_create_time)
                    g = g.sort_values("_dt", ascending=False, na_position="last")
                sol_by_tid[tid] = g.to_dict(orient="records")

    def _latest_solution_desc(tid: str) -> str:
        rows = sol_by_tid.get(tid) or []
        for r in rows:
            desc = r.get("Solution Description")
            if not _is_empty_detail(desc):
                return str(desc).strip()
        return ""

    def _detail_for_open(tid: str) -> tuple[bool, str]:
        g = events_by_tid.get(tid)
        if g is None or g.empty:
            return False, "Chưa có thông tin xử lý"
        statuses = g["Ticket Status"].apply(_norm_status) if "Ticket Status" in g.columns else pd.Series(dtype=str)
        if statuses.isin(REOPEN_HINT_STATUSES).any():
            return True, "Ticket mở lại"
        return False, "Chưa có thông tin xử lý"

    def _detail_for_spare(tid: str) -> str:
        g = events_by_tid.get(tid)
        if g is not None and not g.empty and "Ticket Status" in g.columns:
            g2 = g.copy()
            g2["_st"] = g2["Ticket Status"].apply(_norm_status)
            spare_rows = g2[g2["_st"].str.contains("spare parts", na=False)]
            if not spare_rows.empty:
                if "Create Time" in spare_rows.columns:
                    spare_rows = spare_rows.copy()
                    spare_rows["_dt"] = spare_rows["Create Time"].apply(_parse_create_time)
                    spare_rows = spare_rows.sort_values("_dt", ascending=False, na_position="last")
                for _, row in spare_rows.iterrows():
                    rd = row.get("Record Detail")
                    if not _is_empty_detail(rd):
                        return str(rd).strip()
        return _latest_solution_desc(tid) or "—"

    def _detail_for_appointment(tid: str) -> str:
        rows = appt_by_tid.get(tid) or []
        for r in rows:
            d = r.get("Detail")
            if not _is_empty_detail(d):
                return str(d).strip()
        return _latest_solution_desc(tid) or "—"

    def _detail_for_asp(tid: str) -> str:
        return _latest_solution_desc(tid) or "—"

    if now_dt is None:
        now_dt = datetime.now(VN_TZ).replace(tzinfo=None)

    results = []
    for _, row in open_df.iterrows():
        tid = str(row["Ticket ID"])
        status_raw = str(row.get("Ticket Status") or "").strip()
        status_norm = _norm_status(status_raw)
        create_dt = row["_create_dt"]
        hours, human = _format_duration(create_dt, now_dt)

        is_reopened = False
        detail_note = ""

        if status_norm == "open":
            is_reopened, detail_note = _detail_for_open(tid)
        elif "spare parts" in status_norm:
            detail_note = _detail_for_spare(tid)
        elif status_norm == "appointment":
            detail_note = _detail_for_appointment(tid)
        elif status_norm == "pending for asp close":
            detail_note = _detail_for_asp(tid)
        else:
            detail_note = _latest_solution_desc(tid) or "—"

        err = row.get("Error Code")
        if _is_empty_detail(err):
            err = row.get("Problem Description") or ""
        err = str(err).strip() if not _is_empty_detail(err) else "—"

        results.append({
            "Ticket ID": tid,
            "Station Code": str(row.get("Station Code") or "—").strip() or "—",
            "Charge Point ID": str(row.get("Charge Point ID") or "—").strip() or "—",
            "Ticket Status": status_raw or "—",
            "Create Time": create_dt.strftime("%Y-%m-%d %H:%M:%S") if create_dt else str(row.get("Create Time") or "—"),
            "duration_hours": hours,
            "duration_human": human,
            "Error Code": err,
            "detail_note": detail_note,
            "is_reopened": is_reopened,
            "Region": row.get("Region"),
            "Tech": row.get("Tech"),
            "cp_type": row.get("cp_type"),
            "SLA Status": row.get("SLA Status"),
        })

    results.sort(key=lambda x: (x.get("duration_hours") or 0), reverse=True)
    return results


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
        # Cờ chờ VT / hẹn khách: sheet + fallback theo Ticket Status hiện tại
        # (để ticket đang/đã từng ở Appointment / Pending for spare parts
        # vẫn được coi là có lý do khách quan).
        status_norm = _norm_status(info_row.get("Ticket Status") if info_row is not None else None)
        has_spare = (tid in spare_ids) or ("spare parts" in status_norm)
        has_appt = (tid in appointment_ids) or (status_norm == "appointment")
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


def _finalize_from_raw(
    raw,
    events_raw,
    spare_raw,
    appt_raw,
    meta_extra: dict,
    additional_raw=None,
    solutions_raw=None,
):
    """Chuẩn hóa TI + closed 30 ngày + top open long. additional_raw → handling_type."""
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
        spare_ids=spare_ids,
        appointment_ids=appointment_ids,
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

    open_long = build_open_long_tickets(
        processed,
        events_df=events_raw,
        appt_df=appt_raw,
        solutions_df=solutions_raw,
        top_n=20,
    )
    print(f"[stats] Open long top20: {len(open_long)}")

    meta = {
        **meta_extra,
        "tech_by_region": tech_by_region,
        "row_count": int(len(processed)),
        "closed_count": int(len(closed_enriched)),
        "open_long_count": int(len(open_long)),
    }
    return processed, meta, closed_enriched, open_long


def _load_sample_bundle():
    """Đọc file Excel mẫu làm phao cứu sinh CUỐI CÙNG — chỉ dùng khi cào
    thất bại hoàn toàn VÀ không hề có cache cũ nào (ví dụ lần deploy đầu
    tiên). Trả (raw, events_raw, spare_raw, appt_raw, additional_raw, solutions_raw)
    hoặc None nếu không có file mẫu."""
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
    try:
        solutions_raw = pd.read_excel(SAMPLE_XLSX, sheet_name="Solutions")
    except Exception:
        solutions_raw = pd.DataFrame()
    return raw, events_raw, spare_raw, appt_raw, additional_raw, solutions_raw


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


def _build_payload(df, source, meta, closed_df, open_long=None) -> dict:
    tech_by_region = meta.pop("tech_by_region", {}) or {}
    if not tech_by_region:
        _, _, tech_by_region = _get_static_maps()
    return {
        "version": 2,
        "tickets": tickets_df_to_records(df),
        "closed_tickets": tickets_df_to_records(closed_df),
        "open_long_tickets": open_long or [],
        "meta": {
            **meta,
            "source": source,
            "generated_at": datetime.now(VN_TZ).isoformat(timespec="seconds"),
            "tech_by_region": tech_by_region,
            "allowed_regions": list(ALLOWED_REGIONS),
        },
    }


async def _refresh_stats_cache_impl(source_mode: str | None = None, local_folder: str | None = None) -> dict:
    """Phần thực thi thật sự (chạy dưới STATS_REFRESH_LOCK).

    Luồng thành công:
      1) stats_source.get_raw_bundle(...) → bundles thô (từ CCTS live HOẶC
         từ thư mục Excel local, tuỳ `source_mode` / STATS_DATA_SOURCE)
      2) Chuẩn hoá (stats_data) → cache JSON + charts (trang /stats)

    stats_data.py KHÔNG quan tâm dữ liệu đến từ đâu — chỉ nhận raw sheets
    theo đúng hình dạng cố định từ stats_source rồi xử lý như nhau.
    """
    bundle = await stats_source.get_raw_bundle(mode=source_mode, local_folder=local_folder)

    if bundle is None:
        # Nguồn được chọn (CCTS live hoặc thư mục local) không có dữ liệu.
        old = load_stats_cache()
        if old:
            print(
                "[stats] Không lấy được dữ liệu nguồn mới — GIỮ NGUYÊN cache thống kê cũ "
                f"(generated_at={old.get('meta', {}).get('generated_at', '?')})."
            )
            return old

        # Chưa từng có cache nào (ví dụ lần deploy đầu tiên) → dùng file mẫu
        # làm phao cứu sinh CUỐI CÙNG để trang /stats không trống trơn.
        sample = _load_sample_bundle()
        if sample is None:
            raise RuntimeError(
                "Không lấy được ticket từ nguồn nào (CCTS/local), cũng không có "
                "cache cũ hay file mẫu để hiển thị tạm."
            )
        raw, events_raw, spare_raw, appt_raw, additional_raw, solutions_raw = sample
        _, end_time, end_date_ex = stats_source.scrape_time_range()
        meta_extra = {
            "start_time": None,
            "end_time": end_time,
            "end_date_exclusive": end_date_ex,
            "accounts_ok": [],
            "accounts_fail": [],
            "rounds_used": 0,
            "lookback_days": stats_source.SCRAPE_LOOKBACK_DAYS,
        }
        processed, meta, closed_df, open_long = _finalize_from_raw(
            raw, events_raw, spare_raw, appt_raw, meta_extra,
            additional_raw=additional_raw, solutions_raw=solutions_raw,
        )
        payload = _build_payload(processed, "sample", meta, closed_df, open_long=open_long)
        payload = _prebuild_chart_payloads(payload)
        save_stats_cache(payload)
        print(f"[stats] Cache v2 (sample fallback): TI={meta.get('row_count', 0)}")
        return payload

    raw, events_raw, spare_raw, appt_raw, additional_raw, solutions_raw, meta_extra = bundle
    source = meta_extra.get("source", "unknown")
    processed, meta, closed_df, open_long = _finalize_from_raw(
        raw, events_raw, spare_raw, appt_raw, meta_extra,
        additional_raw=additional_raw, solutions_raw=solutions_raw,
    )
    del raw, events_raw, spare_raw, appt_raw, additional_raw, solutions_raw
    gc.collect()

    payload = _build_payload(processed, source, meta, closed_df, open_long=open_long)
    payload = _prebuild_chart_payloads(payload)
    save_stats_cache(payload)
    print(
        f"[stats] Cache v2: TI={meta.get('row_count', 0)}, "
        f"closed30d={meta.get('closed_count', 0)}, "
        f"open_long={meta.get('open_long_count', 0)}, source={source}"
    )

    return payload


async def refresh_stats_cache(source_mode: str | None = None, local_folder: str | None = None, **_kwargs) -> dict:
    """Làm mới cache thống kê (tickets + closed) từ nguồn dữ liệu hiện tại.

    `source_mode`: None → dùng stats_source.STATS_DATA_SOURCE (env
    STATS_DATA_SOURCE, mặc định "ccts"). Truyền "ccts" hoặc "local" để ép
    dùng đúng 1 nguồn bất kể env — ví dụ chạy tay ở local:
        await refresh_stats_cache(source_mode="local")
    `local_folder`: thư mục Excel khi source_mode="local" (mặc định
    stats_source.STATS_LOCAL_DATA_DIR, tức thư mục "data/").

    Dùng STATS_REFRESH_LOCK kiểu "single-flight": nếu đã có 1 lượt refresh
    khác đang chạy (khởi động / lịch 0h / admin bấm tay xảy ra gần nhau),
    lượt gọi này sẽ ĐỢI lượt kia xong rồi dùng luôn kết quả đó, thay vì
    chạy 2 lần chồng nhau."""
    if STATS_REFRESH_LOCK.locked():
        print("[stats] Đã có 1 lượt làm mới thống kê khác đang chạy — đợi kết quả đó, không chạy lặp lại.")
        async with STATS_REFRESH_LOCK:
            pass
        return load_stats_cache() or {}

    async with STATS_REFRESH_LOCK:
        return await _refresh_stats_cache_impl(source_mode=source_mode, local_folder=local_folder)



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
    # Chạy tay ở local:
    #   python stats_data.py                 → dùng STATS_DATA_SOURCE (env, mặc định "ccts")
    #   python stats_data.py --source local   → ép đọc file Excel trong thư mục "data/"
    #   python stats_data.py --source local --folder duong/dan/khac
    import argparse

    parser = argparse.ArgumentParser(description="Làm mới cache thống kê CCTS.")
    parser.add_argument("--source", choices=["ccts", "local"], default=None,
                         help="Nguồn dữ liệu: ccts (cào live) hoặc local (đọc Excel có sẵn).")
    parser.add_argument("--folder", default=None,
                         help="Thư mục chứa file Excel khi --source local (mặc định: thư mục 'data/').")
    args = parser.parse_args()

    async def _main():
        p = await refresh_stats_cache(source_mode=args.source, local_folder=args.folder)
        print("tickets:", len(p.get("tickets") or []))
        print("meta:", p.get("meta"))

    asyncio.run(_main())