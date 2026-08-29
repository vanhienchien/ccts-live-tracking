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

VỀ THỨ TỰ PROVIDER (_PROVIDERS bên dưới):
1. Esri "World Light Gray Canvas" — provider CHÍNH. Không cần đăng ký/API
   key, miễn phí, KHÔNG cấm việc proxy/cache qua server riêng.
2. CARTO (basemaps.cartocdn.com) — CHỈ dùng khi có biến môi trường
   CARTO_API_KEY (CARTO bắt buộc key từ ~cuối tháng 8/2026, gọi không key
   sẽ bị dán watermark "API KEY REQUIRED"). Lấy key miễn phí (5 triệu
   request/tháng) tại: https://carto.com/basemaps/apikey rồi set biến môi
   trường CARTO_API_KEY=xxxx.

QUAN TRỌNG — ĐÃ BỎ HẲN OpenStreetMap (tile.openstreetmap.org) khỏi danh
sách provider. Lý do: chính sách tile usage của OSM
(operations.osmfoundation.org/policies/tiles) CẤM việc app dựng server
riêng để PROXY/CACHE lại tile OSM cho nhiều người dùng qua 1 địa chỉ IP -
họ chỉ cho phép trình duyệt của từng người dùng gọi trực tiếp. Vì
tile_proxy.py này đúng là 1 kiểu proxy như vậy, OSM đã trả về ảnh cảnh báo
"Access blocked" (osm.wiki/Blocked) thay vì tile thật - lỗi này KHÔNG liên
quan gì đến mạng máy local hay Render, mà do OSM chủ động chặn theo IP
server một khi phát hiện vi phạm. Nếu sau này thật sự cần tile OSM gốc
(đường sá/địa danh chi tiết), phải hoặc (a) để browser gọi trực tiếp OSM
(không qua proxy này, chấp nhận rủi ro 1 số máy/mạng bị chặn), hoặc
(b) dùng provider trả phí có license cho việc proxy (MapTiler, Mapbox,
Stadia Maps...).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, JSONResponse

router = APIRouter()

_CARTO_API_KEY = os.environ.get("CARTO_API_KEY", "").strip()

# Tile trả về nhỏ bất thường (vd ảnh cảnh báo "Access blocked" / watermark
# "API KEY REQUIRED") thường có kích thước rất khác tile bản đồ thật -
# dùng ngưỡng này để KHÔNG cache nhầm ảnh lỗi là tile hợp lệ.
_MIN_VALID_TILE_BYTES = 300


def _esri_url(z: int, x: int, y: int) -> str:
    # LƯU Ý: Esri dùng thứ tự {z}/{y}/{x} (khác OSM/CARTO là {z}/{x}/{y})
    return f"https://services.arcgisonline.com/arcgis/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"


def _carto_url(z: int, x: int, y: int) -> str | None:
    if not _CARTO_API_KEY:
        return None
    sub = random.choice(["a", "b", "c", "d"])
    return f"https://{sub}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png?api_key={_CARTO_API_KEY}"


# Thứ tự thử lần lượt — hàm trả None nghĩa là "bỏ qua provider này"
# (vd CARTO khi chưa cấu hình key). KHÔNG có OSM trong danh sách (xem lý
# do ở docstring đầu file).
_PROVIDERS = [
    ("esri", _esri_url),
    ("carto", _carto_url),
]

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

    client = _get_client()
    resp = None

    for name, url_fn in _PROVIDERS:
        url = url_fn(z, x, y)
        if url is None:
            continue  # provider chưa cấu hình (vd CARTO thiếu API key) -> bỏ qua
        try:
            candidate = await client.get(url)
            candidate.raise_for_status()
            ctype = candidate.headers.get("content-type", "")
            if "image" not in ctype or len(candidate.content) < _MIN_VALID_TILE_BYTES:
                # Trả về 200 nhưng không phải ảnh hợp lệ (vd trang lỗi HTML,
                # hoặc ảnh cảnh báo "Access blocked"/"API KEY REQUIRED" quá
                # nhỏ bất thường) -> coi như thất bại, không cache, thử tiếp.
                print(f"[tile_proxy] Provider '{name}' trả về nội dung đáng ngờ cho {z}/{x}/{y} (type={ctype}, {len(candidate.content)} bytes), thử provider kế tiếp...")
                continue
            resp = candidate
            break
        except httpx.HTTPError as e:
            print(f"[tile_proxy] Provider '{name}' lỗi ({e.__class__.__name__}) cho {z}/{x}/{y}, thử provider kế tiếp...")
            resp = None

    if resp is None:
        # Mọi provider đều thất bại: có cache cũ (dù hết hạn) thì trả tạm,
        # đỡ hơn trắng xoá hoàn toàn.
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