import os
from urllib.parse import quote_plus

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/study_platform")
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes", "on"}

    _cache_redis_url = (os.environ.get("CACHE_REDIS_URL") or "").strip()
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
