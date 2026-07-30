"""
stats_charts_overdue_rate.py — tỷ lệ Overdue khi đóng + ranking KT.

- rates_pct: % overdue / closed
- rates_tick_pct: % (overdue - overdue_chủ_quan) / closed
  overdue_chủ_quan = overdue có chờ VT hoặc hẹn khách
- top10_overdue / top10_efficiency / top10_volume
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from stats_data import (
    ALLOWED_REGIONS,
    _ALLOWED_SET,
    is_excluded_tech,
    is_managed_region,
    records_to_tickets_df,
)

CHART_LOOKBACK_DAYS = 30
MIN_CLOSED_FOR_RATE_RANK = 3  # tránh 1 ticket làm top rate


def _filter_cp(df: pd.DataFrame, cp_type: str) -> pd.DataFrame:
    if df is None or df.empty or cp_type == "all" or "cp_type" not in df.columns:
        return df
    return df[df["cp_type"] == cp_type].copy()


def _rates_for_group(sub: pd.DataFrame) -> dict[str, Any]:
    """
    rate: overdue / closed
    overdue_subjective (chủ quan): overdue ∧ (spare ∨ appointment)
    tick_rate: (overdue - overdue_subjective) / closed  → vạch trên bar
    """
    closed = int(len(sub))
    if not closed:
        return {
            "closed": 0,
            "overdue": 0,
            "overdue_subjective": 0,
            "overdue_tick": 0,
            "rate": 0.0,
            "rate_tick": 0.0,
            "spare_wait": 0,
            "appointment": 0,
        }

    od = sub["is_overdue"].astype(bool) if "is_overdue" in sub.columns else pd.Series([False] * closed)
    if "is_overdue_excuse" in sub.columns:
        subj = sub["is_overdue_excuse"].astype(bool)
    else:
        spare = sub["has_spare_wait"].astype(bool) if "has_spare_wait" in sub.columns else False
        appt = sub["has_appointment"].astype(bool) if "has_appointment" in sub.columns else False
        subj = od & (spare | appt)

    overdue = int(od.sum())
    overdue_subj = int(subj.sum())
    overdue_tick = max(0, overdue - overdue_subj)

    spare_n = int(sub["has_spare_wait"].sum()) if "has_spare_wait" in sub.columns else 0
    appt_n = int(sub["has_appointment"].sum()) if "has_appointment" in sub.columns else 0

    return {
        "closed": closed,
        "overdue": overdue,
        "overdue_subjective": overdue_subj,
        "overdue_tick": overdue_tick,
        "rate": round(overdue / closed, 4),
        "rate_tick": round(overdue_tick / closed, 4),
        "spare_wait": spare_n,
        "appointment": appt_n,
        "efficiency": round(1.0 - (overdue / closed), 4),
    }


def _pack_bar_series(labels, details_list: list[dict]) -> dict[str, Any]:
    return {
        "labels": labels,
        "rates_pct": [round(d["rate"] * 100, 1) for d in details_list],
        "rates_tick_pct": [round(d["rate_tick"] * 100, 1) for d in details_list],
        "efficiency_pct": [round(d.get("efficiency", 0) * 100, 1) for d in details_list],
        "closed_counts": [d["closed"] for d in details_list],
        "overdue_counts": [d["overdue"] for d in details_list],
        "overdue_subjective_counts": [d["overdue_subjective"] for d in details_list],
        "overdue_tick_counts": [d["overdue_tick"] for d in details_list],
        "details": {labels[i]: details_list[i] for i in range(len(labels))},
        "total_closed": sum(d["closed"] for d in details_list),
        "total_overdue": sum(d["overdue"] for d in details_list),
    }


def aggregate_rate_by_region(df: pd.DataFrame) -> dict[str, Any]:
    labels = list(ALLOWED_REGIONS)
    details_list = []
    for r in ALLOWED_REGIONS:
        sub = df[df["Region"] == r] if not df.empty else df
        details_list.append(_rates_for_group(sub if sub is not None else pd.DataFrame()))
    out = _pack_bar_series(labels, details_list)
    out["total_closed"] = int(len(df)) if df is not None else 0
    out["total_overdue"] = (
        int(df["is_overdue"].sum()) if df is not None and len(df) and "is_overdue" in df.columns else 0
    )
    return out


def aggregate_rate_by_tech_per_region(
    df: pd.DataFrame,
    tech_by_region: dict | None = None,
) -> dict[str, Any]:
    result = {}
    for region in ALLOWED_REGIONS:
        sub = df[df["Region"] == region] if not df.empty else df
        known = list((tech_by_region or {}).get(region, []))
        actual = sorted(sub["Tech"].dropna().unique().tolist()) if sub is not None and not sub.empty else []
        techs = []
        seen = set()
        for t in known + actual:
            t = str(t).strip()
            if not t or t in seen or is_excluded_tech(t):
                continue
            seen.add(t)
            techs.append(t)

        details_list = []
        for t in techs:
            tsub = sub[sub["Tech"] == t] if sub is not None and not sub.empty else pd.DataFrame()
            details_list.append(_rates_for_group(tsub))
        pack = _pack_bar_series(techs, details_list)
        pack["total_closed"] = int(len(sub)) if sub is not None else 0
        pack["total_overdue"] = (
            int(sub["is_overdue"].sum()) if sub is not None and len(sub) and "is_overdue" in sub.columns else 0
        )
        result[region] = pack
    return result


def _all_tech_stats(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Mọi KT toàn công ty (đã loại excluded)."""
    if df is None or df.empty or "Tech" not in df.columns:
        return []
    rows = []
    for tech, g in df.groupby("Tech"):
        tech = str(tech).strip()
        if is_excluded_tech(tech):
            continue
        d = _rates_for_group(g)
        d["tech"] = tech
        # region phổ biến nhất của KT (nếu có)
        if "Region" in g.columns and g["Region"].notna().any():
            d["region"] = str(g["Region"].mode().iloc[0])
        else:
            d["region"] = ""
        rows.append(d)
    return rows


def top10_rankings(df: pd.DataFrame) -> dict[str, Any]:
    stats = _all_tech_stats(df)

    def _pack_top(items: list[dict], value_key: str, as_pct: bool = True) -> dict[str, Any]:
        labels = [x["tech"] for x in items]
        details_list = items
        pack = _pack_bar_series(labels, details_list)
        if value_key == "rate":
            pack["values_pct"] = pack["rates_pct"]
        elif value_key == "efficiency":
            pack["values_pct"] = pack["efficiency_pct"]
        elif value_key == "closed":
            pack["values_pct"] = pack["closed_counts"]  # absolute, not pct
        pack["regions"] = [x.get("region", "") for x in items]
        return pack

    # Top OD rate (cao nhất) — cần đủ closed
    by_rate = [x for x in stats if x["closed"] >= MIN_CLOSED_FOR_RATE_RANK]
    by_rate.sort(key=lambda x: (x["rate"], x["overdue"], x["closed"]), reverse=True)
    top_od = by_rate[:10]

    # Top efficiency = 100% - OD rate
    by_eff = [x for x in stats if x["closed"] >= MIN_CLOSED_FOR_RATE_RANK]
    by_eff.sort(key=lambda x: (x["efficiency"], x["closed"]), reverse=True)
    top_eff = by_eff[:10]

    # Top volume = closed count
    by_vol = sorted(stats, key=lambda x: (x["closed"], -x["rate"]), reverse=True)[:10]

    return {
        "top10_overdue": _pack_top(top_od, "rate"),
        "top10_efficiency": _pack_top(top_eff, "efficiency"),
        "top10_volume": _pack_top(by_vol, "closed"),
        "min_closed_for_rate_rank": MIN_CLOSED_FOR_RATE_RANK,
    }


def _payload_for_df(df: pd.DataFrame, tech_by_region: dict | None) -> dict[str, Any]:
    by_region = aggregate_rate_by_region(df)
    by_tech = aggregate_rate_by_tech_per_region(df, tech_by_region)
    tops = top10_rankings(df)
    return {
        "by_region_rates": by_region,
        "by_tech_rates": by_tech,
        **tops,
        "total_closed": by_region.get("total_closed", 0),
        "total_overdue": by_region.get("total_overdue", 0),
        "overall_rate_pct": round(
            (by_region["total_overdue"] / by_region["total_closed"] * 100), 1
        )
        if by_region.get("total_closed")
        else 0.0,
    }


def build_overdue_rate_payload_from_cache(cache: dict[str, Any]) -> dict[str, Any]:
    meta = cache.get("meta") or {}
    tech_by_region = meta.get("tech_by_region") or {}
    df = records_to_tickets_df(cache.get("closed_tickets") or [])

    if not df.empty and "Region" in df.columns:
        df = df[df["Region"].apply(is_managed_region)].copy()
    for col in ("is_overdue", "has_spare_wait", "has_appointment", "is_overdue_excuse"):
        if not df.empty and col in df.columns:
            df[col] = df[col].astype(bool)

    if df.empty:
        empty = _payload_for_df(df, tech_by_region)
        return {
            "chart": "overdue_rate",
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
        "all": _payload_for_df(df, tech_by_region),
        "ev": _payload_for_df(_filter_cp(df, "ev"), tech_by_region),
        "bss": _payload_for_df(_filter_cp(df, "bss"), tech_by_region),
    }
    root = dict(by_cp["all"])
    root["chart"] = "overdue_rate"
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
