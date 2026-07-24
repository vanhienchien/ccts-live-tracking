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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from api_client import CCTSClient
from utils import extract_core_station_code, parse_duration_to_hours
from config import CCTS_ACCOUNTS
import static_data_store

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
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


def _status_color(status):
    return STATUS_COLORS.get(str(status).strip().lower(), "#7f8c8d")


def _severity_color(hours):
    """Trả về (key, mã_màu_nền_nhạt, mã_màu_viền/đậm, màu_chữ) theo số giờ
    tồn đọng của MỘT ticket cụ thể - thang màu vàng (mới) -> cam -> đỏ (tồn lâu)."""
    if hours > 48:
        return "red", "#fab5b5", "#c0392b", "#c0392b"
    elif hours >= 24:
        return "orange", "#ffd9ae", "#e67e22", "#b9650a"
    else:
        return "green", "#b8ffb2", "#34c02f", "#34c02f"


def get_static_data():
    """Toạ độ trạm / phân công kỹ thuật viên / model trụ sạc - giờ đọc từ
    Google Sheets (static_data_store.py) thay vì file cục bộ, để Điều phối
    có thể tự sửa mà không cần deploy lại."""
    return static_data_store.load_static_data()


def reload_static_data():
    return static_data_store.reload_static_data()


# ==========================================
# Cào ticket - đa tài khoản
# ==========================================
async def _fetch_tickets_window_single_account(username, password, ticket_statuses, start_str, stop_str):
    """Cào ticket cho 1 tài khoản, theo khoảng thời gian & danh sách trạng
    thái tuỳ ý. Trả về (list_dict_thô, thành_công_bool). Lỗi đăng nhập/mạng
    được BẮT Ở ĐÂY (không crash cả chu kỳ nếu 1 tài khoản gặp sự cố)."""
    client = CCTSClient(username=username, password=password)
    try:
        await client.login()
    except Exception as e:
        print(f"[-] Đăng nhập thất bại cho [{username}]: {e}")
        return [], False

    payload = {
        "page": {"pageNum": 1, "pageSize": 2000},
        "timezoneOffset": 420,
        "createStartTime": start_str,
        "createStopTime": stop_str,
        "ticketStatus": ticket_statuses,
    }

    try:
        res_data = await client._post(ENDPOINT_FIND_TICKET, payload)
        data = res_data.get("data", {})
        tickets = data.get("list", []) if isinstance(data, dict) else data
        if not isinstance(tickets, list):
            tickets = data.get("records", [])
        for t in tickets:
            t["_source_account"] = username
        return tickets or [], True
    except Exception as e:
        print(f"[-] Lỗi khi cào dữ liệu từ [{username}]: {e}")
        return [], False


async def _fetch_tickets_window_multi_account(ticket_statuses, start_str, stop_str):
    """Cào từ TẤT CẢ tài khoản trong CCTS_ACCOUNTS, gộp lại (chưa khử trùng).
    Trả về (list_dict_thô, có_ít_nhất_1_tài_khoản_thành_công)."""
    all_raw = []
    any_success = False
    for account in CCTS_ACCOUNTS:
        tickets, ok = await _fetch_tickets_window_single_account(
            account["username"], account["password"], ticket_statuses, start_str, stop_str
        )
        if ok:
            any_success = True
        all_raw.extend(tickets)
    return all_raw, any_success


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
        })
    return processed


async def fetch_live_tickets():
    """Cào ticket ĐANG MỞ từ TẤT CẢ tài khoản, gộp + khử trùng theo Ticket ID.
    Trả về (DataFrame, any_success_bool)."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    raw, any_success = await _fetch_tickets_window_multi_account(OPEN_STATUSES, "2026-04-30 17:00:00", now_str)

    processed = _process_raw_tickets(raw)
    if not processed:
        return pd.DataFrame(), any_success

    df = pd.DataFrame(processed)
    if "Ticket ID" in df.columns:
        df = df.drop_duplicates(subset=["Ticket ID"]).reset_index(drop=True)
    if "Problem Description" in df.columns:
        df = df[~df["Problem Description"].astype(str).str.strip().str.startswith("BSS.No2")].copy()

    print(f"[+] Tổng cộng: {len(df)} tickets sau khi gộp và lọc.")
    return df, any_success


def _apply_south_filter_and_coords(df_tickets, coords_map):
    """Gán toạ độ + lọc bỏ trạm miền Bắc (lat >= NORTH_LAT_THRESHOLD). Trả về
    (df_đã_lọc, số_lượng_bị_lọc_bỏ)."""
    def get_coords(station_code):
        core_code = extract_core_station_code(station_code)
        return coords_map.get(core_code)

    df = df_tickets.copy()
    df["coords"] = df["Station Code"].apply(get_coords)

    before = len(df)
    df = df[
        df["coords"].notna()
        & (df["coords"].apply(lambda x: x["lat"] if x else 0) < NORTH_LAT_THRESHOLD)
    ].copy()
    filtered_north_count = before - len(df)
    if filtered_north_count:
        print(f"[+] Đã lọc bỏ {filtered_north_count} ticket thuộc miền Bắc (lat >= {NORTH_LAT_THRESHOLD})")
    return df, filtered_north_count


def _build_station_payload(df_tickets_filtered, cp_model_map, tech_map, region_map, total_tickets_raw, missing_count, filtered_north_count, fetch_success):
    """Phần TÍNH TOÁN THUẦN (không gọi mạng) - gộp ticket đang mở (đã lọc
    miền Nam) với toạ độ trạm, trả về JSON sẵn sàng cho frontend."""
    stations = []

    if not df_tickets_filtered.empty:
        df = df_tickets_filtered.copy()
        df["Model Name"] = df["Charge Box Model"].map(cp_model_map).fillna("N/A")
        df["Hours"] = df["Ticket Duration"].apply(parse_duration_to_hours)

        grouped = df.groupby("Station Code")
        for station_code, group in grouped:
            core_code = extract_core_station_code(station_code)

            region = region_map.get(core_code, "Unknown")
            if region == "KV không quản lý":
                continue

            coords = group.iloc[0]["coords"]  # đã lọc nên chắc chắn có
            lat, lng = coords["lat"], coords["lng"]

            tech_name = tech_map.get(core_code, "Unassigned")
            max_duration = group["Hours"].max()
            station_severity, _, _, _ = _severity_color(max_duration)
            color = station_severity  # "red" / "orange" / "green" - màu marker trên bản đồ

            # Sắp xếp theo thời gian tồn đọng CAO -> THẤP
            group_sorted = group.sort_values("Hours", ascending=False)

            rows_html = ""
            for _, row in group_sorted.iterrows():
                status_color = _status_color(row["Ticket Status"])
                _, bg_color, border_color, text_color = _severity_color(row["Hours"])

                cp_id = str(row["Charge Point ID"])

                rows_html += f"""
                <div style="background:{bg_color};border:1px solid {border_color}55;border-left:4px solid {border_color};
                            border-radius:6px;padding:8px 10px;margin-bottom:8px;
                            box-shadow:0 1px 3px rgba(0,0,0,.06);">
                    <div style="display:flex;justify-content:space-between;align-items:center;
                                gap:6px;margin-bottom:4px;">
                        <span style="font-weight:700;color:#2c3e50;font-size:12.5px;">{cp_id}</span>
                        <span style="background:{status_color};color:#fff;font-size:10px;
                                    padding:2px 8px;border-radius:10px;font-weight:600;white-space:nowrap;">
                            {row['Ticket Status']}
                        </span>
                    </div>
                    <div style="color:#999;font-size:11px;margin-bottom:5px;">
                        {row['Model Name']} &nbsp;·&nbsp; ID {row['Ticket ID']}
                        {f" &nbsp;·&nbsp; {row['Creator']}" if row.get('Creator') else ""}
                    </div>
                    <div style="color:{text_color};font-size:12px;font-weight:700;margin-bottom:5px;">
                        🕐 {row['Ticket Duration']}
                    </div>
                    <div style="color:#555;font-size:12px;line-height:1.45;">
                        {row['Problem Description']}
                    </div>
                </div>
                """

            gmap_url = f"https://www.google.com/maps?q={lat},{lng}"
            header_color = {"red": "#a10000", "orange": "#e67e22", "green": "#39aa2f"}.get(color, "#3498db")
            popup_html = f"""
            <div style="font-family:'Segoe UI',Arial,sans-serif;width:270px;max-width:82vw;box-sizing:border-box;">
                <div style="background:{header_color};margin:-13px -13px 10px -13px;padding:10px 14px;
                            border-radius:5px 5px 0 0;">
                    <a href="{gmap_url}" target="_blank" rel="noopener noreferrer"
                       style="color:#fff;text-decoration:none;font-size:15px;font-weight:700;">
                        📍 {station_code}
                    </a>
                    <div style="color:rgba(255,255,255,.92);font-size:12px;margin-top:3px;
                                display:flex;align-items:center;gap:6px;">
                        <span>🧑‍🔧 {tech_name}</span>
                        <button type="button" class="edit-tech-btn" data-station="{core_code}"
                                data-current-tech="{tech_name}"
                                style="background:rgba(255,255,255,.25);border:none;color:#fff;
                                       border-radius:4px;padding:1px 6px;font-size:11px;cursor:pointer;">
                            ✏️
                        </button>
                    </div>
                </div>
                <div style="max-height:250px;overflow-y:auto;padding-right:4px;margin-right:-4px;">
                    {rows_html}
                </div>
            </div>
            """

            stations.append({
                "code": core_code,
                "station_code": station_code,
                "lat": lat,
                "lng": lng,
                "color": color,
                "popup_html": popup_html,
                "cp_count": int(len(group)),
                "region": region,
                "tech_name": tech_name,
                "is_unassigned": (not tech_name) or tech_name.strip().lower() == "unassigned",
            })

    return {
        "stations": stations,
        "total_tickets": total_tickets_raw,
        "missing_count": missing_count,
        "filtered_north": filtered_north_count,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "fetch_success": fetch_success,
    }


def _build_ticket_rows(df_tickets_filtered, cp_model_map, tech_map, region_map):
    """Danh sách ticket phẳng, chi tiết từng dòng (dùng cho panel danh sách
    Ticket dạng bảng theo từng kỹ thuật viên) - sắp xếp CAO -> THẤP theo giờ tồn."""
    if df_tickets_filtered.empty:
        return []

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
        cp_id = str(row.get("Charge Point ID") or "")
        rows.append({
            "ticket_id": row.get("Ticket ID"),
            "duration": row.get("Ticket Duration"),
            "hours": float(row.get("Hours") or 0),
            "station_code": station_code,
            "cp_id": cp_id,
            "is_bss": cp_id.strip().upper().startswith("BSS"),
            "model_name": row.get("Model Name"),
            "status": row.get("Ticket Status"),
            "description": row.get("Problem Description"),
            "creator": row.get("Creator"),
            "tech_name": tech_name,
            "region": region,
        })
    return rows


async def build_station_markers():
    """Cào ticket mới nhất + gộp với toạ độ trạm, lọc khu vực miền Nam, trả
    JSON cho frontend. Giữ lại để tương thích ngược (không dùng chung phiên
    với thống kê hiệu suất)."""
    coords_map, tech_map, region_map, cp_model_map, _ = get_static_data()
    df_tickets, any_success = await fetch_live_tickets()

    total_tickets = 0 if df_tickets.empty else len(df_tickets)
    missing_count = 0
    filtered_north_count = 0

    if not df_tickets.empty:
        df_tickets, filtered_north_count = _apply_south_filter_and_coords(df_tickets, coords_map)

    payload = _build_station_payload(
        df_tickets, cp_model_map, tech_map, region_map,
        total_tickets, missing_count, filtered_north_count, any_success,
    )
    print(f"[+] Hoàn tất build markers: {len(payload['stations'])} trạm, {total_tickets} tickets ban đầu.")
    return payload


async def build_tech_performance_stats(open_stations):
    """Đếm ticket đã đóng (Pending for local team close / Pending for VOMS
    confirm) tính từ 0h HÔM QUA tới hiện tại (giờ Việt Nam), tách riêng HÔM
    QUA/HÔM NAY theo từng kỹ thuật viên, cộng số ticket đang tồn đọng hiện
    tại của mỗi người (suy ra từ open_stations, không cần gọi thêm API)."""
    _, tech_map, _, _, _ = get_static_data()

    now_vn = datetime.now(VN_TZ)
    today_start = now_vn.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    yesterday_end = today_start - timedelta(seconds=1)

    def _dedupe(raw_tickets):
        seen = set()
        out = []
        for t in raw_tickets:
            tid = t.get("cctsTicketId")
            if tid in seen:
                continue
            seen.add(tid)
            out.append(t)
        return out

    def _count_by_tech(raw_tickets):
        counts = {}
        for item in raw_tickets:
            station_code = item.get("stationCode")
            core_code = extract_core_station_code(station_code) if station_code else None
            tech_name = tech_map.get(core_code, "Unassigned") if core_code else "Unassigned"
            counts[tech_name] = counts.get(tech_name, 0) + 1
        return counts

    try:
        yesterday_raw, _ = await _fetch_tickets_window_multi_account(
            CLOSED_STATUSES,
            yesterday_start.strftime("%Y-%m-%d %H:%M:%S"),
            yesterday_end.strftime("%Y-%m-%d %H:%M:%S"),
        )
        today_raw, _ = await _fetch_tickets_window_multi_account(
            CLOSED_STATUSES,
            today_start.strftime("%Y-%m-%d %H:%M:%S"),
            now_vn.strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception as e:
        print(f"⚠️ Lỗi khi cào thống kê ticket đã đóng (bỏ qua, không ảnh hưởng dữ liệu trạm): {e!r}")
        yesterday_raw, today_raw = [], []

    closed_yesterday = _count_by_tech(_dedupe(yesterday_raw))
    closed_today = _count_by_tech(_dedupe(today_raw))

    open_counts = {}
    for s in open_stations:
        tech = s.get("tech_name") or "Unassigned"
        open_counts[tech] = open_counts.get(tech, 0) + int(s.get("cp_count") or 0)

    all_techs = set(closed_yesterday) | set(closed_today) | set(open_counts)
    return {
        tech: {
            "closed_yesterday": closed_yesterday.get(tech, 0),
            "closed_today": closed_today.get(tech, 0),
            "open_count": open_counts.get(tech, 0),
        }
        for tech in all_techs
    }


async def refresh_all_ccts_data():
    """Điểm vào DUY NHẤT cho 1 chu kỳ làm mới đầy đủ: trạm đang lỗi (đã lọc
    miền Nam) + thống kê hiệu suất kỹ thuật viên + danh sách ticket chi tiết
    cho panel. Trả về (station_payload, tech_stats, ticket_rows)."""
    coords_map, tech_map, region_map, cp_model_map, _ = get_static_data()
    df_tickets, any_success = await fetch_live_tickets()

    total_tickets = 0 if df_tickets.empty else len(df_tickets)
    missing_count = 0
    filtered_north_count = 0
    df_filtered = df_tickets

    if not df_tickets.empty:
        df_filtered, filtered_north_count = _apply_south_filter_and_coords(df_tickets, coords_map)

    station_payload = _build_station_payload(
        df_filtered, cp_model_map, tech_map, region_map,
        total_tickets, missing_count, filtered_north_count, any_success,
    )
    ticket_rows = _build_ticket_rows(df_filtered, cp_model_map, tech_map, region_map)
    tech_stats = await build_tech_performance_stats(station_payload["stations"])

    print(f"[+] Hoàn tất chu kỳ làm mới: {len(station_payload['stations'])} trạm, "
          f"{total_tickets} ticket mở, {len(ticket_rows)} dòng ticket chi tiết.")

    return station_payload, tech_stats, ticket_rows


# ==========================================
# Cache ra file - dữ liệu không mất khi server khởi động lại, hoặc khi TẤT
# CẢ tài khoản CCTS đều đăng nhập thất bại ngay sau lúc restart.
# ==========================================
def save_cache_to_file(station_payload, tech_stats, ticket_rows):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "station_payload": station_payload,
                "tech_stats": tech_stats,
                "ticket_rows": ticket_rows,
            }, f, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Không thể lưu cache dữ liệu ra file: {e}")


def load_cache_from_file():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("station_payload"), data.get("tech_stats", {}), data.get("ticket_rows", [])
    except Exception as e:
        print(f"⚠️ Không thể đọc cache dữ liệu từ file: {e}")
    return None, {}, []


# ==========================================
# Giới hạn xem theo khu vực (Kỹ thuật viên)
# ==========================================
def filter_stations_for_user(stations, user):
    """Kỹ thuật viên chỉ được xem trạm trong CHÍNH khu vực của họ. Trạm chưa
    gán kỹ thuật viên (Unassigned) công khai cho TẤT CẢ, bất kể khu vực. Các
    vai trò khác (Điều phối khu vực trở lên) xem được toàn bộ."""
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
    """Điều phối khu vực / Kỹ thuật chỉ thấy danh sách kỹ thuật viên trong
    CHÍNH khu vực họ kiểm soát. Điều hành/Giám đốc/Admin thấy toàn bộ."""
    role = (user.get("role") or "").strip().lower()
    if role not in ("kỹ thuật", "điều phối khu vực"):
        return tech_by_region

    user_region = (user.get("region") or "").strip()
    return {r: v for r, v in tech_by_region.items() if r == user_region}