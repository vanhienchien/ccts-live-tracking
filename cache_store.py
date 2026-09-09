"""
cache_store.py — Lưu / đọc cache JSON (map + stats) qua Object Storage
S3-compatible (AWS S3, Cloudflare R2, MinIO, …) với fallback file local.

Mục tiêu trên Render: disk container mất khi redeploy → đẩy cache lên
bucket để lần boot sau vẫn có dữ liệu xem ngay, trong lúc cào nền.

Cấu hình (env) — để trống = chỉ dùng file local như trước:

  CACHE_S3_BUCKET          bắt buộc nếu muốn bật object storage
  CACHE_S3_ACCESS_KEY
  CACHE_S3_SECRET_KEY
  CACHE_S3_ENDPOINT        vd https://xxx.r2.cloudflarestorage.com
                           (AWS S3 để trống)
  CACHE_S3_REGION          mặc định auto (R2 thường "auto")
  CACHE_S3_PREFIX          tiền tố key, mặc định "ccts-cache/"

Keys cố định:
  map   → {prefix}last_known_data.json
  stats → {prefix}stats_daily_cache.json

Cài thêm: pip install boto3
"""
from __future__ import annotations

import gzip
import json
import os
import threading
from typing import Any

# Nén gzip khi đẩy JSON lên S3/R2 nếu body >= ngưỡng này. Giảm ~85-90%
# dung lượng (vd cache stats 30 MB → ~3 MB) → tải nhanh hơn nhiều lúc
# cold-start trên Render, đọc lại tự giải nén trong suốt (nhận diện theo
# magic bytes 0x1f 0x8b). Đặt = 0 để tắt.
S3_GZIP_MIN_BYTES = int(os.environ.get("CACHE_S3_GZIP_MIN_BYTES", "65536"))
_GZIP_MAGIC = b"\x1f\x8b"

# Nạp .env NGAY khi import (best-effort). Trước đây chỉ config.py gọi
# load_dotenv(), nên khi chạy script đứng riêng KHÔNG đi qua config
# (vd `python stats_data.py` để cào thống kê ở local) thì các biến
# CACHE_S3_* không được nạp → is_s3_enabled() = False → cache stats chỉ
# ghi file local, KHÔNG bao giờ đẩy lên R2. Gọi ở đây để mọi entrypoint
# dùng cache_store đều thấy cấu hình R2. Trên Render đã set env sẵn nên
# load_dotenv() không có tác dụng phụ.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

# ---------------------------------------------------------------------------
# Cấu hình
# ---------------------------------------------------------------------------
BUCKET = os.environ.get("CACHE_S3_BUCKET", "").strip()
ACCESS_KEY = os.environ.get("CACHE_S3_ACCESS_KEY", "").strip()
SECRET_KEY = os.environ.get("CACHE_S3_SECRET_KEY", "").strip()
ENDPOINT = os.environ.get("CACHE_S3_ENDPOINT", "").strip() or None
REGION = os.environ.get("CACHE_S3_REGION", "auto").strip() or "auto"
PREFIX = os.environ.get("CACHE_S3_PREFIX", "ccts-cache/").strip()
if PREFIX and not PREFIX.endswith("/"):
    PREFIX += "/"

KEY_MAP = f"{PREFIX}last_known_data.json"
KEY_STATS = f"{PREFIX}stats_daily_cache.json"
# Bản "gọn" chỉ gồm meta + 4 chart đã dựng sẵn (KHÔNG kèm mảng tickets thô)
# — đây là thứ DUY NHẤT các route /api/stats/* cần lúc phục vụ request.
# Nhỏ hơn bản đầy đủ ~30-50 lần → cold-start trên Render đọc gần như tức thì.
KEY_STATS_CHARTS = f"{PREFIX}stats_charts_cache.json"

_client = None
_client_lock = threading.Lock()
_s3_enabled_logged = False
_s3_disabled_logged = False


def is_s3_enabled() -> bool:
    global _s3_disabled_logged
    ok = bool(BUCKET and ACCESS_KEY and SECRET_KEY)
    if not ok and not _s3_disabled_logged:
        missing = [
            name for name, val in (
                ("CACHE_S3_BUCKET", BUCKET),
                ("CACHE_S3_ACCESS_KEY", ACCESS_KEY),
                ("CACHE_S3_SECRET_KEY", SECRET_KEY),
            ) if not val
        ]
        print(f"[cache_store] Object storage TẮT — thiếu env: {', '.join(missing)}. "
              "Cache chỉ ghi file local, KHÔNG đẩy lên R2/S3.")
        _s3_disabled_logged = True
    return ok


def _get_client():
    """Lazy boto3 client; None nếu chưa cấu hình hoặc thiếu thư viện."""
    global _client, _s3_enabled_logged
    if not is_s3_enabled():
        return None
    with _client_lock:
        if _client is not None:
            return _client
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            print("[cache_store] Chưa cài boto3 — chỉ dùng file local. "
                  "Chạy: pip install boto3")
            return None
        kwargs = {
            "service_name": "s3",
            "aws_access_key_id": ACCESS_KEY,
            "aws_secret_access_key": SECRET_KEY,
            "region_name": REGION,
            "config": Config(signature_version="s3v4"),
        }
        if ENDPOINT:
            kwargs["endpoint_url"] = ENDPOINT
        _client = boto3.client(**kwargs)
        if not _s3_enabled_logged:
            print(f"[cache_store] Object storage BẬT → bucket={BUCKET!r} "
                  f"endpoint={ENDPOINT or 'AWS default'} prefix={PREFIX!r}")
            _s3_enabled_logged = True
        return _client


def _s3_get_json(key: str) -> dict | list | None:
    client = _get_client()
    if client is None:
        return None
    try:
        obj = client.get_object(Bucket=BUCKET, Key=key)
        body = obj["Body"].read()
        raw_len = len(body)
        if body[:2] == _GZIP_MAGIC:
            body = gzip.decompress(body)
        data = json.loads(body.decode("utf-8"))
        print(f"[cache_store] Đã tải từ S3: s3://{BUCKET}/{key} "
              f"({raw_len} bytes wire → {len(body)} bytes JSON)")
        return data
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            print(f"[cache_store] S3 chưa có key {key}")
        else:
            print(f"[cache_store] Lỗi đọc S3 {key}: {e!r}")
        return None


def _s3_put_json(key: str, data: Any) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        json_len = len(body)
        put_kwargs = {
            "Bucket": BUCKET,
            "Key": key,
            "ContentType": "application/json; charset=utf-8",
        }
        if S3_GZIP_MIN_BYTES and json_len >= S3_GZIP_MIN_BYTES:
            body = gzip.compress(body, compresslevel=6)
            put_kwargs["ContentEncoding"] = "gzip"
        put_kwargs["Body"] = body
        client.put_object(**put_kwargs)
        print(f"[cache_store] Đã ghi S3: s3://{BUCKET}/{key} "
              f"({json_len} bytes JSON → {len(body)} bytes wire)")
        return True
    except Exception as e:
        print(f"[cache_store] Lỗi ghi S3 {key}: {e!r}")
        return False


def _read_local(path: str) -> dict | list | None:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[cache_store] Lỗi đọc local {path}: {e}")
        return None


def _write_local(path: str, data: Any) -> bool:
    if not path:
        return False
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[cache_store] Lỗi ghi local {path}: {e}")
        return False


# ---------------------------------------------------------------------------
# API công khai — map cache
# ---------------------------------------------------------------------------
def load_map_cache(local_path: str = "last_known_data.json") -> dict | None:
    """Ưu tiên local (nhanh) → nếu thiếu thì kéo từ S3 rồi ghi local."""
    data = _read_local(local_path)
    if isinstance(data, dict) and data:
        return data
    remote = _s3_get_json(KEY_MAP)
    if isinstance(remote, dict) and remote:
        _write_local(local_path, remote)
        return remote
    return data if isinstance(data, dict) else None


def save_map_cache(data: dict, local_path: str = "last_known_data.json") -> bool:
    """Ghi local + đẩy S3 (best-effort). Trả về True nếu đã đẩy S3."""
    _write_local(local_path, data)
    return _s3_put_json(KEY_MAP, data)


# ---------------------------------------------------------------------------
# API công khai — stats cache
# ---------------------------------------------------------------------------
def load_stats_cache_file(local_path: str) -> dict | None:
    data = _read_local(local_path)
    if isinstance(data, dict) and data:
        return data
    remote = _s3_get_json(KEY_STATS)
    if isinstance(remote, dict) and remote:
        _write_local(local_path, remote)
        return remote
    return data if isinstance(data, dict) else None


def save_stats_cache_file(data: dict, local_path: str) -> bool:
    """Ghi local + đẩy S3 (best-effort). Trả về True nếu đã đẩy S3."""
    _write_local(local_path, data)
    return _s3_put_json(KEY_STATS, data)


# ---------------------------------------------------------------------------
# API công khai — stats "charts" cache (bản gọn cho route /api/stats/*)
# ---------------------------------------------------------------------------
def load_stats_charts_cache_file(local_path: str) -> dict | None:
    """Ưu tiên local → nếu thiếu thì kéo bản gọn từ S3 rồi ghi local."""
    data = _read_local(local_path)
    if isinstance(data, dict) and data:
        return data
    remote = _s3_get_json(KEY_STATS_CHARTS)
    if isinstance(remote, dict) and remote:
        _write_local(local_path, remote)
        return remote
    return data if isinstance(data, dict) else None


def save_stats_charts_cache_file(data: dict, local_path: str) -> bool:
    """Ghi local + đẩy S3 (best-effort). Trả về True nếu đã đẩy S3."""
    _write_local(local_path, data)
    return _s3_put_json(KEY_STATS_CHARTS, data)
