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
from datetime import datetime

import pandas as pd

from utils import extract_core_station_code, parse_duration_to_hours
from config import CCTS_ACCOUNTS
import github_data_store
from api_client import _is_session_invalidated
from ccts_shared import (
    VN_TZ, CCTS_API_LOCK, ClientPool, SessionKickedError, is_unmanaged_region,
    load_static_data_filtered, OPEN_STATUSES, CLOSED_STATUSES, CLOSED_STATUSES_NORM,
)

CACHE_FILE = "last_known_data.json"

STATUS_COLORS = {
    "open": "#e74c3c",
    "appointment": "#3498db",
    "pending for asp close": "#9b59b6",
    "pending for spare parts": "#e67e22",
    "pending for local team close": "#16a085",
    "pending for voms confirm": "#16a085",
}

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
REOPEN_TRIGGER_STATUSES = CLOSED_STATUSES_NORM | {"pending for asp close"}
REOPEN_LABEL_SUFFIX = " (mở lại)"
NO_INFO_SEVERITY_KEY = "purple_critical"  # frontend cần map key này -> màu tím đậm
ENRICH_MAX_CONCURRENCY = 4  # số request tra cứu chi tiết chạy song song tối đa (tránh bị đá session / rate-limit)

# Trần thời gian giữ CCTS_API_LOCK cho 1 lượt cào/tra cứu. api_client.py đã
# có timeout (5, 30)s cho MỖI request, nhưng nhiều tài khoản/ticket cộng dồn
# (kể cả relogin thử lại) vẫn có thể kéo dài — trần này đảm bảo module kia
# (stats_data.py, dùng chung khoá) không bị chờ vô thời hạn.
CCTS_LOCK_TIMEOUT_SECONDS = 300

# ==========================================
# Đếm real-time số ticket "vừa đóng" theo KTV giữa 2 lần cào liên tiếp (mỗi
# TICKET_REFRESH_SECONDS) — KHÔNG cần tải lại lịch sử, chỉ so sánh snapshot
# Open của LẦN CÀO NGAY TRƯỚC (_latest_ticket_rows/last_known_data.json) với
# lần này (xem _rollup_closed_by_tech bên dưới).
#
# 2026-09-16: đã BỎ phần "phân biệt ticket mới xuất hiện là mới thật hay cũ
# mở lại" từng ở đây (_classify_new_vs_reopened, dựa vào Create Time + tra
# cứu từng ticket) — is_reopened giờ được xác nhận đầy đủ & rẻ hơn cho MỌI
# ticket đang mở qua _fetch_reopen_map_via_export() (export Excel, xem bên
# dưới), không cần đoán qua diff giữa 2 chu kỳ nữa.
CLOSED_COUNTER_FILE = "closed_today_counts.json"

# Mốc bắt đầu cửa sổ cào ticket "đang mở" — dùng CHUNG cho fetch_live_tickets()
# (API list) và _fetch_reopen_map_via_export() (export Excel) để cả 2 luôn
# nhìn cùng 1 phạm vi ticket, tránh lệch tập hợp giữa 2 nguồn.
OPEN_WINDOW_START_STR = "2026-04-30 17:00:00"

# ==========================================
# Xác nhận is_reopened cho MỌI ticket đang mở (không giới hạn EV/overdue như
# _enrich_open_overdue_ev_tickets ở trên) bằng export Excel lọc SERVER-SIDE
# theo ticket_status=OPEN_STATUSES — nhanh (~20s theo thực nghiệm, khác hẳn
# export ĐẦY ĐỦ lịch sử dùng cho /stats có thể mất tới 180s) vì chỉ trả về
# đúng các ticket đang ở 4 trạng thái mở, kèm sheet "Events Record" (lịch sử
# followRecordStatus thật) cho từng ticket đó — y hệt dữ liệu
# scripts/auto_ccts_optimized.py đã dùng để tự phát hiện ticket mở lại.
#
# 2026-09-16: bổ sung theo yêu cầu — trước đó chỉ có 2 nguồn phát hiện mở lại
# (EV+overdue qua tra cứu từng ticket, và diff giữa 2 chu kỳ cào liên tiếp),
# cả 2 đều bỏ sót ticket BSS hoặc ticket đã mở lại từ TRƯỚC khi app bắt đầu
# theo dõi (chưa từng thấy nó "biến mất" để làm mốc so sánh). Nguồn này chạy
# MỖI chu kỳ, không cần job nền riêng, không cần cache 2 lớp.
#
# Tối ưu RAM: export_and_download_tickets() LUÔN parse đủ 6 sheet trước rồi
# mới trim cột theo usecols_map (xem api_client.py) — nếu không khai báo,
# 5 sheet ta không cần (Ticket Information, Appointment, Solutions, Spare
# Parts Record, Additional information) vẫn bị giữ FULL-WIDTH trong RAM cho
# tới khi hàm return. Khai báo trim CHO CẢ 6 sheet (chỉ giữ "Ticket ID" ở 5
# sheet không dùng, và đúng 2 cột cần ở "Events Record") để không có sheet
# nào full-width sống trong RAM, kể cả tạm thời — quan trọng vì 2 tài khoản
# chạy song song (asyncio.gather) nhân đôi mức đỉnh bộ nhớ cùng lúc.
REOPEN_EXPORT_USECOLS = {
    "Ticket Information": ["Ticket ID"],
    "Events Record": ["Ticket ID", "Ticket Status"],
    "Spare Parts Record": ["Ticket ID"],
    "Appointment": ["Ticket ID"],
    "Additional information": ["Ticket ID"],
    "Solutions": ["Ticket ID"],
}

# Timeout riêng cho export "chỉ ticket đang mở" (mặc định hàm dùng 180s —
# hiệu chỉnh cho export ĐẦY ĐỦ lịch sử 60 ngày của /stats, quá rộng rãi cho
# export nhỏ này, thực tế ~20s). Hạ xuống để nhả CCTS_API_LOCK sớm hơn nếu
# có sự cố, thay vì treo gần hết CCTS_LOCK_TIMEOUT_SECONDS mỗi 600s.
REOPEN_EXPORT_TIMEOUT_SECONDS = int(os.environ.get("REOPEN_EXPORT_TIMEOUT_SECONDS", "90"))
# ==========================================
# Cảnh báo SỚM ticket Open + chưa có thông tin xử lý (has_no_info) sắp quá
# hạn 48h — hạ ngưỡng enrichment xuống 47.5h (còn ≤30 phút) để is_no_info_
# critical hiện lên UI TRƯỚC khi ticket thật sự overdue, đủ thời gian cho
# người phân việc (QC) tự kiểm tra + yêu cầu kỹ thuật xử lý tay.
#
# 2026-09-16: ĐÃ BỎ tính năng tự động đóng (ticket_auto_resolver.py, port từ
# scripts/ticket_closer_mul.py) — user quyết định vì rủi ro tính công sai
# cho kỹ thuật (KTV đang xử lý/chuẩn bị đóng tay đúng lúc hệ thống đóng từ xa
# trước). Chỉ còn lại phần CẢNH BÁO (enrichment sớm hơn), không còn hành
# động ghi/đóng ticket tự động nào cả.
# ==========================================
NO_INFO_EARLY_WARNING_HOURS = float(os.environ.get("NO_INFO_EARLY_WARNING_HOURS", "47.5"))


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
    """Gọi API find ticket, gắn _source_account, trả list thô.

    QUAN TRỌNG (2026-09-15): client._post() không raise khi phiên bị đá/hết
    hạn — nó trả về 1 dict lỗi (vd code=512 "logged in elsewhere") giống hệt
    cấu trúc dict thành công. Nếu không kiểm tra ở đây, `data.get("list", [])`
    sẽ ra [] và hàm này coi như "cào thành công, chỉ là không có ticket nào"
    — khiến _fetch_tickets_window_multi_account() tính all_success=True dù
    tài khoản này thực chất bị đá, làm dữ liệu bị THIẾU ÂM THẦM thay vì kích
    hoạt fallback giữ cache cũ. Do đó phải raise tường minh ở đây khi phát
    hiện phiên không hợp lệ, để ClientPool.call_with_retry() biết mà báo
    thất bại (ok=False) đúng lúc."""
    payload = {
        "page": {"pageNum": 1, "pageSize": 2000},
        "timezoneOffset": 420,
        "createStartTime": start_str,
        "createStopTime": stop_str,
        "ticketStatus": ticket_statuses,
    }
    res_data = await client._post(ENDPOINT_FIND_TICKET, payload)

    if _is_session_invalidated(res_data):
        code_str = str(res_data.get("code"))
        message = res_data.get("message")
        if code_str == "512":
            raise SessionKickedError(
                f"[{username}] bị đá session (code=512, message={message!r}) khi gọi findCCTSTicket."
            )
        raise RuntimeError(
            f"[{username}] phiên không hợp lệ sau khi gọi findCCTSTicket "
            f"(code={code_str!r}, message={message!r})."
        )

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
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        _fetch_tickets_window_single_account(
                            account["username"], account["password"], ticket_statuses, start_str, stop_str
                        )
                        for account in CCTS_ACCOUNTS
                    ),
                    return_exceptions=True,
                ),
                timeout=CCTS_LOCK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            print(f"[!] Cào live tickets vượt quá {CCTS_LOCK_TIMEOUT_SECONDS}s — huỷ, nhả khoá, giữ cache cũ.")
            results = [asyncio.TimeoutError("CCTS_API_LOCK timeout")] * total
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
            "Create Time": item.get("createTime"),
            "Creator": item.get("ticketCreator"),
            "Source_Account": item.get("_source_account"),
            "Address": item.get("address") or item.get("stationAddress") or "",
            "Contact": item.get("contact") or "",
            "OwnerUserName": item.get("cctsTicketOwnerUserName") or "",
            "AssistantName": item.get("assistantName") or "",
        })
    return processed


def _combine_address_contact(address, contact) -> str:
    """Gộp địa chỉ + liên hệ thành 1 chuỗi — y hệt logic
    scripts/auto_ccts_optimized.py::enrich_and_filter_data() để 2 nguồn dữ
    liệu (Excel export cục bộ vs cache app) ra cùng định dạng cột Địa chỉ."""
    addr = str(address or "").strip()
    ct = str(contact or "").strip()
    if addr and ct:
        return f"{addr} - Liên hệ: {ct}"
    return addr or ct


async def fetch_live_tickets():
    """Cào ticket ĐANG MỞ từ TẤT CẢ tài khoản, gộp + khử trùng theo Ticket ID.
    Trả về (DataFrame, fetch_success_bool, success_accounts).
    fetch_success=True chỉ khi mọi tài khoản CCTS đều cào thành công.
    success_accounts = [(username, password), ...] các tài khoản đã cào
    thành công trong chu kỳ này.

    QUAN TRỌNG (2026-09-16): phải dùng giờ VN (VN_TZ), KHÔNG dùng
    datetime.now() trần — xem giải thích chi tiết ở _fetch_reopen_map_via_export().
    Bug này tồn tại từ trước (Render chạy UTC, createStopTime bị lùi ~7h so
    với thời điểm thật), khả năng làm ticket vừa tạo trong ~7h gần nhất bị
    thiếu khỏi bản đồ cho tới khi cửa sổ giờ trôi qua đủ xa."""
    now_str = datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    raw, any_success, success_accounts = await _fetch_tickets_window_multi_account(
        OPEN_STATUSES, OPEN_WINDOW_START_STR, now_str
    )

    processed = _process_raw_tickets(raw)
    if not processed:
        return pd.DataFrame(), any_success, success_accounts

    df = pd.DataFrame(processed)
    df = df.fillna("")

    if "Ticket ID" in df.columns:
        df = df.drop_duplicates(subset=["Ticket ID"]).reset_index(drop=True)
    if "Problem Description" in df.columns:
        df = df[~df["Problem Description"].astype(str).str.strip().str.startswith("BSS.No")].copy()

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
    """Với các ticket đang Open + SẮP hoặc ĐÃ overdue (>=NO_INFO_EARLY_WARNING_HOURS,
    mặc định 47.5h/còn ≤30') + là trụ EV (không phải BSS): tra cứu chi tiết
    qua CCTSClient.search_ticket() (song song có giới hạn
    ENRICH_MAX_CONCURRENCY) để phát hiện ticket "mở lại" và ticket chưa có
    bất kỳ thông tin xử lý nào.

    Ngưỡng hạ từ >48h xuống >=NO_INFO_EARLY_WARNING_HOURS (2026-09-16) để
    is_no_info_critical hiện lên UI SỚM HƠN lúc ticket thực sự vượt 48h —
    cho người phân việc (QC) thời gian yêu cầu kỹ thuật xử lý tay trước khi
    quá hạn. KHÔNG có hành động tự động nào chạy theo cờ này (đã bỏ tính
    năng tự đóng — xem comment ở khai báo NO_INFO_EARLY_WARNING_HOURS).

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
        & (df["Hours"] >= NO_INFO_EARLY_WARNING_HOURS)
        & (df["Charge Point ID"].apply(_is_ev_charge_point))
    )
    targets = df[mask]
    if targets.empty:
        return {}

    ticket_ids = sorted({str(t) for t in targets["Ticket ID"].tolist() if t})
    if not ticket_ids:
        return {}

    async def _do_enrich():
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
        return {tid: data for tid, data in results if data is not None}

    async with CCTS_API_LOCK:
        try:
            enrichment_map = await asyncio.wait_for(_do_enrich(), timeout=CCTS_LOCK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            print(f"[!] Tra cứu chi tiết ticket vượt quá {CCTS_LOCK_TIMEOUT_SECONDS}s — huỷ, nhả khoá.")
            enrichment_map = {}

    print(
        f"[+] Tra cứu chi tiết thành công {len(enrichment_map)}/{len(ticket_ids)} "
        f"ticket Open-overdue (EV)."
    )
    return enrichment_map


def _apply_enrichment(status, ticket_id, enrichment_map):
    """Trả về (status_display, severity_override, is_reopened, is_no_info_critical)
    cho 1 ticket, dựa trên enrichment_map (có thể rỗng / không chứa ticket này
    — khi đó trả về mặc định, không đổi gì).

    is_reopened CHỈ áp dụng khi ticket đang ở trạng thái Open hiện tại
    (KHÔNG áp dụng cho Appointment/Pending for spare parts/Pending for ASP
    close dù các trạng thái này cũng thuộc nhóm OPEN theo SLA) — timeline có
    từng đi qua 1 trạng thái "đã xử lý xong" rồi sang Pending for spare
    parts/Appointment là luồng xử lý bình thường, không phải "mở lại"."""
    enrich = enrichment_map.get(str(ticket_id)) if enrichment_map else None
    if not enrich:
        return status, None, False, False

    is_currently_open = str(status).strip().lower() == "open"
    is_reopened = is_currently_open and bool(enrich.get("is_reopened"))
    has_no_info = bool(enrich.get("has_no_info"))

    status_display = f"{status}{REOPEN_LABEL_SUFFIX}" if is_reopened else status
    # Không ép severity tím: overdue chưa có thông tin vẫn đỏ theo giờ.
    # Chỉ giữ is_no_info_critical để frontend hiện cờ cảnh báo.
    severity_override = None
    return status_display, severity_override, is_reopened, has_no_info


def _merge_enrichment(base, additions):
    """Merge `additions` vào `base` theo TỪNG KEY con (is_reopened/has_no_info),
    KHÔNG ghi đè nguyên cả dict — nếu chỉ merge thô bằng {**base, **additions},
    1 ticket đã có has_no_info=True từ nguồn A sẽ bị MẤT field đó khi nguồn B
    chỉ trả về {"is_reopened": True} (thiếu has_no_info), do dict của B thay
    thế toàn bộ dict cũ thay vì bổ sung. Trả về dict MỚI (không sửa base)."""
    merged = dict(base)
    for tid, data in additions.items():
        merged[tid] = {**merged.get(tid, {}), **data}
    return merged


async def _export_events_for_account(username, password, start_str, stop_str):
    """Xuất Excel (lọc server-side theo OPEN_STATUSES) cho 1 tài khoản, trả về
    DataFrame sheet "Events Record" (rỗng nếu lỗi/không có gì). Dùng lại
    ClientPool.call_with_retry() nên tự login/relogin 1 lần khi cần — bản
    thân export_and_download_tickets() cũng đã tự xử lý bị đá session trong
    lúc chờ file (xem api_client.py), ở đây chỉ cần retry nếu toàn bộ lượt
    export thất bại (vd lỗi mạng, timeout tạo task)."""
    async def _action(client):
        dfs = await client.export_and_download_tickets(
            start_time=start_str, end_time=stop_str, ticket_status=OPEN_STATUSES,
            timeout=REOPEN_EXPORT_TIMEOUT_SECONDS, usecols_map=REOPEN_EXPORT_USECOLS,
        )
        if not dfs:
            raise RuntimeError("export_and_download_tickets trả về rỗng")
        return dfs.get("Events Record")

    result, ok = await _pool.call_with_retry(username, password, _action)
    if not ok or result is None:
        return pd.DataFrame()
    return result


async def _fetch_reopen_map_via_export(success_accounts):
    """Xác nhận is_reopened cho MỌI ticket đang mở (không giới hạn EV/overdue)
    bằng export Excel lọc theo OPEN_STATUSES — xem giải thích ở khai báo
    hằng số OPEN_WINDOW_START_STR phía trên. Chạy THÊM song song với
    fetch_live_tickets(), KHÔNG thay thế: nếu export lỗi/timeout, chỉ mất
    is_reopened của CHU KỲ NÀY (map vẫn hiển thị bình thường bằng dữ liệu vị
    trí/trạng thái đã cào được), không raise.

    Trả {ticket_id_str: {"is_reopened": True}} — CHỈ chứa ticket đã XÁC NHẬN
    mở lại (không có nghĩa "chưa xuất hiện ở đây" là "chắc chắn không mở lại"
    — vd export lỗi cho 1 tài khoản thì ticket của tài khoản đó vẫn thiếu)."""
    if not success_accounts:
        return {}

    # QUAN TRỌNG: phải dùng giờ VN (VN_TZ), KHÔNG dùng datetime.now() trần —
    # server chạy trên Render mặc định giờ hệ thống là UTC (không có biến
    # môi trường TZ), trong khi create_export_task() nhận start/end time kèm
    # timezoneOffset=420 (7h) với ngụ ý chuỗi truyền vào LÀ giờ VN. Nếu lỡ
    # truyền datetime.now() (thực chất là giờ UTC) vào như thể là giờ VN, mốc
    # "end_time" gửi lên sẽ bị lùi ~7h so với thời điểm thật -> export bỏ sót
    # ticket vừa tạo trong ~7h gần nhất. Cùng quy ước đã dùng ở
    # stats_source.scrape_time_range() (nguồn export đã chạy ổn định lâu nay).
    now_str = datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")

    async def _do_export():
        print(
            f"[ccts_data] Đã giữ CCTS_API_LOCK — export Events Record (chỉ ticket đang mở) "
            f"để xác nhận is_reopened cho MỌI ticket ({len(success_accounts)} tài khoản)..."
        )
        results = await asyncio.gather(
            *(
                _export_events_for_account(username, password, OPEN_WINDOW_START_STR, now_str)
                for username, password in success_accounts
            ),
            return_exceptions=True,
        )
        print("[ccts_data] Nhả CCTS_API_LOCK (export Events Record).")
        return results

    async with CCTS_API_LOCK:
        try:
            results = await asyncio.wait_for(_do_export(), timeout=CCTS_LOCK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            print(f"[!] Export Events Record vượt quá {CCTS_LOCK_TIMEOUT_SECONDS}s — huỷ, nhả khoá.")
            return {}

    reopen_map: dict[str, dict] = {}
    for result in results:
        if isinstance(result, Exception):
            print(f"[!] Lỗi export Events Record: {result}")
            continue
        events_df = result
        if events_df is None or events_df.empty:
            continue
        events_df = events_df.copy()
        events_df.columns = [str(c).strip() for c in events_df.columns]
        if "Ticket ID" not in events_df.columns or "Ticket Status" not in events_df.columns:
            print("[!] Sheet Events Record thiếu cột Ticket ID/Ticket Status — bỏ qua.")
            continue
        events_df["Ticket ID"] = events_df["Ticket ID"].astype(str).str.strip()
        events_df["_status"] = events_df["Ticket Status"].astype(str).str.strip().str.lower()
        for tid, g in events_df.groupby("Ticket ID"):
            if g["_status"].isin(REOPEN_TRIGGER_STATUSES).any():
                reopen_map[tid] = {"is_reopened": True}

    print(f"[+] Export Events Record xác nhận {len(reopen_map)} ticket đang mở lại (mọi loại trụ).")
    return reopen_map


def _rollup_closed_by_tech(previous_rows, current_open_ids):
    """Ticket có trong snapshot Open lần trước nhưng KHÔNG còn trong lần này
    -> vừa đóng (hoặc chuyển sang trạng thái khác không còn Open) — đếm theo
    tech_name đã gắn ở snapshot trước (ticket đã rời Open nên không còn trong
    dữ liệu lần này để tra tech_name lại). Trả dict {tech_name: count}."""
    counts: dict[str, int] = {}
    for row in previous_rows or []:
        tid = str(row.get("ticket_id") or "")
        if not tid or tid in current_open_ids:
            continue
        tech = row.get("tech_name") or "Unassigned"
        counts[tech] = counts.get(tech, 0) + 1
    return counts


def _today_str_vn():
    return datetime.now(VN_TZ).strftime("%Y-%m-%d")


def _update_closed_today_counter(closed_by_tech: dict) -> dict:
    """Cộng dồn closed_by_tech (số ticket vừa đóng ở CHU KỲ NÀY) vào counter
    theo ngày VN, tự reset khi qua ngày mới (giữ lại counts cũ làm
    "yesterday" trước khi reset). Đẩy R2/S3 như last_known_data.json (xem
    cache_store.py) để không mất số khi Render redeploy giữa ngày.
    Trả full counter {"date": ..., "counts": {tech: n}, "yesterday": {...}}."""
    from cache_store import load_closed_counter_cache, save_closed_counter_cache

    today = _today_str_vn()
    stored = load_closed_counter_cache(CLOSED_COUNTER_FILE) or {}
    if stored.get("date") != today:
        stored = {"date": today, "counts": {}, "yesterday": stored.get("counts") or {}}

    counts = stored.get("counts") or {}
    for tech, n in (closed_by_tech or {}).items():
        counts[tech] = counts.get(tech, 0) + n
    stored = {"date": today, "counts": counts, "yesterday": stored.get("yesterday") or {}}

    save_closed_counter_cache(stored, CLOSED_COUNTER_FILE)
    return stored


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
            "create_time": row.get("Create Time") or "",
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
            "address": _combine_address_contact(row.get("Address"), row.get("Contact")),
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


async def build_tech_performance_stats(open_stations, closed_today_counts=None, closed_yesterday_counts=None):
    """Đếm ticket ĐANG MỞ theo KT + số ticket ĐÃ ĐÓNG hôm nay/hôm qua — 2 số
    sau lấy từ counter tích luỹ real-time (xem _update_closed_today_counter),
    tính bằng cách diff Open-list giữa 2 lần cào, KHÔNG gọi thêm API."""
    closed_today_counts = closed_today_counts or {}
    closed_yesterday_counts = closed_yesterday_counts or {}

    open_counts: dict[str, int] = {}
    for s in open_stations:
        tech = s.get("tech_name") or "Unassigned"
        open_counts[tech] = open_counts.get(tech, 0) + int(s.get("cp_count") or 0)

    all_techs = set(open_counts) | set(closed_today_counts) | set(closed_yesterday_counts)
    return {
        tech: {
            "closed_yesterday": int(closed_yesterday_counts.get(tech, 0)),
            "closed_today": int(closed_today_counts.get(tech, 0)),
            "open_count": open_counts.get(tech, 0),
        }
        for tech in all_techs
    }


async def refresh_all_ccts_data(previous_ticket_rows=None):
    """1 chu kỳ làm mới đầy đủ: trạm + stats KT (open + đã đóng real-time) +
    ticket rows.

    previous_ticket_rows: snapshot ticket_rows của LẦN CÀO NGAY TRƯỚC (main.py
    truyền _latest_ticket_rows vào TRƯỚC KHI ghi đè) — dùng để diff Open-list,
    không tải lại lịch sử. None ở lần chạy đầu tiên (không có gì để diff)."""
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

    # Xác nhận is_reopened cho MỌI ticket đang mở (không giới hạn EV/overdue)
    # bằng export Excel — chạy MỖI chu kỳ, độc lập với việc có snapshot chu
    # kỳ trước hay không (khác với diff bên dưới, vốn cần previous_ticket_rows).
    if any_success:
        export_reopen_map = await _fetch_reopen_map_via_export(success_accounts)
        if export_reopen_map:
            enrichment_map = _merge_enrichment(enrichment_map, export_reopen_map)

    # Đếm "vừa đóng" theo KT — CHỈ khi lần cào NÀY thành công (nếu
    # any_success=False, df_filtered có thể rỗng/thiếu do lỗi, không phải vì
    # ticket thật sự đóng hết -> diff lúc đó sẽ đếm nhầm cả loạt "đã đóng").
    if any_success and previous_ticket_rows is not None:
        current_open_ids = {str(t) for t in df_filtered["Ticket ID"].tolist()} if not df_filtered.empty else set()
        closed_by_tech = _rollup_closed_by_tech(previous_ticket_rows, current_open_ids)
        counter = _update_closed_today_counter(closed_by_tech)
    else:
        counter = _update_closed_today_counter({})

    station_payload = _build_station_payload(
        df_filtered, cp_model_map, tech_map, region_map,
        total_tickets, len(missing_coord_tickets), filtered_north_count, any_success,
        missing_coord_tickets=missing_coord_tickets,
        enrichment_map=enrichment_map,
    )
    ticket_rows = _build_ticket_rows(df_filtered, cp_model_map, tech_map, region_map, enrichment_map=enrichment_map)
    tech_stats = await build_tech_performance_stats(
        station_payload["stations"],
        closed_today_counts=counter.get("counts"),
        closed_yesterday_counts=counter.get("yesterday"),
    )

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