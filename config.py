from datetime import timedelta
import os
from urllib.parse import quote_plus
from urllib.parse import urlsplit, urlunsplit

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _build_redis_url_from_parts() -> str:
    host = (os.environ.get("REDIS_HOST") or "").strip()
    if not host:
        return ""

    port = (os.environ.get("REDIS_PORT") or "6379").strip() or "6379"
    db = (os.environ.get("REDIS_DB") or os.environ.get("CACHE_REDIS_DB") or "0").strip() or "0"
    username = (os.environ.get("REDIS_USERNAME") or "").strip()
    password = (os.environ.get("REDIS_PASSWORD") or os.environ.get("CACHE_REDIS_PASSWORD") or "").strip()
    use_tls = _env_truthy("REDIS_TLS") or _env_truthy("REDIS_SSL")
    scheme = "rediss" if use_tls else "redis"

    auth = ""
    if username and password:
        auth = f"{quote_plus(username)}:{quote_plus(password)}@"
    elif password:
        auth = f":{quote_plus(password)}@"
    elif username:
        auth = f"{quote_plus(username)}@"

    return f"{scheme}://{auth}{host}:{port}/{db}"


def _normalize_redis_url(url_value: str) -> str:
    raw_url = (url_value or "").strip()
    if not raw_url:
        return ""

    parsed = urlsplit(raw_url)
    scheme = parsed.scheme.lower()
    if scheme not in {"redis", "rediss"}:
        return raw_url

    # If URL already includes credentials, keep as-is.
    if parsed.password is not None:
        return raw_url

    # Allow separate password/username env vars (common after migrations).
    username = (os.environ.get("REDIS_USERNAME") or "").strip()
    password = (os.environ.get("REDIS_PASSWORD") or os.environ.get("CACHE_REDIS_PASSWORD") or "").strip()
    if not username and not password:
        return raw_url

    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    if username and password:
        auth = f"{quote_plus(username)}:{quote_plus(password)}@"
    elif password:
        auth = f":{quote_plus(password)}@"
    else:
        auth = f"{quote_plus(username)}@"

    return urlunsplit((parsed.scheme, f"{auth}{netloc}", parsed.path, parsed.query, parsed.fragment))


def _detect_cache_redis_url() -> str:
    direct = (os.environ.get("CACHE_REDIS_URL") or "").strip()
    if direct:
        return _normalize_redis_url(direct)

    compat = (os.environ.get("REDIS_URL") or "").strip()
    if compat:
        return _normalize_redis_url(compat)

    return _build_redis_url_from_parts()

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/study_platform")
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes", "on"}
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    _cache_redis_url = _detect_cache_redis_url()
    _cache_type = (os.environ.get("CACHE_TYPE") or "").strip()
    if not _cache_type:
        _cache_type = "RedisCache" if _cache_redis_url else "SimpleCache"

    CACHE_TYPE = _cache_type
    CACHE_DEFAULT_TIMEOUT = int(os.environ.get("CACHE_DEFAULT_TIMEOUT", "300"))
    CACHE_KEY_PREFIX = os.environ.get("CACHE_KEY_PREFIX", "studyp:")
    CACHE_REDIS_URL = _cache_redis_url or None

    # Performance tuning for first-visit page loads.
    SEND_FILE_MAX_AGE_DEFAULT = int(os.environ.get("STATIC_MAX_AGE_SECONDS", "604800"))
    COMPRESS_MIMETYPES = [
        "text/html",
        "text/css",
        "text/xml",
        "application/json",
        "application/javascript",
        "text/javascript",
        "image/svg+xml",
    ]
    COMPRESS_LEVEL = int(os.environ.get("COMPRESS_LEVEL", "6"))
    COMPRESS_MIN_SIZE = int(os.environ.get("COMPRESS_MIN_SIZE", "500"))

    ##Host videos on the server instead of google drive
    VIDEO_UPLOAD_FOLDER = os.environ.get("VIDEO_UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads", "videos"))
    AUDIO_UPLOAD_FOLDER = os.environ.get("AUDIO_UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads", "audio"))
    # Set a max upload size to prevent server memory crashes (e.g., 500 MB)
    MAX_CONTENT_LENGTH = 500 * 1024 * 1024