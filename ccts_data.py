"""
Module cào & xử lý dữ liệu ticket CCTS + toạ độ trạm.

Logic load_static_data()/fetch_live_tickets() giữ NGUYÊN như bản Streamlit và
bản main.py demo bạn đã chạy thành công - chỉ khác là kết quả cuối cùng được
trả về dưới dạng JSON (list dict) thay vì HTML folium, để phía frontend
(Leaflet.js) tự vẽ marker và có thể cập nhật/animate mượt mà.
"""

import os
import json
from datetime import datetime

import pandas as pd

from api_client import CCTSClient
from utils import extract_core_station_code, parse_duration_to_hours
from config import CCTS_USER, CCTS_PASS

# Màu badge trạng thái ticket - dùng cho popup trạm chuyên nghiệp hơn
STATUS_COLORS = {
    "open": "#e74c3c",
    "appointment": "#3498db",
    "pending for asp close": "#9b59b6",
    "pending for spare parts": "#e67e22",
    "pending for local team close": "#16a085",
    "pending for voms confirm": "#16a085",
}


def _status_color(status):
    return STATUS_COLORS.get(str(status).strip().lower(), "#7f8c8d")


_static_cache = None  # coords_map, tech_map, region_map, cp_model_map (nạp 1 lần, ít khi đổi)


def load_static_data():
    """Tải và parse file JSON/Excel 1 lần duy nhất để tối ưu bộ nhớ (giống
    logic gốc trong app.py/main.py của bạn)."""
    coords_map = {}
    tech_map = {}
    region_map = {}
    cp_model_map = {}

    # # 1. Đọc JSON lấy tọa độ
    # try:
    #     if os.path.exists("station_info.json"):
    #         with open("station_info.json", "r", encoding="utf-8") as f:
    #             station_data = json.load(f)
    #             for entry in station_data:
    #                 store_id = entry.get("store_id")
    #                 lat = entry.get("lat")
    #                 lng = entry.get("lng")
    #                 if store_id and lat and lng:
    #                     core_code = extract_core_station_code(store_id)
    #                     coords_map[core_code] = {"lat": float(lat), "lng": float(lng)}
    # except Exception as e:
    #     print(f"Lỗi đọc station_info.json: {e}")

    # 2. Đọc Excel listLongLat.xlsx làm dự phòng
    try:
        if os.path.exists("listLongLat.xlsx"):
            df_coords = pd.read_excel("listLongLat.xlsx")
            col_map = {str(col).strip().lower(): col for col in df_coords.columns}
            st_col = col_map.get("station code")
            lat_col = col_map.get("lat")
            long_col = col_map.get("long")

            if st_col and lat_col and long_col:
                df_clean = df_coords.dropna(subset=[st_col, lat_col, long_col])
                for _, row in df_clean.iterrows():
                    core_code = extract_core_station_code(row[st_col])
                    if core_code and core_code not in coords_map:
                        coords_map[core_code] = {"lat": float(row[lat_col]), "lng": float(row[long_col])}
    except Exception as e:
        print(f"Lỗi đọc listLongLat.xlsx: {e}")

    # 3. Đọc Kỹ thuật viên & Region từ list_Stations.json
    tech_by_region = {}  # {region: set(tech_name)} - dùng cho tag lọc theo kỹ thuật viên
    try:
        if os.path.exists("list_Stations.json"):
            with open("list_Stations.json", "r", encoding="utf-8") as f:
                list_stations = json.load(f)
                for region, engs in list_stations.items():
                    tech_by_region.setdefault(region, set())
                    for eng, stations in engs.items():
                        tech_by_region[region].add(eng)
                        for st in stations:
                            core_code = extract_core_station_code(st)
                            tech_map[core_code] = eng
                            region_map[core_code] = region
    except Exception as e:
        print(f"Lỗi đọc list_Stations.json: {e}")

    # Chuyển set -> list đã sắp xếp để dễ trả JSON / hiển thị
    tech_by_region = {region: sorted(names) for region, names in tech_by_region.items()}

    # 4. Đọc ChargePoint_Model.xlsx
    try:
        if os.path.exists("ChargePoint_Model.xlsx"):
            df_model = pd.read_excel("ChargePoint_Model.xlsx")
            cp_model_map = dict(zip(df_model["Charge Point Model"], df_model["Name"]))
    except Exception as e:
        print(f"Lỗi đọc ChargePoint_Model.xlsx: {e}")

    return coords_map, tech_map, region_map, cp_model_map, tech_by_region


def get_static_data():
    """Dữ liệu tĩnh (toạ độ trạm, kỹ thuật viên, khu vực, model, danh sách kỹ
    thuật theo khu vực) hiếm khi đổi -> chỉ nạp 1 lần khi server khởi động,
    dùng lại cho mọi lần cào ticket."""
    global _static_cache
    if _static_cache is None:
        _static_cache = load_static_data()
    return _static_cache


def reload_static_data():
    """Gọi hàm này (vd qua 1 endpoint riêng) nếu bạn cập nhật file
    station_info.json/list_Stations.json/... và muốn nạp lại mà không cần
    khởi động lại server."""
    global _static_cache
    _static_cache = load_static_data()
    return _static_cache


async def fetch_live_tickets():
    """Cào dữ liệu ticket trực tiếp thông qua CCTSClient (async)."""
    client = CCTSClient(username=CCTS_USER, password=CCTS_PASS)

    try:
        await client.login()
    except Exception as e:
        print(f"Khởi động phiên đăng nhập CCTS thất bại: {e!r}")
        return pd.DataFrame()

    endpoint = "/ccts/cctsTicket/findCCTSTicket"

    payload = {
        "page": {"pageNum": 1, "pageSize": 2000},
        "timezoneOffset": 420,
        "createStartTime": "2026-04-30 17:00:00",
        "createStopTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ticketStatus": ["Open", "Appointment", "Pending for ASP close", "Pending for spare parts"],
    }

    try:
        res_data = await client._post(endpoint, payload)

        data = res_data.get("data", {})
        tickets = data.get("list", []) if isinstance(data, dict) else data
        if not isinstance(tickets, list):
            tickets = data.get("records", [])

        if not tickets:
            return pd.DataFrame()

        processed_data = []
        for item in tickets:
            processed_data.append({
                "Ticket ID": item.get("cctsTicketId"),
                "Charge Point ID": item.get("chargeBoxId"),
                "Charge Box Model": item.get("chargeBoxModel"),
                "Station Code": item.get("stationCode"),
                "Problem Description": item.get("errorDesc"),
                "Ticket Status": item.get("cctsTicketStatus"),
                "Ticket Duration": item.get("duration"),
            })

        df = pd.DataFrame(processed_data)

        if "Problem Description" in df.columns:
            df = df[~df["Problem Description"].astype(str).str.strip().str.startswith("BSS.No2")].copy()

        return df

    except Exception as e:
        print(f"Gặp lỗi khi xử lý luồng dữ liệu API: {e!r}")
        return pd.DataFrame()


async def build_station_markers():
    """Cào ticket mới nhất + gộp với toạ độ trạm, trả về JSON sẵn sàng để
    frontend (Leaflet.js) vẽ marker trực tiếp - không qua folium nữa."""
    coords_map, tech_map, region_map, cp_model_map, tech_by_region = get_static_data()
    df_tickets = await fetch_live_tickets()

    stations = []
    missing_count = 0
    total_tickets = 0 if df_tickets.empty else len(df_tickets)

    if not df_tickets.empty:
        df_tickets = df_tickets.copy()
        df_tickets["Model Name"] = df_tickets["Charge Box Model"].map(cp_model_map).fillna("N/A")
        df_tickets["Hours"] = df_tickets["Ticket Duration"].apply(parse_duration_to_hours)

        grouped = df_tickets.groupby("Station Code")
        for station_code, group in grouped:
            core_code = extract_core_station_code(station_code)

            region = region_map.get(core_code, "Unknown")
            if region == "KV không quản lý":
                continue

            if core_code not in coords_map:
                missing_count += 1
                continue

            lat = coords_map[core_code]["lat"]
            lng = coords_map[core_code]["lng"]
            tech_name = tech_map.get(core_code, "Unassigned")
            max_duration = group["Hours"].max()
            color = "darkred" if max_duration > 48 else ("orange" if max_duration >= 24 else "green")

            rows_html = ""
            for _, row in group.iterrows():
                status_color = _status_color(row["Ticket Status"])
                rows_html += f"""
                <div style="background:#fff;border:1px solid #eee;border-left:4px solid {status_color};
                            border-radius:6px;padding:8px 10px;margin-bottom:8px;
                            box-shadow:0 1px 3px rgba(0,0,0,.06);">
                    <div style="display:flex;justify-content:space-between;align-items:center;
                                gap:6px;margin-bottom:4px;">
                        <span style="font-weight:700;color:#2c3e50;font-size:12.5px;">⚡ {row['Charge Point ID']}</span>
                        <span style="background:{status_color};color:#fff;font-size:10px;
                                    padding:2px 8px;border-radius:10px;font-weight:600;white-space:nowrap;">
                            {row['Ticket Status']}
                        </span>
                    </div>
                    <div style="color:#999;font-size:11px;margin-bottom:5px;">
                        {row['Model Name']} &nbsp;·&nbsp; ID {row['Ticket ID']}
                    </div>
                    <div style="color:{color if color != 'green' else '#2ca02c'};font-size:12px;
                                font-weight:700;margin-bottom:5px;">
                        🕐 {row['Ticket Duration']}
                    </div>
                    <div style="color:#555;font-size:12px;line-height:1.45;">
                        {row['Problem Description']}
                    </div>
                </div>
                """

            gmap_url = f"https://www.google.com/maps?q={lat},{lng}"
            header_color = {"darkred": "#8b0000", "orange": "#e67e22", "green": "#219150"}.get(color, "#3498db")
            popup_html = f"""
            <div style="font-family:'Segoe UI',Arial,sans-serif;width:270px;max-width:82vw;box-sizing:border-box;">
                <div style="background:{header_color};margin:-13px -13px 10px -13px;padding:10px 14px;
                            border-radius:5px 5px 0 0;">
                    <a href="{gmap_url}" target="_blank" rel="noopener noreferrer"
                       style="color:#fff;text-decoration:none;font-size:15px;font-weight:700;">
                        📍 {station_code}
                    </a>
                    <div style="color:rgba(255,255,255,.92);font-size:12px;margin-top:3px;">
                        🧑‍🔧 {tech_name}
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
            })

    return {
        "stations": stations,
        "total_tickets": total_tickets,
        "missing_count": missing_count,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def filter_stations_for_user(stations, user):
    """Kỹ thuật viên chỉ được xem trạm trong CHÍNH khu vực của họ (bao gồm
    trạm của chính họ, đồng nghiệp kỹ thuật khác cùng khu vực). RIÊNG trạm
    chưa gán kỹ thuật viên (Unassigned) được công khai cho TẤT CẢ mọi người,
    bất kể khu vực - để ai cũng biết có trạm mới xuất hiện. Các vai trò khác
    (Điều phối khu vực trở lên) xem được toàn bộ, không giới hạn."""
    role = (user.get("role") or "").strip().lower()
    if role != "kỹ thuật":
        return stations

    user_region = (user.get("region") or "").strip().lower()

    result = []
    for s in stations:
        tech_name = (s.get("tech_name") or "").strip()
        if not tech_name or tech_name.lower() == "unassigned":
            result.append(s)  # Unassigned -> luôn hiển thị, không phân biệt khu vực
            continue
        if user_region and (s.get("region") or "").strip().lower() == user_region:
            result.append(s)
    return result