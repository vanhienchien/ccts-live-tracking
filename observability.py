"""Telemetry lỗi từ xa (Sentry) — dùng chung cho endpoint, scanner, fcm_service.

Bật khi có env `SENTRY_DSN`. Không có DSN / chưa cài `sentry-sdk` → mọi hàm là
no-op, KHÔNG crash. Không gửi PII: `send_default_pii=False` + `before_send`
lọc header/cookie/query nhạy cảm.

Cấu hình:
    SENTRY_DSN       — bắt buộc để bật
    SENTRY_ENV       — môi trường (mặc định "production")
    SENTRY_RELEASE   — tuỳ chọn, gắn version
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("ccts.observability")

# DSN Sentry project "python-fastapi". Env SENTRY_DSN được ưu tiên (đổi project /
# đặt rỗng "-" để TẮT). DSN là ingest key chỉ-GỬI (không đọc được dữ liệu).
_DEFAULT_DSN = (
    "https://5aa67b50d5935dee62420093879b96ed"
    "@o4512038907084800.ingest.us.sentry.io/4512038926745600"
)
_DSN_ENV = os.environ.get("SENTRY_DSN", "").strip()
# "-" hoặc "0"/"off" ở env = chủ động tắt telemetry.
if _DSN_ENV in ("-", "0", "off", "false", "none"):
    _DSN = ""
else:
    _DSN = _DSN_ENV or _DEFAULT_DSN
_ENV = os.environ.get("SENTRY_ENV", "production").strip() or "production"
_RELEASE = os.environ.get("SENTRY_RELEASE", "").strip() or None

_on = False

try:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration
    _HAVE_SDK = True
except Exception as e:  # pragma: no cover - phụ thuộc môi trường deploy
    sentry_sdk = None
    FastApiIntegration = StarletteIntegration = None
    _HAVE_SDK = False
    _IMPORT_ERR = e

_SENSITIVE = (
    "password", "passwd", "pwd", "token", "authorization", "auth",
    "secret", "api_key", "apikey", "cookie", "set-cookie", "session",
)


def _redact_qs(qs: str) -> str:
    out = []
    for kv in qs.split("&"):
        key = kv.split("=", 1)[0].lower()
        out.append(f"{kv.split('=', 1)[0]}=[redacted]" if key in _SENSITIVE else kv)
    return "&".join(out)


def _scrub(event, hint):
    try:
        req = event.get("request")
        if isinstance(req, dict):
            headers = req.get("headers")
            if isinstance(headers, dict):
                for k in list(headers):
                    if k.lower() in _SENSITIVE:
                        headers[k] = "[redacted]"
            if req.get("cookies"):
                req["cookies"] = "[redacted]"
            qs = req.get("query_string")
            if isinstance(qs, str) and any(s in qs.lower() for s in ("token=", "secret=")):
                req["query_string"] = _redact_qs(qs)
    except Exception:
        pass
    return event


def init_sentry() -> bool:
    """Khởi tạo 1 lần. True nếu telemetry đang bật."""
    global _on
    if _on:
        return True
    if not _DSN:
        logger.info("[sentry] SENTRY_DSN chưa đặt — tắt telemetry.")
        return False
    if not _HAVE_SDK:
        logger.warning("[sentry] chưa cài sentry-sdk (%r) — tắt telemetry.", _IMPORT_ERR)
        return False
    try:
        sentry_sdk.init(
            dsn=_DSN,
            environment=_ENV,
            release=_RELEASE,
            traces_sample_rate=0.0,          # chỉ theo dõi lỗi, không APM
            send_default_pii=False,
            max_breadcrumbs=50,
            before_send=_scrub,
            integrations=[StarletteIntegration(), FastApiIntegration()],
        )
        _on = True
        logger.info("[sentry] đã khởi tạo (env=%s, release=%s).", _ENV, _RELEASE or "-")
    except Exception as e:
        logger.error("[sentry] init thất bại: %r", e)
        _on = False
    return _on


def is_enabled() -> bool:
    return _on


def capture_exception(exc=None, **tags) -> None:
    if not _on:
        return
    try:
        if tags:
            with sentry_sdk.new_scope() as scope:
                for k, v in tags.items():
                    scope.set_tag(k, str(v))
                sentry_sdk.capture_exception(exc)
        else:
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def capture_message(message: str, level: str = "info", **tags) -> None:
    if not _on:
        return
    try:
        with sentry_sdk.new_scope() as scope:
            for k, v in tags.items():
                scope.set_tag(k, str(v))
            sentry_sdk.capture_message(message, level=level)
    except Exception:
        pass


def set_user(username, role=None, region=None) -> None:
    """Gắn danh tính (không token/mật khẩu) vào scope request hiện tại."""
    if not _on:
        return
    try:
        sentry_sdk.set_user({"username": username, "role": role, "region": region})
    except Exception:
        pass


def breadcrumb(message: str, category: str = "app", **data) -> None:
    if not _on:
        return
    try:
        sentry_sdk.add_breadcrumb(
            category=category, message=message, level="info", data=data or None
        )
    except Exception:
        pass
