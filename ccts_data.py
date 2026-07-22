"""
Module cào & xử lý dữ liệu ticket CCTS + toạ độ trạm.
Hỗ trợ nhiều tài khoản CCTS (esmanager + itsmanager).
"""

import os
import json
from datetime import datetime

import pandas as pd

from api_client import CCTSClient
from utils import extract_core_station_code, parse_duration_to_hours
from config import CCTS_ACCOUNTS   # Sử dụng danh sách tài khoản từ config


# Màu badge trạng thái ticket
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


_static_cache = None


def load_static_data():
    """Tải và parse file JSON/Excel 1 lần duy nhất."""
    coords_map = {}
    tech_map = {}
    region_map = {}
    cp_model_map = {}

    # Đọc Excel listLongLat.xlsx
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

    # Đọc Kỹ thuật viên & Region từ list_Stations.json
    tech_by_region = {}
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

    tech_by_region = {region: sorted(names) for region, names in tech_by_region.items()}

    # Đọc ChargePoint_Model.xlsx
    try:
        if os.path.exists("ChargePoint_Model.xlsx"):
            df_model = pd.read_excel("ChargePoint_Model.xlsx")
            cp_model_map = dict(zip(df_model["Charge Point Model"], df_model["Name"]))
    except Exception as e:
        print(f"Lỗi đọc ChargePoint_Model.xlsx: {e}")

    return coords_map, tech_map, region_map, cp_model_map, tech_by_region


def get_static_data():
    global _static_cache
    if _static_cache is None:
        _static_cache = load_static_data()
    return _static_cache


def reload_static_data():
    global _static_cache
    _static_cache = load_static_data()
    return _static_cache


async def _fetch_tickets_for_single_user(username: str, password: str) -> list:
    """Cào ticket cho một tài khoản duy nhất."""
    client = CCTSClient(username=username, password=password)

    try:
        await client.login()
        print(f"[+] Đăng nhập thành công tài khoản: {username}")
    except Exception as e:
        print(f"[-] Đăng nhập thất bại cho [{username}]: {e}")
        return []

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
                "Creator": item.get("ticketCreator"),
                "Source_Account": username   # Để trace nguồn
            })

        print(f"[+] Lấy được {len(processed_data)} tickets từ {username}")
        return processed_data

    except Exception as e:
        print(f"[-] Lỗi khi cào dữ liệu từ [{username}]: {e}")
        return []


async def fetch_live_tickets():
    """Cào ticket từ TẤT CẢ tài khoản trong config và gộp lại."""
    all_tickets = []

    for account in CCTS_ACCOUNTS:
        user_tickets = await _fetch_tickets_for_single_user(
            account["username"], 
            account["password"]
        )
        all_tickets.extend(user_tickets)

    if not all_tickets:
        print("[-] Không lấy được ticket nào từ các tài khoản.")
        return pd.DataFrame()

    df = pd.DataFrame(all_tickets)

    # Loại bỏ trùng lặp theo Ticket ID
    if "Ticket ID" in df.columns:
        df = df.drop_duplicates(subset=["Ticket ID"]).reset_index(drop=True)

    # Lọc BSS.No2
    if "Problem Description" in df.columns:
        df = df[~df["Problem Description"].astype(str).str.strip().str.startswith("BSS.No2")].copy()

    print(f"[+] Tổng cộng: {len(df)} tickets sau khi gộp và lọc.")
    return df


# ==================== Các hàm còn lại giữ nguyên ====================

async def build_station_markers():
    """Cào ticket mới nhất + gộp với toạ độ trạm, lọc khu vực miền Nam, trả JSON cho frontend."""
    coords_map, tech_map, region_map, cp_model_map, tech_by_region = get_static_data()
    df_tickets = await fetch_live_tickets()

    stations = []
    missing_count = 0
    filtered_north_count = 0
    total_tickets = 0 if df_tickets.empty else len(df_tickets)

    if not df_tickets.empty:
        df_tickets = df_tickets.copy()
        
        # === GÁN TỌA ĐỘ VÀ LỌC MIỀN NAM ===
        def get_coords(station_code):
            core_code = extract_core_station_code(station_code)
            return coords_map.get(core_code)

        # Thêm cột tọa độ
        df_tickets["coords"] = df_tickets["Station Code"].apply(get_coords)
        
        # Lọc bỏ trạm miền Bắc (lat >= 16.2)
        before_filter = len(df_tickets)
        df_tickets = df_tickets[
            df_tickets["coords"].notna() & 
            (df_tickets["coords"].apply(lambda x: x["lat"] if x else 0) < 16.2)
        ].copy()
        
        filtered_north_count = before_filter - len(df_tickets)
        print(f"[+] Đã lọc bỏ {filtered_north_count} ticket thuộc miền Bắc (lat >= 16.2)")

        # Tiếp tục xử lý như cũ
        df_tickets["Model Name"] = df_tickets["Charge Box Model"].map(cp_model_map).fillna("N/A")
        df_tickets["Hours"] = df_tickets["Ticket Duration"].apply(parse_duration_to_hours)

        grouped = df_tickets.groupby("Station Code")
        for station_code, group in grouped:
            core_code = extract_core_station_code(station_code)

            region = region_map.get(core_code, "Unknown")
            if region == "KV không quản lý":
                continue

            coords = group.iloc[0]["coords"]  # Đã lọc nên chắc chắn có
            lat = coords["lat"]
            lng = coords["lng"]
            
            tech_name = tech_map.get(core_code, "Unassigned")
            max_duration = group["Hours"].max()
            color = "darkred" if max_duration > 48 else ("orange" if max_duration >= 24 else "green")

            rows_html = ""
            for _, row in group.iterrows():
                color_ticket = "darkred" if row["Hours"] > 48 else ("orange" if row["Hours"] >= 24 else "green")
                status_color = {"darkred": "#8b0000", "orange": "#e67e22", "green": "#219150"}.get(color_ticket, "#3498db")
                bg_color = {"darkred": "#ffb5b5", "orange": "#ffd398", "green": "#d4ffce"}.get(color_ticket, "#e7f3fa")
                rows_html += f"""
                <div style="background:{bg_color};border:1px solid #eee;border-left:4px solid {status_color};
                            border-radius:6px;padding:8px 10px;margin-bottom:8px;
                            box-shadow:0 1px 3px rgba(0,0,0,.06);">
                    <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;margin-bottom:4px;">
                        <span style="background:{status_color};color:#fff;font-size:10px;padding:2px 8px;border-radius:10px;font-weight:600;">
                            {row['Ticket Status']}
                        </span>
                    </div>
                    <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;margin-bottom:4px;">
                        <span style="font-weight:700;color:#2c3e50;font-size:12.5px;">⚡ {row['Charge Point ID']}({row['Model Name']})</span>
                    </div>

                    <div style="display:flex;justify-content:space-between;align-items:center;gap:5px;margin-bottom:3px;">
                         Ticket ID: {row['Ticket ID']} · Creator: {row.get('Creator', 'N/A')}
                    </div>
                    <div style="color:{color if color != 'green' else '#2ca02c'};font-size:12px;font-weight:700;margin-bottom:5px;">
                        🕐 {row['Ticket Duration']}
                    </div>
                    <div style="color:#555;font-size:12px;line-height:1.45;">
                        {row['Problem Description']}
                    </div>
                </div>
                """

            gmap_url = f"https://www.google.com/maps?q={lat},{lng}"
            
            header_color = {"darkred": "#a10000", "orange": "#e67e22", "green": "#1E9428"}.get(color, "#3498db")
            popup_html = f"""
            <div style="font-family:'Segoe UI',Arial,sans-serif;width:270px;max-width:82vw;box-sizing:border-box;">
                <div style="background:{header_color};margin:-13px -13px 10px -13px;padding:10px 14px;border-radius:5px 5px 0 0;">
                    <a href="{gmap_url}" target="_blank" rel="noopener noreferrer" style="color:#fff;text-decoration:none;font-size:15px;font-weight:700;">
                        📍 {station_code}
                    </a>
                    <div style="color:rgba(255,255,255,.92);font-size:14px;margin-top:3px;">
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

    print(f"[+] Hoàn tất build markers: {len(stations)} trạm, {total_tickets} tickets ban đầu.")

    return {
        "stations": stations,
        "total_tickets": total_tickets,
        "missing_count": missing_count,
        "filtered_north": filtered_north_count,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def filter_stations_for_user(stations, user):
    """Giữ nguyên logic phân quyền."""
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