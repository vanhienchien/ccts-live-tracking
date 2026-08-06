"""
stats_charts_volume.py — biểu đồ đường số ticket theo ngày.

Đọc cache tickets từ stats_data, phân tích theo:
- khu vực (ALLOWED_REGIONS)
- tổng cộng tất cả khu vực (1 đường, kèm TB/ngày, đỉnh, đáy — total_series)
- kỹ thuật viên trong từng khu vực (ma trận tech × ngày, frontend vẽ heatmap)
- bộ lọc EV / BSS / all

Không cào API.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from stats_data import (
    ALLOWED_REGIONS,
    _ALLOWED_SET,
    is_excluded_tech,
    is_managed_region,
    records_to_tickets_df,
)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
# Module này chỉ lấy 30 ngày gần nhất trong data đã cào (45 ngày).
CHART_LOOKBACK_DAYS = 30



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