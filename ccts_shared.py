"""
ccts_shared.py — Tiện ích DÙNG CHUNG giữa ccts_data.py (bản đồ realtime, cào
mỗi 15 phút) và stats_data.py (thống kê, cào lúc 0h / khi khởi động).

Gom vào đây để tránh code trùng lặp và đảm bảo 2 module không bao giờ xung
đột khi cùng lúc gọi API CCTS:

1. `CCTS_API_LOCK`  — DUY NHẤT 1 khoá cho toàn chương trình. Bất kỳ request
   nào gọi sang CCTS (login, find ticket, export Excel...) đều phải giữ khoá
   này trong lúc gọi mạng. Nhờ vậy nếu 15' (ccts_data) và 0h (stats_data)
   trùng nhau, module đến sau sẽ tự động ĐỢI thay vì cào chồng lên nhau
   (tránh bị server CCTS đá session / rate-limit).
   - Nếu project đã có sẵn `ccts_gate.py` định nghĩa khoá này thì dùng lại
     (để không phá vỡ code cũ khác đang import từ đó); nếu chưa có thì tạo
     mới ở đây.

2. `STATS_REFRESH_LOCK` — khoá RIÊNG, chỉ áp dụng nội bộ cho stats_data.py,
   dùng để gộp (single-flight) nhiều lượt "cào thống kê" gọi gần như cùng
   lúc (khởi động server + lịch 0h + admin bấm nút làm mới tay) thành 1 lượt
   duy nhất, tránh cào 2-3 lần liên tiếp gây lãng phí & dễ bị khoá tài khoản.

3. `STATS_SCRAPE_ACCOUNTS` — 2 tài khoản CỐ ĐỊNH dùng riêng cho việc cào
   thống kê (không lấy từ config.CCTS_ACCOUNTS, vì danh sách đó dùng cho bản
   đồ realtime và có thể thay đổi). Vì việc cào thống kê chủ yếu chạy vào
   0h — thời điểm không ai dùng các tài khoản tổng — nên cố định luôn 2 tài
   khoản dưới đây cho an toàn & dễ kiểm soát.

4. `ClientPool` — pool phiên đăng nhập tái sử dụng + cơ chế "gọi API, nếu bị
   đá session thì tự huỷ cache & login lại đúng 1 lần rồi thử lại". Logic
   này trước đây bị viết lặp lại ở cả ccts_data.py lẫn stats_data.py; giờ
   dùng chung 1 chỗ. Mỗi module vẫn nên giữ 1 instance ClientPool RIÊNG của
   mình (vòng đời & tần suất cào khác nhau), chỉ dùng chung LỚP (class) và
   chung CCTS_API_LOCK.
"""
from __future__ import annotations

import asyncio
from zoneinfo import ZoneInfo

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# ------------------------------------------------------------------
# Khu vực KHÔNG còn được quản lý.
# ------------------------------------------------------------------
# Nhãn chuẩn cho "khu vực không quản lý" — dùng xuyên suốt chương trình.
UNMANAGED_REGION_LABEL = "KV không quản lý"

# Các khu vực công ty đã RÚT KHỎI / NGỪNG quản lý. Khai báo ở đây để chỉ
# cần sửa 1 chỗ duy nhất khi có thay đổi phạm vi hoạt động; toàn bộ chương
# trình (bản đồ, danh sách ticket, thống kê) sẽ tự coi các khu vực này là
# "KV không quản lý" — kể cả khi dữ liệu gốc trên GitHub (StationData.csv)
# chưa kịp cập nhật.
#   - HCM: công ty đã rút khỏi khu vực TP.HCM (từ 08/2026).
DEPRECATED_REGIONS = {"HCM"}

_UNMANAGED_ALIASES = {
    "kv không quản lý", "kv khong quan ly", "không quản lý", "khong quan ly", "unmanaged",
}


def is_unmanaged_region(region) -> bool:
    """True nếu khu vực này KHÔNG (còn) được quản lý: rỗng, đúng nhãn
    "KV không quản lý", một biến thể gõ khác của nhãn đó, hoặc thuộc
    DEPRECATED_REGIONS (vd HCM)."""
    if not region:
        return True
    r = str(region).strip()
    if r in DEPRECATED_REGIONS:
        return True
    if r.lower() in _UNMANAGED_ALIASES:
        return True
    return False


def load_static_data_filtered():
    """Bọc github_data_store.load_static_data(): tự động chuẩn hoá các khu
    vực đã NGỪNG quản lý (DEPRECATED_REGIONS, vd HCM) thành
    UNMANAGED_REGION_LABEL trong region_map, và loại các khu vực đó khỏi
    tech_by_region. Cả ccts_data.py (bản đồ realtime) lẫn stats_data.py
    (thống kê) đều nên lấy static data qua hàm này thay vì gọi thẳng
    github_data_store, để chỉ cần đổi DEPRECATED_REGIONS ở 1 chỗ là áp dụng
    toàn chương trình.

    Trả (coords_map, tech_map, region_map, cp_model_map, tech_by_region) —
    giống hệt chữ ký của github_data_store.load_static_data()."""
    import github_data_store

    coords_map, tech_map, region_map, cp_model_map, tech_by_region = github_data_store.load_static_data()

    region_map = {
        code: (UNMANAGED_REGION_LABEL if is_unmanaged_region(region) else region)
        for code, region in (region_map or {}).items()
    }
    tech_by_region = {
        region: names
        for region, names in (tech_by_region or {}).items()
        if not is_unmanaged_region(region)
    }
    return coords_map, tech_map, region_map, cp_model_map, tech_by_region

# ------------------------------------------------------------------
# Khoá dùng chung cho MỌI lần gọi API CCTS (login, find ticket, export...).
# ------------------------------------------------------------------
try:
    from ccts_gate import CCTS_API_LOCK  # type: ignore  # dùng lại nếu đã có sẵn
except Exception:
    CCTS_API_LOCK = asyncio.Lock()

# Khoá riêng cho việc cào thống kê (gộp các lượt gọi gần nhau thành 1).
STATS_REFRESH_LOCK = asyncio.Lock()

# ------------------------------------------------------------------
# 2 tài khoản CỐ ĐỊNH dùng để cào thống kê (0h / khi khởi động).
# ------------------------------------------------------------------
STATS_SCRAPE_ACCOUNTS = [
    {"username": "esmanager", "password": "Ccts123."},
    {"username": "itsmanagermt", "password": "Duynam1234@"},
]


class ClientPool:
    """Pool các CCTSClient đã login, khoá theo username, tự relogin khi cần."""

    def __init__(self):
        self._pool: dict[str, object] = {}

    def get(self, username):
        return self._pool.get(username)

    def set(self, username, client):
        self._pool[username] = client

    def invalidate(self, username):
        if username in self._pool:
            self._pool.pop(username, None)
            print(f"[~] Đã huỷ session cache của [{username}]")

    async def get_or_login(self, username, password):
        """Lấy client từ pool; nếu chưa có thì login mới và cache lại.
        Trả về (client, is_fresh_login)."""
        from api_client import CCTSClient

        client = self.get(username)
        if client is not None:
            return client, False
        client = CCTSClient(username=username, password=password)
        await client.login()
        self.set(username, client)
        print(f"[+] Login mới & cache session cho [{username}]")
        return client, True

    async def call_with_retry(self, username, password, action):
        """Gọi `await action(client)`; nếu lỗi (có thể do bị đá session) thì
        huỷ cache + login lại 1 lần rồi thử lại đúng 1 lần nữa.
        Trả về (result, success_bool). Không raise ra ngoài."""
        try:
            client, _ = await self.get_or_login(username, password)
        except Exception as e:
            print(f"[-] Đăng nhập thất bại cho [{username}]: {e}")
            self.invalidate(username)
            return None, False

        try:
            result = await action(client)
            return result, True
        except Exception as e:
            print(f"[-] Lỗi khi gọi API cho [{username}] (có thể bị đá session): {e}")
            self.invalidate(username)
            try:
                client, _ = await self.get_or_login(username, password)
                result = await action(client)
                print(f"[+] Gọi lại thành công sau khi login mới cho [{username}]")
                return result, True
            except Exception as e2:
                print(f"[-] Gọi lại vẫn thất bại cho [{username}]: {e2}")
                self.invalidate(username)
                return None, False