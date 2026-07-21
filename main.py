"""
CCTS Live Map - ứng dụng web độc lập (FastAPI + WebSocket), thay thế bản
Streamlit trước đây để có trải nghiệm mượt hơn (vị trí nhân sự cập nhật
real-time không cần load lại trang).

Chạy thử local (Windows - xem ghi chú NotImplementedError bên dưới):
    uvicorn main:app --host 0.0.0.0 --port 8000

Xem README.md đi kèm để biết cách cấu hình biến môi trường & deploy.
"""

import sys
import asyncio

# ==========================================
# QUAN TRỌNG - CHỈ ẢNH HƯỞNG TRÊN WINDOWS:
# Khi chạy `uvicorn --reload` trên Windows, cơ chế theo dõi file thay đổi
# (WatchFiles) khiến asyncio chuyển sang dùng SelectorEventLoop thay vì
# ProactorEventLoop mặc định. SelectorEventLoop KHÔNG hỗ trợ tạo subprocess
# trên Windows, mà Playwright Async API bắt buộc phải tạo subprocess để mở
# trình duyệt -> gây lỗi "NotImplementedError" khi gọi client.login().
# Dòng dưới đây ép asyncio dùng ProactorEventLoopPolicy (hỗ trợ subprocess)
# ngay từ đầu, TRƯỚC khi uvicorn/bất kỳ code nào khác kịp chạy.
# Trên Linux/macOS (vd khi deploy lên Render) dòng này không có tác dụng gì
# (an toàn, không cần gỡ ra khi deploy) vì vấn đề này chỉ tồn tại trên Windows.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
# ==========================================

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import users_store
from ccts_data import build_station_markers, get_static_data, filter_stations_for_user
from location_hub import hub
from config import SESSION_COOKIE_NAME, TICKET_REFRESH_SECONDS, TRACCAR_TOKEN

app = FastAPI(title="CCTS Live Map")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Session đơn giản: token ngẫu nhiên -> thông tin user, lưu trong bộ nhớ server.
# (Bị reset khi server khởi động lại - người dùng chỉ cần đăng nhập lại, chấp
# nhận được với quy mô ứng dụng nội bộ nhỏ như thế này.)
SESSIONS = {}

_latest_station_payload = {
    "stations": [], "total_tickets": 0, "missing_count": 0, "updated_at": None,
}


def get_current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return SESSIONS.get(token)


async def refresh_stations_once():
    """build_station_markers() giờ là 1 coroutine thực sự (CCTSClient dùng
    Playwright ASYNC API), nên chỉ cần await trực tiếp - không còn cần
    asyncio.to_thread() nữa."""
    global _latest_station_payload
    _latest_station_payload = await build_station_markers()


async def broadcast_stations_update():
    """Đẩy danh sách trạm mới nhất tới TỪNG kết nối riêng biệt (không dùng
    broadcast_all chung) vì mỗi người có thể thấy 1 tập trạm khác nhau -
    Kỹ thuật viên chỉ thấy trạm trong khu vực của họ (filter_stations_for_user)."""
    async with hub.lock:
        targets = list(hub.connections.items())

    dead = []
    for ws, viewer in targets:
        filtered = filter_stations_for_user(_latest_station_payload["stations"], viewer)
        payload = {**_latest_station_payload, "stations": filtered, "type": "stations_update"}
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)

    if dead:
        async with hub.lock:
            for ws in dead:
                hub.connections.pop(ws, None)


async def refresh_stations_loop():
    """Cứ mỗi TICKET_REFRESH_SECONDS (mặc định 10 phút), cào lại ticket và đẩy
    (broadcast) danh sách trạm mới nhất tới mọi client đang mở web - lớp bản
    đồ TRẠM sẽ được vẽ lại, còn marker VỊ TRÍ NHÂN SỰ không bị ảnh hưởng."""
    while True:
        await asyncio.sleep(TICKET_REFRESH_SECONDS)
        try:
            await refresh_stations_once()
            await broadcast_stations_update()
        except Exception as e:
            print(f"Lỗi làm mới dữ liệu ticket: {e}")


@app.on_event("startup")
async def on_startup():
    get_static_data()  # nạp toạ độ trạm 1 lần khi khởi động
    await refresh_stations_once()
    asyncio.create_task(refresh_stations_loop())


# ==========================================
# Đăng nhập / Đăng xuất
# ==========================================
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if get_current_user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = users_store.verify_login(username, password)
    if not user:
        return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": "Sai tên đăng nhập hoặc mật khẩu."})

    import uuid
    token = uuid.uuid4().hex
    SESSIONS[token] = user

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE_NAME, token, httponly=True, samesite="lax", max_age=60 * 60 * 12
    )
    return response


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        SESSIONS.pop(token, None)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ==========================================
# Trang bản đồ chính
# ==========================================
@app.post("/api/admin/refresh-stations")
async def api_refresh_stations(request: Request):
    user = get_current_user(request)
    # Kiểm tra nếu chưa đăng nhập hoặc không phải Admin
    if not user or (user.get("role") or "").strip().lower() != "admin":
        return JSONResponse({"error": "Bạn không có quyền thực hiện thao tác này."}, status_code=403)
    
    # Gọi hàm làm mới dữ liệu từ CCTS và broadcast cho tất cả client (đã lọc theo quyền xem)
    await refresh_stations_once()
    await broadcast_stations_update()
    return {"status": "ok", "message": "Đã cập nhật dữ liệu trạm mới nhất!"}

@app.get("/", response_class=HTMLResponse)
async def map_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    
    return templates.TemplateResponse(
        request=request, 
        name="map.html", 
        context={"request": request, "user": user}
    )


@app.get("/api/stations")
async def api_stations(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    filtered = filter_stations_for_user(_latest_station_payload["stations"], user)
    return {**_latest_station_payload, "stations": filtered}


@app.get("/api/technicians")
async def api_technicians(request: Request):
    """Danh sách kỹ thuật viên gom theo khu vực (để hiển thị tag lọc trên bản
    đồ), kèm trạng thái online/offline (đối chiếu với danh sách đang kết nối
    WebSocket). Nếu 1 tên trong list_Stations.json không khớp được với tài
    khoản nào trong Sheet Users, online sẽ trả về null (không xác định)."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    _, _, _, _, tech_by_region = get_static_data()
    all_users = users_store.list_users_public()

    name_to_username = {}
    for u in all_users:
        if (u.get("role") or "").strip().lower() == "kỹ thuật":
            key = (u.get("full_name") or "").strip().lower()
            if key:
                name_to_username[key] = u["username"]

    online_set = set(hub.online_usernames())

    regions_out = {}
    for region, names in tech_by_region.items():
        items = []
        for name in names:
            username = name_to_username.get(name.strip().lower())
            online = (username.strip().lower() in online_set) if username else None
            items.append({"tech_name": name, "username": username, "online": online})
        regions_out[region] = items

    return {"regions": regions_out, "unassigned": {"tech_name": "Unassigned", "username": None, "online": None}}


@app.get("/api/traccar")
async def api_traccar(id: str, lat: float, lon: float, accuracy: float = None, token: str = None):
    """Endpoint tương thích với giao thức HTTP đơn giản của app Traccar Client
    (Android/iOS, mã nguồn mở, miễn phí) - dùng để lấy vị trí liên tục kể cả
    khi màn hình điện thoại tắt (trình duyệt không làm được việc này do giới
    hạn nền tảng của iOS/Android).

    Cấu hình trong app Traccar Client:
    - Server URL: https://<domain-của-bạn>/api/traccar?token=<TRACCAR_TOKEN>
    - Device Identifier: đúng bằng username của kỹ thuật viên đó trong Sheet Users

    TRACCAR_TOKEN là 1 chuỗi bí mật tự đặt trong biến môi trường (xem README),
    dùng để tránh người lạ gửi vị trí giả vào hệ thống (giao thức Traccar gốc
    không hỗ trợ xác thực phức tạp hơn query param)."""
    if TRACCAR_TOKEN and token != TRACCAR_TOKEN:
        return JSONResponse({"error": "invalid token"}, status_code=403)

    user_info = users_store.get_user_info(id)
    if not user_info:
        return JSONResponse({"error": f"Không tìm thấy tài khoản '{id}'"}, status_code=404)

    await hub.update_location(
        id, user_info, lat, lon, accuracy,
        stations=_latest_station_payload["stations"],
    )
    return {"status": "ok"}


# ==========================================
# WebSocket: vị trí nhân sự real-time
# ==========================================
@app.websocket("/ws/location")
async def ws_location(websocket: WebSocket):
    token = websocket.cookies.get(SESSION_COOKIE_NAME)
    user = SESSIONS.get(token) if token else None
    if not user:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    await hub.register(websocket, user)

    try:
        await websocket.send_json(hub.snapshot_for(user))
        filtered = filter_stations_for_user(_latest_station_payload["stations"], user)
        await websocket.send_json({**_latest_station_payload, "stations": filtered, "type": "stations_update"})
        await websocket.send_json({"type": "presence_update", "online_usernames": hub.online_usernames()})

        while True:
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                continue

            if data.get("type") == "location":
                lat, lng = data.get("lat"), data.get("lng")
                accuracy = data.get("accuracy")
                if lat is not None and lng is not None:
                    # Lấy role/region MỚI NHẤT (có cache) - phòng trường hợp
                    # Admin vừa đổi chức vụ của người này trên Sheets.
                    fresh_info = users_store.get_user_info(user["username"]) or user
                    await hub.update_location(
                        user["username"], fresh_info, lat, lng, accuracy,
                        stations=_latest_station_payload["stations"],
                    )
    finally:
        await hub.unregister(websocket)


if __name__ == "__main__":
    import uvicorn
    # Trên Windows, cơ chế --reload (WatchFiles) có thể ép lại SelectorEventLoop
    # dù đã set policy ở trên, nên tắt hẳn reload khi chạy trên Windows để đảm
    # bảo ổn định. Trên Linux/macOS (kể cả khi deploy) vẫn giữ reload khi cần.
    use_reload = sys.platform != "win32"
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=use_reload)