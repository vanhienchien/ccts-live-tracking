"""
tile_proxy.py — Proxy tile bản đồ (OpenStreetMap) qua chính server CCTS Live
Map (FastAPI), thay vì để trình duyệt gọi thẳng *.tile.openstreetmap.org.

VÌ SAO CẦN FILE NÀY:
- Một số máy có hosts file / firewall / phần mềm bảo mật chặn domain
  tile.openstreetmap.org (vd trỏ về 127.0.0.1 -> ERR_CONNECTION_REFUSED)
  khiến nền bản đồ trắng xoá, dù heatmap/ranh giới vẫn vẽ được bình thường
  (2 thứ đó không cần domain ngoài, chỉ vẽ từ payload JSON đã có).
- Proxy tile qua CÙNG domain với app (domain mà user đang đăng nhập/dùng
  hàng ngày) -> trình duyệt không bao giờ cần resolve tile.openstreetmap.org
  nữa -> không còn phụ thuộc hosts/firewall của từng máy khách.

CÁCH GẮN VÀO main.py:
    import tile_proxy
    app.include_router(tile_proxy.router)

    # Và ở on_startup() thêm (không bắt buộc, chỉ để dọn cache định kỳ):
    # asyncio.create_task(tile_proxy.cache_cleanup_loop())

FRONTEND (stats_chart_heatmap.js) đổi tileLayer URL thành:
    /api/tiles/{z}/{x}/{y}.png

LƯU Ý (OSM Tile Usage Policy — https://operations.osmfoundation.org/policies/tiles/):
- Bắt buộc User-Agent định danh rõ ứng dụng (đã set _HEADERS bên dưới, SỬA
  lại email liên hệ cho đúng).
- Không được đánh tải nặng / cache lại quá lâu quá nhiều thứ vô lý; đã có
  cache đĩa 7 ngày để giảm request lặp lại.
- Nếu traffic lớn (nhiều người dùng cùng lúc), cân nhắc tự host tile
  server riêng hoặc dùng provider trả phí (MapTiler/Mapbox) thay vì gọi
  trực tiếp OSM ở quy mô lớn.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, JSONResponse

router = APIRouter()

_OSM_SUBDOMAINS = ["a", "b", "c"]
_OSM_URL_TMPL = "https://{sub}.tile.openstreetmap.org/{z}/{x}/{y}.png"

_CACHE_DIR = Path(__file__).parent / "_tile_cache"
_CACHE_DIR.mkdir(exist_ok=True)
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 ngày, tile nền hầu như không đổi

# BẮT BUỘC theo chính sách OSM: định danh rõ app + liên hệ (SỬA email bên dưới)
_HEADERS = {
    "User-Agent": "CCTS-LiveMap/1.0 (contact: your-email@example.com)"
}

_REQUEST_TIMEOUT = 6.0  # giây

# 1 client httpx dùng chung, tái sử dụng connection pool thay vì mở mới mỗi request
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
    return _client


def _cache_path(z: int, x: int, y: int) -> Path:
    key = hashlib.sha1(f"{z}/{x}/{y}".encode()).hexdigest()
    return _CACHE_DIR / f"{key}.png"


@router.get("/api/tiles/{z}/{x}/{y}.png")
async def get_tile(z: int, x: int, y: int, request: Request):
    # Chặn giá trị z/x/y bất thường (tránh bị lợi dụng làm proxy mở ra ngoài)
    if not (0 <= z <= 19):
        return JSONResponse({"error": "invalid tile coords"}, status_code=400)

    # Yêu cầu đã đăng nhập, khớp với các route /api/stats/* khác — tránh ai
    # cũng gọi được endpoint này để "mượn" server làm proxy tải ảnh miễn phí.
    from main import get_current_user  # import trễ để tránh vòng lặp import
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    cache_file = _cache_path(z, x, y)
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < _CACHE_TTL_SECONDS:
        return Response(
            content=cache_file.read_bytes(),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    sub = random.choice(_OSM_SUBDOMAINS)
    url = _OSM_URL_TMPL.format(sub=sub, z=z, x=x, y=y)
    client = _get_client()
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError:
        # OSM lỗi/timeout: có cache cũ (dù hết hạn) thì trả tạm, đỡ hơn trắng xoá
        if cache_file.exists():
            return Response(content=cache_file.read_bytes(), media_type="image/png")
        return JSONResponse({"error": "upstream tile fetch failed"}, status_code=502)

    try:
        cache_file.write_bytes(resp.content)
    except OSError:
        pass  # Không cache được thì thôi, vẫn trả ảnh về cho client

    return Response(
        content=resp.content,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def cache_cleanup_loop():
    """Tuỳ chọn: dọn file cache quá hạn mỗi 6 tiếng, tránh phình đĩa vô hạn.
    Gọi qua asyncio.create_task(tile_proxy.cache_cleanup_loop()) trong
    on_startup() của main.py nếu muốn bật."""
    while True:
        await asyncio.sleep(6 * 3600)
        now = time.time()
        removed = 0
        for f in _CACHE_DIR.glob("*.png"):
            try:
                if now - f.stat().st_mtime > _CACHE_TTL_SECONDS:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            print(f"[tile_proxy] Đã dọn {removed} tile cache quá hạn.")
