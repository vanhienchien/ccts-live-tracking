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
    sh = get_spreadsheet()
    ws = sh.worksheet(sheet_name)
    df = get_as_dataframe(ws, evaluate_formulas=True)

    if df is None or df.empty:
        return pd.DataFrame(columns=columns)

    df = df.dropna(how="all")
    for col in columns:
        if col not in df.columns:
            df[col] = ""
    df = df[columns].fillna("")
    for col in columns:
        df[col] = df[col].astype(str).str.strip()
    return df


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


def update_station_assignment(station_code, new_engineer_name, region=None):
    """Ghi TRỰC TIẾP vào đúng 1 dòng trên sheet StationAssignments - đổi kỹ
    thuật viên phụ trách 1 trạm cụ thể. Nếu `region` được truyền vào (khu vực
    của kỹ thuật viên mới), cũng cập nhật luôn cột 'region' cho khớp - tránh
    tình trạng trạm bị lệch khu vực so với người phụ trách mới. Nếu trạm chưa
    từng có dòng nào, tự thêm 1 dòng mới."""
    target_core = extract_core_station_code(station_code)

    sh = get_spreadsheet()
    ws = sh.worksheet(ASSIGNMENTS_SHEET)
    station_col = ws.col_values(1)  # cột A = station_code

    row_idx = None
    for i, val in enumerate(station_col):
        if extract_core_station_code(val) == target_core:
            row_idx = i + 1  # gspread dùng chỉ số dòng 1-based
            break

    if row_idx is None:
        ws.append_row([station_code, region or "", new_engineer_name], value_input_option="USER_ENTERED")
    else:
        ws.update_cell(row_idx, 3, new_engineer_name)  # cột C = engineer_name
        if region:
            ws.update_cell(row_idx, 2, region)  # cột B = region

    _cache["ts"] = 0.0  # buộc đọc lại dữ liệu MỚI ở lần gọi kế tiếp, không dùng cache cũ
    return target_core