"""
Quản lý vị trí trực tiếp (in-memory) & phát (broadcast) qua WebSocket.

QUAN TRỌNG: vị trí KHÔNG được lưu xuống Google Sheets nữa - đây là dữ liệu tức
thời, chỉ tồn tại trong bộ nhớ khi server đang chạy. Nếu server khởi động lại,
vị trí sẽ được cập nhật lại ngay khi có ping tiếp theo từ app di động CCTS.
Lựa chọn này giúp hệ thống đơn giản và tránh gọi Google Sheets API dồn dập
khi có nhiều người cùng di chuyển liên tục.

Nguồn vị trí DUY NHẤT: app di động CCTS (Flutter, chính người dùng đăng
nhập) -> POST /api/location (main.py) -> hub.update_location() ở dưới. Đã bỏ
hẳn app Traccar Client / endpoint /api/traccar cũ - kỹ thuật viên không cần
cài thêm app nào khác ngoài CCTS.
"""

import asyncio
import time
from math import radians, sin, cos, sqrt, atan2

import users_store

STATION_PROXIMITY_METERS = 10   # Ngưỡng coi như "đang đứng tại trạm" (theo yêu cầu: ~10m)
ONLINE_THRESHOLD_SECONDS = 120  # Không có cập nhật vị trí quá 2 phút -> coi như offline


def haversine_meters(lat1, lng1, lat2, lng2):
    R = 6371000.0
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def find_nearby_station_among(lat, lng, stations, threshold=STATION_PROXIMITY_METERS):
    """Tìm trạm gần nhất trong DANH SÁCH TRẠM ĐANG CÓ SỰ CỐ (không phải toàn
    bộ coords_map tĩnh) - đúng theo yêu cầu: chỉ tính khoảng cách tới các trạm
    đang gim trên bản đồ. `stations` là list dict có ít nhất 'lat','lng',
    'station_code'. Trả về (station_code, khoảng cách mét) nếu trong ngưỡng,
    ngược lại (None, None)."""
    nearest_code, nearest_dist = None, None
    for s in stations or []:
        d = haversine_meters(lat, lng, s["lat"], s["lng"])
        if nearest_dist is None or d < nearest_dist:
            nearest_dist, nearest_code = d, s.get("station_code")
    if nearest_dist is not None and nearest_dist <= threshold:
        return nearest_code, round(nearest_dist, 1)
    return None, None


class LocationHub:
    def __init__(self):
        self.locations = {}        # username -> {lat, lng, accuracy, full_name, role, region, updated_at, nearby_station, nearby_since}
        self.connections = {}      # websocket -> user dict (dùng để lọc quyền xem)
        self.repair_sessions = {}  # username -> {"station_code": ..., "started_at": epoch}
        self.lock = asyncio.Lock()

    async def register(self, websocket, user):
        async with self.lock:
            self.connections[websocket] = user
        await self.broadcast_all({"type": "presence_update", "online_usernames": self.online_usernames()})

    async def unregister(self, websocket):
        async with self.lock:
            self.connections.pop(websocket, None)
        await self.broadcast_all({"type": "presence_update", "online_usernames": self.online_usernames()})
        # Lưu ý: KHÔNG xoá vị trí khỏi self.locations khi 1 người ngắt kết nối
        # tạm thời (mất mạng, tắt màn hình...), để tránh marker biến mất/hiện
        # lại liên tục gây rối mắt cho người đang theo dõi bản đồ.

    def online_usernames(self):
        """'Online' giờ được xác định theo THỜI ĐIỂM CẬP NHẬT VỊ TRÍ GẦN NHẤT
        (trong vòng ONLINE_THRESHOLD_SECONDS), KHÔNG chỉ dựa vào việc có đang
        mở kết nối WebSocket hay không. Lý do: vị trí có thể đến từ 1 app di
        động riêng (vd Traccar Client) không hề mở WebSocket tới server này,
        nhưng vẫn nên được coi là 'đang online' nếu vẫn gửi vị trí đều đặn."""
        now = time.time()
        recent = {
            uname for uname, loc in self.locations.items()
            if now - loc.get("updated_at", 0) <= ONLINE_THRESHOLD_SECONDS
        }
        connected = {v["username"].strip().lower() for v in self.connections.values()}
        return list(recent | connected)

    def visible_locations_for(self, viewer):
        """Trả về list vị trí mà viewer được phép xem, theo đúng quy tắc phân
        cấp: Admin xem tất cả; vai trò khác chỉ xem cấp thấp hơn mình + chính mình."""
        result = []
        for username, loc in self.locations.items():
            is_self = username.lower() == viewer["username"].lower()
            if is_self or users_store.can_view(viewer["role"], loc["role"]):
                result.append({"username": username, **loc})
        return result

    async def update_location(self, username, user_info, lat, lng, accuracy, stations=None, tech_stats=None):
        """`stations` PHẢI là danh sách trạm đang có sự cố hiện tại (từ
        _latest_station_payload["stations"] trong main.py) - đây chính là
        điểm sửa quan trọng: trước đây tính khoảng cách tới TOÀN BỘ trạm tĩnh
        (coords_map), giờ chỉ tính tới các trạm đang thực sự hiển thị lỗi
        trên bản đồ, đúng yêu cầu.

        `tech_stats` (tuỳ chọn): dict {closed_yesterday, closed_today,
        open_count} của CHÍNH người này (main.py tra theo full_name) - để
        hiển thị ngay trong popup vị trí của họ trên bản đồ."""
        nearby_code, _ = find_nearby_station_among(lat, lng, stations)

        now = time.time()
        session = self.repair_sessions.get(username)

        if nearby_code:
            if session and session["station_code"] == nearby_code:
                started_at = session["started_at"]  # vẫn ở trạm cũ -> giữ nguyên mốc thời gian bắt đầu
            else:
                started_at = now  # vừa đến 1 trạm mới (hoặc trạm khác) -> tính lại từ đầu
                self.repair_sessions[username] = {"station_code": nearby_code, "started_at": started_at}
        else:
            self.repair_sessions.pop(username, None)
            started_at = None

        loc = {
            "lat": lat,
            "lng": lng,
            "accuracy": accuracy,
            "full_name": user_info["full_name"],
            "role": user_info["role"],
            "region": user_info["region"],
            "updated_at": now,
            "nearby_station": nearby_code,
            "nearby_since": started_at,  # epoch giây - frontend tự tính + hiển thị thời lượng đang sửa trạm
            "closed_yesterday": (tech_stats or {}).get("closed_yesterday", 0),
            "closed_today": (tech_stats or {}).get("closed_today", 0),
            "open_count": (tech_stats or {}).get("open_count", 0),
        }
        async with self.lock:
            self.locations[username] = loc

        message = {"type": "location_update", "username": username, **loc}
        await self._broadcast_to_permitted(username, loc["role"], message)
        await self.broadcast_all({"type": "presence_update", "online_usernames": self.online_usernames()})

    async def _broadcast_to_permitted(self, username, role, message):
        async with self.lock:
            targets = list(self.connections.items())

        dead = []
        for ws, viewer in targets:
            is_self = viewer["username"].lower() == username.lower()
            if is_self or users_store.can_view(viewer["role"], role):
                try:
                    await ws.send_json(message)
                except Exception:
                    dead.append(ws)

        if dead:
            async with self.lock:
                for ws in dead:
                    self.connections.pop(ws, None)

    async def broadcast_all(self, message):
        """Gửi 1 thông điệp (vd cập nhật danh sách trạm, presence) tới TẤT CẢ
        kết nối đang mở, không lọc quyền."""
        async with self.lock:
            targets = list(self.connections.keys())

        dead = []
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)

        if dead:
            async with self.lock:
                for ws in dead:
                    self.connections.pop(ws, None)

    def snapshot_for(self, viewer):
        return {"type": "snapshot", "locations": self.visible_locations_for(viewer)}


hub = LocationHub()