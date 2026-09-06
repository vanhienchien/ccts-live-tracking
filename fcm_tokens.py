"""Lưu FCM device token theo username — file-based (không cần DB).

App di động (Flutter `PushService`) gọi `POST /api/mobile/fcm-token`:
  - action=register  → lưu / cập nhật token cho user đang đăng nhập
  - action=delete    → gỡ token (khi đăng xuất)

Cấu trúc file (mặc định `fcm_tokens.json` cạnh main.py, đổi bằng env
`FCM_TOKENS_FILE`):

    {
      "tokens": {
        "<fcm_token>": {"username": "abc", "updated_at": 1712345678.0}
      }
    }

Khoá theo token (mỗi thiết bị = 1 token duy nhất trên toàn hệ) nên:
  - đổi user trên cùng máy → token gắn lại sang user mới,
  - xoá theo token rất gọn,
  - tra "token của 1 user" = quét dict (số user nội bộ nhỏ, chi phí không đáng kể).

Mọi thao tác bọc lock + ghi nguyên tử (temp file + os.replace) để endpoint
và scanner nền chạy song song không hỏng file.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time

logger = logging.getLogger("ccts.fcm_tokens")

_FILE = os.environ.get("FCM_TOKENS_FILE", "fcm_tokens.json")
_STALE_SECONDS = int(os.environ.get("FCM_TOKEN_MAX_AGE_DAYS", "60")) * 86400

_lock = threading.RLock()


def _load() -> dict:
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("tokens"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:  # file hỏng → coi như trống, ghi đè lần sau
        logger.warning("[fcm_tokens] Không đọc được %s (%r) — coi như trống.", _FILE, e)
    return {"tokens": {}}


def _save(data: dict) -> None:
    try:
        d = os.path.dirname(os.path.abspath(_FILE)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".fcm_tokens_", suffix=".json", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _FILE)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except Exception as e:
        logger.error("[fcm_tokens] Ghi %s thất bại: %r", _FILE, e)


def register(username: str, token: str) -> None:
    """Lưu / cập nhật token cho user. Idempotent — gọi lại chỉ làm mới timestamp."""
    username = (username or "").strip()
    token = (token or "").strip()
    if not username or not token:
        return
    with _lock:
        data = _load()
        data["tokens"][token] = {"username": username, "updated_at": time.time()}
        _prune_locked(data)
        _save(data)
    logger.info("[fcm_tokens] register user=%s token=…%s", username, token[-8:])


def delete(token: str) -> None:
    """Gỡ 1 token (đăng xuất / token bị FCM báo không hợp lệ)."""
    token = (token or "").strip()
    if not token:
        return
    with _lock:
        data = _load()
        if data["tokens"].pop(token, None) is not None:
            _save(data)
            logger.info("[fcm_tokens] delete token=…%s", token[-8:])


def tokens_for(username: str) -> list[str]:
    """Danh sách token còn hạn của 1 user."""
    username = (username or "").strip().lower()
    if not username:
        return []
    with _lock:
        data = _load()
        return [
            tok
            for tok, meta in data["tokens"].items()
            if (meta.get("username") or "").strip().lower() == username
        ]


def _prune_locked(data: dict) -> None:
    cutoff = time.time() - _STALE_SECONDS
    stale = [t for t, m in data["tokens"].items() if (m.get("updated_at") or 0) < cutoff]
    for t in stale:
        data["tokens"].pop(t, None)
    if stale:
        logger.info("[fcm_tokens] prune %d token quá hạn (>%dd).", len(stale), _STALE_SECONDS // 86400)


def stats() -> dict:
    with _lock:
        data = _load()
        users = {m.get("username") for m in data["tokens"].values()}
        return {"tokens": len(data["tokens"]), "users": len([u for u in users if u])}
