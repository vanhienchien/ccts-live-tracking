"""Gửi push notification qua Firebase Cloud Messaging (Firebase Admin SDK).

Khởi tạo LƯỜI (lần gửi đầu tiên). Nguồn credential, theo thứ tự:
  1. env  FIREBASE_CREDENTIALS_JSON  — dán nguyên nội dung service-account JSON
  2. env  FIREBASE_CREDENTIALS_FILE  — đường dẫn file (mặc định
     `firebase_service_account.json` cạnh main.py)

LƯU Ý: `service_account.json` sẵn có trên server là của project **ccts-bot**
(chỉ để đọc Google Sheets) — KHÔNG dùng được cho FCM. FCM cần service-account
của project Firebase **ccts-mobie** (khớp `google-services.json` trong app).
Chưa cấu hình → module chạy ở chế độ "chỉ log", KHÔNG gửi, KHÔNG crash; app
di động vẫn có local-polling near-overdue làm fallback.
"""

from __future__ import annotations

import json
import logging
import os
import threading

import fcm_tokens
import observability

logger = logging.getLogger("ccts.fcm")

try:  # firebase-admin là dependency mềm — thiếu vẫn không sập app
    import firebase_admin
    from firebase_admin import credentials, messaging
    _HAVE_SDK = True
except Exception as e:  # pragma: no cover - phụ thuộc môi trường deploy
    firebase_admin = None
    credentials = None
    messaging = None
    _HAVE_SDK = False
    logger.warning("[fcm] Chưa cài firebase-admin (%r) — chế độ chỉ log.", e)

_CRED_FILE = os.environ.get("FIREBASE_CREDENTIALS_FILE", "firebase_service_account.json")

_lock = threading.Lock()
_app = None          # firebase_admin.App
_init_done = False
_enabled = False


def _load_credential():
    raw = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
    if raw:
        try:
            return credentials.Certificate(json.loads(raw))
        except Exception as e:
            logger.error("[fcm] FIREBASE_CREDENTIALS_JSON không hợp lệ: %r", e)
            return None
    if os.path.exists(_CRED_FILE):
        try:
            return credentials.Certificate(_CRED_FILE)
        except Exception as e:
            logger.error("[fcm] Đọc %s thất bại: %r", _CRED_FILE, e)
            return None
    return None


def _ensure_init() -> bool:
    """True nếu FCM sẵn sàng gửi. Chạy đúng 1 lần."""
    global _app, _init_done, _enabled
    if _init_done:
        return _enabled
    with _lock:
        if _init_done:
            return _enabled
        _init_done = True
        if not _HAVE_SDK:
            _enabled = False
            return False
        cred = _load_credential()
        if cred is None:
            logger.warning(
                "[fcm] Chưa có credential Firebase (FIREBASE_CREDENTIALS_JSON / %s) "
                "— chế độ chỉ log.", _CRED_FILE,
            )
            _enabled = False
            return False
        try:
            _app = firebase_admin.initialize_app(cred, name="ccts-fcm")
            _enabled = True
            logger.info("[fcm] Firebase Admin SDK đã khởi tạo — sẵn sàng gửi push.")
        except Exception as e:
            logger.error("[fcm] initialize_app thất bại: %r", e)
            observability.capture_exception(e, where="fcm_init")
            _enabled = False
        return _enabled


def is_enabled() -> bool:
    return _ensure_init()


def send_notification(username: str, ticket_id: str, title: str, body: str) -> dict:
    """Gửi 1 notification tới MỌI thiết bị của `username`.

    - message gồm `notification` (title/body) + `data` {"ticketId": ...} để app
      bấm vào mở đúng `TicketDetailScreen`.
    - token bị FCM báo không hợp lệ (Unregistered / SenderId mismatch) → xoá
      khỏi store luôn.

    Trả về {"sent": n, "failed": n, "skipped": bool}.
    """
    tokens = fcm_tokens.tokens_for(username)
    if not tokens:
        logger.info("[fcm] Bỏ qua — user=%s chưa có token.", username)
        return {"sent": 0, "failed": 0, "skipped": True}

    if not _ensure_init():
        logger.info(
            "[fcm] (chỉ log) → user=%s ticket=%s | %s — %s | %d token",
            username, ticket_id, title, body, len(tokens),
        )
        return {"sent": 0, "failed": 0, "skipped": True}

    data = {"ticketId": str(ticket_id or ""), "type": "near_overdue"}
    sent = failed = 0
    for tok in tokens:
        msg = messaging.Message(
            token=tok,
            notification=messaging.Notification(title=title, body=body),
            data=data,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="ccts_near_overdue", tag=str(ticket_id or "") or None,
                ),
            ),
        )
        try:
            messaging.send(msg, app=_app)
            sent += 1
        except Exception as e:
            failed += 1
            name = type(e).__name__
            if name in ("UnregisteredError", "SenderIdMismatchError") or "not a valid FCM" in str(e):
                fcm_tokens.delete(tok)
                logger.info("[fcm] Token …%s không hợp lệ (%s) — đã xoá.", tok[-8:], name)
            else:
                logger.warning("[fcm] Gửi tới …%s lỗi: %r", tok[-8:], e)
                observability.capture_exception(e, where="fcm_send", ticket_id=ticket_id)
    logger.info("[fcm] user=%s ticket=%s → sent=%d failed=%d", username, ticket_id, sent, failed)
    return {"sent": sent, "failed": failed, "skipped": False}
