"""
stats_charts_top_error_poles.py — Top 20 trụ (Charge Point) lên Error Code nhiều nhất trong 30 ngày.

- Nhóm theo Charge Point ID ("Mã trụ").
- Với mỗi trụ: Mã trạm, kỹ thuật phụ trách, tổng số ticket lỗi, và breakdown
  tần suất từng mã lỗi (top 5 mã, phần còn lại gộp vào "Khác").
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

from stats_data import is_excluded_tech

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


def build_top_error_poles(df: pd.DataFrame) -> dict[str, Any]:
    empty = {
        "labels": [],
        "cp_ids": [],
        "station_codes": [],
        "techs": [],
        "counts": [],
        "error_breakdowns": [],
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

    cp_counts = work["_cp"].value_counts()
    top_cps = list(cp_counts.head(TOP_N).index)

    labels: list[str] = []
    station_codes: list[str] = []
    techs: list[str] = []
    counts: list[int] = []
    error_breakdowns: list[list[dict[str, Any]]] = []

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

        labels.append(cp)
        station_codes.append(station)
        techs.append(tech)
        counts.append(cnt)
        error_breakdowns.append(top_breakdown)

    return {
        "labels": labels,
        "cp_ids": labels,
        "station_codes": station_codes,
        "techs": techs,
        "counts": counts,
        "error_breakdowns": error_breakdowns,
        "unique_poles": int(work["_cp"].nunique()),
        "total_tickets_with_error_code": int(len(work)),
    }
