"""Scanner nền: quét ticket sắp quá hạn (45h ≤ tồn < 48h) → gửi FCM push.

Thay cho `sla_alert.py` cũ (đã xoá). Vòng lặp `asyncio` chạy suốt đời tiến
trình, mỗi `SCAN_INTERVAL_SECONDS` (mặc định 300s = 5 phút):

  1. Lấy `_latest_ticket_rows` mới nhất (do `refresh_stations_loop` trong
     main.py cập nhật) qua callable truyền vào — tránh import vòng.
  2. Lọc `is_near_overdue == True`.
  3. Map `tech_name` (họ tên) → `username` bằng `users_store.list_users_public()`
     (cache 60s sẵn có), giống hệt `/api/technicians`.
  4. Dedup: KHÔNG gửi lại 1 ticket trong vòng `DEDUP_SECONDS` (24h) — lưu
     `{ticket_id: last_sent_ts}` ra file để sống qua restart.
  5. Gọi `fcm_service.send_notification(...)` — chạy trong thread (blocking HTTP)
     để không chặn event loop.

Backward-compatible: `fcm_service` tự về chế độ "chỉ log" nếu chưa có
credential Firebase → scanner vẫn chạy, chỉ ghi log, không gửi, không crash.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time

import fcm_service
import observability
import users_store

logger = logging.getLogger("ccts.near_overdue_scanner")

SCAN_INTERVAL_SECONDS = int(os.environ.get("NEAR_OVERDUE_SCAN_SECONDS", "300"))
DEDUP_SECONDS = int(os.environ.get("NEAR_OVERDUE_DEDUP_HOURS", "24")) * 3600
_STATE_FILE = os.environ.get("NEAR_OVERDUE_STATE_FILE", "fcm_notified.json")


# ── state dedup (ticket_id → last_sent_ts) ────────────────────────────────
def _load_state() -> dict:
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): float(v) for k, v in data.items()}
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("[near_overdue] Không đọc được %s (%r).", _STATE_FILE, e)
    return {}


def _save_state(state: dict) -> None:
    try:
        d = os.path.dirname(os.path.abspath(_STATE_FILE)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".fcm_notified_", suffix=".json", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _STATE_FILE)
    except Exception as e:
        logger.error("[near_overdue] Ghi %s thất bại: %r", _STATE_FILE, e)


def _name_to_username() -> dict:
    """{họ tên viết thường: username} cho user role 'kỹ thuật'."""
    out = {}
    try:
        for u in users_store.list_users_public():
            if (u.get("role") or "").strip().lower() == "kỹ thuật":
                key = (u.get("full_name") or "").strip().lower()
                if key:
                    out[key] = u["username"]
    except Exception as e:
        logger.warning("[near_overdue] Không lấy được danh sách user: %r", e)
        observability.capture_exception(e, where="near_overdue_name_map")
    return out


def _scan_once_sync(rows: list) -> None:
    now = time.time()
    state = _load_state()
    # dọn entry cũ hơn 2×dedup để file không phình mãi
    state = {k: v for k, v in state.items() if now - v < DEDUP_SECONDS * 2}

    near = [
        r for r in (rows or [])
        if r.get("is_near_overdue") and str(r.get("ticket_id") or "").strip()
    ]
    if not near:
        _save_state(state)
        logger.info("[near_overdue] Quét: 0 ticket near-overdue.")
        return

    name_map = _name_to_username()
    sent_total = skipped_dedup = no_user = 0

    for r in near:
        tid = str(r.get("ticket_id")).strip()
        if now - state.get(tid, 0) < DEDUP_SECONDS:
            skipped_dedup += 1
            continue

        tech_name = (r.get("tech_name") or "Unassigned").strip()
        username = name_map.get(tech_name.lower())
        if not username:
            no_user += 1
            logger.info("[near_overdue] ticket=%s tech=%r → không map được username, bỏ qua.", tid, tech_name)
            continue

        hours = float(r.get("hours") or 0)
        station = str(r.get("station_code") or "—")
        title = "⚠️ Ticket sắp quá hạn"
        body = f"Ticket {tid} · Trạm {station} · {hours:.1f}h · còn ~{max(0.0, 48 - hours):.1f}h"

        res = fcm_service.send_notification(username, tid, title, body)
        # Ghi nhận đã xử lý kể cả khi "skipped" (chưa có token / chưa bật FCM) —
        # tránh spam log mỗi 5 phút cho cùng 1 ticket; hết 24h sẽ thử lại.
        state[tid] = now
        if res.get("sent", 0) > 0:
            sent_total += 1

    _save_state(state)
    logger.info(
        "[near_overdue] Quét: %d near-overdue | gửi=%d dedup_bỏ=%d không_có_user=%d | FCM_bật=%s",
        len(near), sent_total, skipped_dedup, no_user, fcm_service.is_enabled(),
    )


async def near_overdue_loop(rows_provider) -> None:
    """`rows_provider`: callable trả về list `_latest_ticket_rows` hiện tại."""
    logger.info(
        "[near_overdue] Scanner khởi động — chu kỳ %ds, dedup %dh.",
        SCAN_INTERVAL_SECONDS, DEDUP_SECONDS // 3600,
    )
    # trễ nhẹ để dữ liệu ticket lần cào đầu tiên kịp về
    await asyncio.sleep(min(60, SCAN_INTERVAL_SECONDS))
    while True:
        try:
            rows = rows_provider() or []
            await asyncio.to_thread(_scan_once_sync, rows)
        except Exception as e:
            logger.error("[near_overdue] Lỗi vòng quét (bỏ qua, thử lại sau): %r", e)
            observability.capture_exception(e, where="near_overdue_loop")
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
