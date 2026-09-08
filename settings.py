from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def _load_local_env() -> None:
    """Load a small .env file without adding a runtime dependency."""
    path = BASE_DIR / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_local_env()


def _first_nonempty_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


_LEGACY_BRAVE_ENV = bool(
    os.getenv("BRAVE_SEARCH_API_KEY") or os.getenv("BRAVE_SEARCH_BASE_URL")
)
_DEFAULT_WEB_SEARCH_PROVIDER = os.getenv(
    "WEB_SEARCH_PROVIDER", "brave" if _LEGACY_BRAVE_ENV else "bocha"
)
_PROVIDER_API_KEYS = {
    "bocha": os.getenv("BOCHA_API_KEY", ""),
    "tavily": os.getenv("TAVILY_API_KEY", ""),
    "brave": os.getenv("BRAVE_SEARCH_API_KEY", ""),
}
_PROVIDER_BASE_URLS = {
    "bocha": os.getenv("BOCHA_BASE_URL", "https://api.bochaai.com/v1"),
    "tavily": os.getenv("TAVILY_BASE_URL", "https://api.tavily.com"),
    "brave": os.getenv(
        "BRAVE_SEARCH_BASE_URL", "https://api.search.brave.com/res/v1"
    ),
}
_DEFAULT_WEB_SEARCH_API_KEY = os.getenv(
    "WEB_SEARCH_API_KEY",
    _PROVIDER_API_KEYS.get(_DEFAULT_WEB_SEARCH_PROVIDER, ""),
)
_DEFAULT_WEB_SEARCH_BASE_URL = os.getenv(
    "WEB_SEARCH_BASE_URL",
    _PROVIDER_BASE_URLS.get(
        _DEFAULT_WEB_SEARCH_PROVIDER, "https://api.bochaai.com/v1"
    ),
)
_DEFAULT_MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "anthropic")
_DEFAULT_MODEL_ID = os.getenv(
    "MODEL_ID",
    "step-3.5-flash-2603" if _DEFAULT_MODEL_PROVIDER == "stepfun" else "claude-sonnet-4-6",
)
_DEFAULT_MODEL_BASE_URL = os.getenv(
    "MODEL_BASE_URL",
    os.getenv(
        "ANTHROPIC_BASE_URL",
        "https://api.stepfun.com/step_plan/v1"
        if _DEFAULT_MODEL_PROVIDER == "stepfun"
        else "https://api.anthropic.com",
    ),
)


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    base_dir: Path = BASE_DIR
    host: str = os.getenv("YINGBAN_HOST", "127.0.0.1")
    # Managed platforms such as Railway inject PORT and use it for health checks.
    # Keep YINGBAN_PORT as the local fallback for backwards compatibility.
    port: int = int(os.getenv("PORT", os.getenv("YINGBAN_PORT", "8765")))
    database_path: Path = Path(
        os.getenv("YINGBAN_DATABASE", str(BASE_DIR / "data" / "yingban.db"))
    )
    sqlite_journal_mode: str = os.getenv(
        "YINGBAN_SQLITE_JOURNAL_MODE", "WAL"
    ).strip().upper()
    sqlite_vfs: str = os.getenv("YINGBAN_SQLITE_VFS", "").strip()
    database_mount: str = os.getenv("YINGBAN_DATABASE_MOUNT", "").strip()
    movie_seed_path: Path = Path(
        os.getenv("YINGBAN_MOVIES", str(BASE_DIR / "data" / "movies.json"))
    )
    web_dir: Path = BASE_DIR / "web"
    invite_pepper: str = os.getenv(
        "YINGBAN_INVITE_PEPPER", "development-invite-pepper-change-me"
    )
    session_secret: str = os.getenv(
        "YINGBAN_SESSION_SECRET", "development-session-secret-change-me"
    )
    admin_token: str = os.getenv("YINGBAN_ADMIN_TOKEN", "")
    cookie_secure: bool = _bool_env("YINGBAN_COOKIE_SECURE", False)
    session_days: int = int(os.getenv("YINGBAN_SESSION_DAYS", "30"))
    model_provider: str = _DEFAULT_MODEL_PROVIDER
    model_api_key: str = _first_nonempty_env(
        "MODEL_API_KEY", "ANTHROPIC_API_KEY", "STEPFUN_API_KEY"
    )
    model_id: str = _DEFAULT_MODEL_ID
    model_base_url: str = _DEFAULT_MODEL_BASE_URL.rstrip("/")
    model_timeout_seconds: float = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    tmdb_api_key: str = os.getenv("TMDB_API_KEY", "")
    tmdb_base_url: str = os.getenv(
        "TMDB_BASE_URL", "https://api.themoviedb.org/3"
    ).rstrip("/")
    tmdb_image_base_url: str = os.getenv(
        "TMDB_IMAGE_BASE_URL", "https://image.tmdb.org/t/p/w500"
    ).rstrip("/")
    web_search_provider: str = _DEFAULT_WEB_SEARCH_PROVIDER
    web_search_api_key: str = _DEFAULT_WEB_SEARCH_API_KEY
    web_search_base_url: str = _DEFAULT_WEB_SEARCH_BASE_URL.rstrip("/")
    web_reader_enabled: bool = _bool_env("WEB_READER_ENABLED", True)
    web_reader_base_url: str = os.getenv(
        "WEB_READER_BASE_URL", "https://r.jina.ai"
    ).rstrip("/")
    voice_provider: str = os.getenv("VOICE_PROVIDER", "doubao")
    voice_api_key: str = _first_nonempty_env("VOICE_API_KEY", "STEPFUN_API_KEY")
    voice_app_id: str = os.getenv("VOICE_APP_ID", "")
    voice_access_token: str = os.getenv("VOICE_ACCESS_TOKEN", "")
    voice_base_url: str = os.getenv(
        "VOICE_BASE_URL", "https://openspeech.bytedance.com"
    ).rstrip("/")
    voice_stt_model: str = os.getenv("VOICE_STT_MODEL", "volc.bigasr.auc_turbo")
    voice_tts_model: str = os.getenv("VOICE_TTS_MODEL", "seed-tts-2.0")
    voice_chat_model: str = os.getenv("VOICE_CHAT_MODEL", "stepaudio-2.5-chat")
    voice_realtime_model: str = os.getenv(
        "VOICE_REALTIME_MODEL", "stepaudio-2.5-realtime"
    )
    voice_name: str = os.getenv("VOICE_NAME", "zh_female_xiaohe_uranus_bigtts")
    voice_audio_format: str = os.getenv("VOICE_AUDIO_FORMAT", "mp3")
    voice_timeout_seconds: float = float(os.getenv("VOICE_TIMEOUT_SECONDS", "60"))
    image_api_key: str = _first_nonempty_env("IMAGE_API_KEY", "STEPFUN_API_KEY")
    image_base_url: str = os.getenv(
        "IMAGE_BASE_URL", "https://api.stepfun.com/step_plan/v1"
    ).rstrip("/")
    image_model: str = os.getenv("IMAGE_MODEL", "step-image-edit-2")
    image_timeout_seconds: float = float(os.getenv("IMAGE_TIMEOUT_SECONDS", "60"))

    @property
    def demo_mode(self) -> bool:
        return not bool(self.model_api_key)

    def security_warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.invite_pepper.startswith("development-"):
            warnings.append("YINGBAN_INVITE_PEPPER is using a development default")
        if self.session_secret.startswith("development-"):
            warnings.append("YINGBAN_SESSION_SECRET is using a development default")
        if not self.admin_token:
            warnings.append("YINGBAN_ADMIN_TOKEN is empty; web admin login is disabled")
        if not self.cookie_secure:
            warnings.append("YINGBAN_COOKIE_SECURE is off; enable it behind HTTPS")
        return warnings
