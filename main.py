"""
CCTS Live Map - ứng dụng web độc lập (FastAPI + WebSocket), thay thế bản
Streamlit trước đây để có trải nghiệm mượt hơn (vị trí nhân sự cập nhật
real-time không cần load lại trang).

Chạy thử local:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

Xem README.md đi kèm để biết cách cấu hình biến môi trường & deploy.
"""

import asyncio

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import users_store
from ccts_data import build_station_markers, get_static_data
from location_hub import hub
from config import SESSION_COOKIE_NAME, TICKET_REFRESH_SECONDS
import sys
import asyncio

# Sửa lỗi NotImplementedError của Playwright trên Windows
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

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


async def refresh_stations_loop():
    """Cứ mỗi TICKET_REFRESH_SECONDS (mặc định 10 phút), cào lại ticket và đẩy
    (broadcast) danh sách trạm mới nhất tới mọi client đang mở web - lớp bản
    đồ TRẠM sẽ được vẽ lại, còn marker VỊ TRÍ NHÂN SỰ không bị ảnh hưởng."""
    while True:
        await asyncio.sleep(TICKET_REFRESH_SECONDS)
        try:
            await refresh_stations_once()
            await hub.broadcast_all({"type": "stations_update", **_latest_station_payload})
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
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = users_store.verify_login(username, password)
    if not user:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Sai tên đăng nhập hoặc mật khẩu."}
        )

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
@app.get("/", response_class=HTMLResponse)
async def map_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("map.html", {"request": request, "user": user})


@app.get("/api/stations")
async def api_stations(request: Request):
    if not get_current_user(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return _latest_station_payload


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
        await websocket.send_json({"type": "stations_update", **_latest_station_payload})

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
                    await hub.update_location(user["username"], fresh_info, lat, lng, accuracy)
    finally:
        await hub.unregister(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)