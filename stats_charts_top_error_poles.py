"""
stats_charts_top_error_poles.py — Top 20 trụ (Charge Point) lên Error Code nhiều nhất trong 30 ngày.

- Nhóm theo Charge Point ID ("Mã trụ").
- Với mỗi trụ: Mã trạm, kỹ thuật phụ trách, tổng số ticket lỗi, và breakdown
  tần suất từng mã lỗi (top 5 mã, phần còn lại gộp vào "Khác").
- Thêm thông tin ticket MỚI NHẤT của trụ: Create Time + trạng thái đóng/mở
  (dựa trên Ticket Status hiện tại).
- Đây KHÔNG phải chart độc lập: build_top_error_poles() được gọi trực tiếp
  từ stats_charts_error_codes.py để gộp chung vào payload "Top mã lỗi"
  (cùng /api/stats/error-codes, cùng bộ filter 30 ngày / cp_type đã áp dụng
  sẵn ở phía gọi).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import pandas as pd

from stats_data import (
    is_excluded_tech,
    OPEN_STATUSES,
    CLOSED_STATUSES,
    _norm_status,
    _parse_create_time,
)

TOP_N = 20
MAX_CODES_SHOWN = 5

# Giữ đồng bộ với stats_charts_error_codes._CODE_RE — tách riêng ở đây để
# tránh circular import (error_codes.py import build_top_error_poles từ
# module này).
_CODE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize_error_code(raw) -> str | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip()
    if not s or s in {"----", "-", "nan", "None", "null"}:
        return None
    m = _CODE_RE.match(s)
    if not m:
        return None
    return m.group(1).upper()


def _is_open_status(status_raw) -> bool:
    """True nếu ticket đang mở (Open / Appointment / Pending spare / ASP close…)."""
    st = _norm_status(status_raw)
    if not st:
        return False
    if st in CLOSED_STATUSES:
        return False
    if st in {"closed", "resolved", "done"}:
        return False
    if st in OPEN_STATUSES:
        return True
    if "spare parts" in st:
        return True
    # Không rõ → coi là đang mở (an toàn hơn khi hiển thị cảnh báo)
    return True


def _status_label(status_raw) -> str:
    """Nhãn ngắn: 'Đang mở (…) / 'Đã đóng (…)'."""
    if status_raw is None or (isinstance(status_raw, float) and pd.isna(status_raw)):
        return "—"
    raw = str(status_raw).strip()
    if not raw or raw.lower() in {"nan", "none", "null"}:
        return "—"
    if _is_open_status(raw):
        return f"Đang mở ({raw})"
    return f"Đã đóng ({raw})"


def build_top_error_poles(df: pd.DataFrame) -> dict[str, Any]:
    empty = {
        "labels": [],
        "cp_ids": [],
        "station_codes": [],
        "techs": [],
        "counts": [],
        "error_breakdowns": [],
        "latest_create_times": [],
        "latest_ticket_statuses": [],
        "latest_ticket_ids": [],
        "latest_is_open": [],
        "unique_poles": 0,
        "total_tickets_with_error_code": 0,
    }
    if df is None or df.empty:
        return empty
    if "Error Code" not in df.columns or "Charge Point ID" not in df.columns:
        return empty

    work = df.copy()
    work["_code"] = work["Error Code"].apply(normalize_error_code)
    work = work[work["_code"].notna()].copy()
    if work.empty:
        return empty

    work["_cp"] = work["Charge Point ID"].astype(str).str.strip()
    work = work[(work["_cp"] != "") & (work["_cp"].str.lower() != "nan")].copy()
    if work.empty:
        return empty

    # Parse Create Time để chọn ticket mới nhất theo từng trụ
    if "Create Time" in work.columns:
        work["_dt"] = work["Create Time"].apply(_parse_create_time)
    else:
        work["_dt"] = pd.NaT

    cp_counts = work["_cp"].value_counts()
    top_cps = list(cp_counts.head(TOP_N).index)

    labels: list[str] = []
    station_codes: list[str] = []
    techs: list[str] = []
    counts: list[int] = []
    error_breakdowns: list[list[dict[str, Any]]] = []
    latest_create_times: list[str] = []
    latest_ticket_statuses: list[str] = []
    latest_ticket_ids: list[str] = []
    latest_is_open: list[bool] = []

    for cp in top_cps:
        sub = work[work["_cp"] == cp]
        cnt = int(len(sub))

        station = "—"
        if "Station Code" in sub.columns:
            sc = sub["Station Code"].dropna().astype(str).str.strip()
            sc = sc[sc != ""]
            if len(sc):
                station = str(sc.value_counts().index[0])

        tech = "—"
        if "Tech" in sub.columns:
            tech_series = sub["Tech"].dropna().astype(str).str.strip()
            filtered = tech_series[~tech_series.apply(is_excluded_tech)]
            pool = filtered if len(filtered) else tech_series
            if len(pool):
                tech = str(pool.value_counts().index[0])

        code_counter = Counter(sub["_code"])
        breakdown_sorted = code_counter.most_common()
        top_breakdown = [
            {"code": code, "count": int(n)} for code, n in breakdown_sorted[:MAX_CODES_SHOWN]
        ]
        other_n = sum(n for _, n in breakdown_sorted[MAX_CODES_SHOWN:])
        if other_n > 0:
            top_breakdown.append({"code": "Khác", "count": int(other_n)})

        # Ticket mới nhất của trụ (Create Time lớn nhất)
        sub_sorted = sub.sort_values("_dt", ascending=False, na_position="last")
        latest = sub_sorted.iloc[0]
        latest_dt = latest.get("_dt")
        if latest_dt is not None and not pd.isna(latest_dt):
            latest_ct = (
                latest_dt.strftime("%Y-%m-%d %H:%M:%S")
                if hasattr(latest_dt, "strftime")
                else str(latest_dt)
            )
        else:
            raw_ct = latest.get("Create Time")
            latest_ct = str(raw_ct).strip() if raw_ct is not None and str(raw_ct).strip() else "—"

        status_raw = latest.get("Ticket Status") if "Ticket Status" in sub.columns else None
        is_open = _is_open_status(status_raw)
        status_label = _status_label(status_raw)
        tid = str(latest.get("Ticket ID") or "").strip() or "—"

        labels.append(cp)
        station_codes.append(station)
        techs.append(tech)
        counts.append(cnt)
        error_breakdowns.append(top_breakdown)
        latest_create_times.append(latest_ct)
        latest_ticket_statuses.append(status_label)
        latest_ticket_ids.append(tid)
        latest_is_open.append(bool(is_open))

    return {
        "labels": labels,
        "cp_ids": labels,
        "station_codes": station_codes,
        "techs": techs,
        "counts": counts,
        "error_breakdowns": error_breakdowns,
        "latest_create_times": latest_create_times,
        "latest_ticket_statuses": latest_ticket_statuses,
        "latest_ticket_ids": latest_ticket_ids,
        "latest_is_open": latest_is_open,
        "unique_poles": int(work["_cp"].nunique()),
        "total_tickets_with_error_code": int(len(work)),
    }