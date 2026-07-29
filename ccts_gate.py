"""
Khóa dùng chung cho mọi thao tác gọi API CCTS.

stats_data (export Excel) và ccts_data (find ticket live) dùng chung tài khoản
→ không chạy song song để tránh đá session / rate limit / export task chồng chéo.
"""

from __future__ import annotations

import asyncio

# Một process = một lock. Mọi coroutine CCTS phải async with CCTS_API_LOCK.
CCTS_API_LOCK = asyncio.Lock()
