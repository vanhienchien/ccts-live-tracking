"""
CCTS Live Map - ứng dụng web độc lập (FastAPI + WebSocket).

Chạy thử local:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

Xem README.md đi kèm để biết cách cấu hình biến môi trường & deploy.
"""

import asyncio
import hashlib
import json
import uuid

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import users_store
import ccts_data
import stats_data
import tile_proxy
from ccts_data import get_static_data, filter_stations_for_user, filter_tech_by_region_for_user
from location_hub import hub
from config import SESSION_COOKIE_NAME, TICKET_REFRESH_SECONDS
import os

# auto | 1 | 0 — local test: set STATS_REFRESH_ON_STARTUP=0 để chỉ dùng cache có sẵn
_STATS_STARTUP_MODE = os.environ.get("STATS_REFRESH_ON_STARTUP", "1").strip().lower()


app = FastAPI(title="CCTS Live Map")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(tile_proxy.router)

SESSIONS = {}

_latest_station_payload = {
    "stations": [], "total_tickets": 0, "missing_count": 0, "updated_at": None, "fetch_success": True,
}
_latest_tech_stats = {}
_latest_ticket_rows = []
_refresh_paused = False  # True = tạm dừng chu kỳ cào tự động (chỉ admin bật/tắt)

# Chữ ký (hash) của lần broadcast_stations_update() gần nhất - dùng để BỎ QUA
# việc gửi lại toàn bộ payload trạm qua WebSocket cho mọi client nếu dữ liệu
# không hề đổi so với lần gửi trước (vd. chu kỳ cào này TẤT CẢ tài khoản đều
# lỗi -> vẫn giữ dữ liệu cũ -> không có lý do gửi lại y hệt cho từng client).
# Đây là nguồn tốn băng thông WebSocket lớn thứ 2 sau việc nhúng HTML vào
# payload (đã bỏ ở ccts_data.py) - mỗi chu kỳ refresh_stations_loop() (mỗi
# TICKET_REFRESH_SECONDS) trước đây LUÔN broadcast dù thất bại hay không.
_last_broadcast_signature = None


def _station_payload_signature(payload) -> str:
    """Hash nội dung 'stations' (bỏ qua 'updated_at' vì trường này luôn đổi
    mỗi chu kỳ dù dữ liệu ticket không đổi chút nào)."""
    stations = payload.get("stations", [])
    raw = json.dumps(stations, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_current_user(request: Request):
    """Lấy user hiện tại từ cookie (web) HOẶC header 'Authorization: Bearer
    <token>' (app di động). Cả 2 đều tra cùng 1 dict SESSIONS - token của
    app tạo ra ở POST /api/mobile/login cũng nằm trong dict này, nên mọi
    endpoint hiện có (/api/stations, /api/technicians...) dùng lại được
    nguyên vẹn, không cần sửa gì thêm."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        auth_header = request.headers.get("authorization") or ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    if not token:
        return None
    return SESSIONS.get(token)


def require_admin(user):
    return bool(user) and (user.get("role") or "").strip().lower() == "admin"


def can_view_stats(user) -> bool:
    """Trang Thống kê: chỉ điều phối trở lên (không cho kỹ thuật viên)."""
    if not user:
        return False
    role = (user.get("role") or "").strip().lower()
    return role != "kỹ thuật"


async def refresh_stations_once() -> bool:
    """Cào lại toàn bộ dữ liệu (trạm + thống kê hiệu suất + ticket chi tiết).
    Nếu TẤT CẢ tài khoản CCTS đều thất bại (fetch_success=False), GIỮ NGUYÊN
    dữ liệu cũ trong bộ nhớ - không ghi đè bằng dữ liệu rỗng.
    Trả về True nếu có dữ liệu MỚI được chấp nhận (đáng để broadcast), False
    nếu lần này thất bại và dữ liệu trong bộ nhớ không đổi."""
    global _latest_station_payload, _latest_tech_stats, _latest_ticket_rows

    new_payload, new_stats, new_rows = await ccts_data.refresh_all_ccts_data()

    if new_payload.get("fetch_success"):
        _latest_station_payload = new_payload
        _latest_tech_stats = new_stats
        _latest_ticket_rows = new_rows
        ccts_data.save_cache_to_file(new_payload, new_stats, new_rows)
        return True
    else:
        print("⚠️ Tất cả tài khoản CCTS đều thất bại lần cào này - "
              "GIỮ NGUYÊN dữ liệu lần cào gần nhất thành công, không xoá bản đồ.")
        return False


async def broadcast_stations_update(force: bool = False):
    """Gửi payload trạm mới nhất cho MỌI client đang mở WebSocket.
    Mặc định (force=False): BỎ QUA việc gửi nếu nội dung 'stations' y hệt lần
    gửi trước (vd. chu kỳ cào này thất bại toàn bộ, hoặc không ticket nào
    thay đổi) - tránh phát lại payload giống hệt cho từng client, mỗi
    TICKET_REFRESH_SECONDS, chiếm phần lớn băng thông WebSocket."""
    global _last_broadcast_signature

    signature = _station_payload_signature(_latest_station_payload)
    if not force and signature == _last_broadcast_signature:
        print("[ws] Dữ liệu trạm không đổi so với lần gửi trước - bỏ qua broadcast.")
        return

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

    _last_broadcast_signature = signature


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

    # Cào stats lúc startup:
    #   STATS_REFRESH_ON_STARTUP=1     → luôn cào (prod / cần data mới)
    #   STATS_REFRESH_ON_STARTUP=0     → không cào (local test, dùng cache sẵn)
    #   STATS_REFRESH_ON_STARTUP=auto  → chỉ cào khi CHƯA có cache (mặc định)
    # Lịch 0h VN và POST /api/admin/refresh-stats vẫn cào bình thường.
    do_stats_startup = False
    if _STATS_STARTUP_MODE in ("1", "true", "yes", "on"):
        do_stats_startup = True
    elif _STATS_STARTUP_MODE in ("0", "false", "no", "off"):
        do_stats_startup = False
    else:
        do_stats_startup = not bool(stats_cached)

    if do_stats_startup:
        print(f"[stats] Startup: sẽ cào nền (mode={_STATS_STARTUP_MODE!r}, có_cache={bool(stats_cached)}).")
        asyncio.create_task(_run_stats_refresh("khởi động"))
    else:
        print(f"[stats] Startup: BỎ QUA cào — dùng cache có sẵn (mode={_STATS_STARTUP_MODE!r}). "
              f"Muốn cào: STATS_REFRESH_ON_STARTUP=1 hoặc admin refresh-stats / đợi 0h.")
        # Cache cũ có thể chưa có charts đã nướng → bổ sung nền, không cào CCTS
        if stats_cached:
            async def _ensure_charts_bg():
                try:
                    for key in ("daily_volume", "overdue_rate", "heatmap", "error_codes"):
                        stats_data.ensure_chart_in_cache(key)
                except Exception as e:
                    print(f"[stats] ensure charts nền lỗi: {e!r}")
            asyncio.create_task(_ensure_charts_bg())

    async def _run_stations_refresh_once_bg():
        # QUAN TRỌNG: không await trực tiếp trong on_startup — CCTS có thể
        # mất vài chục giây đến vài phút để xử lý export (đã thấy trong log
        # thực tế), và ASGI lifespan "startup" phải trả về nhanh để Uvicorn
        # bind + accept connection kịp trước khi nền tảng cloud (Render...)
        # timeout port-scan và kill tiến trình (exit 137). Cache cũ (nếu có)
        # vẫn phục vụ bình thường trong lúc lượt cào đầu tiên này chạy nền.
        try:
            await refresh_stations_once()
        except Exception as e:
            print(f"⚠️ Lỗi lần cào đầu tiên khi khởi động ({e!r}) - tiếp tục chạy với dữ liệu cache (nếu có).")

    asyncio.create_task(_run_stations_refresh_once_bg())
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


@app.post("/api/mobile/login")
async def api_mobile_login(request: Request):
    """Đăng nhập dành cho APP DI ĐỘNG - nhận/trả JSON, KHÔNG redirect và
    KHÔNG set cookie như /login (dành cho web). Dùng lại đúng
    users_store.verify_login() nên không cần sửa gì bên users_store.

    Token trả về được lưu vào CHUNG dict SESSIONS với web - nhờ vậy
    get_current_user() ở trên tự nhận diện được, không cần route/logic
    riêng cho từng endpoint dữ liệu (/api/stations, /api/technicians...).

    LƯU Ý: token này KHÔNG tự hết hạn (giống hệt cách SESSIONS đang hoạt
    động cho web hiện tại - chỉ mất khi server restart). Nếu sau này cần
    "đăng xuất từ xa" 1 thiết bị, thêm hàm xoá theo token hoặc theo
    username tại đây.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Dữ liệu gửi lên không hợp lệ."}, status_code=400)

    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    if not username or not password:
        return JSONResponse({"error": "Vui lòng nhập tên đăng nhập và mật khẩu."}, status_code=400)

    user = users_store.verify_login(username, password)
    if not user:
        return JSONResponse({"error": "Sai tên đăng nhập hoặc mật khẩu."}, status_code=401)

    token = uuid.uuid4().hex
    SESSIONS[token] = user

    return {"status": "ok", "token": token, "user": user}


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
    """Trang thống kê / trực quan dữ liệu ticket.
    Chỉ điều phối trở lên được xem; kỹ thuật viên bị chặn."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not can_view_stats(user):
        return RedirectResponse("/", status_code=302)
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
    if not can_view_stats(user):
        return JSONResponse({"error": "forbidden", "detail": "Kỹ thuật viên không được xem thống kê."}, status_code=403)

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
    if not can_view_stats(user):
        return JSONResponse({"error": "forbidden", "detail": "Kỹ thuật viên không được xem thống kê."}, status_code=403)

    payload = stats_data.ensure_chart_in_cache("overdue_rate")
    if payload is None:
        return JSONResponse(
            {"error": "Chưa có dữ liệu thống kê", "detail": "Đợi cào 0h hoặc admin refresh-stats."},
            status_code=503,
        )
    return payload



@app.get("/api/stats/error-codes")
async def api_stats_error_codes(request: Request):
    """Top 20 Error Code trong 30 ngày + KT gặp nhiều nhất."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not can_view_stats(user):
        return JSONResponse({"error": "forbidden", "detail": "Kỹ thuật viên không được xem thống kê."}, status_code=403)
    payload = stats_data.ensure_chart_in_cache("error_codes")
    if payload is None:
        return JSONResponse({"error": "Chưa có dữ liệu thống kê"}, status_code=503)
    return payload


@app.get("/api/stats/heatmap")
async def api_stats_heatmap(request: Request):
    """Bản đồ nhiệt số ticket & ticket Overdue theo vị trí trạm, tách theo 5 khu vực."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not can_view_stats(user):
        return JSONResponse({"error": "forbidden", "detail": "Kỹ thuật viên không được xem thống kê."}, status_code=403)
    payload = stats_data.ensure_chart_in_cache("heatmap")
    if payload is None:
        return JSONResponse(
            {"error": "Chưa có dữ liệu thống kê", "detail": "Đợi cào 0h hoặc admin refresh-stats."},
            status_code=503,
        )
    return payload


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


@app.post("/api/location")
async def api_mobile_location(request: Request):
    """Nhận vị trí GPS gửi lên TỪ APP DI ĐỘNG CCTS (Flutter) - đây là NGUỒN
    VỊ TRÍ DUY NHẤT của kỹ thuật viên. Đã bỏ hẳn app Traccar Client và
    endpoint GET /api/traccar cũ (dùng 1 token tĩnh dùng chung + tham số
    'id' tự khai, ai biết token cũng gọi được) - endpoint này xác thực theo
    ĐÚNG người dùng đang đăng nhập trên app (Bearer token từ
    POST /api/mobile/login, tra qua get_current_user() y hệt mọi endpoint
    dữ liệu khác), an toàn hơn và không cần quản lý thêm 1 token riêng.

    Gọi hub.update_location() - dữ liệu đi thẳng vào LocationHub (chỉ lưu
    RAM, KHÔNG ghi Google Sheets) và được broadcast qua WebSocket cho các
    client (web/app) khác đang được phép xem.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Dữ liệu gửi lên không hợp lệ."}, status_code=400)

    try:
        lat = float(body.get("lat"))
        lng = float(body.get("lng"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Thiếu hoặc sai định dạng lat/lng."}, status_code=400)

    accuracy = body.get("accuracy")
    try:
        accuracy = float(accuracy) if accuracy is not None else None
    except (TypeError, ValueError):
        accuracy = None

    await hub.update_location(
        user["username"], user, lat, lng, accuracy,
        stations=_latest_station_payload["stations"],
        tech_stats=_tech_stats_for_user(user),
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

        # LƯU Ý: đã bỏ nhận vị trí từ trình duyệt (web) qua WebSocket để tránh
        # tốn dung lượng/pin của người dùng khi mở web. Vị trí giờ CHỈ đến từ
        # 1 nguồn HTTP duy nhất (không qua WebSocket này): app di động CCTS
        # (Flutter) qua POST /api/location - đã bỏ hẳn app Traccar Client và
        # endpoint /api/traccar cũ. Kết nối WebSocket ở đây chỉ còn dùng để:
        # nhận snapshot ban đầu, nhận cập nhật trạm (stations_update) và
        # presence - không còn nhận/gửi vị trí từ phía client nữa. Nếu client
        # cũ vẫn gửi message "location" lên, server sẽ bỏ qua (không xử lý,
        # không lưu, không broadcast).
        while True:
            try:
                await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                continue
    finally:
        await hub.unregister(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)