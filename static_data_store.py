"""
Đọc (và ghi) dữ liệu tĩnh (toạ độ trạm, phân công kỹ thuật viên theo khu vực,
tên model trụ sạc) từ Google Sheets thay vì file cục bộ (listLongLat.xlsx,
list_Stations.json, ChargePoint_Model.xlsx) - để Điều phối/Admin có thể tự
thêm/sửa trực tiếp trên Sheets mà KHÔNG CẦN deploy lại code trên Render.

Dùng chung 1 Google Spreadsheet với tab "Users" (đỡ phải quản lý nhiều link).
Cần tạo thêm 3 tab (sheet) trong đúng spreadsheet đó:

- "StationAssignments"  cột: station_code | region | engineer_name
- "StationCoords"       cột: station_code | lat | long
- "ChargePointModels"   cột: charge_point_model | name

Ví dụ "StationAssignments":
    station_code | region        | engineer_name
    C.BTH0073    | Miền Nam      | Nguyễn Quốc Phi
    C.LDO10282   | Miền Nam      | Unassigned
    ...

Cột engineer_name để trống hoặc ghi "Unassigned" nếu trạm chưa có ai phụ trách.
"""

import time

import pandas as pd
from gspread_dataframe import get_as_dataframe

from config import get_spreadsheet
from utils import extract_core_station_code

ASSIGNMENTS_SHEET = "StationAssignments"
COORDS_SHEET = "StationCoords"
MODELS_SHEET = "ChargePointModels"

CACHE_TTL_SECONDS = 120  # tránh gọi Google Sheets liên tục mỗi lần build map

_cache = {"data": None, "ts": 0.0}


def _read_sheet_df(sheet_name, columns):
    """Đọc sheet với debug tốt hơn."""
    try:
        sh = get_spreadsheet()
        ws = sh.worksheet(sheet_name)

        # Lấy raw data để debug
        values = ws.get_all_values()
        print(f"   Số dòng dữ liệu: {len(values)}")
        if values:
            print(f"   Header mẫu: {values[0][:5] if len(values[0]) > 0 else 'Empty'}")

        df = get_as_dataframe(ws, evaluate_formulas=True)

        if df is None or df.empty:
            print(f"   ⚠️ DataFrame rỗng sau get_as_dataframe cho {sheet_name}")
            return pd.DataFrame(columns=columns)

        print(f"   DataFrame shape: {df.shape}, columns: {list(df.columns)}")

        df = df.dropna(how="all")
        for col in columns:
            if col not in df.columns:
                print(f"   ⚠️ Cột '{col}' không tồn tại, thêm cột rỗng")
                df[col] = ""
        df = df[columns].fillna("")
        for col in columns:
            df[col] = df[col].astype(str).str.strip()
        return df
    except Exception as e:
        print(f"❌ Lỗi khi đọc sheet '{sheet_name}': {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame(columns=columns)


def load_static_data(force=False):
    """Trả về (coords_map, tech_map, region_map, cp_model_map, tech_by_region)
    - có cache TTL để không gọi Sheets liên tục."""
    now = time.time()
    if not force and _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL_SECONDS:
        return _cache["data"]

    coords_map = {}
    tech_map = {}
    region_map = {}
    cp_model_map = {}
    tech_by_region_set = {}

    try:
        df_coords = _read_sheet_df(COORDS_SHEET, ["station_code", "lat", "long"])
        for _, row in df_coords.iterrows():
            if not row["station_code"] or not row["lat"] or not row["long"]:
                continue
            core_code = extract_core_station_code(row["station_code"])
            try:
                coords_map[core_code] = {"lat": float(row["lat"]), "lng": float(row["long"])}
            except ValueError:
                continue
    except Exception as e:
        print(f"⚠️ Lỗi đọc sheet '{COORDS_SHEET}': {e}")

    try:
        df_assign = _read_sheet_df(ASSIGNMENTS_SHEET, ["station_code", "region", "engineer_name"])
        for _, row in df_assign.iterrows():
            if not row["station_code"]:
                continue
            core_code = extract_core_station_code(row["station_code"])
            region = row["region"] or "Unknown"
            eng = row["engineer_name"] or "Unassigned"
            tech_map[core_code] = eng
            region_map[core_code] = region
            if eng and eng.strip().lower() != "unassigned":
                tech_by_region_set.setdefault(region, set()).add(eng)
        
    except Exception as e:
        print(f"⚠️ Lỗi đọc sheet '{ASSIGNMENTS_SHEET}': {e}")

    tech_by_region = {region: sorted(names) for region, names in tech_by_region_set.items()}

    try:
        df_model = _read_sheet_df(MODELS_SHEET, ["charge_point_model", "name"])
        cp_model_map = dict(zip(df_model["charge_point_model"], df_model["name"]))
    except Exception as e:
        print(f"⚠️ Lỗi đọc sheet '{MODELS_SHEET}': {e}")

    data = (coords_map, tech_map, region_map, cp_model_map, tech_by_region)
    _cache["data"] = data
    _cache["ts"] = now
    return data


def reload_static_data():
    return load_static_data(force=True)


def update_station_assignment(station_code, new_engineer_name):
    """Cập nhật engineer_name và region (lấy từ sheet Users).
    Nếu trạm chưa tồn tại → thêm dòng mới."""
    target_core = extract_core_station_code(station_code)

    sh = get_spreadsheet()
    ws_assign = sh.worksheet(ASSIGNMENTS_SHEET)
    
    # ==================== LẤY REGION TỪ SHEET USERS ====================
    new_region = "Unknown"
    try:
        ws_users = sh.worksheet("Users")
        users_data = ws_users.get_all_values()
        
        if users_data and len(users_data) > 1:
            # Giả sử cột engineer_name ở cột B hoặc tìm theo tên cột
            headers = [str(h).strip().lower() for h in users_data[0]]
            name_col_idx = None
            region_col_idx = None
            
            for i, h in enumerate(headers):
                if 'name' in h or 'technician' in h or 'engineer' in h:
                    name_col_idx = i
                if 'region' in h or 'khu vực' in h or 'mien' in h:
                    region_col_idx = i
            
            if name_col_idx is not None and region_col_idx is not None:
                for row in users_data[1:]:
                    if len(row) > max(name_col_idx, region_col_idx):
                        if str(row[name_col_idx]).strip() == new_engineer_name.strip():
                            new_region = str(row[region_col_idx]).strip()
                            break
    except Exception as e:
        print(f"⚠️ Không tìm thấy region từ Users sheet: {e}")

    # ==================== CẬP NHẬT / THÊM VÀO ASSIGNMENTS ====================
    station_col = ws_assign.col_values(1)  # Cột A = station_code

    row_idx = None
    for i, val in enumerate(station_col):
        if extract_core_station_code(str(val)) == target_core:
            row_idx = i + 1
            break

    if row_idx is None:
        # Thêm dòng mới
        ws_assign.append_row([station_code, new_region, new_engineer_name], 
                            value_input_option="USER_ENTERED")
        print(f"✅ Thêm trạm mới: {station_code} → {new_engineer_name} ({new_region})")
    else:
        # Cập nhật
        ws_assign.update_cell(row_idx, 2, new_region)   # Cột B = region
        ws_assign.update_cell(row_idx, 3, new_engineer_name)  # Cột C = engineer_name
        print(f"✅ Cập nhật trạm {station_code}: {new_engineer_name} ({new_region})")

    _cache["ts"] = 0.0  # Buộc reload
    return target_core