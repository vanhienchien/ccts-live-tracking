"""
stats_source.py — CHỈ lo việc LẤY dữ liệu ticket thô (raw sheets), từ 1
trong 2 nguồn:

  1) "ccts"  — cào trực tiếp từ CCTS (2 tài khoản cố định trong
     ccts_shared.STATS_SCRAPE_ACCOUNTS), có retry theo vòng + timeout,
     dùng chung CCTS_API_LOCK với ccts_data.py. Đây là nguồn mặc định cho
     production / lịch 0h / admin bấm refresh.

  2) "local" — đọc các file Excel CÓ SẴN (đúng định dạng CCTS export: đủ
     sheet "Ticket Information", "Events Record", "Spare Parts Record",
     "Appointment", "Additional information", "Solutions") trong 1 thư
     mục trên máy (mặc định thư mục "data/" cạnh file này). Mỗi file được
     coi như 1 "tài khoản" (nhãn = tên file), gộp + khử trùng Ticket ID
     giống hệt khi cào live nhiều tài khoản. KHÔNG gọi CCTS.

Cả 2 nguồn trả về CÙNG 1 hình dạng dữ liệu:

    (raw, events_raw, spare_raw, appt_raw, additional_raw, solutions_raw, meta_extra)

để stats_data.py xử lý (chuẩn hoá / tính overdue / open-long...) y hệt
nhau — không cần quan tâm dữ liệu đến từ CCTS hay từ file Excel tay.

Module này KHÔNG chứa logic chuẩn hoá/tính toán ticket — chỉ lấy & gộp dữ
liệu thô. Logic chuẩn hoá vẫn ở stats_data.py.
"""

from __future__ import annotations

import asyncio
import gc
import glob
import os
from datetime import datetime, timedelta

import pandas as pd

from ccts_shared import VN_TZ, CCTS_API_LOCK, STATS_SCRAPE_ACCOUNTS, ClientPool

# ------------------------------------------------------------------
# Cấu hình chọn nguồn dữ liệu.
# ------------------------------------------------------------------
# "ccts"  → luôn cào live (mặc định, production).
# "local" → luôn đọc thư mục Excel, không gọi CCTS.
STATS_DATA_SOURCE = os.environ.get("STATS_DATA_SOURCE", "ccts").strip().lower()

# Thư mục chứa file Excel khi dùng nguồn "local". Mặc định là thư mục
# "data/" NẰM CẠNH file này (trước đây hard-code path Windows tuyệt đối
# C:\Users\Admin\CCTS_DATA\data — os.path.join bỏ qua phần dirname() khi
# gặp path tuyệt đối thứ 2, nên chỉ chạy được đúng trên máy đã tạo ra nó;
# chưa từng "nổ" vì STATS_DATA_SOURCE mặc định là "ccts", không phải "local").
STATS_LOCAL_DATA_DIR = os.environ.get(
    "STATS_LOCAL_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "data"),
)

REQUIRED_SHEETS = [
    "Ticket Information", "Events Record", "Spare Parts Record",
    "Appointment", "Additional information", "Solutions",
]

# Cột thực sự dùng ở downstream, theo từng sheet — trim ngay khi vừa đọc
# xong (dù đọc live hay đọc file local) để không giữ full-width trong RAM.
# Cột không tồn tại trong sheet sẽ tự bị bỏ qua (không lỗi).
SHEET_KEEP_COLUMNS: dict[str, list[str]] = {
    "Ticket Information": [
        "Ticket ID", "Station Code", "Charge Point ID", "Create Time",
        "SLA Status", "Severity", "Problem Description",
        "Address", "Error Code", "Ticket Status",
    ],
    "Events Record": [
        "Ticket ID", "Ticket Status", "Create Time", "Record Detail",
    ],
    "Spare Parts Record": [
        "Ticket ID",
    ],
    "Appointment": [
        "Ticket ID", "Create Time", "Detail",
    ],
    "Additional information": [
        "Ticket ID", "TicketID", "Ticket",
        "Handling type", "Handling Type", "Handle type", "Handle Type",
    ],
    "Solutions": [
        "Ticket ID", "Solution Description", "Create Time",
    ],
}

SCRAPE_LOOKBACK_DAYS = 60
MAX_SCRAPE_ROUNDS = 10
SCRAPE_RETRY_DELAY_SECONDS = 20
EXPORT_TIMEOUT_SECONDS = int(os.environ.get("EXPORT_TIMEOUT_SECONDS", "180"))


def _trim_sheet_columns(name: str, df: pd.DataFrame) -> pd.DataFrame:
    keep = SHEET_KEEP_COLUMNS.get(name)
    if not keep or df is None or df.empty:
        return df
    cols_present = [c for c in df.columns if str(c).strip() in keep]
    if not cols_present:
        return df
    return df.loc[:, cols_present].copy()


def scrape_time_range() -> tuple[str, str, str]:
    """[start, end) giờ VN: end = 0h hôm nay, start = end - 60 ngày."""
    now = datetime.now(VN_TZ)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=SCRAPE_LOOKBACK_DAYS)
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d"),
    )


def _merge_bundles(bundles: dict[str, dict[str, pd.DataFrame]]):
    """Gộp + khử trùng Ticket ID của nhiều tài khoản/file thành 1 bộ raw
    duy nhất. Dùng chung cho cả 2 nguồn "ccts" và "local"."""
    ti_frames = [b["Ticket Information"] for b in bundles.values() if "Ticket Information" in b]
    ev_frames = [b["Events Record"] for b in bundles.values() if "Events Record" in b]
    sp_frames = [b["Spare Parts Record"] for b in bundles.values() if "Spare Parts Record" in b]
    ap_frames = [b["Appointment"] for b in bundles.values() if "Appointment" in b]
    ai_frames = [b["Additional information"] for b in bundles.values() if "Additional information" in b]
    sol_frames = [b["Solutions"] for b in bundles.values() if "Solutions" in b]

    raw = pd.concat(ti_frames, ignore_index=True) if ti_frames else pd.DataFrame()
    if "Ticket ID" in raw.columns:
        raw["Ticket ID"] = raw["Ticket ID"].astype(str).str.strip()
        before = len(raw)
        raw = raw.drop_duplicates(subset=["Ticket ID"])
        print(f"[stats-source] TI gộp {before} → {len(raw)}")
    events_raw = pd.concat(ev_frames, ignore_index=True) if ev_frames else pd.DataFrame()
    spare_raw = pd.concat(sp_frames, ignore_index=True) if sp_frames else pd.DataFrame()
    appt_raw = pd.concat(ap_frames, ignore_index=True) if ap_frames else pd.DataFrame()
    additional_raw = pd.concat(ai_frames, ignore_index=True) if ai_frames else pd.DataFrame()
    solutions_raw = pd.concat(sol_frames, ignore_index=True) if sol_frames else pd.DataFrame()
    if not events_raw.empty and "Ticket ID" in events_raw.columns:
        events_raw["Ticket ID"] = events_raw["Ticket ID"].astype(str).str.strip()

    return raw, events_raw, spare_raw, appt_raw, additional_raw, solutions_raw


# ------------------------------------------------------------------
# Nguồn 1: CCTS live (cào qua API, retry theo vòng)
# ------------------------------------------------------------------
_stats_pool = ClientPool()  # pool phiên đăng nhập RIÊNG của module này


async def _export_one_account(username, password, start_time, end_time) -> dict | None:
    """Export Excel cho 1 tài khoản (tự relogin 1 lần nếu bị đá session,
    qua ClientPool dùng chung). Trả dict các sheet DataFrame (đã gắn
    _source_account), hoặc None nếu thất bại."""

    async def _action(client):
        try:
            dfs = await asyncio.wait_for(
                client.export_and_download_tickets(
                    start_time=start_time,
                    end_time=end_time,
                    ticket_status=None,
                    usecols_map=SHEET_KEEP_COLUMNS,
                ),
                timeout=EXPORT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"export_and_download_tickets quá {EXPORT_TIMEOUT_SECONDS}s "
                "(CCTS không trả về file sẵn sàng) — bỏ cuộc để nhả khoá."
            )
        if not dfs:
            raise RuntimeError("export_and_download_tickets trả về rỗng")
        return dfs

    dfs, ok = await _stats_pool.call_with_retry(username, password, _action)
    if not ok or not dfs:
        return None

    out = {}
    for name, df in dfs.items():
        if df is None or (hasattr(df, "empty") and df.empty):
            continue
        part = _trim_sheet_columns(name, df)
        if part is df:
            part = df.copy()
        part["_source_account"] = username
        out[name] = part
        del df
    del dfs
    return out if out else None


async def fetch_live_bundle():
    """Cào 2 tài khoản cố định qua CCTS, retry theo vòng (tối đa
    MAX_SCRAPE_ROUNDS, cách nhau SCRAPE_RETRY_DELAY_SECONDS giây).

    Trả (raw, events_raw, spare_raw, appt_raw, additional_raw,
    solutions_raw, meta_extra), hoặc None nếu SAU TỐI ĐA số vòng vẫn
    không lấy được ticket nào từ bất kỳ tài khoản nào."""
    start_time, end_time, end_date_ex = scrape_time_range()
    print(f"[stats-source] Khoảng cào [start, end): {start_time} → {end_time} (không gồm {end_date_ex})")

    pending = {acc["username"]: acc for acc in STATS_SCRAPE_ACCOUNTS if acc.get("username")}
    bundles: dict[str, dict] = {}

    round_no = 0
    while pending and round_no < MAX_SCRAPE_ROUNDS:
        round_no += 1
        print(f"[stats-source] Vòng cào {round_no}/{MAX_SCRAPE_ROUNDS} — còn {len(pending)}: {list(pending)}")
        async with CCTS_API_LOCK:
            for username, acc in list(pending.items()):
                password = acc.get("password") or ""
                print(f"[stats-source] Đang cào account: {username} ...")
                try:
                    bundle = await _export_one_account(username, password, start_time, end_time)
                except Exception as e:
                    bundle = None
                    print(f"[stats-source]   → lỗi {username}: {e}")

                if bundle and "Ticket Information" in bundle:
                    bundles[username] = bundle
                    pending.pop(username, None)
                    print(f"[stats-source]   → OK {username}: TI={len(bundle['Ticket Information'])}")
                else:
                    print(f"[stats-source]   → chưa được: {username} (thử lại vòng sau)")

        if pending and round_no < MAX_SCRAPE_ROUNDS:
            print(f"[stats-source] Đợi {SCRAPE_RETRY_DELAY_SECONDS}s trước vòng kế tiếp...")
            await asyncio.sleep(SCRAPE_RETRY_DELAY_SECONDS)

    ok_accounts = list(bundles.keys())
    fail_accounts = list(pending.keys())
    if fail_accounts:
        print(f"[stats-source] Sau {round_no} vòng vẫn chưa cào được: {fail_accounts}")

    meta_extra = {
        "start_time": start_time,
        "end_time": end_time,
        "end_date_exclusive": end_date_ex,
        "accounts_ok": ok_accounts,
        "accounts_fail": fail_accounts,
        "rounds_used": round_no,
        "lookback_days": SCRAPE_LOOKBACK_DAYS,
        "source": "live" if not fail_accounts else "live_partial",
    }

    if not bundles:
        print(f"[stats-source] Cào thất bại toàn bộ sau {round_no} vòng — không có ticket nào.")
        return None

    raw, events_raw, spare_raw, appt_raw, additional_raw, solutions_raw = _merge_bundles(bundles)
    del bundles
    gc.collect()
    return raw, events_raw, spare_raw, appt_raw, additional_raw, solutions_raw, meta_extra


# ------------------------------------------------------------------
# Nguồn 2: file Excel có sẵn trong 1 thư mục (KHÔNG gọi CCTS)
# ------------------------------------------------------------------
def _read_bundle_from_xlsx(path: str, account_label: str) -> dict[str, pd.DataFrame]:
    out = {}
    for sheet in REQUIRED_SHEETS:
        try:
            df = pd.read_excel(path, sheet_name=sheet)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        part = _trim_sheet_columns(sheet, df)
        if part is df:
            part = df.copy()
        part["_source_account"] = account_label
        out[sheet] = part
    return out


def load_local_bundle(folder: str | None = None):
    """Đọc TẤT CẢ file .xlsx trong `folder` (mặc định STATS_LOCAL_DATA_DIR)
    làm nguồn dữ liệu thay cho việc cào CCTS. Mỗi file được coi như 1
    "tài khoản" (nhãn = tên file), gộp + khử trùng Ticket ID giống hệt khi
    cào live nhiều tài khoản.

    Trả None nếu thư mục không tồn tại, không có file .xlsx nào, hoặc
    không đọc được ticket nào từ bất kỳ file nào."""
    folder = folder or STATS_LOCAL_DATA_DIR
    if not os.path.isdir(folder):
        print(f"[stats-source] Thư mục local '{folder}' không tồn tại.")
        return None

    paths = sorted(glob.glob(os.path.join(folder, "*.xlsx")))
    if not paths:
        print(f"[stats-source] Không tìm thấy file .xlsx nào trong '{folder}'.")
        return None

    bundles: dict[str, dict] = {}
    for path in paths:
        label = os.path.splitext(os.path.basename(path))[0]
        print(f"[stats-source] Đang đọc file local: {path} (nhãn={label})")
        bundle = _read_bundle_from_xlsx(path, label)
        if bundle and "Ticket Information" in bundle:
            bundles[label] = bundle
            print(f"[stats-source]   → OK {label}: TI={len(bundle['Ticket Information'])}")
        else:
            print(f"[stats-source]   → bỏ qua {path}: thiếu sheet 'Ticket Information'")

    if not bundles:
        print(f"[stats-source] Không đọc được ticket nào từ {len(paths)} file trong '{folder}'.")
        return None

    _, end_time, end_date_ex = scrape_time_range()
    meta_extra = {
        "start_time": None,
        "end_time": end_time,
        "end_date_exclusive": end_date_ex,
        "accounts_ok": list(bundles.keys()),
        "accounts_fail": [],
        "rounds_used": 0,
        "lookback_days": None,  # dữ liệu local: không rõ khoảng ngày đã cào
        "source": "local",
        "local_files": paths,
    }
    raw, events_raw, spare_raw, appt_raw, additional_raw, solutions_raw = _merge_bundles(bundles)
    return raw, events_raw, spare_raw, appt_raw, additional_raw, solutions_raw, meta_extra


# ------------------------------------------------------------------
# Điểm gọi DUY NHẤT mà stats_data.py cần biết tới.
# ------------------------------------------------------------------
async def get_raw_bundle(mode: str | None = None, local_folder: str | None = None):
    """mode: None → dùng STATS_DATA_SOURCE (env, mặc định "ccts").
    "ccts" → luôn cào live. "local" → luôn đọc thư mục Excel
    (local_folder hoặc STATS_LOCAL_DATA_DIR).

    Trả (raw, events_raw, spare_raw, appt_raw, additional_raw,
    solutions_raw, meta_extra) hoặc None nếu nguồn được chọn không có
    dữ liệu."""
    chosen = (mode or STATS_DATA_SOURCE).strip().lower()
    if chosen == "local":
        return load_local_bundle(local_folder)
    return await fetch_live_bundle()
