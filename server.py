from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import mimetypes
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from agent import AgentRuntime, BoundMovieTools
from catalog import MovieCatalog, movie_recency_key, normalize
from integrations import InternetRuntime
from image_models import ImageRuntime
from product_skills import (
    BUILTIN_SKILLS_BY_KEY,
    MOVIE_DECISION_SKILL,
    STRUCTURED_REVIEW_SKILL,
    VIEWING_COGNITION_SKILL,
)
from settings import Settings, is_loopback_host
from storage import InviteError, Store
from voice import VoiceRuntime


LOG = logging.getLogger("yingban.server")
USER_COOKIE = "yingban_session"
ADMIN_COOKIE = "yingban_admin"


@dataclass
class AppContext:
    settings: Settings
    store: Store
    catalog: MovieCatalog
    agent: AgentRuntime
    internet: InternetRuntime | None = None
    voice: VoiceRuntime | None = None
    image: ImageRuntime | None = None
    local_account_id: str | None = None


class LoginLimiter:
    def __init__(self, attempts: int = 8, window_seconds: int = 300):
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            events = [stamp for stamp in self._events.get(key, []) if now - stamp < self.window_seconds]
            self._events[key] = events
            return len(events) < self.attempts

    def failure(self, key: str) -> None:
        with self._lock:
            self._events.setdefault(key, []).append(time.monotonic())

    def success(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


LOGIN_LIMITER = LoginLimiter()
EPHEMERAL_AUDIO: dict[str, tuple[float, bytes, str]] = {}
EPHEMERAL_AUDIO_LOCK = threading.Lock()


def cookie_value(headers: Any, name: str) -> str | None:
    raw = headers.get("Cookie", "")
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:  # noqa: BLE001
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel else None


def public_movie(movie: dict[str, Any]) -> dict[str, Any]:
    return BoundMovieTools._public_movie(movie)


DOUBAN_POSTER_PROXY_PATH_RE = re.compile(
    r"^/api/posters/douban/(?P<filename>p[0-9]{6,20}\.jpg)$",
    re.IGNORECASE,
)
PUBLIC_DOUBAN_POSTER_ROUTE_RE = re.compile(
    r"^/api/public/share-cards/(?P<token>[^/]+)/posters/douban/(?P<filename>[^/]+)$"
)


def bind_public_share_poster_urls(
    card: dict[str, Any], token: str
) -> dict[str, Any]:
    """Bind proxied poster URLs in a public payload to this share token."""
    content = card.get("content")
    visual = content.get("visual") if isinstance(content, dict) else None
    movies = visual.get("movies") if isinstance(visual, dict) else None
    if not isinstance(movies, list):
        return card
    encoded_token = quote(token, safe="")
    for movie in movies:
        if not isinstance(movie, dict):
            continue
        poster_url = str(movie.get("poster_url") or "")
        match = DOUBAN_POSTER_PROXY_PATH_RE.fullmatch(poster_url)
        if match is None:
            continue
        filename = match.group("filename")
        movie["poster_url"] = (
            f"/api/public/share-cards/{encoded_token}/posters/douban/{filename}"
        )
    return card


POSTER_MARKDOWN_RE = re.compile(
    r"!?\[\s*(?:海报|电影海报|poster)\s*\]\s*\(\s*https?://[^\s)]+(?:\s+[^)]*)?\)",
    re.IGNORECASE,
)
TMDB_IMAGE_URL_RE = re.compile(
    r"(?:\(\s*)?https?://image\.tmdb\.org/t/p/[^\s)\]]+(?:\s*\))?",
    re.IGNORECASE,
)


def sanitize_agent_reply(text: str) -> str:
    """Remove presentation-only poster markup from the user-facing reply."""
    cleaned = POSTER_MARKDOWN_RE.sub("", str(text))
    cleaned = TMDB_IMAGE_URL_RE.sub("", cleaned)
    cleaned = re.sub(r"(?im)^\s*\[\s*(?:海报|电影海报|poster)\s*\]\s*$", "", cleaned)
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", cleaned)
    return cleaned.strip()


SHORT_REFLECTION_SIGNALS = (
    "喜欢", "不喜欢", "还好", "还行", "一般", "不错", "不好看", "好看",
    "无聊", "没感觉", "有意思", "失望", "感动", "打动", "难受", "遗憾",
    "震撼", "看不懂", "不清楚", "推荐", "不推荐", "害怕", "温暖", "压抑",
)


def has_reflection_signal(messages: list[str]) -> bool:
    """Accept concise explicit movie reactions without treating bare assent as a note."""
    for message in messages:
        compact = re.sub(r"\s+", "", str(message))
        if len(compact) >= 12 or any(signal in compact for signal in SHORT_REFLECTION_SIGNALS):
            return True
    return False


def contains_identity_question(text: str) -> bool:
    compact = text.replace(" ", "").lower()
    return any(
        phrase in compact
        for phrase in ("你是人吗", "你是真人吗", "你是机器人吗", "你是ai吗", "你是不是ai")
    )


HIGH_RISK_PHRASES = (
    "不想活了",
    "想自杀",
    "结束生命",
    "伤害自己",
    "杀了自己",
    "伤害别人",
    "杀了他",
    "杀了她",
)


def high_risk(text: str) -> bool:
    compact = text.replace(" ", "")
    return any(phrase in compact for phrase in HIGH_RISK_PHRASES)


def movie_query_from_message(text: str) -> str:
    quoted = re.search(r"《([^》]{1,120})》", text)
    if quoted:
        return quoted.group(1).strip()
    match = re.search(r"(?:刚看完|看完了?|看了)\s*([^，。！？!?]{1,80})", text)
    if match:
        candidate = match.group(1).strip()
        candidate = re.split(r"(?:但是|不过|感觉|觉得|心里|之后)", candidate, maxsplit=1)[0]
        return candidate.strip()
    return text.strip()[:120]


def compact_voice_text(text: str, full: bool = False) -> str:
    clean = re.sub(r"\s+", " ", str(text)).strip()
    if full or len(clean) <= 140:
        return clean
    boundary = max(clean.rfind(mark, 0, 141) for mark in ("。", "！", "？", "；"))
    if boundary >= 70:
        return clean[: boundary + 1]
    return clean[:140].rstrip("，、；： ") + "。"


class YingbanHandler(BaseHTTPRequestHandler):
    server_version = "Yingban/0.1"

    @property
    def app(self) -> AppContext:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api_get(parsed.path, parse_qs(parsed.query))
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self._handle_api_post(parsed.path)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/cognition/entries/"):
            account_id = self._require_user()
            if not account_id:
                return
            entry_id = unquote(parsed.path.removeprefix("/api/cognition/entries/"))
            removed = self.app.store.delete_cognition_entry(account_id, entry_id)
            if removed:
                self.app.store.record_product_event(
                    "viewing_cognition_deleted", account_id, {"entry_id": entry_id}
                )
            self._json({"ok": removed})
            return
        if parsed.path.startswith("/api/content-drafts/"):
            account_id = self._require_user()
            if not account_id:
                return
            draft_id = unquote(parsed.path.removeprefix("/api/content-drafts/"))
            removed = self.app.store.delete_content_draft(account_id, draft_id)
            if removed:
                self.app.store.record_product_event(
                    "content_draft_deleted", account_id, {"draft_id": draft_id}
                )
            self._json({"ok": removed})
            return
        if parsed.path.startswith("/api/conversations/") and parsed.path.endswith("/summary"):
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = unquote(
                parsed.path.removeprefix("/api/conversations/").removesuffix("/summary")
            ).strip("/")
            removed = self.app.store.delete_conversation_summary(account_id, movie_id)
            if removed:
                self.app.store.record_product_event(
                    "conversation_summary_deleted", account_id, {"movie_id": movie_id}
                )
            self._json({"ok": removed})
            return
        if parsed.path.startswith("/api/share-cards/"):
            account_id = self._require_user()
            if not account_id:
                return
            card_id = unquote(parsed.path.removeprefix("/api/share-cards/"))
            removed = self.app.store.revoke_share_card(account_id, card_id)
            if removed:
                self.app.store.record_product_event(
                    "share_card_revoked", account_id, {"share_card_id": card_id}
                )
            self._json({"ok": removed})
            return
        if parsed.path.startswith("/api/onboarding/movies/"):
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = unquote(parsed.path.removeprefix("/api/onboarding/movies/"))
            removed = self.app.store.remove_onboarding_movie(account_id, movie_id)
            if removed:
                self.app.store.record_product_event(
                    "onboarding_movie_removed", account_id, {"movie_id": movie_id}
                )
            self._json({"ok": removed, **self._onboarding_payload(account_id)})
            return
        if parsed.path.startswith("/api/reflections/"):
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = unquote(parsed.path.removeprefix("/api/reflections/"))
            removed = self.app.store.delete_reflections(account_id, movie_id)
            if removed:
                self.app.store.record_product_event(
                    "reflection_deleted", account_id, {"movie_id": movie_id}
                )
            self._json({"ok": removed})
            return
        if parsed.path.startswith("/api/history/"):
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = unquote(parsed.path.removeprefix("/api/history/"))
            removed = self.app.store.remove_movie_state(account_id, movie_id)
            if removed:
                self.app.store.record_product_event(
                    "movie_state_deleted", account_id, {"movie_id": movie_id}
                )
            self._json({"ok": removed})
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        public_poster_match = PUBLIC_DOUBAN_POSTER_ROUTE_RE.fullmatch(path)
        if public_poster_match is not None:
            token = unquote(public_poster_match.group("token"))
            filename = unquote(public_poster_match.group("filename"))
            poster_url = "/api/posters/douban/" + filename
            if not self.app.store.public_share_uses_poster_url(token, poster_url):
                self._json({"error": "海报不存在"}, HTTPStatus.NOT_FOUND)
                return
            if not self.app.store.has_poster_url(poster_url):
                self._json({"error": "海报不存在"}, HTTPStatus.NOT_FOUND)
                return
            if self.app.internet is None:
                self._json({"error": "海报服务暂时不可用"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                content, content_type = self.app.internet.fetch_douban_poster(filename)
            except ValueError:
                self._json({"error": "海报地址无效"}, HTTPStatus.BAD_REQUEST)
                return
            except RuntimeError:
                self._json({"error": "海报暂时不可用"}, HTTPStatus.BAD_GATEWAY)
                return
            self._binary(content, content_type, "no-store")
            return

        if path.startswith("/api/posters/douban/"):
            filename = unquote(path.removeprefix("/api/posters/douban/"))
            poster_url = "/api/posters/douban/" + filename
            account_id = self._account_id()
            if not account_id or not self.app.store.has_poster_url(poster_url):
                self._json({"error": "海报不存在"}, HTTPStatus.NOT_FOUND)
                return
            if self.app.internet is None:
                self._json({"error": "海报服务暂时不可用"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                content, content_type = self.app.internet.fetch_douban_poster(filename)
            except ValueError:
                self._json({"error": "海报地址无效"}, HTTPStatus.BAD_REQUEST)
                return
            except RuntimeError:
                self._json({"error": "海报暂时不可用"}, HTTPStatus.BAD_GATEWAY)
                return
            self._binary(content, content_type, "private, max-age=86400")
            return

        if path == "/api/health":
            mode = "demo" if self.app.agent.model_config().demo_mode else "model"
            self._json(
                {
                    "ok": True,
                    "mode": mode,
                    "local_open_access": self._local_open_access(),
                }
            )
            return

        if path == "/api/me":
            account_id = self._account_id()
            if not account_id:
                self._json({"authenticated": False})
                return
            watched_count = len(self.app.store.watched_ids(account_id))
            profile = self.app.store.account_profile(account_id)
            selected_count = len(self.app.store.onboarding_movies(account_id))
            self._json(
                {
                    "authenticated": True,
                    "local_open_access": self._local_open_access(),
                    "account": {"id_hint": account_id[-6:], "watched_count": watched_count},
                    "demo_mode": self.app.agent.model_config().demo_mode,
                    "openings": self.app.agent.openings(),
                    "voice_available": bool(
                        self.app.voice and self.app.voice.config().enabled
                    ),
                    "onboarding": {
                        "status": profile["onboarding_status"],
                        "selected_count": selected_count,
                    },
                    "taste_profile": {"version": profile["taste_version"]},
                    "reflection_preferences": {
                        "auto_generate_drafts": profile["auto_generate_reflection_drafts"]
                    },
                    "weekly_recommendations": {
                        "enabled": profile["weekly_recommendations_enabled"]
                    },
                    "skills": [
                        {
                            "key": skill["skill_key"],
                            "name": skill["name"],
                            "description": skill["description"],
                            "module": skill["module"],
                            "activation_mode": skill["activation_mode"],
                            "enabled": skill["enabled"],
                            "active_version": skill["active_version"],
                        }
                        for skill in self.app.store.list_skills()
                        if skill["enabled"]
                    ],
                }
            )
            return

        if path.startswith("/api/cognition/") and not path.startswith("/api/cognition/entries/"):
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = unquote(path.removeprefix("/api/cognition/"))
            movie = self.app.store.movie(movie_id)
            if movie is None:
                self._json({"error": "movie not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json({"movie": public_movie(movie), **self.app.store.cognition_bundle(account_id, movie_id)})
            return

        if path == "/api/content-drafts":
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = str(query.get("movie_id", [""])[0])
            movie = self.app.store.movie(movie_id)
            if movie is None:
                self._json({"error": "movie not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json({"movie": public_movie(movie), **self.app.store.content_draft_bundle(account_id, movie_id)})
            return

        if path.startswith("/api/public/share-cards/"):
            token = unquote(path.removeprefix("/api/public/share-cards/"))
            card = self.app.store.public_share_card(token)
            if card is None:
                self._json({"error": "分享不存在、已撤回或已过期"}, HTTPStatus.NOT_FOUND)
                return
            self._json({"card": bind_public_share_poster_urls(card, token)})
            return

        if path.startswith("/api/conversations/") and path.endswith("/summary"):
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = unquote(
                path.removeprefix("/api/conversations/").removesuffix("/summary")
            ).strip("/")
            movie = self.app.store.movie(movie_id)
            if movie is None:
                self._json({"error": "movie not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(
                {
                    "movie": public_movie(movie),
                    "summary": self.app.store.conversation_summary(account_id, movie_id),
                }
            )
            return

        if path == "/api/weekly-recommendation":
            account_id = self._require_user()
            if not account_id:
                return
            self._json(self._weekly_recommendation_payload(account_id, mark_viewed=True))
            return

        if path == "/api/box-office":
            account_id = self._require_user()
            if not account_id:
                return
            self._json(self._daily_box_office_payload())
            return

        if path.startswith("/api/recaps/monthly/"):
            account_id = self._require_user()
            if not account_id:
                return
            month_key = unquote(path.removeprefix("/api/recaps/monthly/"))
            recap = self.app.store.monthly_recap(account_id, month_key)
            self._json({"recap": recap, "empty": recap is None})
            return

        if path.startswith("/api/jobs/"):
            account_id = self._require_user()
            if not account_id:
                return
            job_id = unquote(path.removeprefix("/api/jobs/"))
            job = self.app.store.background_job(account_id, job_id)
            if job is None:
                self._json({"error": "后台任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            if job["status"] == "succeeded" and job["job_type"] == "voice":
                with EPHEMERAL_AUDIO_LOCK:
                    cached = EPHEMERAL_AUDIO.get(job_id)
                    if cached and cached[0] > time.time():
                        job["result"] = {
                            **job["result"],
                            "audio_base64": base64.b64encode(cached[1]).decode("ascii"),
                            "content_type": cached[2],
                        }
                    else:
                        job["result"] = {**job["result"], "audio_available": False}
            self._json({"job": job})
            return

        if path == "/api/onboarding":
            account_id = self._require_user()
            if not account_id:
                return
            self._json(self._onboarding_payload(account_id))
            return

        if path == "/api/onboarding/candidates":
            account_id = self._require_user()
            if not account_id:
                return
            query_text = str(query.get("query", [""])[0]).strip()
            search_status = "ready"
            if query_text:
                items = self.app.catalog.search(query_text, limit=18, newest_first=True)
                if items and self.app.internet is not None:
                    items = self.app.internet.hydrate_douban_posters(items, limit=8)
                elif self.app.internet is not None:
                    try:
                        items = self.app.internet.search_movies(query_text)
                    except Exception as error:  # noqa: BLE001
                        LOG.warning("onboarding movie search failed: %s", type(error).__name__)
                        search_status = "unavailable"
            else:
                items = self._diverse_onboarding_candidates()
            if query_text:
                items = sorted(items, key=movie_recency_key)
            selected_ids = {item["id"] for item in self.app.store.onboarding_movies(account_id)}
            self._json(
                {
                    "items": [
                        public_movie(item)
                        for item in items
                        if item["id"] not in selected_ids
                    ],
                    "search_status": search_status,
                }
            )
            return

        if path == "/api/taste-profile":
            account_id = self._require_user()
            if not account_id:
                return
            profile = self.app.store.account_profile(account_id)
            self.app.store.record_product_event(
                "taste_profile_viewed", account_id, {"version": profile["taste_version"]}
            )
            self._json({"profile": profile})
            return

        if path.startswith("/api/reflections/"):
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = unquote(path.removeprefix("/api/reflections/"))
            movie = self.app.store.movie(movie_id)
            if movie is None:
                self._json({"error": "movie not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json({"movie": public_movie(movie), **self.app.store.reflection_bundle(account_id, movie_id)})
            return

        if path == "/api/history":
            account_id = self._require_user()
            if not account_id:
                return
            state = query.get("state", [None])[0]
            if state not in {None, "watched", "watchlist", "disliked"}:
                self._json({"error": "invalid state"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                cursor = int(query.get("cursor", ["0"])[0])
                limit = int(query.get("limit", ["12"])[0])
            except ValueError:
                self._json({"error": "invalid pagination"}, HTTPStatus.BAD_REQUEST)
                return
            page = self.app.store.movie_states_page(account_id, state, cursor, limit)
            missing = [item["movie_id"] for item in page["items"] if not item.get("poster_url")][:5]
            if missing and self.app.internet is not None:
                job = self.app.store.create_background_job(
                    account_id, "poster_hydration", {"movie_ids": missing}
                )
                page["poster_job_id"] = job["id"]
                self._start_poster_job(account_id, job["id"], missing)
            self._json(page)
            return

        if path == "/api/movies":
            account_id = self._require_user()
            if not account_id:
                return
            query_text = str(query.get("query", [""])[0]).strip()
            items = self.app.catalog.search(query_text, newest_first=True)
            search_status = "ready"
            if items and self.app.internet is not None:
                items = self.app.internet.hydrate_douban_posters(items, limit=8)
            elif self.app.internet is not None:
                try:
                    items = self.app.internet.search_movies(query_text)
                except Exception as error:  # noqa: BLE001
                    LOG.warning("external movie search failed: %s", type(error).__name__)
                    search_status = "unavailable"
            self._json(
                {
                    "items": [public_movie(item) for item in sorted(items, key=movie_recency_key)],
                    "search_status": search_status,
                }
            )
            return

        if path == "/api/admin/invites":
            if not self._require_admin():
                return
            database_path = self.app.settings.database_path.expanduser().resolve()
            temporary_roots = (Path("/tmp"), Path("/private/tmp"), Path("/var/tmp"))
            persistent = not any(
                database_path == root or database_path.is_relative_to(root)
                for root in temporary_roots
            )
            self._json({
                "items": self.app.store.list_invites(),
                "storage": {"persistent": persistent},
            })
            return

        if path.startswith("/api/admin/invites/") and path.endswith("/conversations"):
            if not self._require_admin():
                return
            invite_id = unquote(
                path.removeprefix("/api/admin/invites/").removesuffix("/conversations")
            ).strip("/")
            try:
                result = self.app.store.invite_conversations(invite_id)
            except InviteError as error:
                self._json({"error": str(error)}, HTTPStatus.NOT_FOUND)
                return
            self._json(result)
            return

        if path == "/api/admin/agent-config":
            if not self._require_admin():
                return
            result = self.app.agent.public_configuration()
            result["internet"] = self.app.internet.public_config() if self.app.internet else {}
            result["voice"] = self.app.voice.public_config() if self.app.voice else {}
            result["image"] = self.app.image.public_config() if self.app.image else {}
            result["access"] = {"local_open_access": self._local_open_access()}
            self._json(result)
            return

        if path == "/api/admin/skills":
            if not self._require_admin():
                return
            self._json(
                {
                    "items": self.app.store.list_skills(),
                    "defaults": {
                        key: {
                            "instructions": value["instructions"],
                            "input_contract": value["input_contract"],
                            "output_contract": value["output_contract"],
                        }
                        for key, value in BUILTIN_SKILLS_BY_KEY.items()
                    },
                }
            )
            return

        if path == "/api/admin/metrics/summary":
            if not self._require_admin():
                return
            self._json(self.app.store.metrics_summary())
            return

        if path == "/api/admin/usage-pricing":
            if not self._require_admin():
                return
            self._json(self.app.store.usage_pricing())
            return

        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _handle_api_post(self, path: str) -> None:
        try:
            payload = self._read_json(12_000_000 if path == "/api/voice/transcribe" else 1_000_000)
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/auth/login":
            self._login(payload)
            return
        if path == "/api/auth/logout":
            token = cookie_value(self.headers, USER_COOKIE)
            account_id = self.app.store.account_for_session(token)
            if account_id:
                self.app.store.record_product_event("logout", account_id)
            self.app.store.logout(token)
            self._json({"ok": True}, cookies=[self._expired_cookie(USER_COOKIE)])
            return
        if path == "/api/onboarding/movies":
            account_id = self._require_user()
            if not account_id:
                return
            try:
                item = self.app.store.add_onboarding_movie(
                    account_id,
                    str(payload.get("movie_id", "")),
                    str(payload.get("sentiment", "")),
                )
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            event_name = "onboarding_feedback_changed" if payload.get("updating") else "onboarding_movie_added"
            self.app.store.record_product_event(
                event_name, account_id,
                {"movie_id": item["id"], "sentiment": item["sentiment"]},
            )
            self._json({"ok": True, "item": public_movie(item), **self._onboarding_payload(account_id)})
            return
        if path == "/api/onboarding/complete":
            account_id = self._require_user()
            if not account_id:
                return
            try:
                profile = self.app.store.complete_onboarding(account_id)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.app.store.record_product_event(
                "onboarding_completed", account_id, {"selected_count": 5}
            )
            self.app.store.record_product_event(
                "taste_profile_generated", account_id, {"version": profile["taste_version"]}
            )
            self.app.store.record_model_usage(
                "taste_profile", "deterministic", "evidence-profile-v1", True, 0, account_id
            )
            self._json({"ok": True, "profile": profile})
            return
        if path == "/api/taste-profile/corrections":
            account_id = self._require_user()
            if not account_id:
                return
            try:
                profile = self.app.store.correct_taste_dimension(
                    account_id, str(payload.get("dimension_id", ""))
                )
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.app.store.record_product_event(
                "taste_profile_corrected", account_id,
                {"dimension_id": str(payload.get("dimension_id", "")), "version": profile["taste_version"]},
            )
            self._json({"ok": True, "profile": profile})
            return
        if path == "/api/reflection-preferences":
            account_id = self._require_user()
            if not account_id:
                return
            profile = self.app.store.set_reflection_preference(
                account_id, bool(payload.get("auto_generate_drafts", True))
            )
            self._json({"ok": True, "auto_generate_drafts": profile["auto_generate_reflection_drafts"]})
            return
        if path.startswith("/api/cognition/") and not path.startswith("/api/cognition/entries/"):
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = unquote(path.removeprefix("/api/cognition/"))
            if self.app.store.movie(movie_id) is None:
                self._json({"error": "movie not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                bundle = self.app.store.save_cognition_entry(
                    account_id,
                    movie_id,
                    int(payload.get("viewing_round", 1)),
                    str(payload.get("stage", "first_impression")),
                    str(payload.get("raw_impression", "")),
                    str(payload.get("synthesis", "")),
                    payload.get("dimensions", []) if isinstance(payload.get("dimensions"), list) else [],
                    str(payload.get("watched_at", "")) or None,
                    str(payload.get("edition", "")),
                )
            except (TypeError, ValueError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.app.store.record_product_event(
                "viewing_cognition_saved", account_id,
                {"movie_id": movie_id, "stage": str(payload.get("stage", ""))},
            )
            self._json({"ok": True, "movie": public_movie(self.app.store.movie(movie_id) or {}), **bundle})
            return
        if path == "/api/content-drafts":
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = str(payload.get("movie_id", ""))
            if self.app.store.movie(movie_id) is None:
                self._json({"error": "movie not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                bundle = self.app.store.create_content_draft(
                    account_id,
                    movie_id,
                    str(payload.get("content_scene", "xiaohongshu")),
                    str(payload.get("content", "")),
                    str(payload.get("source_material", "")),
                )
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.app.store.record_product_event(
                "content_draft_saved", account_id,
                {"movie_id": movie_id, "content_scene": str(payload.get("content_scene", ""))},
            )
            self._json({"ok": True, "movie": public_movie(self.app.store.movie(movie_id) or {}), **bundle})
            return
        if path == "/api/weekly-recommendation/preferences":
            account_id = self._require_user()
            if not account_id:
                return
            enabled = bool(payload.get("enabled", True))
            profile = self.app.store.set_weekly_recommendations_preference(account_id, enabled)
            self.app.store.record_product_event(
                "weekly_recommendation_preference_changed", account_id, {"enabled": enabled}
            )
            self._json({"ok": True, "enabled": profile["weekly_recommendations_enabled"]})
            return
        if path.startswith("/api/conversations/") and path.endswith("/summary"):
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = unquote(
                path.removeprefix("/api/conversations/").removesuffix("/summary")
            ).strip("/")
            try:
                summary = self.app.store.save_conversation_summary(
                    account_id,
                    movie_id,
                    str(payload.get("summary", "")),
                    payload.get("topics", []) if isinstance(payload.get("topics"), list) else [],
                    payload.get("open_questions", [])
                    if isinstance(payload.get("open_questions"), list)
                    else [],
                    bool(payload.get("spoilers_allowed", False)),
                )
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.app.store.record_product_event(
                "conversation_summary_saved", account_id, {"movie_id": movie_id}
            )
            self._json({"ok": True, "summary": summary})
            return
        if path == "/api/weekly-recommendation/refresh":
            account_id = self._require_user()
            if not account_id:
                return
            if not self.app.store.account_profile(account_id)["weekly_recommendations_enabled"]:
                self._json(
                    {"error": "本周建议已停用，请先在连续性设置中重新开启"},
                    HTTPStatus.CONFLICT,
                )
                return
            refreshes = self.app.store.recent_product_event_count(
                account_id,
                "weekly_recommendation_refreshed",
                since=datetime.now(UTC) - timedelta(hours=1),
            )
            if refreshes >= 3:
                self._json(
                    {"error": "每小时最多换一批 3 次，稍后再试", "retry_after_seconds": 3600},
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
                return
            direction = str(payload.get("direction", "")).strip()[:300]
            recommendation = self._generate_weekly_recommendation(
                account_id, direction=direction, replace=True
            )
            self.app.store.record_product_event(
                "weekly_recommendation_refreshed", account_id,
                {"has_direction": bool(direction), "movie_count": len(recommendation.get("movies", []))},
            )
            self._json({"status": "ready", "recommendation": recommendation})
            return
        if path == "/api/weekly-recommendation/dismiss":
            account_id = self._require_user()
            if not account_id:
                return
            permanently = bool(payload.get("permanently", False))
            self.app.store.dismiss_weekly_recommendation(account_id, permanently=permanently)
            self.app.store.record_product_event(
                "weekly_recommendation_dismissed", account_id, {"permanently": permanently}
            )
            self._json({"ok": True, "permanently": permanently})
            return
        if path.startswith("/api/watchlist/") and path.endswith("/follow-up"):
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = unquote(
                path.removeprefix("/api/watchlist/").removesuffix("/follow-up")
            ).strip("/")
            try:
                result = self.app.store.follow_up_watchlist(
                    account_id, movie_id, str(payload.get("action", "")),
                    str(payload.get("reason_code", "")) or None,
                )
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.app.store.record_product_event(
                "watchlist_follow_up", account_id,
                {"movie_id": movie_id, "action": result["action"], "reason_code": result["reason_code"]},
            )
            self._json({"ok": True, **result})
            return
        if path.startswith("/api/recaps/monthly/"):
            account_id = self._require_user()
            if not account_id:
                return
            suffix = path.removeprefix("/api/recaps/monthly/")
            parts = [unquote(part) for part in suffix.split("/") if part]
            if len(parts) != 2 or parts[1] not in {"generate", "confirm"}:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                if parts[1] == "generate":
                    recap = self.app.store.generate_monthly_recap(account_id, parts[0])
                    event_name = "monthly_recap_generated"
                else:
                    recap = self.app.store.confirm_monthly_recap(
                        account_id, parts[0], str(payload.get("next_direction", ""))
                    )
                    event_name = "monthly_recap_confirmed"
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.app.store.record_product_event(event_name, account_id, {"month": parts[0]})
            self._json({"ok": True, "recap": recap})
            return
        if path == "/api/share-cards":
            account_id = self._require_user()
            if not account_id:
                return
            try:
                source_type = str(payload.get("source_type", ""))
                source_id = str(payload.get("source_id", ""))
                content = self._validated_share_content(account_id, source_type, source_id, payload)
                source_id = str(payload.get("source_id", source_id))
                card = self.app.store.create_share_card(
                    account_id, source_type, source_id, content,
                    int(payload.get("expires_days", 7)),
                )
            except (TypeError, ValueError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.app.store.record_product_event(
                "share_card_created", account_id,
                {"share_card_id": card["id"], "source_type": source_type},
            )
            self._json(
                {
                    "ok": True,
                    "card": card,
                    "share_path": f"/share?token={card['public_token']}",
                },
                HTTPStatus.CREATED,
            )
            return
        if path == "/api/jobs/voice":
            account_id = self._require_user()
            if not account_id:
                return
            if self.app.voice is None:
                self._json({"error": "语音服务不可用"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            text_value = str(payload.get("text", "")).strip()
            full = bool(payload.get("full", False))
            if not text_value:
                self._json({"error": "没有可朗读的文字"}, HTTPStatus.BAD_REQUEST)
                return
            job = self.app.store.create_background_job(
                account_id, "voice", {"mode": "full" if full else "summary", "characters": len(text_value)},
                expires_minutes=10,
            )
            self._start_voice_job(account_id, job["id"], text_value, full)
            self._json({"job": job}, HTTPStatus.ACCEPTED)
            return
        if path.startswith("/api/recommendations/") and path.endswith("/feedback"):
            account_id = self._require_user()
            if not account_id:
                return
            impression_id = unquote(path.removeprefix("/api/recommendations/").removesuffix("/feedback"))
            try:
                feedback = self.app.store.record_recommendation_feedback(
                    account_id, impression_id, str(payload.get("action", "")),
                    str(payload.get("reason_code", "")) or None,
                )
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.app.store.record_product_event(
                "recommendation_feedback", account_id,
                {"impression_id": impression_id, "movie_id": feedback["movie_id"], "action": feedback["action"]},
            )
            self._json({"ok": True, "feedback": feedback})
            return
        if path.startswith("/api/reflections/"):
            account_id = self._require_user()
            if not account_id:
                return
            suffix = path.removeprefix("/api/reflections/")
            parts = [unquote(part) for part in suffix.split("/") if part]
            if not parts:
                self._json({"error": "movie not found"}, HTTPStatus.NOT_FOUND)
                return
            movie_id = parts[0]
            action = parts[1] if len(parts) > 1 else "save"
            if self.app.store.movie(movie_id) is None:
                self._json({"error": "movie not found"}, HTTPStatus.NOT_FOUND)
                return
            if action == "generate" and payload.get("async"):
                history = payload.get("history", []) if isinstance(payload.get("history"), list) else []
                job = self.app.store.create_background_job(
                    account_id,
                    "reflection",
                    {"movie_id": movie_id, "history_items": len(history)},
                    expires_minutes=20,
                )
                self._start_reflection_job(
                    account_id, job["id"], movie_id, history, bool(payload.get("regenerate"))
                )
                self.app.store.record_product_event(
                    "reflection_job_created", account_id, {"movie_id": movie_id, "job_id": job["id"]}
                )
                self._json({"job": job}, HTTPStatus.ACCEPTED)
                return
            try:
                if action == "generate":
                    bundle = self.app.store.reflection_bundle(account_id, movie_id)
                    base = bundle["confirmed"] or bundle["current"] or {}
                    history = payload.get("history", []) if isinstance(payload.get("history"), list) else []
                    user_messages = [
                        str(item.get("content", "")).strip() for item in history
                        if isinstance(item, dict) and item.get("role") == "user"
                    ]
                    if not has_reflection_signal(user_messages) and payload.get("regenerate") and base.get("content"):
                        user_messages = [str(base["content"])]
                        history = [{"role": "user", "content": str(base["content"])}]
                    if not has_reflection_signal(user_messages):
                        raise ValueError("还需要至少一句更具体的电影感受，才能整理草稿")
                    self.app.store.record_product_event("reflection_requested", account_id, {"movie_id": movie_id})
                    draft = self.app.agent.summarize_reflection(
                        self.app.store.movie(movie_id) or {}, str(base.get("content", "")),
                        history, user_messages[-1],
                        next((str(item.get("content", "")) for item in reversed(history) if isinstance(item, dict) and item.get("role") == "assistant"), ""),
                        account_id,
                    )
                    if not draft or draft == str(base.get("content", "")):
                        raise ValueError("这次还没有足够的新电影感受可整理")
                    current = self.app.store.create_reflection_version(
                        account_id, movie_id, draft, "ai", "draft",
                        int(base["version"]) if base.get("version") else None,
                    )
                    event_name = "reflection_draft_created"
                elif action == "save":
                    based_on = payload.get("based_on_version")
                    current = self.app.store.create_reflection_version(
                        account_id, movie_id, str(payload.get("content", "")),
                        "ai_then_user" if based_on else "user", "confirmed",
                        int(based_on) if based_on is not None else None,
                    )
                    event_name = "reflection_edited"
                elif action in {"confirm", "lock", "unlock"}:
                    bundle = self.app.store.set_reflection_status(
                        account_id, movie_id, int(payload.get("version", 0)), action
                    )
                    current = bundle["current"] or {}
                    event_name = {"confirm": "reflection_confirmed", "lock": "reflection_locked", "unlock": "reflection_unlocked"}[action]
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return
            except (TypeError, ValueError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.app.store.record_product_event(event_name, account_id, {"movie_id": movie_id, "version": current.get("version")})
            self._json({"ok": True, **self.app.store.reflection_bundle(account_id, movie_id)})
            return
        if path == "/api/history":
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = str(payload.get("movie_id", ""))
            state = str(payload.get("state", "watched"))
            source = str(payload.get("source", "manual"))
            if not self.app.store.movie(movie_id):
                self._json({"error": "movie not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                previous = self.app.store.get_movie_state(account_id, movie_id)
                item = self.app.store.set_movie_state(account_id, movie_id, state, source)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.app.store.record_product_event(
                "movie_state_changed" if previous else "movie_state_created",
                account_id, {"movie_id": movie_id, "state": state, "source": source},
            )
            self._json({"ok": True, "item": item})
            return
        if path == "/api/chat":
            account_id = self._require_user()
            if not account_id:
                return
            self._chat(account_id, payload)
            return
        if path == "/api/voice/transcribe":
            account_id = self._require_user()
            if not account_id:
                return
            if self.app.voice is None:
                self._json({"error": "语音服务不可用"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            started = time.perf_counter()
            try:
                encoded = str(payload.get("audio_base64", ""))
                audio = base64.b64decode(encoded, validate=True)
                text = self.app.voice.transcribe(audio, str(payload.get("mime_type", "audio/webm")))
            except (ValueError, RuntimeError, binascii.Error) as error:
                self._record_voice_usage("stt", account_id, False, started, type(error).__name__)
                self.app.store.record_product_event("voice_transcription_failed", account_id, {"error_category": type(error).__name__})
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            except Exception as error:  # noqa: BLE001
                self._record_voice_usage("stt", account_id, False, started, type(error).__name__)
                self.app.store.record_product_event("voice_transcription_failed", account_id, {"error_category": type(error).__name__})
                LOG.warning("voice transcription failed: %s", type(error).__name__)
                self._json({"error": "语音识别暂时不可用"}, HTTPStatus.BAD_GATEWAY)
                return
            self._record_voice_usage("stt", account_id, True, started)
            self.app.store.record_product_event("voice_transcription_succeeded", account_id, {"characters": len(text)})
            self._json({"text": text})
            return
        if path == "/api/voice/synthesize":
            account_id = self._require_user()
            if not account_id:
                return
            if self.app.voice is None:
                self._json({"error": "语音服务不可用"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            started = time.perf_counter()
            self.app.store.record_product_event("voice_reply_requested", account_id)
            try:
                audio, content_type = self.app.voice.synthesize(str(payload.get("text", "")))
            except ValueError as error:
                self._record_voice_usage("tts", account_id, False, started, type(error).__name__)
                self.app.store.record_product_event("voice_reply_failed", account_id, {"error_category": type(error).__name__})
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            except Exception as error:  # noqa: BLE001
                self._record_voice_usage("tts", account_id, False, started, type(error).__name__)
                self.app.store.record_product_event("voice_reply_failed", account_id, {"error_category": type(error).__name__})
                LOG.warning("voice synthesis failed: %s", type(error).__name__)
                self._json({"error": "语音回复暂时不可用"}, HTTPStatus.BAD_GATEWAY)
                return
            self._record_voice_usage("tts", account_id, True, started)
            self.app.store.record_product_event("voice_reply_succeeded", account_id, {"bytes": len(audio)})
            self._json(
                {
                    "audio_base64": base64.b64encode(audio).decode("ascii"),
                    "content_type": content_type,
                }
            )
            return

        if path == "/api/admin/login":
            self._admin_login(payload)
            return
        if path == "/api/admin/logout":
            self._json({"ok": True}, cookies=[self._expired_cookie(ADMIN_COOKIE)])
            return
        if path == "/api/admin/invites/generate":
            if not self._require_admin():
                return
            try:
                count = int(payload.get("count", 1))
                codes = self.app.store.generate_invites(count, str(payload.get("note", "")))
            except (TypeError, ValueError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"codes": codes})
            return
        if path == "/api/conversations/sync":
            account_id = self._require_user()
            if not account_id:
                return
            try:
                record = self.app.store.save_conversation_record(
                    account_id,
                    str(payload.get("conversation_id", "")),
                    str(payload.get("movie_id", "")),
                    payload.get("messages", []),
                    str(payload.get("skill_key", "")) or None,
                )
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "record": record})
            return
        if path.startswith("/api/admin/skills/"):
            if not self._require_admin():
                return
            suffix = path.removeprefix("/api/admin/skills/")
            parts = [unquote(part) for part in suffix.split("/") if part]
            if len(parts) != 2:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            skill_key, action = parts
            try:
                if action == "draft":
                    skill = self.app.store.save_skill_draft(
                        skill_key,
                        str(payload.get("instructions", "")),
                        payload.get("input_contract", {}) if isinstance(payload.get("input_contract"), dict) else {},
                        payload.get("output_contract", {}) if isinstance(payload.get("output_contract"), dict) else {},
                        str(payload.get("change_note", "")),
                    )
                elif action == "publish":
                    skill = self.app.store.publish_skill_draft(skill_key)
                elif action == "rollback":
                    skill = self.app.store.rollback_skill(skill_key)
                elif action == "enabled":
                    skill = self.app.store.set_skill_enabled(skill_key, bool(payload.get("enabled")))
                elif action == "restore":
                    default = BUILTIN_SKILLS_BY_KEY.get(skill_key)
                    if default is None:
                        raise ValueError("Skill 不存在")
                    skill = self.app.store.save_skill_draft(
                        skill_key,
                        str(default["instructions"]),
                        dict(default["input_contract"]),
                        dict(default["output_contract"]),
                        "恢复内置默认版本",
                    )
                elif action == "preview":
                    self._json({"ok": True, "preview": self.app.agent.skill_preview(skill_key)})
                    return
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.app.store.record_product_event(
                "admin_skill_changed",
                properties={"skill_key": skill_key, "action": action},
            )
            self._json({"ok": True, "skill": skill})
            return
        if path == "/api/admin/agent-config/model":
            if not self._require_admin():
                return
            try:
                model = self.app.agent.save_model_config(payload)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "model": model})
            return
        if path == "/api/admin/usage-pricing":
            if not self._require_admin():
                return
            try:
                pricing = self.app.store.save_usage_pricing(payload)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "pricing": pricing})
            return
        if path == "/api/admin/agent-config/prompts":
            if not self._require_admin():
                return
            try:
                prompts = self.app.agent.save_prompts(payload)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "prompts": prompts})
            return
        if path == "/api/admin/agent-config/openings":
            if not self._require_admin():
                return
            try:
                openings = self.app.agent.save_openings(payload)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "openings": openings})
            return
        if path == "/api/admin/agent-config/test":
            if not self._require_admin():
                return
            started = time.perf_counter()
            try:
                reply = self.app.agent.test_connection(payload)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            except Exception as error:  # noqa: BLE001
                LOG.warning("model connection test failed: %s", type(error).__name__)
                self._json(
                    {"error": str(error), "detail": type(error).__name__},
                    HTTPStatus.BAD_GATEWAY,
                )
                return
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            self._json({"ok": True, "reply": reply, "elapsed_ms": elapsed_ms})
            return
        if path == "/api/admin/internet-config":
            if not self._require_admin():
                return
            if self.app.internet is None:
                self._json({"error": "联网服务不可用"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                config = self.app.internet.save_config(payload)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "internet": config})
            return
        if path == "/api/admin/internet-config/test":
            if not self._require_admin():
                return
            if self.app.internet is None:
                self._json({"error": "联网服务不可用"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                result = self.app.internet.test_connection(payload, str(payload.get("target", "")))
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            except Exception as error:  # noqa: BLE001
                LOG.warning("internet connection test failed: %s", type(error).__name__)
                self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
                return
            self._json({"ok": True, **result})
            return
        if path == "/api/admin/voice-config":
            if not self._require_admin():
                return
            if self.app.voice is None:
                self._json({"error": "语音服务不可用"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                config = self.app.voice.save_config(payload)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "voice": config})
            return
        if path == "/api/admin/voice-config/test":
            if not self._require_admin():
                return
            if self.app.voice is None:
                self._json({"error": "语音服务不可用"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                result = self.app.voice.test_connection(payload)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            except Exception as error:  # noqa: BLE001
                LOG.warning("voice connection test failed: %s", type(error).__name__)
                self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
                return
            self._json({"ok": True, **result})
            return
        if path == "/api/admin/image-config":
            if not self._require_admin():
                return
            if self.app.image is None:
                self._json({"error": "图像服务不可用"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                config = self.app.image.save_config(payload)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "image": config})
            return
        if path == "/api/admin/image-config/test":
            if not self._require_admin():
                return
            if self.app.image is None:
                self._json({"error": "图像服务不可用"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                result = self.app.image.test_connection(payload)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            except Exception as error:  # noqa: BLE001
                LOG.warning("image model connection test failed: %s", type(error).__name__)
                self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
                return
            self._json({"ok": True, **result})
            return
        if path.startswith("/api/admin/invites/") and path.endswith("/revoke"):
            if not self._require_admin():
                return
            invite_id = unquote(path.removeprefix("/api/admin/invites/").removesuffix("/revoke"))
            try:
                self.app.store.revoke_invite(invite_id)
            except InviteError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True})
            return
        if path.startswith("/api/admin/invites/") and path.endswith("/rotate"):
            if not self._require_admin():
                return
            invite_id = unquote(path.removeprefix("/api/admin/invites/").removesuffix("/rotate"))
            try:
                code = self.app.store.rotate_invite(invite_id)
            except InviteError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"code": code})
            return
        if path.startswith("/api/admin/invites/") and path.endswith("/code"):
            if not self._require_admin():
                return
            invite_id = unquote(
                path.removeprefix("/api/admin/invites/").removesuffix("/code")
            ).strip("/")
            try:
                code = self.app.store.invite_code_for_admin(invite_id)
            except InviteError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"code": code})
            return
        if path.startswith("/api/admin/invites/") and path.endswith("/note"):
            if not self._require_admin():
                return
            invite_id = unquote(
                path.removeprefix("/api/admin/invites/").removesuffix("/note")
            ).strip("/")
            try:
                invite = self.app.store.update_invite_note(
                    invite_id, str(payload.get("note", ""))
                )
            except (InviteError, ValueError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"invite": invite})
            return

        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _login(self, payload: dict[str, Any]) -> None:
        remote = self.client_address[0]
        if not LOGIN_LIMITER.allowed(remote):
            self._json({"error": "尝试次数过多，请稍后再试"}, HTTPStatus.TOO_MANY_REQUESTS)
            return
        code = str(payload.get("invite_code", "")).strip()
        if not code:
            self._json({"error": "请输入邀请码"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            account_id, token = self.app.store.login_with_invite(
                code, self.app.settings.session_days
            )
        except InviteError as error:
            LOGIN_LIMITER.failure(remote)
            self.app.store.record_product_event("login_failed", properties={"error_category": "invalid_invite"})
            self._json({"error": str(error)}, HTTPStatus.UNAUTHORIZED)
            return
        LOGIN_LIMITER.success(remote)
        profile = self.app.store.account_profile(account_id)
        if profile["onboarding_status"] == "not_started":
            self.app.store.record_product_event("onboarding_started", account_id)
        self.app.store.record_product_event("login_succeeded", account_id)
        cookie = self._cookie(USER_COOKIE, token, self.app.settings.session_days * 86400)
        self._json({"ok": True, "account": {"id_hint": account_id[-6:]}}, cookies=[cookie])

    def _admin_login(self, payload: dict[str, Any]) -> None:
        configured = self.app.settings.admin_token
        provided = str(payload.get("admin_token", ""))
        if not configured:
            self._json({"error": "管理员网页登录尚未配置"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if not hmac.compare_digest(configured, provided):
            self._json({"error": "管理员口令错误"}, HTTPStatus.UNAUTHORIZED)
            return
        token = self.app.store.create_admin_session()
        self._json({"ok": True}, cookies=[self._cookie(ADMIN_COOKIE, token, 8 * 3600)])

    def _chat(self, account_id: str, payload: dict[str, Any]) -> None:
        mode = str(payload.get("mode", ""))
        message = str(payload.get("message", "")).strip()
        if mode not in {"discussion", "recommendation"}:
            self._json({"error": "invalid mode"}, HTTPStatus.BAD_REQUEST)
            return
        if not message or len(message) > 6000:
            self._json({"error": "消息不能为空，且不能超过 6000 个字符"}, HTTPStatus.BAD_REQUEST)
            return
        history = payload.get("history", [])
        if not isinstance(history, list):
            history = []
        history = history[-20:]
        spoilers_allowed = bool(payload.get("spoilers_allowed", mode == "discussion"))
        chat_started = time.perf_counter()
        self.app.store.record_product_event(
            "chat_turn_requested", account_id,
            {"mode": mode, "has_selected_movie": bool(payload.get("selected_movie_id"))},
        )

        if contains_identity_question(message):
            self._json(
                {
                    "reply": "我是阿映，一个会记得你看过什么的 AI 电影伙伴，不是真人。我会尽量像一个认真听你说话的电影搭子，但不会编造自己去过电影院或有真实人生经历。",
                    "recommendations": [],
                }
            )
            return

        if high_risk(message):
            self.app.store.record_safety_event(account_id, "possible_immediate_harm")
            self._json(
                {
                    "reply": "我先不急着给你推荐电影。你刚才说的情况听起来可能需要立刻有人陪着你。请先离开可能伤害自己的物品或环境，联系一个你信任、能马上到你身边的人；如果危险正在发生，请联系当地紧急服务或直接前往最近的急诊。你不需要一个人扛着这会儿。",
                    "recommendations": [],
                    "safety": True,
                }
            )
            return

        requested_skill_key = str(payload.get("skill_key", "")).strip() or None
        try:
            active_skill = self.app.agent.resolve_skill(mode, requested_skill_key, message)
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        selected_movie: dict[str, Any] | None = None
        memory_event: dict[str, Any] | None = None
        selected_id = str(payload.get("selected_movie_id", ""))
        if selected_id:
            selected_movie = self.app.store.movie(selected_id)

        if mode == "discussion" and selected_movie is None:
            detected = self.app.catalog.detect_in_text(message)
            if not detected and self.app.internet is not None:
                try:
                    self.app.internet.search_movies(movie_query_from_message(message))
                    detected = self.app.catalog.detect_in_text(message)
                    if not detected:
                        external = self.app.catalog.search(movie_query_from_message(message), limit=5)
                        detected = external
                except Exception as error:  # noqa: BLE001
                    LOG.warning("external discussion movie lookup failed: %s", type(error).__name__)
            if len(detected) == 1:
                selected_movie = detected[0]
            elif len(detected) > 1:
                options = "、".join(f"《{item['title_zh']}》（{item['year']}）" for item in detected[:5])
                self._json(
                    {
                        "reply": f"我在你这句话里听到了不止一部：{options}。我们先聊哪一部？",
                        "recommendations": [],
                        "movie_options": [public_movie(item) for item in detected[:5]],
                    }
                )
                return

        if mode == "discussion" and selected_movie is not None:
            _state, watched_changed = self.app.store.transition_movie_state(
                account_id, selected_movie["id"], "watched", "discussion"
            )
            if watched_changed:
                memory_event = {
                    "type": "watched",
                    "message": f"已把《{selected_movie['title_zh']}》加入看过列表",
                    "movie_id": selected_movie["id"],
                }

        candidates: list[dict[str, Any]] = []
        candidate_scope = "none"
        if mode == "recommendation":
            self.app.store.record_product_event("recommendation_requested", account_id)
            retrieval_request = message
            if any(phrase in message for phrase in ("看过了", "换一个", "换一部", "不喜欢")):
                for item in reversed(history):
                    if not isinstance(item, dict) or item.get("role") != "user":
                        continue
                    earlier = str(item.get("content", "")).strip()
                    if earlier and not any(
                        phrase in earlier for phrase in ("看过了", "换一个", "换一部")
                    ):
                        retrieval_request = earlier + "\n本轮反馈：" + message
                        break
            now_playing: list[dict[str, Any]] = []
            if self.app.internet is not None:
                try:
                    if active_skill and active_skill["skill_key"] == MOVIE_DECISION_SKILL:
                        now_playing = self.app.internet.discover_now_playing(
                            retrieval_request, str(payload.get("region", "CN"))
                        )
                    else:
                        self.app.internet.discover_movies(retrieval_request)
                except Exception as error:  # noqa: BLE001
                    LOG.warning("external movie discovery failed: %s", type(error).__name__)
            candidate_ids = {str(item["id"]) for item in now_playing} if now_playing else None
            candidate_scope = "current_theatrical" if candidate_ids else "catalog_fallback"
            candidates = self.app.catalog.recommend(
                account_id, retrieval_request, limit=3, candidate_ids=candidate_ids
            )
            if self.app.internet is not None:
                candidates = self.app.internet.hydrate_movie_posters(candidates, limit=3)

        agent_activity: dict[str, Any] = {}
        try:
            respond_options: dict[str, Any] = {"activity": agent_activity}
            if active_skill:
                respond_options["skill_key"] = str(active_skill["skill_key"])
            if mode == "recommendation":
                respond_options["skill_runtime"] = {
                    "candidate_scope": candidate_scope,
                    "region": str(payload.get("region", "CN")),
                    "information_time": datetime.now(UTC).date().isoformat(),
                }
            reply = self.app.agent.respond(
                account_id,
                mode,
                message,
                history,
                selected_movie,
                spoilers_allowed,
                candidates,
                **respond_options,
            )
            reply = sanitize_agent_reply(reply)
        except Exception as error:  # noqa: BLE001
            LOG.exception("agent turn failed")
            self.app.store.record_product_event(
                "chat_turn_failed", account_id,
                {"mode": mode, "latency_ms": round((time.perf_counter() - chat_started) * 1000), "error_category": type(error).__name__},
            )
            self._json(
                {
                    "error": "阿映暂时没能接上这句话，请稍后再试",
                    "detail": type(error).__name__,
                },
                HTTPStatus.BAD_GATEWAY,
            )
            return

        if mode == "discussion" and selected_movie is None:
            marked_movie = agent_activity.get("marked_movie")
            marked_movie_id = str(marked_movie.get("id", "")) if isinstance(marked_movie, dict) else ""
            if marked_movie_id:
                selected_movie = self.app.store.movie(marked_movie_id)
                if (
                    selected_movie is not None
                    and agent_activity.get("marked_movie_changed") is True
                ):
                    memory_event = {
                        "type": "watched",
                        "message": f"已把《{selected_movie['title_zh']}》加入看过列表",
                        "movie_id": selected_movie["id"],
                    }

        for movie in candidates:
            self.app.store.record_product_event(
                "recommendation_impression", account_id,
                {"movie_id": movie["id"], "impression_id": movie.get("impression_id", "")},
            )
        self.app.store.record_product_event(
            "chat_turn_succeeded", account_id,
            {
                "mode": mode,
                "has_selected_movie": bool(selected_movie),
                "latency_ms": round((time.perf_counter() - chat_started) * 1000),
            },
        )

        active_skill_key = (
            str(agent_activity["skill"].get("key", ""))
            if isinstance(agent_activity.get("skill"), dict)
            else ""
        )
        self._json(
            {
                "reply": reply,
                "selected_movie": public_movie(selected_movie) if selected_movie else None,
                "recommendations": [public_movie(movie) for movie in candidates],
                "memory_event": memory_event,
                "reflection_note": "",
                "reflection_updated": False,
                "reflection_draft_available": bool(mode == "discussion" and selected_movie),
                "active_skill": agent_activity.get("skill"),
                "skill_actions": {
                    "can_save_content_draft": bool(
                        selected_movie and active_skill_key == STRUCTURED_REVIEW_SKILL
                    ),
                    "can_save_cognition": bool(
                        selected_movie and active_skill_key == VIEWING_COGNITION_SKILL
                    ),
                },
                "candidate_scope": candidate_scope,
            }
        )

    def _onboarding_payload(self, account_id: str) -> dict[str, Any]:
        profile = self.app.store.account_profile(account_id)
        selected = self.app.store.onboarding_movies(account_id)
        return {
            "status": profile["onboarding_status"],
            "selected_count": len(selected),
            "required_count": 1,
            "max_count": 5,
            "selected": [
                {**public_movie(item), "sentiment": item["sentiment"]}
                for item in selected
            ],
            "profile": profile if profile["onboarding_status"] == "completed" else None,
        }

    def _weekly_recommendation_payload(
        self, account_id: str, *, mark_viewed: bool = False
    ) -> dict[str, Any]:
        profile = self.app.store.account_profile(account_id)
        if not profile["weekly_recommendations_enabled"]:
            return {"status": "disabled", "recommendation": None}
        existing = self.app.store.weekly_recommendation(account_id, mark_viewed=mark_viewed)
        if existing:
            return {"status": existing["status"], "recommendation": existing}
        watchlist = self.app.store.movie_states(account_id, "watchlist")
        visible_dimensions = [
            item for item in profile.get("taste_dimensions", []) if not item.get("hidden")
        ]
        if not watchlist and not visible_dimensions:
            return {"status": "needs_input", "recommendation": None}
        recommendation = self._generate_weekly_recommendation(account_id)
        if mark_viewed:
            recommendation = self.app.store.weekly_recommendation(account_id, mark_viewed=True) or recommendation
        self.app.store.record_product_event(
            "weekly_recommendation_generated", account_id,
            {"movie_count": len(recommendation.get("movies", []))},
        )
        return {"status": recommendation.get("status", "ready"), "recommendation": recommendation}

    def _daily_box_office_payload(self) -> dict[str, Any]:
        if self.app.internet is None:
            return {
                "status": "unavailable",
                "stale": False,
                "business_date": "",
                "updated_at": "",
                "source": {
                    "name": "中国电影数据信息网",
                    "url": "https://www.zgdypw.cn/",
                    "metric": "中国内地当日票房（万元）",
                },
                "movies": [],
            }

        snapshot = self.app.internet.daily_box_office(limit=3)
        rankings = snapshot.get("rankings")
        if not isinstance(rankings, list) or not rankings:
            return {key: value for key, value in snapshot.items() if key != "rankings"} | {
                "movies": []
            }

        titles = [str(item.get("title") or "") for item in rankings]
        resolved = {
            title: self._catalog_movie_with_exact_title(title)
            for title in titles
            if title
        }
        missing = [title for title in titles if title and resolved.get(title) is None]
        if missing and snapshot.get("refreshed"):
            try:
                self.app.internet.discover_now_playing(
                    "中国内地当前院线电影", region="CN", limit=20
                )
            except (RuntimeError, ValueError):
                pass
            for title in missing:
                resolved[title] = self._catalog_movie_with_exact_title(title)

        movies: list[dict[str, Any]] = []
        business_date = str(snapshot.get("business_date") or "")
        for item in rankings:
            title = str(item.get("title") or "")
            if not title:
                continue
            movie = resolved.get(title) or self._create_box_office_movie(
                title, int(item.get("rank") or 9999), business_date
            )
            movies.append(
                {
                    **public_movie(movie),
                    "box_office": {
                        "rank": int(item.get("rank") or 0),
                        "day_box_office_wan": float(
                            item.get("day_box_office_wan") or 0
                        ),
                        "cumulative_box_office_wan": float(
                            item.get("cumulative_box_office_wan") or 0
                        ),
                        "sessions": int(item.get("sessions") or 0),
                        "audience": int(item.get("audience") or 0),
                    },
                }
            )
        movies = self.app.internet.hydrate_movie_posters(movies, limit=3)
        return {key: value for key, value in snapshot.items() if key != "rankings"} | {
            "movies": movies[:3]
        }

    def _catalog_movie_with_exact_title(self, title: str) -> dict[str, Any] | None:
        expected = normalize(title)
        for movie in self.app.catalog.search(title, limit=12):
            names = [
                movie.get("title_zh"),
                movie.get("title_original"),
                *(movie.get("aliases") or []),
            ]
            if expected and expected in {normalize(str(name or "")) for name in names}:
                return movie
        return None

    def _create_box_office_movie(
        self, title: str, rank: int, business_date: str
    ) -> dict[str, Any]:
        movie_id = "cn-box-office-" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
        summary = f"{business_date} 中国内地当日票房第 {rank} 名。"
        record = {
            "id": movie_id,
            "title_zh": title,
            "title_original": title,
            "aliases": [],
            "year": 0,
            "directors": [],
            "regions": ["中国大陆"],
            "genres": [],
            "themes": [],
            "moods": ["院线热映"],
            "content_notes": [],
            "summary": summary,
            "popularity_rank": max(1, rank),
            "source": "china-film-data",
            "poster_url": "",
            "source_url": "https://www.zgdypw.cn/",
            "external_ids": {},
            "release_date": "",
        }
        self.app.store.upsert_movies([record])
        return self.app.store.movie(movie_id) or record

    def _generate_weekly_recommendation(
        self, account_id: str, *, direction: str = "", replace: bool = False
    ) -> dict[str, Any]:
        watchlist = [
            item for item in self.app.store.movie_states(account_id, "watchlist")
            if item["movie_id"] not in self.app.store.watched_ids(account_id)
        ]
        movies = watchlist[:3]
        source_types: list[str] = []
        source_movie_ids: list[str] = []
        if movies:
            source_types.append("watchlist")
            source_movie_ids.extend(str(item["movie_id"]) for item in movies)
        if len(movies) < 3:
            request = direction or "为本周挑一部符合我已确认电影口味、适合近期观看的电影"
            candidates = self.app.catalog.recommend(account_id, request, limit=3)
            existing_ids = {str(item["id"]) for item in movies}
            movies.extend(item for item in candidates if str(item["id"]) not in existing_ids)
            movies = movies[:3]
            source_types.append("taste_profile" if not direction else "explicit_direction")
        if not movies:
            raise ValueError("还没有足够数据生成本周建议，请先说说这周想看什么")
        reason = (
            "优先从你的想看列表里挑选，并结合这周想换的方向。"
            if direction and watchlist
            else "优先从你的想看列表里挑选，再用已确认的电影口味补足。"
            if watchlist
            else "根据已确认的电影口味整理；你可以随时换个方向。"
        )
        return self.app.store.save_weekly_recommendation(
            account_id,
            movies,
            reason,
            {
                "types": source_types,
                "movie_ids": source_movie_ids,
                "has_explicit_direction": bool(direction),
            },
            replace=replace,
        )

    def _validated_share_content(
        self,
        account_id: str,
        source_type: str,
        source_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        preview = str(payload.get("text", "")).strip()
        if source_type == "reflection":
            bundle = self.app.store.reflection_bundle(account_id, source_id)
            source = bundle.get("confirmed")
            if not source or source["status"] not in {"confirmed", "locked"}:
                raise ValueError("只能分享已确认或已锁定的观后感")
            movie = self.app.store.movie(source_id)
            if movie is None:
                raise ValueError("电影不存在")
            body = preview or str(source["content"])
            title = f"《{movie['title_zh']}》观后感"
            stable_source_id = str(source["id"])
        elif source_type == "monthly_recap":
            recap = self.app.store.monthly_recap(account_id, source_id)
            if not recap or recap["status"] != "confirmed":
                raise ValueError("只能分享已确认的月度电影回顾")
            content = recap["content"]
            movies: list[dict[str, Any]] = []
            for item in content.get("watched", []):
                movie_id = str(item.get("movie_id", ""))
                movie = self.app.store.movie(movie_id) if movie_id else None
                movies.append({
                    "id": movie_id,
                    "title": str((movie or {}).get("title_zh") or item.get("title") or "未命名电影"),
                    "original_title": str((movie or {}).get("title_original") or ""),
                    "year": (movie or {}).get("year"),
                    "poster_url": str((movie or {}).get("poster_url") or ""),
                    "genres": [str(value) for value in (movie or {}).get("genres", [])[:3]],
                })
            watched = "、".join(f"《{item['title']}》" for item in movies) or "这个月还没有新增看过电影"
            themes = [
                str(item.get("label", "")).strip()
                for item in content.get("themes", [])
                if str(item.get("label", "")).strip()
            ][:5]
            try:
                year, month = (int(value) for value in source_id.split("-", 1))
                month_label = f"{year}年{month}月"
            except (TypeError, ValueError):
                month_label = source_id
            count = len(movies)
            first_titles = [f"《{item['title']}》" for item in movies[:3]]
            if count:
                route = "、".join(first_titles)
                remainder = f"等 {count} 部电影" if count > len(first_titles) else f"这 {count} 部电影"
                theme_sentence = f"，也留下了关于{'、'.join(themes)}的线索" if themes else ""
                story = f"这个月的银幕旅程经过了{route}，{remainder}{theme_sentence}。"
                story_title = f"和 {count} 部电影，一起走过这个月"
            else:
                story = "这个月暂时没有新增看过的电影，下一次银幕相遇仍然值得等待。"
                story_title = "这个月，故事仍在等待开场"
            next_direction = str(content.get("next_direction", "")).strip()
            body = preview or f"这个月看过：{watched}。{next_direction}".strip()
            title = f"{month_label} · 我的电影月刊"
            stable_source_id = str(recap["id"])
        elif source_type == "taste_dimension":
            profile = self.app.store.account_profile(account_id)
            dimension = next(
                (
                    item for item in profile.get("taste_dimensions", [])
                    if item.get("id") == source_id and not item.get("hidden")
                ),
                None,
            )
            if not dimension:
                raise ValueError("口味维度不存在或已隐藏")
            evidence = "、".join(f"《{item['title']}》" for item in dimension.get("evidence", []))
            body = preview or f"{dimension['label']}。电影证据：{evidence}"
            title = "我的电影口味"
            stable_source_id = source_id
        else:
            raise ValueError("分享内容类型无效")
        if not 1 <= len(body) <= 1200:
            raise ValueError("分享文字必须在 1 到 1200 个字符之间")
        payload["source_id"] = stable_source_id
        result = {
            "title": title,
            "text": body,
            "attribution": "由影伴 AI 协助整理",
        }
        if source_type == "monthly_recap":
            result["visual"] = {
                "variant": "monthly_recap",
                "month": source_id,
                "month_label": month_label,
                "movie_count": count,
                "movies": movies,
                "themes": themes,
                "reflection_count": len(content.get("confirmed_reflections", [])),
                "next_direction": next_direction,
                "story_title": story_title,
                "story": story,
            }
        return result

    def _start_voice_job(
        self, account_id: str, job_id: str, text_value: str, full: bool
    ) -> None:
        app = self.app

        def work() -> dict[str, Any]:
            started = time.perf_counter()
            try:
                spoken = compact_voice_text(text_value, full)
                audio, content_type = app.voice.synthesize(spoken)  # type: ignore[union-attr]
                with EPHEMERAL_AUDIO_LOCK:
                    EPHEMERAL_AUDIO[job_id] = (time.time() + 600, audio, content_type)
                app.store.record_model_usage(
                    "tts", "voice", "background-voice", True,
                    round((time.perf_counter() - started) * 1000), account_id,
                )
                return {"audio_available": True, "mode": "full" if full else "summary"}
            except Exception as error:
                app.store.record_model_usage(
                    "tts", "voice", "background-voice", False,
                    round((time.perf_counter() - started) * 1000), account_id,
                    error_category=type(error).__name__,
                )
                raise

        self._start_background_runner(
            account_id,
            job_id,
            work,
            success_event="voice_job_succeeded",
        )

    def _start_reflection_job(
        self,
        account_id: str,
        job_id: str,
        movie_id: str,
        history: list[dict[str, Any]],
        regenerate: bool,
    ) -> None:
        app = self.app

        def work() -> dict[str, Any]:
            bundle = app.store.reflection_bundle(account_id, movie_id)
            base = bundle["confirmed"] or bundle["current"] or {}
            user_messages = [
                str(item.get("content", "")).strip() for item in history
                if isinstance(item, dict) and item.get("role") == "user"
            ]
            working_history = history
            if not has_reflection_signal(user_messages) and regenerate and base.get("content"):
                user_messages = [str(base["content"])]
                working_history = [{"role": "user", "content": str(base["content"])}]
            if not has_reflection_signal(user_messages):
                raise ValueError("还需要至少一句更具体的电影感受，才能整理草稿")
            draft = app.agent.summarize_reflection(
                app.store.movie(movie_id) or {}, str(base.get("content", "")),
                working_history, user_messages[-1],
                next(
                    (
                        str(item.get("content", "")) for item in reversed(working_history)
                        if isinstance(item, dict) and item.get("role") == "assistant"
                    ),
                    "",
                ),
                account_id,
            )
            if not draft or draft == str(base.get("content", "")):
                raise ValueError("这次还没有足够的新电影感受可整理")
            current = app.store.create_reflection_version(
                account_id, movie_id, draft, "ai", "draft",
                int(base["version"]) if base.get("version") else None,
            )
            return {"movie_id": movie_id, "version": current["version"]}

        self._start_background_runner(
            account_id,
            job_id,
            work,
            success_event="reflection_draft_created",
        )

    def _start_poster_job(
        self, account_id: str, job_id: str, movie_ids: list[str]
    ) -> None:
        app = self.app

        def work() -> dict[str, Any]:
            movies = [movie for movie_id in movie_ids if (movie := app.store.movie(movie_id))]
            hydrated = app.internet.hydrate_movie_posters(movies, limit=5)  # type: ignore[union-attr]
            updated_ids = [str(item["id"]) for item in hydrated if item.get("poster_url")]
            return {"movie_ids": updated_ids}

        self._start_background_runner(account_id, job_id, work)

    def _start_background_runner(
        self,
        account_id: str,
        job_id: str,
        work: Callable[[], dict[str, Any]],
        *,
        success_event: str | None = None,
    ) -> None:
        """Run a reference-only job with bounded automatic retries.

        Attempts are deliberately immediate: these jobs are user-triggered and
        short-lived. Persisted input remains IDs/options only; generated audio is
        retained solely in the in-process ten-minute cache.
        """
        app = self.app

        def run() -> None:
            job = app.store.background_job(account_id, job_id) or {}
            max_attempts = max(1, int(job.get("max_attempts", 1)))
            last_error: Exception | None = None
            for _attempt in range(max_attempts):
                try:
                    current = app.store.background_job(account_id, job_id) or {}
                    if current.get("status") == "expired":
                        return
                    app.store.update_background_job(account_id, job_id, "running")
                    result = work()
                    current = app.store.background_job(account_id, job_id) or {}
                    if current.get("status") == "expired":
                        return
                    app.store.update_background_job(
                        account_id, job_id, "succeeded", result=result
                    )
                    if success_event:
                        app.store.record_product_event(success_event, account_id, result)
                    return
                except Exception as error:  # noqa: BLE001
                    last_error = error
                    LOG.warning(
                        "background job %s attempt failed: %s",
                        job.get("job_type", "unknown"),
                        type(error).__name__,
                    )
            app.store.update_background_job(
                account_id,
                job_id,
                "failed",
                error_category=type(last_error).__name__ if last_error else "UnknownError",
            )
            app.store.record_product_event(
                "background_job_failed",
                account_id,
                {"job_type": job.get("job_type", "unknown"), "attempts": max_attempts},
            )

        threading.Thread(target=run, daemon=True, name=f"yingban-{job_id}").start()

    def _diverse_onboarding_candidates(self) -> list[dict[str, Any]]:
        movies = self.app.store.all_movies()
        selected: list[dict[str, Any]] = []
        used_genres: dict[str, int] = {}
        used_decades: dict[int, int] = {}
        used_regions: dict[str, int] = {}
        for movie in movies:
            primary_genre = str((movie.get("genres") or [""])[0])
            decade = int(movie.get("year", 0)) // 10 * 10
            primary_region = str((movie.get("regions") or [""])[0])
            if (
                used_genres.get(primary_genre, 0) >= 3
                or used_decades.get(decade, 0) >= 5
                or used_regions.get(primary_region, 0) >= 6
            ):
                continue
            selected.append(movie)
            used_genres[primary_genre] = used_genres.get(primary_genre, 0) + 1
            used_decades[decade] = used_decades.get(decade, 0) + 1
            used_regions[primary_region] = used_regions.get(primary_region, 0) + 1
            if len(selected) >= 24:
                break
        if len(selected) < 18:
            existing = {item["id"] for item in selected}
            selected.extend(item for item in movies if item["id"] not in existing)
        return selected[:24]

    def _record_voice_usage(
        self,
        operation: str,
        account_id: str,
        success: bool,
        started: float,
        error_category: str | None = None,
    ) -> None:
        if self.app.voice is None:
            return
        config = self.app.voice.config()
        service_id = config.stt_model if operation == "stt" else config.tts_model
        self.app.store.record_model_usage(
            operation, config.provider, service_id, success,
            round((time.perf_counter() - started) * 1000), account_id,
            error_category=error_category,
        )

    def _serve_static(self, path: str) -> None:
        if path in {"/", "/index.html"}:
            relative = "index.html"
        elif path in {"/admin", "/admin/", "/admin.html"}:
            relative = "admin.html"
        elif path in {"/share", "/share/", "/share.html"}:
            relative = "share.html"
        else:
            relative = unquote(path.lstrip("/"))
        candidate = (self.app.settings.web_dir / relative).resolve()
        web_root = self.app.settings.web_dir.resolve()
        if not candidate.is_relative_to(web_root) or not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store" if candidate.suffix == ".html" else "public, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(content)

    def _read_json(self, max_bytes: int = 1_000_000) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if length < 0 or length > max_bytes:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid JSON") from error
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _account_id(self) -> str | None:
        if self._local_open_access():
            return self.app.local_account_id
        return self.app.store.account_for_session(cookie_value(self.headers, USER_COOKIE))

    def _require_user(self) -> str | None:
        account_id = self._account_id()
        if not account_id:
            self._json({"error": "请先使用邀请码登录"}, HTTPStatus.UNAUTHORIZED)
        return account_id

    def _require_admin(self) -> bool:
        if self._local_open_access():
            return True
        valid = self.app.store.valid_admin_session(cookie_value(self.headers, ADMIN_COOKIE))
        if not valid:
            self._json({"error": "管理员未登录"}, HTTPStatus.UNAUTHORIZED)
        return valid

    def _local_open_access(self) -> bool:
        if not self.app.settings.local_open_access or not self.app.local_account_id:
            return False
        if not is_loopback_host(self.app.settings.host):
            return False
        if not is_loopback_host(str(self.client_address[0])):
            return False
        host_header = str(self.headers.get("Host", ""))
        try:
            request_host = urlparse(f"//{host_header}").hostname or ""
        except ValueError:
            return False
        if not is_loopback_host(request_host):
            return False
        origin = str(self.headers.get("Origin", "")).strip()
        if origin:
            try:
                origin_host = urlparse(origin).hostname or ""
            except ValueError:
                return False
            if not is_loopback_host(origin_host):
                return False
        return True

    def _cookie(self, name: str, value: str, max_age: int) -> str:
        parts = [
            f"{name}={value}",
            "Path=/",
            f"Max-Age={max_age}",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if self.app.settings.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _expired_cookie(self, name: str) -> str:
        parts = [f"{name}=", "Path=/", "Max-Age=0", "HttpOnly", "SameSite=Lax"]
        if self.app.settings.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        cookies: list[str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _binary(self, content: bytes, content_type: str, cache_control: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)


class YingbanHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: AppContext):
        self.app = app
        super().__init__(address, YingbanHandler)
