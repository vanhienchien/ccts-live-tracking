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

import users_store
import ccts_data
import stats_data
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
_refresh_paused = False  # True = tạm dừng chu kỳ cào tự động (chỉ admin bật/tắt)


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
        if _refresh_paused:
            print(f"⏸ Chu kỳ cào đang TẠM DỪNG — bỏ qua lần này (mỗi {TICKET_REFRESH_SECONDS}s kiểm tra lại).")
            continue
        try:
            await refresh_stations_once()
            await broadcast_stations_update()
        except Exception as e:
            print(f"⚠️ Lỗi làm mới dữ liệu ticket (giữ nguyên dữ liệu cũ): {e!r}")


async def _seconds_until_next_midnight_vn() -> float:
    """Số giây đến 00:00:05 giờ Việt Nam kế tiếp."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    vn = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime.now(vn)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return max(1.0, (tomorrow - now).total_seconds())


async def _run_stats_refresh(label: str):
    """Chạy 1 lượt cào thống kê nền, log thống nhất theo `label` (vd "0h",
    "khởi động"). stats_data.refresh_stats_cache() đã tự lo:
    - Cào 2 tài khoản cố định, thử lại tối đa 10 vòng nếu chưa được ngay.
    - Tự giữ nguyên cache cũ (KHÔNG ghi đè) nếu sau 10 vòng vẫn thất bại.
    - Tự gộp (single-flight) nếu có 1 lượt cào khác đang chạy cùng lúc.
    Vì vậy ở đây chỉ cần log, không cần tự xử lý giữ cache cũ nữa."""
    try:
        print(f"[stats] === Bắt đầu cào thống kê ({label}) ===")
        await stats_data.refresh_stats_cache()
        print(f"[stats] === Cào thống kê ({label}) hoàn tất ===")
    except Exception as e:
        print(f"[stats] Lỗi cào thống kê ({label}) (giữ cache cũ nếu có): {e!r}")


async def stats_midnight_loop():
    """Mỗi ngày lúc ~0h VN: cào lại thống kê 45 ngày và ghi cache."""
    while True:
        wait_s = await _seconds_until_next_midnight_vn()
        hours = wait_s / 3600
        print(f"[stats] Lịch cào 0h: còn ~{hours:.1f}h (đợi {int(wait_s)}s)...")
        await asyncio.sleep(wait_s)
        await _run_stats_refresh("định kỳ 0h")


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

    # Stats: nạp cache ngay (nếu có) để /stats không trống; cào mới chạy NỀN — không chặn startup
    stats_cached = stats_data.load_stats_cache()
    if stats_cached:
        print(f"✅ Đã nạp stats cache ({stats_cached.get('total_tickets', 0)} ticket, "
              f"cập nhật {stats_cached.get('generated_at', '?')}).")
    else:
        print("[stats] Chưa có cache — trang /stats tạm trống đến khi cào nền xong.")

    # Mỗi lần restart: cào lại 45 ngày → 0h hôm nay (2 tài khoản cố định).
    # Chạy NỀN (không await) — không chặn startup, không lag web. Việc tự
    # retry / giữ cache cũ khi thất bại đã nằm sẵn trong refresh_stats_cache().
    asyncio.create_task(_run_stats_refresh("khởi động"))

    try:
        await refresh_stations_once()
    except Exception as e:
        print(f"⚠️ Lỗi lần cào đầu tiên khi khởi động ({e!r}) - tiếp tục chạy với dữ liệu cache (nếu có).")

    asyncio.create_task(refresh_stations_loop())
    asyncio.create_task(stats_midnight_loop())


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


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    """Trang thống kê / trực quan dữ liệu ticket."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        request=request, name="stats.html", context={"user": user}
    )


@app.get("/api/stats/daily-volume")
async def api_stats_daily_volume(request: Request):
    """
    Trả payload Chart.js từ cache (đã cào lúc 0h).
    Không gọi API CCTS khi user mở trang thống kê.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    payload = stats_data.get_cached_daily_volume()
    if payload is None:
        return JSONResponse(
            {
                "error": "Chưa có dữ liệu thống kê",
                "detail": "Hệ thống sẽ tự cào lúc 0h mỗi ngày. Admin có thể bấm làm mới thủ công.",
                "labels": [],
                "regions": [],
                "datasets": {},
                "total_tickets": 0,
            },
            status_code=503,
        )
    return payload



@app.get("/api/stats/overdue-rate")
async def api_stats_overdue_rate(request: Request):
    """Tỷ lệ ticket đóng bị Overdue theo KV / KT (30 ngày Close Time)."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    cache = stats_data.load_stats_cache()
    if not cache:
        return JSONResponse(
            {"error": "Chưa có dữ liệu thống kê", "detail": "Đợi cào 0h hoặc restart."},
            status_code=503,
        )
    try:
        from stats_charts_overdue_rate import build_overdue_rate_payload_from_cache
        return build_overdue_rate_payload_from_cache(cache)
    except Exception as e:
        print(f"[stats] Lỗi overdue-rate: {e!r}")
        return JSONResponse({"error": str(e)}, status_code=500)



@app.get("/api/stats/error-codes")
async def api_stats_error_codes(request: Request):
    """Top 20 Error Code trong 30 ngày + KT gặp nhiều nhất."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cache = stats_data.load_stats_cache()
    if not cache:
        return JSONResponse({"error": "Chưa có dữ liệu thống kê"}, status_code=503)
    try:
        from stats_charts_error_codes import build_error_codes_payload_from_cache
        return build_error_codes_payload_from_cache(cache)
    except Exception as e:
        print(f"[stats] Lỗi error-codes: {e!r}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/admin/refresh-stats")
async def api_admin_refresh_stats(request: Request):
    """Admin: ép cào lại thống kê ngay (không đợi 0h)."""
    user = get_current_user(request)
    if not require_admin(user):
        return JSONResponse({"error": "Bạn không có quyền thực hiện thao tác này."}, status_code=403)
    try:
        payload = await stats_data.refresh_stats_cache()
        return {
            "status": "ok",
            "message": f"Đã cập nhật thống kê ({payload.get('total_tickets', 0)} ticket).",
            "generated_at": payload.get("generated_at"),
            "total_tickets": payload.get("total_tickets", 0),
        }
    except Exception as e:
        print(f"[stats] Lỗi admin refresh-stats: {e!r}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/admin/refresh-stations")
async def api_refresh_stations(request: Request):
    user = get_current_user(request)
    if not require_admin(user):
        return JSONResponse({"error": "Bạn không có quyền thực hiện thao tác này."}, status_code=403)

    await refresh_stations_once()
    await broadcast_stations_update()
    return {"status": "ok", "message": "Đã cập nhật dữ liệu trạm mới nhất!"}


@app.get("/api/admin/refresh-status")
async def api_refresh_status(request: Request):
    """Trạng thái chu kỳ cào tự động (paused hay đang chạy)."""
    user = get_current_user(request)
    if not require_admin(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return {
        "paused": _refresh_paused,
        "interval_seconds": TICKET_REFRESH_SECONDS,
    }


@app.post("/api/admin/toggle-refresh")
async def api_toggle_refresh(request: Request):
    """Bật/tắt chu kỳ cào tự động. Chỉ admin. Không ảnh hưởng nút refresh thủ công."""
    global _refresh_paused
    user = get_current_user(request)
    if not require_admin(user):
        return JSONResponse({"error": "Bạn không có quyền thực hiện thao tác này."}, status_code=403)

    _refresh_paused = not _refresh_paused
    if _refresh_paused:
        msg = "Đã TẠM DỪNG chu kỳ cào tự động."
        print(f"⏸ Admin [{user.get('username')}] tạm dừng cào dữ liệu.")
    else:
        msg = "Đã BẬT LẠI chu kỳ cào tự động."
        print(f"▶ Admin [{user.get('username')}] bật lại cào dữ liệu.")
    return {
        "status": "ok",
        "paused": _refresh_paused,
        "message": msg,
        "interval_seconds": TICKET_REFRESH_SECONDS,
    }


@app.get("/api/stations")
async def api_stations(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    filtered = filter_stations_for_user(_latest_station_payload["stations"], user)
    return {**_latest_station_payload, "stations": filtered}


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