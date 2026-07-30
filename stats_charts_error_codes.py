"""
stats_charts_error_codes.py — Top 20 mã lỗi (Error Code) trong 30 ngày.

- Nhóm theo mã đầu (A0110 từ "A0110 (SmokeAlarm)").
- Chọn 1 tên hiển thị đại diện.
- Với mỗi mã: kỹ thuật viên có nhiều ticket lỗi đó nhất.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from stats_data import (
    is_excluded_tech,
    is_managed_region,
    records_to_tickets_df,
)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
CHART_LOOKBACK_DAYS = 30
TOP_N = 20

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


def _display_name_for_group(raw_values: list[str], code: str) -> str:
    """Ưu tiên chuỗi có mô tả trong ngoặc; không thì code."""
    best = None
    for v in raw_values:
        v = str(v).strip()
        if "(" in v and ")" in v:
            # chuẩn: CODE (Name)
            best = v
            break
        if best is None and v:
            best = v
    if best:
        # đảm bảo bắt đầu bằng code
        if not best.upper().startswith(code):
            return f"{code} ({best})"
        return best
    return code


def _filter_last_n_days(df: pd.DataFrame, n_days: int = CHART_LOOKBACK_DAYS) -> pd.DataFrame:
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


def build_top_errors(df: pd.DataFrame) -> dict[str, Any]:
    empty = {
        "labels": [],
        "codes": [],
        "display_names": [],
        "counts": [],
        "top_techs": [],
        "top_tech_counts": [],
        "total_tickets_with_code": 0,
    }
    if df is None or df.empty:
        return empty

    work = df.copy()
    if "Error Code" not in work.columns:
        return empty

    work["_code"] = work["Error Code"].apply(normalize_error_code)
    work = work[work["_code"].notna()].copy()
    if work.empty:
        return empty

    # raw variants per code
    variants: dict[str, list[str]] = defaultdict(list)
    for _, row in work.iterrows():
        variants[row["_code"]].append(str(row.get("Error Code") or ""))

    code_counts = work["_code"].value_counts()
    top_codes = list(code_counts.head(TOP_N).index)

    labels = []
    display_names = []
    counts = []
    top_techs = []
    top_tech_counts = []

    for code in top_codes:
        sub = work[work["_code"] == code]
        cnt = int(len(sub))
        disp = _display_name_for_group(variants[code], code)

        # KT gặp nhiều nhất (bỏ excluded)
        tech_series = sub["Tech"].dropna().astype(str).str.strip() if "Tech" in sub.columns else pd.Series(dtype=str)
        tech_series = tech_series[~tech_series.apply(is_excluded_tech)]
        if len(tech_series):
            tc = tech_series.value_counts()
            top_tech = str(tc.index[0])
            top_tech_n = int(tc.iloc[0])
        else:
            top_tech = "—"
            top_tech_n = 0

        labels.append(code)
        display_names.append(disp)
        counts.append(cnt)
        top_techs.append(top_tech)
        top_tech_counts.append(top_tech_n)

    return {
        "labels": labels,
        "codes": labels,
        "display_names": display_names,
        "counts": counts,
        "top_techs": top_techs,
        "top_tech_counts": top_tech_counts,
        "total_tickets_with_code": int(len(work)),
        "unique_codes": int(work["_code"].nunique()),
    }


def _payload_for_df(df: pd.DataFrame) -> dict[str, Any]:
    top = build_top_errors(df)
    return {
        "top20": top,
        "total_tickets": int(len(df)) if df is not None else 0,
        "total_with_error_code": top.get("total_tickets_with_code", 0),
    }


def build_error_codes_payload_from_cache(cache: dict[str, Any]) -> dict[str, Any]:
    meta = cache.get("meta") or {}
    df = records_to_tickets_df(cache.get("tickets") or [])
    if not df.empty and "Region" in df.columns:
        df = df[df["Region"].apply(is_managed_region)].copy()
    df = _filter_last_n_days(df, CHART_LOOKBACK_DAYS)

    if df.empty:
        empty = _payload_for_df(df)
        return {
            "chart": "error_codes",
            "cp_type": "all",
            "by_cp_type": {"all": empty, "ev": empty, "bss": empty},
            **empty,
            "chart_days": CHART_LOOKBACK_DAYS,
            "scrape_days": meta.get("lookback_days"),
            "source": meta.get("source", "unknown"),
            "generated_at": meta.get("generated_at"),
            "counts": {"all": 0, "ev": 0, "bss": 0},
            "meta": meta,
        }

    by_cp = {
        "all": _payload_for_df(df),
        "ev": _payload_for_df(_filter_cp(df, "ev")),
        "bss": _payload_for_df(_filter_cp(df, "bss")),
    }
    root = dict(by_cp["all"])
    root["chart"] = "error_codes"
    root["by_cp_type"] = by_cp
    root["cp_type"] = "all"
    root["chart_days"] = CHART_LOOKBACK_DAYS
    root["scrape_days"] = meta.get("lookback_days")
    root["source"] = meta.get("source", "unknown")
    root["generated_at"] = meta.get("generated_at")
    root["counts"] = {
        "all": int(len(df)),
        "ev": int(len(_filter_cp(df, "ev"))),
        "bss": int(len(_filter_cp(df, "bss"))),
    }
    root["meta"] = {
        "end_date_exclusive": meta.get("end_date_exclusive"),
        "accounts_ok": meta.get("accounts_ok"),
    }
    return root
