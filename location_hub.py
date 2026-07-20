"""
Quản lý vị trí trực tiếp (in-memory) & phát (broadcast) qua WebSocket.

QUAN TRỌNG: vị trí KHÔNG được lưu xuống Google Sheets nữa - đây là dữ liệu tức
thời, chỉ tồn tại trong bộ nhớ khi server đang chạy. Nếu server khởi động lại,
vị trí sẽ được cập nhật lại ngay khi trình duyệt của người dùng gửi ping tiếp
theo (thường trong vài giây). Lựa chọn này giúp hệ thống đơn giản và tránh gọi
Google Sheets API dồn dập khi có nhiều người cùng di chuyển liên tục.
"""

import asyncio
import time
from math import radians, sin, cos, sqrt, atan2

import users_store
from ccts_data import get_static_data

STATION_PROXIMITY_METERS = 150  # Ngưỡng coi như "đang ở tại trạm"


def haversine_meters(lat1, lng1, lat2, lng2):
    R = 6371000.0
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def find_nearby_station(lat, lng, threshold=STATION_PROXIMITY_METERS):
    """Tìm trạm gần nhất với 1 toạ độ, trả về (mã trạm, khoảng cách mét) nếu
    nằm trong ngưỡng, ngược lại (None, None)."""
    coords_map, _, _, _ = get_static_data()
    nearest_code, nearest_dist = None, None
    for code, c in coords_map.items():
        d = haversine_meters(lat, lng, c["lat"], c["lng"])
        if nearest_dist is None or d < nearest_dist:
            nearest_dist, nearest_code = d, code
    if nearest_dist is not None and nearest_dist <= threshold:
        return nearest_code, round(nearest_dist, 1)
    return None, None


class LocationHub:
    def __init__(self):
        self.locations = {}    # username -> {lat, lng, accuracy, full_name, role, region, updated_at, nearby_station, nearby_distance}
        self.connections = {}  # websocket -> user dict (dùng để lọc quyền xem)
        self.lock = asyncio.Lock()

    async def register(self, websocket, user):
        async with self.lock:
            self.connections[websocket] = user

    async def unregister(self, websocket):
        async with self.lock:
            self.connections.pop(websocket, None)
        # Lưu ý: KHÔNG xoá vị trí khỏi self.locations khi 1 người ngắt kết nối
        # tạm thời (mất mạng, tắt màn hình...), để tránh marker biến mất/hiện
        # lại liên tục gây rối mắt cho người đang theo dõi bản đồ.

    def visible_locations_for(self, viewer):
        """Trả về list vị trí mà viewer được phép xem, theo đúng quy tắc phân
        cấp: Admin xem tất cả; vai trò khác chỉ xem cấp thấp hơn mình + chính mình."""
        result = []
        for username, loc in self.locations.items():
            is_self = username.lower() == viewer["username"].lower()
            if is_self or users_store.can_view(viewer["role"], loc["role"]):
                result.append({"username": username, **loc})
        return result

    async def update_location(self, username, user_info, lat, lng, accuracy):
        nearby_code, nearby_dist = find_nearby_station(lat, lng)
        loc = {
            "lat": lat,
            "lng": lng,
            "accuracy": accuracy,
            "full_name": user_info["full_name"],
            "role": user_info["role"],
            "region": user_info["region"],
            "updated_at": time.time(),
            "nearby_station": nearby_code,
            "nearby_distance": nearby_dist,
        }
        async with self.lock:
            self.locations[username] = loc

        message = {"type": "location_update", "username": username, **loc}
        await self._broadcast_to_permitted(username, loc["role"], message)

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
        """Gửi 1 thông điệp (vd cập nhật danh sách trạm) tới TẤT CẢ kết nối
        đang mở, không lọc quyền (mọi người đều thấy chung 1 danh sách trạm)."""
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