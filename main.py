"""
CCTS Live Map - ứng dụng web độc lập (FastAPI + WebSocket).

Chạy thử local:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

Xem README.md đi kèm để biết cách cấu hình biến môi trường & deploy.
"""

import asyncio
import uuid

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI
import users_store
import ccts_data
import static_data_store
from ccts_data import get_static_data, filter_stations_for_user, filter_tech_by_region_for_user
from location_hub import hub
from config import SESSION_COOKIE_NAME, TICKET_REFRESH_SECONDS, TRACCAR_TOKEN

app = FastAPI(title="CCTS Live Map")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

SESSIONS = {}

_latest_station_payload = {
    "stations": [], "total_tickets": 0, "missing_count": 0, "updated_at": None, "fetch_success": True,
}
_latest_tech_stats = {}
_latest_ticket_rows = []


def get_current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return SESSIONS.get(token)


def require_admin(user):
    return bool(user) and (user.get("role") or "").strip().lower() == "admin"


async def refresh_stations_once():
    """Cào lại toàn bộ dữ liệu (trạm + thống kê hiệu suất + ticket chi tiết).
    Nếu TẤT CẢ tài khoản CCTS đều thất bại (fetch_success=False), GIỮ NGUYÊN
    dữ liệu cũ trong bộ nhớ - không ghi đè bằng dữ liệu rỗng."""
    global _latest_station_payload, _latest_tech_stats, _latest_ticket_rows

    new_payload, new_stats, new_rows = await ccts_data.refresh_all_ccts_data()

    if new_payload.get("fetch_success"):
        _latest_station_payload = new_payload
        _latest_tech_stats = new_stats
        _latest_ticket_rows = new_rows
        ccts_data.save_cache_to_file(new_payload, new_stats, new_rows)
    else:
        print("⚠️ Tất cả tài khoản CCTS đều thất bại lần cào này - "
              "GIỮ NGUYÊN dữ liệu lần cào gần nhất thành công, không xoá bản đồ.")


async def broadcast_stations_update():
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
    while True:
        await asyncio.sleep(TICKET_REFRESH_SECONDS)
        try:
            await refresh_stations_once()
            await broadcast_stations_update()
        except Exception as e:
            print(f"⚠️ Lỗi làm mới dữ liệu ticket (giữ nguyên dữ liệu cũ): {e!r}")


@app.on_event("startup")
async def on_startup():
    global _latest_station_payload, _latest_tech_stats, _latest_ticket_rows

    get_static_data()

    cached_payload, cached_stats, cached_rows = ccts_data.load_cache_from_file()
    if cached_payload:
        _latest_station_payload = cached_payload
        _latest_tech_stats = cached_stats
        _latest_ticket_rows = cached_rows
        print("✅ Đã nạp dữ liệu từ lần cào gần nhất (file cache).")

    try:
        await refresh_stations_once()
    except Exception as e:
        print(f"⚠️ Lỗi lần cào đầu tiên khi khởi động ({e!r}) - tiếp tục chạy với dữ liệu cache (nếu có).")

    asyncio.create_task(refresh_stations_loop())


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if get_current_user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = users_store.verify_login(username, password)
    if not user:
        return templates.TemplateResponse(
            request=request, name="login.html", context={"error": "Sai tên đăng nhập hoặc mật khẩu."}
        )

    token = uuid.uuid4().hex
    SESSIONS[token] = user

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, samesite="lax", max_age=60 * 60 * 12)
    return response


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        SESSIONS.pop(token, None)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse)
async def map_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request=request, name="map.html", context={"user": user})


@app.post("/api/admin/refresh-stations")
async def api_refresh_stations(request: Request):
    user = get_current_user(request)
    if not require_admin(user):
        return JSONResponse({"error": "Bạn không có quyền thực hiện thao tác này."}, status_code=403)

    await refresh_stations_once()
    await broadcast_stations_update()
    return {"status": "ok", "message": "Đã cập nhật dữ liệu trạm mới nhất!"}


@app.get("/api/stations")
async def api_stations(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    filtered = filter_stations_for_user(_latest_station_payload["stations"], user)
    return {**_latest_station_payload, "stations": filtered}


@app.post("/api/assign-technician")
async def api_assign_technician(request: Request):
    """Đổi kỹ thuật viên phụ trách 1 trạm - Điều phối khu vực trở lên được
    dùng, với quyền chỉnh sửa TOÀN BỘ trạm (giống Admin), không giới hạn theo
    khu vực. Khi gán 1 kỹ thuật viên, khu vực (region) trên StationAssignments
    cũng được cập nhật theo đúng khu vực của kỹ thuật viên đó (lấy từ Sheet Users)."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    role = (user.get("role") or "").strip().lower()
    if role not in ("điều phối khu vực", "điều hành", "giám đốc", "admin"):
        return JSONResponse({"error": "Bạn không có quyền đổi kỹ thuật viên phụ trách."}, status_code=403)

    body = await request.json()
    station_code = (body.get("station_code") or "").strip()
    new_engineer_name = (body.get("engineer_name") or "").strip()

    if not station_code:
        return JSONResponse({"error": "Thiếu mã trạm."}, status_code=400)
    if not new_engineer_name:
        return JSONResponse({"error": "Thiếu tên kỹ thuật viên."}, status_code=400)

    all_users = users_store.list_users_public()
    new_region = None

    # Xác thực engineer_name: phải là "Unassigned" hoặc đúng 1 tài khoản có
    # role "Kỹ thuật" trong danh sách nhân sự thật (không cho gõ tên tuỳ ý)
    if new_engineer_name.strip().lower() != "unassigned":
        matched_tech = next(
            (u for u in all_users
             if (u.get("role") or "").strip().lower() == "kỹ thuật"
             and (u.get("full_name") or "").strip().lower() == new_engineer_name.strip().lower()),
            None,
        )
        if not matched_tech:
            return JSONResponse(
                {"error": f"'{new_engineer_name}' không có trong danh sách kỹ thuật viên của công ty."},
                status_code=400,
            )
        new_region = (matched_tech.get("region") or "").strip() or None

    try:
        static_data_store.update_station_assignment(station_code, new_engineer_name, region=new_region)
    except Exception as e:
        return JSONResponse({"error": f"Lỗi khi ghi vào Google Sheets: {e}"}, status_code=500)

    # Nạp lại dữ liệu tĩnh (đã đổi) + cào lại để gắn tên kỹ thuật viên mới lên
    # bản đồ ngay, rồi đẩy (broadcast) cho mọi người đang mở web
    await refresh_stations_once()
    await broadcast_stations_update()

    return {"status": "ok", "station_code": station_code, "engineer_name": new_engineer_name}


@app.get("/api/assignable-technicians")
async def api_assignable_technicians(request: Request):
    """Danh sách ĐẦY ĐỦ (không giới hạn khu vực) tên các kỹ thuật viên thật
    trong công ty - dùng riêng cho modal đổi kỹ thuật viên phụ trách (Điều
    phối khu vực trở lên). Khác với /api/technicians (dùng cho tag lọc + panel
    Danh sách Ticket), endpoint này KHÔNG bị giới hạn theo khu vực của viewer,
    vì giờ Điều phối khu vực có quyền sửa TOÀN BỘ trạm."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    role = (user.get("role") or "").strip().lower()
    if role not in ("điều phối khu vực", "điều hành", "giám đốc", "admin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    all_users = users_store.list_users_public()
    names = sorted({
        (u.get("full_name") or "").strip()
        for u in all_users
        if (u.get("role") or "").strip().lower() == "kỹ thuật" and (u.get("full_name") or "").strip()
    })
    return {"technicians": names}


@app.get("/api/technicians")
async def api_technicians(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    _, _, _, _, tech_by_region = get_static_data()
    tech_by_region = filter_tech_by_region_for_user(tech_by_region, user)
    all_users = users_store.list_users_public()

    name_to_username = {}
    for u in all_users:
        if (u.get("role") or "").strip().lower() == "kỹ thuật":
            key = (u.get("full_name") or "").strip().lower()
            if key:
                name_to_username[key] = u["username"]

    online_set = set(hub.online_usernames())

    def stat_for(tech_name):
        return _latest_tech_stats.get(tech_name, {"closed_yesterday": 0, "closed_today": 0, "open_count": 0})

    regions_out = {}
    for region, names in tech_by_region.items():
        items = []
        for name in names:
            username = name_to_username.get(name.strip().lower())
            online = (username.strip().lower() in online_set) if username else None
            items.append({"tech_name": name, "username": username, "online": online, **stat_for(name)})
        regions_out[region] = items

    return {
        "regions": regions_out,
        "unassigned": {"tech_name": "Unassigned", "username": None, "online": None, **stat_for("Unassigned")},
    }


@app.get("/api/tech-tickets/{tech_name}")
async def api_tech_tickets(tech_name: str, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    rows = [r for r in _latest_ticket_rows if (r.get("tech_name") or "Unassigned") == tech_name]

    role = (user.get("role") or "").strip().lower()
    if role == "kỹ thuật" and tech_name != "Unassigned":
        user_region = (user.get("region") or "").strip().lower()
        rows = [r for r in rows if (r.get("region") or "").strip().lower() == user_region]

    return {"tech_name": tech_name, "tickets": rows, "count": len(rows)}


def _tech_stats_for_user(user_info):
    """Tra thống kê hiệu suất (đóng hôm qua/hôm nay, đang tồn) của CHÍNH
    người này, theo họ tên đầy đủ - dùng để hiển thị ngay trong popup vị trí
    của họ trên bản đồ (không phải trong tag lọc kỹ thuật)."""
    full_name = (user_info.get("full_name") or "").strip()
    return _latest_tech_stats.get(full_name, {"closed_yesterday": 0, "closed_today": 0, "open_count": 0})


@app.get("/api/traccar")
async def api_traccar(id: str, lat: float, lon: float, accuracy: float = None, token: str = None):
    if TRACCAR_TOKEN and token != TRACCAR_TOKEN:
        return JSONResponse({"error": "invalid token"}, status_code=403)

    user_info = users_store.get_user_info(id)
    if not user_info:
        return JSONResponse({"error": f"Không tìm thấy tài khoản '{id}'"}, status_code=404)

    await hub.update_location(
        id, user_info, lat, lon, accuracy,
        stations=_latest_station_payload["stations"],
        tech_stats=_tech_stats_for_user(user_info),
    )
    return {"status": "ok"}


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not require_admin(user):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request=request, name="admin.html", context={"user": user})


@app.get("/api/admin/users")
async def api_admin_list_users(request: Request):
    user = get_current_user(request)
    if not require_admin(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return {"users": users_store.list_users_public(), "role_labels": users_store.ROLE_LABELS}


@app.post("/api/admin/users/{username}/role")
async def api_admin_update_role(username: str, request: Request):
    user = get_current_user(request)
    if not require_admin(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    body = await request.json()
    new_role = body.get("role", "")
    try:
        canonical_role = users_store.update_user_role(username, new_role)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    return {"status": "ok", "username": username, "role": canonical_role}


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
                    fresh_info = users_store.get_user_info(user["username"]) or user
                    await hub.update_location(
                        user["username"], fresh_info, lat, lng, accuracy,
                        stations=_latest_station_payload["stations"],
                        tech_stats=_tech_stats_for_user(fresh_info),
                    )
    finally:
        await hub.unregister(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)