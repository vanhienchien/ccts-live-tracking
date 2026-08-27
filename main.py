"""
Module cào & xử lý dữ liệu ticket CCTS + toạ độ trạm.
Hỗ trợ nhiều tài khoản CCTS (esmanager + itsmanager) - cào song song từng
tài khoản, gộp + khử trùng theo Ticket ID.

Lỗi đăng nhập của TỪNG tài khoản được bắt riêng (không crash cả chu kỳ), và
hàm cào trả kèm cờ "có ít nhất 1 tài khoản thành công hay không" để main.py
biết mà quyết định giữ lại dữ liệu cũ (cache) nếu TẤT CẢ tài khoản đều lỗi,
thay vì xoá sạch bản đồ.
"""

import os
import json
import re
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd

from utils import extract_core_station_code, parse_duration_to_hours
from config import CCTS_ACCOUNTS
import github_data_store
from ccts_shared import VN_TZ, CCTS_API_LOCK, ClientPool, is_unmanaged_region, load_static_data_filtered

CACHE_FILE = "last_known_data.json"

STATUS_COLORS = {
    "open": "#e74c3c",
    "appointment": "#3498db",
    "pending for asp close": "#9b59b6",
    "pending for spare parts": "#e67e22",
    "pending for local team close": "#16a085",
    "pending for voms confirm": "#16a085",
}

CLOSED_STATUSES = ["Pending for local team close", "Pending for VOMS confirm"]
OPEN_STATUSES = ["Open", "Appointment", "Pending for ASP close", "Pending for spare parts"]
ENDPOINT_FIND_TICKET = "/ccts/cctsTicket/findCCTSTicket"

NORTH_LAT_THRESHOLD = 16.2  # Trạm có lat >= ngưỡng này bị coi là miền Bắc -> loại bỏ

# ==========================================
# Làm giàu dữ liệu ticket "Open" đang overdue (>48h) — CHỈ áp dụng cho trụ
# SẠC (EV), KHÔNG áp dụng cho trụ đổi pin (BSS).
#
# - Nếu timeline cho thấy ticket từng ở 1 trong các trạng thái "đã xử lý
#   xong" (Pending for local team close / Pending for VOMS confirm /
#   Pending for ASP close) trước khi quay lại Open -> ticket này ĐÃ MỞ LẠI,
#   hiển thị trạng thái là "Open (mở lại)".
# - Nếu ticket Open mà timeline KHÔNG có bất kỳ thông tin xử lý nào (chỉ có
#   đúng bản ghi tạo ticket ban đầu) -> cực kỳ nguy hiểm (khó giải trình cho
#   bên thứ 3), tô màu RIÊNG (tím đậm) để cảnh báo.
# ==========================================
REOPEN_TRIGGER_STATUSES = {
    "pending for local team close",
    "pending for voms confirm",
    "pending for asp close",
}
REOPEN_LABEL_SUFFIX = " (mở lại)"
NO_INFO_SEVERITY_KEY = "purple_critical"  # frontend cần map key này -> màu tím đậm
ENRICH_MAX_CONCURRENCY = 4  # số request tra cứu chi tiết chạy song song tối đa (tránh bị đá session / rate-limit)


def _status_color(status):
    return STATUS_COLORS.get(str(status).strip().lower(), "#7f8c8d")


def _severity_color(hours):
    """Trả về (key, mã_màu_nền_nhạt, mã_màu_viền/đậm, màu_chữ) theo số giờ
    tồn đọng của MỘT ticket cụ thể - thang màu vàng (mới) -> cam -> đỏ (tồn lâu)."""
    if hours > 48:
        return "red", "#ff9f94", "#b32a1b", "#b32a1b"
    elif hours >= 24:
        return "orange", "#ffca9c", "#ce6b15", "#ce6b15"
    else:
        return "green", "#93ffab", "#26ac43", "#26ac43"


# Tài khoản tạo ticket (bỏ khỏi danh sách owner/assistant hiển thị)
_CREATOR_ACCOUNTS = {"thailong", "quangle"}


def _compact_account_list(raw_str, keep_es_its_only=False):
    """Thu gọn danh sách account dạng 'A_1; A_2; A_3' → 'A_1 (2, 3)'.
    - Loại bỏ thailong / quangle (người tạo ticket).
    - Nếu keep_es_its_only=True: chỉ giữ account bắt đầu bằng ES hoặc ITS.
    """
    if not raw_str:
        return ""
    names = [n.strip() for n in str(raw_str).split(";") if n.strip()]
    names = [n for n in names if n.lower() not in _CREATOR_ACCOUNTS]
    if keep_es_its_only:
        def _keep(n):
            u = n.upper()
            return (
                u.startswith(("ES", "ITS"))
                or "ESMANAGER" in u
                or "ITSMANAGER" in u
            )
        names = [n for n in names if _keep(n)]
    if not names:
        return ""

    groups = defaultdict(list)
    singles = []
    for name in names:
        m = re.match(r"^(.+?)_(\d+)$", name)
        if m:
            base, num = m.group(1), m.group(2)
            groups[base].append(num)
        else:
            singles.append(name)

    result = []
    seen_bases = []
    for name in names:
        m = re.match(r"^(.+?)_(\d+)$", name)
        if m:
            base = m.group(1)
            if base not in seen_bases:
                seen_bases.append(base)

    for base in seen_bases:
        nums = groups[base]
        nums_sorted = sorted(nums, key=lambda x: int(x) if x.isdigit() else x)
        if len(nums_sorted) == 1:
            result.append(f"{base}_{nums_sorted[0]}")
        else:
            first = nums_sorted[0]
            rest = ", ".join(nums_sorted[1:])
            result.append(f"{base}_{first} ({rest})")

    for name in singles:
        if name not in result:
            result.append(name)

    return "; ".join(result)


def _build_owners_display(owner_raw, assistant_raw):
    """Gộp owner (chỉ ES/ITS) + assistant, đã lọc creator và thu gọn.
    Khử trùng theo từng segment sau khi compact."""
    owner_part = _compact_account_list(owner_raw, keep_es_its_only=True)
    assist_part = _compact_account_list(assistant_raw, keep_es_its_only=False)
    seen = set()
    result = []
    for part in (owner_part, assist_part):
        if not part:
            continue
        for seg in part.split("; "):
            seg = seg.strip()
            if seg and seg not in seen:
                seen.add(seg)
                result.append(seg)
    return "; ".join(result)


def get_static_data():
    """Toạ độ trạm / phân công kỹ thuật viên / model trụ sạc - đọc trực tiếp
    từ GitHub (github_data_store.py) tại runtime. Đã tự lọc/chuẩn hoá các
    khu vực NGỪNG quản lý (vd HCM) thành "KV không quản lý"."""
    return load_static_data_filtered()


def reload_static_data():
    github_data_store.reload_static_data()
    return load_static_data_filtered()


# ==========================================
# Cào ticket - đa tài khoản
# ==========================================
_pool = ClientPool()


async def _post_find_tickets(client, username, ticket_statuses, start_str, stop_str):
    """Gọi API find ticket, gắn _source_account, trả list thô."""
    payload = {
        "page": {"pageNum": 1, "pageSize": 2000},
        "timezoneOffset": 420,
        "createStartTime": start_str,
        "createStopTime": stop_str,
        "ticketStatus": ticket_statuses,
    }
    res_data = await client._post(ENDPOINT_FIND_TICKET, payload)
    data = res_data.get("data", {})
    tickets = data.get("list", []) if isinstance(data, dict) else data
    if not isinstance(tickets, list):
        tickets = data.get("records", [])
    for t in tickets or []:
        t["_source_account"] = username
    return tickets or []


async def _fetch_tickets_window_single_account(username, password, ticket_statuses, start_str, stop_str):
    """Cào ticket cho 1 tài khoản. Tái sử dụng session; lỗi thì relogin 1 lần.
    Trả về (list_dict_thô, thành_công_bool)."""

    async def _action(client):
        return await _post_find_tickets(client, username, ticket_statuses, start_str, stop_str)

    tickets, ok = await _pool.call_with_retry(username, password, _action)
    return (tickets or []), ok


async def _fetch_tickets_window_multi_account(ticket_statuses, start_str, stop_str):
    """Cào từ TẤT CẢ tài khoản trong CCTS_ACCOUNTS, gộp lại (chưa khử trùng).

    Cào SONG SONG các tài khoản (mỗi tài khoản dùng session/token riêng nên
    chạy đồng thời an toàn) thay vì tuần tự như trước — giữ CCTS_API_LOCK
    (khoá DÙNG CHUNG với stats_data.py) trong thời gian ngắn hơn, giảm thời
    gian chặn lượt cào thống kê 0h nếu 2 việc rơi trùng giờ.

    Trả về (list_dict_thô, all_success_bool, success_accounts).
    all_success = True chỉ khi *mọi* tài khoản đều lấy thành công.
    success_accounts = [(username, password), ...] các tài khoản cào thành
    công, GIỮ ĐÚNG THỨ TỰ trong CCTS_ACCOUNTS (dù chạy song song) — thứ tự
    này còn được dùng làm ưu tiên tra cứu chi tiết ticket (xem
    _enrich_open_overdue_ev_tickets: tài khoản đầu tiên thử trước, tài
    khoản sau chỉ là fallback).
    """
    all_raw = []
    success_accounts = []
    failed_accounts = []
    total = len(CCTS_ACCOUNTS)

    async with CCTS_API_LOCK:
        print(f"[ccts_data] Đã giữ CCTS_API_LOCK — cào live tickets ({total} tài khoản song song)...")
        results = await asyncio.gather(
            *(
                _fetch_tickets_window_single_account(
                    account["username"], account["password"], ticket_statuses, start_str, stop_str
                )
                for account in CCTS_ACCOUNTS
            ),
            return_exceptions=True,
        )
        print("[ccts_data] Nhả CCTS_API_LOCK.")

    success_count = 0
    for account, result in zip(CCTS_ACCOUNTS, results):
        username = account["username"]
        if isinstance(result, Exception):
            print(f"[!] Lỗi cào tài khoản [{username}]: {result}")
            failed_accounts.append(username)
            continue
        tickets, ok = result
        if ok:
            success_count += 1
            all_raw.extend(tickets)
            success_accounts.append((username, account["password"]))
        else:
            failed_accounts.append(username)

    all_success = (total > 0) and (success_count == total)
    if all_success:
        print(f"[+] Cào thành công toàn bộ {total}/{total} tài khoản.")
    else:
        print(
            f"[-] Chỉ {success_count}/{total} tài khoản thành công "
            f"(lỗi: {', '.join(failed_accounts) or 'n/a'}) "
            f"→ không chấp nhận dữ liệu mới, giữ cache cũ."
        )
    return all_raw, all_success, success_accounts



def _process_raw_tickets(raw_tickets):
    processed = []
    for item in raw_tickets:
        processed.append({
            "Ticket ID": item.get("cctsTicketId"),
            "Charge Point ID": item.get("chargeBoxId"),
            "Charge Box Model": item.get("chargeBoxModel"),
            "Station Code": item.get("stationCode"),
            "Problem Description": item.get("errorDesc"),
            "Ticket Status": item.get("cctsTicketStatus"),
            "Ticket Duration": item.get("duration"),
            "Creator": item.get("ticketCreator"),
            "Source_Account": item.get("_source_account"),
            "Address": item.get("address") or item.get("stationAddress") or "",
            "OwnerUserName": item.get("cctsTicketOwnerUserName") or "",
            "AssistantName": item.get("assistantName") or "",
        })
    return processed


async def fetch_live_tickets():
    """Cào ticket ĐANG MỞ từ TẤT CẢ tài khoản, gộp + khử trùng theo Ticket ID.
    Trả về (DataFrame, fetch_success_bool, success_accounts).
    fetch_success=True chỉ khi mọi tài khoản CCTS đều cào thành công.
    success_accounts = [(username, password), ...] các tài khoản đã cào
    thành công trong chu kỳ này."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    raw, any_success, success_accounts = await _fetch_tickets_window_multi_account(
        OPEN_STATUSES, "2026-04-30 17:00:00", now_str
    )

    processed = _process_raw_tickets(raw)
    if not processed:
        return pd.DataFrame(), any_success, success_accounts

    df = pd.DataFrame(processed)
    df = df.fillna("")

    if "Ticket ID" in df.columns:
        df = df.drop_duplicates(subset=["Ticket ID"]).reset_index(drop=True)
    if "Problem Description" in df.columns:
        df = df[~df["Problem Description"].astype(str).str.strip().str.startswith("BSS.No2")].copy()

    print(f"[+] Tổng cộng: {len(df)} tickets sau khi gộp và lọc.")
    return df, any_success, success_accounts


def _apply_south_filter_and_coords(df_tickets, coords_map):
    def get_coords(station_code):
        core_code = extract_core_station_code(station_code)
        return coords_map.get(core_code)

    df = df_tickets.copy()
    df["coords"] = df["Station Code"].apply(get_coords)

    missing_mask = df["coords"].isna()
    missing_df = df[missing_mask]

    missing_coord_tickets = [
        {
            "ticket_id": str(row.get("Ticket ID") or ""),
            "station_code": str(row.get("Station Code") or ""),
            "cp_id": str(row.get("Charge Point ID") or ""),
        }
        for _, row in missing_df.iterrows()
    ]
    if missing_coord_tickets:
        print(f"[+] Có {len(missing_coord_tickets)} ticket thuộc trạm CHƯA CÓ toạ độ trong StationData.")

    with_coords_df = df[~missing_mask].copy()
    before = len(with_coords_df)
    south_df = with_coords_df[
        with_coords_df["coords"].apply(lambda x: x["lat"]) < NORTH_LAT_THRESHOLD
    ].copy()
    filtered_north_count = before - len(south_df)
    if filtered_north_count:
        print(f"[+] Đã lọc bỏ {filtered_north_count} ticket thuộc miền Bắc (lat >= {NORTH_LAT_THRESHOLD})")

    return south_df, filtered_north_count, missing_coord_tickets


def _is_ev_charge_point(cp_id) -> bool:
    """True nếu là trụ SẠC (EV) — mã CP KHÔNG bắt đầu bằng 'BSS' (trụ đổi pin).
    Chỉ trụ EV mới cần tra cứu chi tiết / đổi màu cảnh báo theo yêu cầu."""
    return not str(cp_id or "").strip().upper().startswith("BSS")


def _classify_ticket_timeline(timeline):
    """Từ timeline (list dict followRecordStatus/createTime) của 1 ticket
    Open-overdue, xác định:
    - is_reopened: từng ở 1 trong các trạng thái "đã xử lý xong" trước đó
      (Pending for local team close / VOMS confirm / ASP close) rồi quay
      lại Open.
    - has_no_info: timeline KHÔNG có bất kỳ bản ghi xử lý nào ngoài bản ghi
      tạo ticket ban đầu (rất nguy hiểm, khó giải trình bên thứ 3)."""
    timeline = timeline or []
    is_reopened = any(
        str(entry.get("followRecordStatus", "")).strip().lower() in REOPEN_TRIGGER_STATUSES
        for entry in timeline
    )
    has_no_info = len(timeline) <= 1
    return {"is_reopened": is_reopened, "has_no_info": has_no_info}


async def _lookup_ticket_enrichment(clients, ticket_id):
    """Tra cứu ticket lần lượt qua danh sách client (theo đúng thứ tự ưu
    tiên — client đầu tiên trong success_accounts trước). Dừng ngay khi
    tìm thấy ở tài khoản nào đó; chỉ thử tài khoản kế tiếp nếu tài khoản
    trước KHÔNG tìm thấy ticket (result rỗng) hoặc lỗi.

    Trước đây chỉ tra cứu bằng 1 tài khoản DUY NHẤT (success_accounts[0]),
    nên ticket được TẠO/GẮN ở tài khoản kia sẽ luôn trả về None (không tìm
    thấy) và không bao giờ được enrich (is_reopened / has_no_info)."""
    for client in clients:
        try:
            result = await client.search_ticket(ticket_id)
        except Exception as e:
            print(f"[!] Lỗi tra cứu ticket {ticket_id} qua [{client.username}]: {e}")
            continue
        if result:
            return _classify_ticket_timeline(result.get("timeline"))
    return None


async def _enrich_open_overdue_ev_tickets(df_tickets_filtered, success_accounts):
    """Với các ticket đang Open + overdue (>48h) + là trụ EV (không phải
    BSS): tra cứu chi tiết qua CCTSClient.search_ticket() (song song có
    giới hạn ENRICH_MAX_CONCURRENCY) để phát hiện ticket "mở lại" và ticket
    Open-overdue chưa có bất kỳ thông tin xử lý nào.

    Đăng nhập TẤT CẢ tài khoản đã cào live thành công trong chu kỳ này
    (success_accounts) — không chỉ tài khoản đầu tiên. Với mỗi ticket, tra
    cứu lần lượt theo đúng thứ tự success_accounts, dừng ngay khi tìm thấy;
    chỉ tài khoản kế tiếp mới bị gọi nếu tài khoản trước không tìm thấy
    ticket đó (ticket được tạo/gắn ở tài khoản khác, vd tài khoản không
    phải esmanager).

    Trả về dict {ticket_id_str: {"is_reopened": bool, "has_no_info": bool}}
    — chỉ chứa các ticket đã tra cứu thành công. Không raise."""
    if df_tickets_filtered.empty or not success_accounts:
        return {}

    df = df_tickets_filtered.copy()
    df["Hours"] = df["Ticket Duration"].apply(parse_duration_to_hours)

    mask = (
        (df["Ticket Status"].astype(str).str.strip().str.lower() == "open")
        & (df["Hours"] > 48)
        & (df["Charge Point ID"].apply(_is_ev_charge_point))
    )
    targets = df[mask]
    if targets.empty:
        return {}

    ticket_ids = sorted({str(t) for t in targets["Ticket ID"].tolist() if t})
    if not ticket_ids:
        return {}

    async with CCTS_API_LOCK:
        print(
            f"[ccts_data] Đã giữ CCTS_API_LOCK — tra cứu chi tiết {len(ticket_ids)} "
            f"ticket Open-overdue (EV) bằng {len(success_accounts)} tài khoản "
            f"({', '.join(u for u, _ in success_accounts)})..."
        )
        clients = []
        for username, password in success_accounts:
            try:
                client, _ = await _pool.get_or_login(username, password)
                clients.append(client)
            except Exception as e:
                print(f"[!] Không thể đăng nhập [{username}] để tra cứu ticket Open-overdue: {e}")

        if not clients:
            print("[ccts_data] Nhả CCTS_API_LOCK (tra cứu chi tiết - không có tài khoản nào đăng nhập được).")
            return {}

        semaphore = asyncio.Semaphore(ENRICH_MAX_CONCURRENCY)

        async def _bounded_lookup(ticket_id):
            async with semaphore:
                return ticket_id, await _lookup_ticket_enrichment(clients, ticket_id)

        results = await asyncio.gather(*(_bounded_lookup(tid) for tid in ticket_ids))
        print("[ccts_data] Nhả CCTS_API_LOCK (tra cứu chi tiết).")

    enrichment_map = {tid: data for tid, data in results if data is not None}
    print(
        f"[+] Tra cứu chi tiết thành công {len(enrichment_map)}/{len(ticket_ids)} "
        f"ticket Open-overdue (EV)."
    )
    return enrichment_map


def _apply_enrichment(status, ticket_id, enrichment_map):
    """Trả về (status_display, severity_override, is_reopened, is_no_info_critical)
    cho 1 ticket, dựa trên enrichment_map (có thể rỗng / không chứa ticket này
    — khi đó trả về mặc định, không đổi gì)."""
    enrich = enrichment_map.get(str(ticket_id)) if enrichment_map else None
    if not enrich:
        return status, None, False, False

    is_reopened = bool(enrich.get("is_reopened"))
    has_no_info = bool(enrich.get("has_no_info"))

    status_display = f"{status}{REOPEN_LABEL_SUFFIX}" if is_reopened else status
    # Không ép severity tím: overdue chưa có thông tin vẫn đỏ theo giờ.
    # Chỉ giữ is_no_info_critical để frontend hiện cờ cảnh báo.
    severity_override = None
    return status_display, severity_override, is_reopened, has_no_info


def _build_station_payload(
    df_tickets_filtered,
    cp_model_map,
    tech_map,
    region_map,
    total_tickets_raw,
    missing_count,
    filtered_north_count,
    fetch_success,
    missing_coord_tickets=None,
    enrichment_map=None,
):
    """Gộp ticket đang mở (đã lọc miền Nam) với toạ độ trạm.
    Chỉ gửi DỮ LIỆU THÔ — frontend tự dựng popup HTML.

    enrichment_map: dict {ticket_id: {"is_reopened":.., "has_no_info":..}} —
    kết quả tra cứu chi tiết cho ticket Open-overdue (EV), dùng để đổi tên
    trạng thái hiển thị ("Open (mở lại)") và ép severity "purple_critical"
    (frontend cần tự map key này sang màu tím đậm)."""
    enrichment_map = enrichment_map or {}
    stations = []

    if not df_tickets_filtered.empty:
        df = df_tickets_filtered.copy()
        df["Model Name"] = df["Charge Box Model"].map(cp_model_map).fillna("N/A")
        df["Hours"] = df["Ticket Duration"].apply(parse_duration_to_hours)

        grouped = df.groupby("Station Code")
        for station_code, group in grouped:
            core_code = extract_core_station_code(station_code)

            region = region_map.get(core_code, "Unknown")
            if is_unmanaged_region(region):
                continue

            coords = group.iloc[0]["coords"]
            lat, lng = coords["lat"], coords["lng"]

            tech_name = tech_map.get(core_code, "Unassigned")
            max_duration = group["Hours"].max()
            station_severity, _, _, _ = _severity_color(max_duration)
            color = station_severity

            group_sorted = group.sort_values("Hours", ascending=False)

            # Address trạm lấy từ ticket tồn lâu nhất
            top_row = group_sorted.iloc[0]
            station_address = str(top_row.get("Address") or "").strip()

            tickets_out = []
            station_has_no_info_critical = False
            for _, row in group_sorted.iterrows():
                hours = float(row["Hours"])
                severity_key, _, _, _ = _severity_color(hours)

                status_display, severity_override, is_reopened, is_no_info_critical = _apply_enrichment(
                    row["Ticket Status"], row["Ticket ID"], enrichment_map
                )
                if severity_override:
                    severity_key = severity_override
                if is_no_info_critical:
                    station_has_no_info_critical = True

                ticket_owners = _build_owners_display(
                    row.get("OwnerUserName") or "",
                    row.get("AssistantName") or "",
                )
                tickets_out.append({
                    "ticket_id": row["Ticket ID"],
                    "cp_id": str(row["Charge Point ID"]),
                    "status": row["Ticket Status"],
                    "status_display": status_display,
                    "model_name": row["Model Name"],
                    "creator": row.get("Creator") or "",
                    "duration": row["Ticket Duration"],
                    "hours": hours,
                    "severity": severity_key,
                    "description": row["Problem Description"],
                    "is_near_overdue": 45 <= hours < 48,
                    "is_reopened": is_reopened,
                    "is_no_info_critical": is_no_info_critical,
                    "address": str(row.get("Address") or "").strip(),
                    "owners": ticket_owners,
                })

            # Không đổi màu trạm sang tím — giữ theo thang giờ (đỏ nếu >48h).
            stations.append({
                "code": core_code,
                "station_code": station_code,
                "lat": lat,
                "lng": lng,
                "color": color,
                "tickets": tickets_out,
                "cp_count": int(len(group)),
                "region": region,
                "tech_name": tech_name,
                "is_unassigned": (not tech_name) or tech_name.strip().lower() == "unassigned",
                "is_bss_station": str(station_code).strip().upper().startswith("B."),
                "has_near_overdue": bool(
                    ((group_sorted["Hours"] >= 45) & (group_sorted["Hours"] < 48)).any()
                ),
                "has_no_info_critical": station_has_no_info_critical,
                "address": station_address,
            })

    return {
        "stations": stations,
        "total_tickets": total_tickets_raw,
        "with_coords_count": total_tickets_raw - missing_count,
        "missing_count": missing_count,
        "missing_coord_tickets": missing_coord_tickets or [],
        "filtered_north": filtered_north_count,
        "updated_at": datetime.now(VN_TZ).isoformat(timespec="seconds"),
        "fetch_success": fetch_success,
    }


def _build_ticket_rows(df_tickets_filtered, cp_model_map, tech_map, region_map, enrichment_map=None):
    """Danh sách ticket phẳng cho panel theo KT — sắp xếp CAO → THẤP theo giờ tồn."""
    if df_tickets_filtered.empty:
        return []

    enrichment_map = enrichment_map or {}

    df = df_tickets_filtered.copy()
    df["Model Name"] = df["Charge Box Model"].map(cp_model_map).fillna("N/A")
    df["Hours"] = df["Ticket Duration"].apply(parse_duration_to_hours)
    df = df.sort_values("Hours", ascending=False)

    rows = []
    for _, row in df.iterrows():
        station_code = row.get("Station Code")
        core_code = extract_core_station_code(station_code) if station_code else None
        tech_name = tech_map.get(core_code, "Unassigned") if core_code else "Unassigned"
        region = region_map.get(core_code, "Unknown") if core_code else "Unknown"
        if is_unmanaged_region(region):
            continue
        cp_id = str(row.get("Charge Point ID") or "")
        hours = float(row.get("Hours") or 0)

        status_display, severity_override, is_reopened, is_no_info_critical = _apply_enrichment(
            row.get("Ticket Status"), row.get("Ticket ID"), enrichment_map
        )

        rows.append({
            "ticket_id": row.get("Ticket ID"),
            "duration": row.get("Ticket Duration"),
            "hours": hours,
            "station_code": station_code,
            "is_bss_station": str(station_code or "").strip().upper().startswith("B."),
            "cp_id": cp_id,
            "is_bss": cp_id.strip().upper().startswith("BSS"),
            "model_name": row.get("Model Name"),
            "status": row.get("Ticket Status"),
            "status_display": status_display,
            "description": row.get("Problem Description"),
            "creator": row.get("Creator"),
            "tech_name": tech_name,
            "region": region,
            "is_near_overdue": 45 <= hours < 48,
            "address": row.get("Address") or "",
            "owners": _build_owners_display(
                row.get("OwnerUserName") or "",
                row.get("AssistantName") or "",
            ),
            "severity_override": severity_override,
            "is_reopened": is_reopened,
            "is_no_info_critical": is_no_info_critical,
        })
    return rows


async def build_station_markers():
    """Cào ticket mới nhất + gộp toạ độ, lọc miền Nam — tương thích ngược."""
    coords_map, tech_map, region_map, cp_model_map, _ = get_static_data()
    df_tickets, any_success, success_accounts = await fetch_live_tickets()

    total_tickets = 0 if df_tickets.empty else len(df_tickets)
    filtered_north_count = 0
    missing_coord_tickets = []

    if not df_tickets.empty:
        df_tickets, filtered_north_count, missing_coord_tickets = _apply_south_filter_and_coords(
            df_tickets, coords_map
        )

    enrichment_map = await _enrich_open_overdue_ev_tickets(df_tickets, success_accounts)

    payload = _build_station_payload(
        df_tickets, cp_model_map, tech_map, region_map,
        total_tickets, len(missing_coord_tickets), filtered_north_count, any_success,
        missing_coord_tickets=missing_coord_tickets,
        enrichment_map=enrichment_map,
    )
    print(f"[+] Hoàn tất build markers: {len(payload['stations'])} trạm, {total_tickets} tickets ban đầu.")
    return payload


async def build_tech_performance_stats(open_stations):
    """Chỉ đếm ticket ĐANG MỞ theo KT — KHÔNG gọi API ticket đã đóng.

    closed_yesterday / closed_today tạm = 0.
    Sau này thay bằng Engineer_info.csv (hoặc nguồn tĩnh khác).
    """
    open_counts: dict[str, int] = {}
    for s in open_stations:
        tech = s.get("tech_name") or "Unassigned"
        open_counts[tech] = open_counts.get(tech, 0) + int(s.get("cp_count") or 0)

    return {
        tech: {
            "closed_yesterday": 0,
            "closed_today": 0,
            "open_count": count,
        }
        for tech, count in open_counts.items()
    }


async def refresh_all_ccts_data():
    """1 chu kỳ làm mới đầy đủ: trạm + stats KT (chỉ open) + ticket rows."""
    coords_map, tech_map, region_map, cp_model_map, _ = get_static_data()
    df_tickets, any_success, success_accounts = await fetch_live_tickets()

    total_tickets = 0 if df_tickets.empty else len(df_tickets)
    filtered_north_count = 0
    missing_coord_tickets = []
    df_filtered = df_tickets

    if not df_tickets.empty:
        df_filtered, filtered_north_count, missing_coord_tickets = _apply_south_filter_and_coords(
            df_tickets, coords_map
        )

    enrichment_map = await _enrich_open_overdue_ev_tickets(df_filtered, success_accounts)

    station_payload = _build_station_payload(
        df_filtered, cp_model_map, tech_map, region_map,
        total_tickets, len(missing_coord_tickets), filtered_north_count, any_success,
        missing_coord_tickets=missing_coord_tickets,
        enrichment_map=enrichment_map,
    )
    ticket_rows = _build_ticket_rows(df_filtered, cp_model_map, tech_map, region_map, enrichment_map=enrichment_map)
    tech_stats = await build_tech_performance_stats(station_payload["stations"])

    print(
        f"[+] Hoàn tất chu kỳ làm mới: {len(station_payload['stations'])} trạm, "
        f"{total_tickets} ticket mở, {len(ticket_rows)} dòng ticket chi tiết, "
        f"{len(missing_coord_tickets)} ticket thiếu toạ độ."
    )

    return station_payload, tech_stats, ticket_rows


# ==========================================
# Cache ra file
# ==========================================
def save_cache_to_file(station_payload, tech_stats, ticket_rows):
    payload = {
        "station_payload": station_payload,
        "tech_stats": tech_stats,
        "ticket_rows": ticket_rows,
    }
    try:
        from cache_store import save_map_cache
        save_map_cache(payload, CACHE_FILE)
    except Exception as e:
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception as e2:
            print(f"⚠️ Không thể lưu cache dữ liệu ra file: {e2}")
        print(f"⚠️ cache_store save_map: {e}")


def load_cache_from_file():
    try:
        from cache_store import load_map_cache
        data = load_map_cache(CACHE_FILE)
        if isinstance(data, dict):
            return (
                data.get("station_payload"),
                data.get("tech_stats", {}),
                data.get("ticket_rows", []),
            )
    except Exception as e:
        print(f"⚠️ cache_store load_map: {e}")
        try:
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return (
                        data.get("station_payload"),
                        data.get("tech_stats", {}),
                        data.get("ticket_rows", []),
                    )
        except Exception as e2:
            print(f"⚠️ Không thể đọc cache dữ liệu từ file: {e2}")
    return None, {}, []


# ==========================================
# Giới hạn xem theo khu vực (Kỹ thuật viên)
# ==========================================
def filter_stations_for_user(stations, user):
    """KT chỉ xem trạm trong khu vực của họ. Unassigned công khai cho tất cả."""
    role = (user.get("role") or "").strip().lower()
    if role != "kỹ thuật":
        return stations

    user_region = (user.get("region") or "").strip().lower()

    result = []
    for s in stations:
        tech_name = (s.get("tech_name") or "").strip()
        if not tech_name or tech_name.lower() == "unassigned":
            result.append(s)
            continue
        if user_region and (s.get("region") or "").strip().lower() == user_region:
            result.append(s)
    return result


def filter_tech_by_region_for_user(tech_by_region, user):
    """KT chỉ thấy KT trong khu vực mình. Điều phối khu vực trở lên (Admin)
    thấy TOÀN BỘ KT mọi khu vực — như Admin, không còn bị giới hạn theo
    khu vực quản lý của mình."""
    role = (user.get("role") or "").strip().lower()
    if role != "kỹ thuật":
        return tech_by_region

    user_region = (user.get("region") or "").strip()
    return {r: v for r, v in tech_by_region.items() if r == user_region}