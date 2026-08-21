from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import mimetypes
import re
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from agent import AgentRuntime, BoundMovieTools
from catalog import MovieCatalog
from integrations import InternetRuntime
from settings import Settings
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
        if parsed.path.startswith("/api/history/"):
            account_id = self._require_user()
            if not account_id:
                return
            movie_id = unquote(parsed.path.removeprefix("/api/history/"))
            removed = self.app.store.remove_movie_state(account_id, movie_id)
            self._json({"ok": removed})
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/health":
            mode = "demo" if self.app.agent.model_config().demo_mode else "model"
            self._json({"ok": True, "mode": mode})
            return

        if path == "/api/me":
            account_id = self._account_id()
            if not account_id:
                self._json({"authenticated": False})
                return
            watched_count = len(self.app.store.watched_ids(account_id))
            self._json(
                {
                    "authenticated": True,
                    "account": {"id_hint": account_id[-6:], "watched_count": watched_count},
                    "demo_mode": self.app.agent.model_config().demo_mode,
                    "openings": self.app.agent.openings(),
                    "voice_available": bool(
                        self.app.voice and self.app.voice.config().enabled
                    ),
                }
            )
            return

        if path == "/api/history":
            account_id = self._require_user()
            if not account_id:
                return
            state = query.get("state", [None])[0]
            if state not in {None, "watched", "watchlist", "disliked"}:
                self._json({"error": "invalid state"}, HTTPStatus.BAD_REQUEST)
                return
            items = self.app.store.movie_states(account_id, state)
            if state == "watched" and self.app.internet is not None:
                items = self.app.internet.hydrate_movie_posters(items, limit=5)
            self._json({"items": items})
            return

        if path == "/api/movies":
            account_id = self._require_user()
            if not account_id:
                return
            query_text = query.get("query", [""])[0]
            items = self.app.catalog.search(query_text)
            if not items and self.app.internet is not None:
                try:
                    items = self.app.internet.search_movies(query_text)
                except Exception as error:  # noqa: BLE001
                    LOG.warning("external movie search failed: %s", type(error).__name__)
            self._json({"items": [public_movie(item) for item in items]})
            return

        if path == "/api/admin/invites":
            if not self._require_admin():
                return
            self._json({"items": self.app.store.list_invites()})
            return

        if path == "/api/admin/agent-config":
            if not self._require_admin():
                return
            result = self.app.agent.public_configuration()
            result["internet"] = self.app.internet.public_config() if self.app.internet else {}
            result["voice"] = self.app.voice.public_config() if self.app.voice else {}
            self._json(result)
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
            self.app.store.logout(token)
            self._json({"ok": True}, cookies=[self._expired_cookie(USER_COOKIE)])
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
                item = self.app.store.set_movie_state(account_id, movie_id, state, source)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True, "item": item})
            return
        if path == "/api/chat":
            account_id = self._require_user()
            if not account_id:
                return
            self._chat(account_id, payload)
            return
        if path == "/api/voice/transcribe":
            if not self._require_user():
                return
            if self.app.voice is None:
                self._json({"error": "语音服务不可用"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                encoded = str(payload.get("audio_base64", ""))
                audio = base64.b64decode(encoded, validate=True)
                text = self.app.voice.transcribe(audio, str(payload.get("mime_type", "audio/webm")))
            except (ValueError, RuntimeError, binascii.Error) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            except Exception as error:  # noqa: BLE001
                LOG.warning("voice transcription failed: %s", type(error).__name__)
                self._json({"error": "语音识别暂时不可用"}, HTTPStatus.BAD_GATEWAY)
                return
            self._json({"text": text})
            return
        if path == "/api/voice/synthesize":
            if not self._require_user():
                return
            if self.app.voice is None:
                self._json({"error": "语音服务不可用"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                audio, content_type = self.app.voice.synthesize(str(payload.get("text", "")))
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            except Exception as error:  # noqa: BLE001
                LOG.warning("voice synthesis failed: %s", type(error).__name__)
                self._json({"error": "语音回复暂时不可用"}, HTTPStatus.BAD_GATEWAY)
                return
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
                codes = self.app.store.generate_invites(count)
            except (TypeError, ValueError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"codes": codes})
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
            self._json({"error": str(error)}, HTTPStatus.UNAUTHORIZED)
            return
        LOGIN_LIMITER.success(remote)
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
            self.app.store.set_movie_state(
                account_id, selected_movie["id"], "watched", "discussion"
            )
            memory_event = {
                "type": "watched",
                "message": f"已把《{selected_movie['title_zh']}》加入看过列表",
                "movie_id": selected_movie["id"],
            }

        candidates: list[dict[str, Any]] = []
        if mode == "recommendation":
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
            if self.app.internet is not None:
                try:
                    self.app.internet.discover_movies(retrieval_request)
                except Exception as error:  # noqa: BLE001
                    LOG.warning("external movie discovery failed: %s", type(error).__name__)
            candidates = self.app.catalog.recommend(account_id, retrieval_request, limit=3)
            if self.app.internet is not None:
                candidates = self.app.internet.hydrate_movie_posters(candidates, limit=3)

        try:
            reply = self.app.agent.respond(
                account_id,
                mode,
                message,
                history,
                selected_movie,
                spoilers_allowed,
                candidates,
            )
            reply = sanitize_agent_reply(reply)
        except Exception as error:  # noqa: BLE001
            LOG.exception("agent turn failed")
            self._json(
                {
                    "error": "阿映暂时没能接上这句话，请稍后再试",
                    "detail": type(error).__name__,
                },
                HTTPStatus.BAD_GATEWAY,
            )
            return

        reflection_note = ""
        reflection_updated = False
        if mode == "discussion" and selected_movie is not None:
            current_state = self.app.store.get_movie_state(account_id, selected_movie["id"]) or {}
            existing_note = str(current_state.get("note") or "")
            try:
                reflection_note = self.app.agent.summarize_reflection(
                    selected_movie,
                    existing_note,
                    history,
                    message,
                    reply,
                )
                if reflection_note and reflection_note != existing_note:
                    self.app.store.save_movie_reflection(
                        account_id, selected_movie["id"], reflection_note
                    )
                    reflection_updated = True
            except Exception as error:  # noqa: BLE001
                LOG.warning("reflection note update failed: %s", type(error).__name__)
                reflection_note = existing_note

        self._json(
            {
                "reply": reply,
                "selected_movie": public_movie(selected_movie) if selected_movie else None,
                "recommendations": [public_movie(movie) for movie in candidates],
                "memory_event": memory_event,
                "reflection_note": reflection_note,
                "reflection_updated": reflection_updated,
            }
        )

    def _serve_static(self, path: str) -> None:
        if path in {"/", "/index.html"}:
            relative = "index.html"
        elif path in {"/admin", "/admin/", "/admin.html"}:
            relative = "admin.html"
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
        return self.app.store.account_for_session(cookie_value(self.headers, USER_COOKIE))

    def _require_user(self) -> str | None:
        account_id = self._account_id()
        if not account_id:
            self._json({"error": "请先使用邀请码登录"}, HTTPStatus.UNAUTHORIZED)
        return account_id

    def _require_admin(self) -> bool:
        valid = self.app.store.valid_admin_session(cookie_value(self.headers, ADMIN_COOKIE))
        if not valid:
            self._json({"error": "管理员未登录"}, HTTPStatus.UNAUTHORIZED)
        return valid

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


class YingbanHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: AppContext):
        self.app = app
        super().__init__(address, YingbanHandler)
