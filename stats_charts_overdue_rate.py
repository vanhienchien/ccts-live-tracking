"""
stats_charts_overdue_rate.py — tỷ lệ Overdue + ranking KT.

Hai nguồn dữ liệu SONG SONG (không còn dùng chung 1 tập "closed" như trước):

- df_created — ticket được TẠO trong CHART_LOOKBACK_DAYS ngày gần nhất, MỌI
  trạng thái (đang mở lẫn đã đóng). Overdue = SLA Status hiện tại ==
  "Overdue". Dùng cho: by_region_rates, by_tech_rates, top10_overdue, và
  trục % Overdue (Y) của performance_matrix.
- df_closed — ticket ĐÃ ĐÓNG trong 30 ngày (Events → Pending for local team
  close), như cũ. Dùng cho: top10_efficiency, top10_volume,
  resolution_boxplot, và trục khối lượng (X) của performance_matrix.
  Hiệu suất (efficiency) = tỷ lệ ticket đóng có SLA Ontime, tính tương
  đương 1 − overdue/closed trên chính tập đã đóng.

- rates_pct: % overdue / (số ticket trong nhóm — TẠO trong 30 ngày)
- rates_tick_pct: % (overdue - overdue_chủ_quan) / nhóm
  overdue_chủ_quan = overdue có chờ VT hoặc hẹn khách
- top10_overdue: xếp theo df_created · top10_efficiency/top10_volume: xếp theo df_closed
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
    onsite / remote: từ handling_type (Additional information)
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
            "onsite": 0,
            "remote": 0,
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

    onsite_n = remote_n = 0
    if "handling_type" in sub.columns:
        ht = sub["handling_type"].astype(str).str.lower()
        onsite_n = int(ht.eq("onsite").sum())
        remote_n = int(ht.eq("remote").sum())

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
        "onsite": onsite_n,
        "remote": remote_n,
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
        "onsite_counts": [d.get("onsite", 0) for d in details_list],
        "remote_counts": [d.get("remote", 0) for d in details_list],
        "details": {labels[i]: details_list[i] for i in range(len(labels))},
        "total_closed": sum(d["closed"] for d in details_list),
        "total_overdue": sum(d["overdue"] for d in details_list),
    }


def aggregate_rate_by_region(df_created: pd.DataFrame) -> dict[str, Any]:
    """df_created: ticket TẠO trong 30 ngày (mọi trạng thái)."""
    df = df_created
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
    df_created: pd.DataFrame,
    tech_by_region: dict | None = None,
) -> dict[str, Any]:
    """df_created: ticket TẠO trong 30 ngày (mọi trạng thái)."""
    df = df_created
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


def _safe_str(val, default: str = "—") -> str:
    """Chuyển giá trị pandas/None/NaN thành str an toàn cho JSON."""
    if val is None:
        return default
    try:
        if isinstance(val, float) and pd.isna(val):
            return default
    except Exception:
        pass
    s = str(val).strip()
    if not s or s.lower() in {"nan", "none", "null", "----", "-"}:
        return default
    return s


def _serialize_overdue_tickets_for_tech(df: pd.DataFrame, tech: str) -> list[dict[str, Any]]:
    """Danh sách ticket is_overdue==True của 1 KT (từ df_created), sort Create Time cũ → mới.

    Cấu trúc gần giống open_long để frontend tái sử dụng style bảng.
    Mọi field đều được làm sạch NaN để JSON-safe.
    """
    if df is None or df.empty or "Tech" not in df.columns:
        return []
    sub = df[df["Tech"].astype(str).str.strip() == str(tech).strip()].copy()
    if sub.empty or "is_overdue" not in sub.columns:
        return []
    sub = sub[sub["is_overdue"].astype(bool)].copy()
    if sub.empty:
        return []

    # sort theo Create Time tăng dần (cũ nhất trước)
    if "Create Time" in sub.columns:
        sub = sub.copy()
        sub["_sort_dt"] = pd.to_datetime(sub["Create Time"], errors="coerce")
        sub = sub.sort_values("_sort_dt", ascending=True, na_position="last")

    rows: list[dict[str, Any]] = []
    for _, row in sub.iterrows():
        err = row.get("Error Code")
        if (
            err is None
            or (isinstance(err, float) and pd.isna(err))
            or str(err).strip() in ("", "----", "-", "nan", "None", "null")
        ):
            err = row.get("Problem Description") or ""
        err = _safe_str(err, "—")

        has_spare = bool(row.get("has_spare_wait")) if "has_spare_wait" in row.index else False
        has_appt = bool(row.get("has_appointment")) if "has_appointment" in row.index else False
        # tránh NaN khi đọc bool
        try:
            if pd.isna(has_spare):
                has_spare = False
        except Exception:
            has_spare = bool(has_spare)
        try:
            if pd.isna(has_appt):
                has_appt = False
        except Exception:
            has_appt = bool(has_appt)

        is_excuse = bool(row.get("is_overdue_excuse")) if "is_overdue_excuse" in row.index else (has_spare or has_appt)
        try:
            if pd.isna(is_excuse):
                is_excuse = has_spare or has_appt
        except Exception:
            pass

        excuse_parts = []
        if has_spare:
            excuse_parts.append("chờ VT")
        if has_appt:
            excuse_parts.append("hẹn khách")
        excuse_note = " · ".join(excuse_parts) if excuse_parts else ""

        # handling_type: có thể là NaN → phải None hoặc str, không được để float nan
        ht_raw = row.get("handling_type") if "handling_type" in row.index else None
        if ht_raw is None or (isinstance(ht_raw, float) and pd.isna(ht_raw)):
            ht = None
        else:
            ht_s = str(ht_raw).strip().lower()
            ht = ht_s if ht_s and ht_s not in {"nan", "none", "null", ""} else None

        region_val = row.get("Region")
        if region_val is None or (isinstance(region_val, float) and pd.isna(region_val)):
            region_val = None
        else:
            region_val = str(region_val).strip() or None

        cp_type_val = row.get("cp_type")
        if cp_type_val is None or (isinstance(cp_type_val, float) and pd.isna(cp_type_val)):
            cp_type_val = None
        else:
            cp_type_val = str(cp_type_val).strip() or None

        rows.append({
            "Ticket ID": _safe_str(row.get("Ticket ID"), ""),
            "Station Code": _safe_str(row.get("Station Code"), "—"),
            "Charge Point ID": _safe_str(row.get("Charge Point ID"), "—"),
            "Ticket Status": _safe_str(row.get("Ticket Status"), "—"),
            "Create Time": _safe_str(row.get("Create Time"), "—"),
            "Create Date": _safe_str(row.get("Create Date"), "—"),
            "Error Code": err,
            "SLA Status": _safe_str(row.get("SLA Status"), "—"),
            "Severity": _safe_str(row.get("Severity"), "—"),
            "Region": region_val,
            "Tech": tech,
            "cp_type": cp_type_val,
            "handling_type": ht,          # None hoặc "onsite"/"remote" — không còn nan
            "has_spare_wait": bool(has_spare),
            "has_appointment": bool(has_appt),
            "is_overdue_excuse": bool(is_excuse),
            "excuse_note": excuse_note,
        })
    return rows


def top10_rankings(df_created: pd.DataFrame, df_closed: pd.DataFrame) -> dict[str, Any]:
    """
    top10_overdue: xếp theo tỷ lệ overdue trên ticket TẠO trong 30 ngày (df_created).
    top10_efficiency / top10_volume: xếp theo ticket ĐÃ ĐÓNG trong 30 ngày (df_closed) — như cũ.

    top10_overdue còn kèm overdue_tickets: {tech: [ticket_dict, ...]} để frontend
    hiển thị danh sách chứng minh khi user bấm nút mở rộng.
    """
    od_stats = _all_tech_stats(df_created)
    perf_stats = _all_tech_stats(df_closed)

    def _pack_top(items: list[dict], value_key: str) -> dict[str, Any]:
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

    # Top OD rate (cao nhất) — trên ticket TẠO 30 ngày, cần đủ số lượng
    by_rate = [x for x in od_stats if x["closed"] >= MIN_CLOSED_FOR_RATE_RANK]
    by_rate.sort(key=lambda x: (x["rate"], x["overdue"], x["closed"]), reverse=True)
    top_od = by_rate[:10]

    # Danh sách ticket Overdue thực tế cho từng KT trong top 10
    overdue_tickets: dict[str, list[dict[str, Any]]] = {}
    for item in top_od:
        tech = item["tech"]
        overdue_tickets[tech] = _serialize_overdue_tickets_for_tech(df_created, tech)

    top_od_pack = _pack_top(top_od, "rate")
    top_od_pack["overdue_tickets"] = overdue_tickets

    # Top efficiency = 100% - OD rate — trên ticket ĐÃ ĐÓNG 30 ngày
    by_eff = [x for x in perf_stats if x["closed"] >= MIN_CLOSED_FOR_RATE_RANK]
    by_eff.sort(key=lambda x: (x["efficiency"], x["closed"]), reverse=True)
    top_eff = by_eff[:10]

    # Top volume = số ticket đã đóng
    by_vol = sorted(perf_stats, key=lambda x: (x["closed"], -x["rate"]), reverse=True)[:10]

    return {
        "top10_overdue": top_od_pack,
        "top10_efficiency": _pack_top(top_eff, "efficiency"),
        "top10_volume": _pack_top(by_vol, "closed"),
        "min_closed_for_rate_rank": MIN_CLOSED_FOR_RATE_RANK,
    }


# Góc phần tư performance matrix (volume × overdue rate)
QUADRANT_STAR = "star"          # high vol, low OD  — ngôi sao
QUADRANT_OVERLOAD = "overload"  # high vol, high OD — quá tải
QUADRANT_IMPROVE = "improve"    # low vol, high OD  — cần cải thiện
QUADRANT_IDLE = "idle"          # low vol, low OD   — ít việc


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return float(s[mid - 1] + s[mid]) / 2.0


def performance_quadrant(df_created: pd.DataFrame, df_closed: pd.DataFrame) -> dict[str, Any]:
    """Scatter matrix: X = khối lượng (ticket đã đóng / tại trạm), Y = % overdue.

    X lấy từ df_closed (khối lượng công việc thực đã xử lý — như cũ).
    Y (% overdue) lấy từ df_created (ticket TẠO trong 30 ngày, mọi trạng thái).
    Chia 4 góc bằng median volume & median overdue rate toàn công ty.
    Chỉ KT có ≥ MIN_CLOSED_FOR_RATE_RANK ticket đã đóng (đủ dữ liệu khối lượng).
    """
    od_stats = {x["tech"]: x for x in _all_tech_stats(df_created)}
    perf_stats = _all_tech_stats(df_closed)
    eligible = [x for x in perf_stats if x["closed"] >= MIN_CLOSED_FOR_RATE_RANK]
    if not eligible:
        return {
            "points": [],
            "median_volume": 0,
            "median_rate_pct": 0,
            "quadrant_counts": {
                QUADRANT_STAR: 0,
                QUADRANT_OVERLOAD: 0,
                QUADRANT_IMPROVE: 0,
                QUADRANT_IDLE: 0,
            },
            "min_closed": MIN_CLOSED_FOR_RATE_RANK,
            "x_axis": "onsite",
            "x_axis_label": "Ticket Tại trạm",
        }

    # Trục X = ticket Tại trạm (onsite) — phản ánh khối lượng di chuyển thực tế.
    # Nếu cache cũ thiếu handling_type (onsite=0 hết) → fallback closed.
    use_onsite = any(int(x.get("onsite") or 0) > 0 for x in eligible)
    volumes = [
        float(x.get("onsite") or 0) if use_onsite else float(x["closed"])
        for x in eligible
    ]
    rates = [
        float(od_stats[x["tech"]]["rate"]) * 100.0 if x["tech"] in od_stats else 0.0
        for x in eligible
    ]
    med_vol = _median(volumes)
    med_rate = _median(rates)

    points = []
    counts = {
        QUADRANT_STAR: 0,
        QUADRANT_OVERLOAD: 0,
        QUADRANT_IMPROVE: 0,
        QUADRANT_IDLE: 0,
    }
    for x in eligible:
        closed = int(x["closed"])
        onsite = int(x.get("onsite") or 0)
        remote = int(x.get("remote") or 0)
        vol_x = onsite if use_onsite else closed
        od = od_stats.get(x["tech"])
        overdue_n = int(od["overdue"]) if od else 0
        rate_pct = round(float(od["rate"]) * 100.0, 1) if od else 0.0
        high_vol = vol_x >= med_vol
        high_od = rate_pct >= med_rate
        if high_vol and not high_od:
            q = QUADRANT_STAR
        elif high_vol and high_od:
            q = QUADRANT_OVERLOAD
        elif not high_vol and high_od:
            q = QUADRANT_IMPROVE
        else:
            q = QUADRANT_IDLE
        counts[q] += 1
        points.append({
            "tech": x["tech"],
            "region": x.get("region") or "",
            "closed": closed,
            "overdue": overdue_n,
            "rate_pct": rate_pct,
            "onsite": onsite,
            "remote": remote,
            "quadrant": q,
            "x": vol_x,
            "y": rate_pct,
        })

    order = {QUADRANT_STAR: 0, QUADRANT_OVERLOAD: 1, QUADRANT_IMPROVE: 2, QUADRANT_IDLE: 3}
    points.sort(key=lambda p: (order.get(p["quadrant"], 9), -p["x"]))

    return {
        "points": points,
        "median_volume": round(med_vol, 1),
        "median_rate_pct": round(med_rate, 1),
        "quadrant_counts": counts,
        "min_closed": MIN_CLOSED_FOR_RATE_RANK,
        "x_axis": "onsite" if use_onsite else "closed",
        "x_axis_label": "Ticket Tại trạm" if use_onsite else "Ticket đã đóng",
    }




def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    ceil = min(f + 1, len(sorted_vals) - 1)
    if f == ceil:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[ceil] - sorted_vals[f]) * (k - f))


# Trục X boxplot: ngày. Phần hiển thị chính 0–30 ngày; outlier > 30 ngày
# được gắn cờ far=True (frontend vẽ ở mép "…" + tooltip đầy đủ).
RESOLUTION_AXIS_DAYS = 30.0


def _box_stats_from_rows(rows: list[dict]) -> dict | None:
    """rows: {duration_days, ticket_id, station, cp_id}."""
    if not rows:
        return None
    vals = sorted(float(r["duration_days"]) for r in rows)
    q1 = _percentile(vals, 25)
    med = _percentile(vals, 50)
    q3 = _percentile(vals, 75)
    iqr = q3 - q1
    low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    inliers = [v for v in vals if low <= v <= high]
    outliers = []
    for r in rows:
        d = float(r["duration_days"])
        if d < low or d > high:
            outliers.append({
                "ticket_id": r.get("ticket_id"),
                "station": r.get("station"),
                "cp_id": r.get("cp_id"),
                "duration_days": round(d, 2),
                "far": d > RESOLUTION_AXIS_DAYS,  # ngoài trục 7 ngày
                "problem": r.get("problem") or "",
            })
    # sort outliers by duration desc, cap payload
    outliers.sort(key=lambda o: o["duration_days"], reverse=True)
    under_2 = sum(1 for v in vals if v <= 2.0)
    over_2 = len(vals) - under_2
    return {
        "count": len(vals),
        "min": round(inliers[0] if inliers else vals[0], 2),
        "q1": round(q1, 2),
        "median": round(med, 2),
        "q3": round(q3, 2),
        "max": round(inliers[-1] if inliers else vals[-1], 2),
        "mean": round(sum(vals) / len(vals), 2),
        "whisker_low": round(inliers[0] if inliers else vals[0], 2),
        "whisker_high": round(inliers[-1] if inliers else vals[-1], 2),
        "under_2_days": under_2,
        "over_2_days": over_2,
        "pct_under_2": round(under_2 / len(vals) * 100, 1) if vals else 0.0,
        "outliers": outliers[:30],
    }


def aggregate_resolution_boxplot(
    df: pd.DataFrame,
    tech_by_region: dict | None = None,
) -> dict:
    """Boxplot thời gian xử lý (ngày) theo KT — tách từng khu vực.

    duration_days = (Close Time − Create Time) / 24h.
    Trả by_region[region] = { labels, boxes, ... } để frontend tab theo KV.
    """
    empty_region = lambda: {
        "labels": [], "boxes": [], "unit": "days",
        "total_tickets": 0, "axis_max_days": RESOLUTION_AXIS_DAYS,
    }
    empty = {
        "by_region": {r: empty_region() for r in ALLOWED_REGIONS},
        "unit": "days",
        "axis_max_days": RESOLUTION_AXIS_DAYS,
        "total_tickets": 0,
    }
    if df is None or df.empty:
        return empty

    d = df.copy()
    if "Region" in d.columns:
        d = d[d["Region"].isin(_ALLOWED_SET)].copy()
    if "Tech" in d.columns:
        d = d[~d["Tech"].apply(is_excluded_tech)].copy()

    # duration: ưu tiên duration_hours → ngày; fallback nếu đã có duration_days
    if "duration_days" not in d.columns:
        if "duration_hours" in d.columns:
            d["duration_days"] = pd.to_numeric(d["duration_hours"], errors="coerce") / 24.0
        else:
            return empty
    d = d.dropna(subset=["duration_days"])
    d = d[d["duration_days"] >= 0]
    if d.empty:
        return empty

    by_region: dict = {}
    total = 0
    for region in ALLOWED_REGIONS:
        sub = d[d["Region"] == region] if "Region" in d.columns else d.iloc[0:0]
        known = list((tech_by_region or {}).get(region, []))
        actual = sorted(sub["Tech"].dropna().unique().tolist()) if not sub.empty else []
        techs, seen = [], set()
        for t in known + actual:
            t = str(t).strip()
            if not t or t in seen or is_excluded_tech(t):
                continue
            seen.add(t)
            techs.append(t)

        labels, boxes = [], []
        for tech in techs:
            tsub = sub[sub["Tech"] == tech] if not sub.empty else sub
            if tsub.empty:
                continue
            rows = []
            for _, row in tsub.iterrows():
                rows.append({
                    "duration_days": float(row["duration_days"]),
                    "ticket_id": str(row.get("Ticket ID") or ""),
                    "station": str(row.get("Station Code") or ""),
                    "cp_id": str(row.get("Charge Point ID") or row.get("cp_id") or ""),
                    "problem": str(row.get("Problem Description") or row.get("problem") or "").strip(),
                })
            stats = _box_stats_from_rows(rows)
            if not stats:
                continue
            labels.append(tech)
            boxes.append({"tech": tech, "region": region, **stats})
            total += stats["count"]

        by_region[region] = {
            "labels": labels,
            "boxes": boxes,
            "unit": "days",
            "total_tickets": int(sum(b["count"] for b in boxes)),
            "axis_max_days": RESOLUTION_AXIS_DAYS,
        }

    return {
        "by_region": by_region,
        "unit": "days",
        "axis_max_days": RESOLUTION_AXIS_DAYS,
        "total_tickets": int(total),
    }


def _payload_for_df(df_created: pd.DataFrame, df_closed: pd.DataFrame, tech_by_region: dict | None) -> dict[str, Any]:
    by_region = aggregate_rate_by_region(df_created)
    by_tech = aggregate_rate_by_tech_per_region(df_created, tech_by_region)
    tops = top10_rankings(df_created, df_closed)
    quad = performance_quadrant(df_created, df_closed)
    reso = aggregate_resolution_boxplot(df_closed, tech_by_region)
    return {
        "by_region_rates": by_region,
        "by_tech_rates": by_tech,
        **tops,
        "performance_matrix": quad,
        "resolution_boxplot": reso,
        # total_closed/total_overdue/overall_rate_pct: tính trên ticket TẠO
        # trong 30 ngày (df_created) — đây là số liệu Overdue chính của chart.
        "total_closed": by_region.get("total_closed", 0),
        "total_overdue": by_region.get("total_overdue", 0),
        "overall_rate_pct": round(
            (by_region["total_overdue"] / by_region["total_closed"] * 100), 1
        )
        if by_region.get("total_closed")
        else 0.0,
        # Số ticket ĐÃ ĐÓNG thực tế trong 30 ngày (để đối chiếu / hiển thị phụ).
        "total_closed_actual": int(len(df_closed)) if df_closed is not None else 0,
    }


def _created_window_df(cache: dict[str, Any]) -> pd.DataFrame:
    """Ticket được TẠO trong CHART_LOOKBACK_DAYS ngày gần nhất, mọi trạng thái
    (mở lẫn đóng). Nguồn: cache["tickets"] (60 ngày) đã có is_overdue/
    has_spare_wait/has_appointment/is_overdue_excuse từ stats_data.py."""
    meta = cache.get("meta") or {}
    end_date_ex = meta.get("end_date_exclusive")
    df = records_to_tickets_df(cache.get("tickets") or [])
    if df.empty:
        return df
    if "Region" in df.columns:
        df = df[df["Region"].apply(is_managed_region)].copy()
    for col in ("is_overdue", "has_spare_wait", "has_appointment", "is_overdue_excuse"):
        if col in df.columns:
            df[col] = df[col].astype(bool)
        else:
            df[col] = False
    if end_date_ex and "Create Date" in df.columns:
        try:
            end_dt = pd.Timestamp(end_date_ex)
            start_dt = end_dt - pd.Timedelta(days=CHART_LOOKBACK_DAYS)
            cd = pd.to_datetime(df["Create Date"], errors="coerce")
            df = df[(cd >= start_dt) & (cd < end_dt)].copy()
        except Exception as e:
            print(f"[stats] overdue_rate: lỗi lọc cửa sổ ngày tạo: {e!r}")
    return df


def build_overdue_rate_payload_from_cache(cache: dict[str, Any]) -> dict[str, Any]:
    meta = cache.get("meta") or {}
    tech_by_region = meta.get("tech_by_region") or {}

    df_closed = records_to_tickets_df(cache.get("closed_tickets") or [])
    if not df_closed.empty and "Region" in df_closed.columns:
        df_closed = df_closed[df_closed["Region"].apply(is_managed_region)].copy()
    for col in ("is_overdue", "has_spare_wait", "has_appointment", "is_overdue_excuse"):
        if not df_closed.empty and col in df_closed.columns:
            df_closed[col] = df_closed[col].astype(bool)

    df_created = _created_window_df(cache)

    if df_created.empty and df_closed.empty:
        empty = _payload_for_df(df_created, df_closed, tech_by_region)
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
        "all": _payload_for_df(df_created, df_closed, tech_by_region),
        "ev": _payload_for_df(_filter_cp(df_created, "ev"), _filter_cp(df_closed, "ev"), tech_by_region),
        "bss": _payload_for_df(_filter_cp(df_created, "bss"), _filter_cp(df_closed, "bss"), tech_by_region),
    }
    root = dict(by_cp["all"])
    root["chart"] = "overdue_rate"
    root["by_cp_type"] = by_cp
    root["resolution_boxplot"] = by_cp["all"].get("resolution_boxplot") or {}
    root["resolution_boxplot_by_cp"] = {
        "all": by_cp["all"].get("resolution_boxplot") or {},
        "ev": by_cp["ev"].get("resolution_boxplot") or {},
        "bss": by_cp["bss"].get("resolution_boxplot") or {},
    }
    root["cp_type"] = "all"
    root["chart_days"] = CHART_LOOKBACK_DAYS
    root["scrape_days"] = meta.get("lookback_days")
    root["source"] = meta.get("source", "unknown")
    root["generated_at"] = meta.get("generated_at")
    root["counts"] = {
        "all": int(len(df_created)),
        "ev": int(len(_filter_cp(df_created, "ev"))),
        "bss": int(len(_filter_cp(df_created, "bss"))),
    }
    root["meta"] = {
        "end_date_exclusive": meta.get("end_date_exclusive"),
        "accounts_ok": meta.get("accounts_ok"),
    }
    return root