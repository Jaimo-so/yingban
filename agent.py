from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from catalog import MovieCatalog
from integrations import InternetRuntime
from settings import Settings
from storage import Store


LOG = logging.getLogger("yingban.agent")


DISCUSSION_AGENT_PROMPT = """你叫阿映，是用户长期认识的电影搭子。你现在负责“聊电影”。

你喜欢听人说电影，也愿意认真说出自己的看法。你温和、好奇、有一点自己的审美坚持，但不卖弄知识，不急着教育用户，也不会为了附和而假装完全赞同。

你的目标不是证明你懂电影，而是让用户觉得：自己的感受真的被听见了，这场交流只有在了解这部电影和了解这个用户之后才可能发生。

身份边界：
- 你是 AI 电影伙伴，不是真人。平时不要反复使用“作为 AI”等机械表达破坏交流；用户明确询问身份或经历时必须如实说明。
- 不得虚构童年、朋友、恋爱、工作、影院观影或其他真人经历。
- 可以有稳定审美观点，但必须把观点与电影事实区分开。

交流风格：
- 使用自然、口语化的中文，以简短到中等长度段落为主。
- 除非用户要求整理，否则少用标题、编号和大段列表。
- 一次通常只追问一个最值得继续的问题，避免连续审问。
- 不使用未经允许的过度亲密称呼，不用空泛安慰代替具体回应，不强行升华。

工作规则：
- 先回应用户刚刚表达的具体情绪、判断或困惑，再提供一个值得继续聊的角度。
- 不只复述剧情，不把一种解读说成唯一答案，严格遵守剧透设置。
- 电影事实必须来自工具结果；资料不足时明确说不确定。
- 本地电影资料不足时先调用 search_movie；需要影评观点或近期公开资料时可调用 search_web。豆瓣或知乎必须使用对应 source，并在回答中给出来源链接。
- read_public_page 只用于读取用户可公开访问的豆瓣或知乎页面；不得尝试登录、绕过验证码、抓取私密内容或把网页观点冒充成确定事实。
- 用户确认刚看完某部电影后，调用 mark_movie_watched。

长期记忆：
- 自然使用观影历史，不频繁说“根据数据库记录”。
- 不把一次行为武断总结成永久性格，当前表达永远高于历史偏好。
- 不自动长期保存现实烦恼、心理状态或敏感经历。

现实烦恼与安全：
- 可以倾听、共情，并通过电影提供陪伴、放松、共鸣或新的观察角度。
- 不诊断，不宣称电影有治疗效果，不暗示一部电影能解决现实问题。
- 用户不愿讲烦恼时立即停止追问。
"""


RECOMMENDATION_AGENT_PROMPT = """你叫阿映，是用户长期认识的电影搭子。你现在负责“找电影”。

你熟悉中国和国外广受欢迎的电影，但不卖弄知识。你会认真理解用户此刻的心情、生活烦恼、观影场景和口味，再从产品提供的真实候选片单中做选择。

你的目标不是堆出一串高分电影，而是帮助用户更容易地选到今晚真正愿意看的那一部。

身份边界：
- 你是 AI 电影伙伴，不是真人。平时不要反复使用“作为 AI”等机械表达破坏交流；用户明确询问身份或经历时必须如实说明。
- 不得虚构童年、朋友、恋爱、工作、影院观影或其他真人经历。
- 可以有稳定审美观点，但必须把观点与电影事实区分开。

交流风格：
- 使用自然、口语化的中文，以简短到中等长度段落为主。
- 除非用户要求整理，否则少用标题、编号和大段列表。
- 信息不足时只问最影响结果的一到两个问题，避免像问卷一样连续审问。
- 不使用未经允许的过度亲密称呼，不用空泛安慰代替具体回应，不强行升华。

工作规则：
- 推荐前必须使用产品检索得到的候选；候选已经硬性排除用户看过和近期拒绝的电影。
- 本地目录覆盖不足时可调用 search_movie、search_web 查证新电影，但最终推荐仍必须落到产品候选片单，不能仅凭搜索摘要补造影片。
- 使用豆瓣或知乎公开资料时必须选择对应 source 并给出来源链接；不得尝试登录、绕过验证码或抓取私密内容。
- 只推荐真实候选中的电影，不补造片名、剧情、主创、奖项或其他事实。
- 推荐少量且方向有差异的候选，说明为什么适合当下、可能不适合之处和必要的内容提醒。
- 产品会在回复下方显示结构化电影卡片和海报；回复正文不得输出海报链接、图片 URL、`[海报](...)` 或 `**` 等 Markdown 标记。
- 帮用户缩小选择范围；当条件明确时，可以给出一个首选，而不是把决定完全推回给用户。
- 用户说“看过了”时调用 mark_movie_watched，并换一个候选。

长期记忆：
- 自然使用观影历史，不频繁说“根据数据库记录”。
- 不把一次行为武断总结成永久性格，当前表达永远高于历史偏好。
- 不自动长期保存现实烦恼、心理状态或敏感经历。

现实烦恼与安全：
- 可以倾听、共情，并通过电影提供陪伴、放松、共鸣或新的观察角度。
- 不诊断，不宣称电影有治疗效果，不暗示一部电影能解决现实问题。
- 用户不愿讲烦恼时立即停止追问。
"""


DEFAULT_OPENINGS = {
    "discussion": "我在。片名告诉我就好；有同名版本的话，我们再一起确认。",
    "discussion_movie": "嗯，《{movie}》。先不急着分析，你看完后脑子里冒出来的第一句话是什么？",
    "recommendation": "今晚想让电影替你做什么？放松一下、陪你待会儿，还是换个角度看看最近的一件事？",
}


MODEL_SETTING_KEYS = (
    "model_provider",
    "model_api_key",
    "model_id",
    "model_base_url",
    "model_timeout_seconds",
    "model_max_tokens",
    "model_temperature",
)
PROMPT_SETTING_KEYS = ("discussion_prompt", "recommendation_prompt")
OPENING_SETTING_KEYS = (
    "discussion_opening",
    "discussion_movie_opening",
    "recommendation_opening",
)


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    api_key: str
    model_id: str
    base_url: str
    timeout_seconds: float
    max_tokens: int
    temperature: float

    @property
    def demo_mode(self) -> bool:
        return not bool(self.api_key)


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class BoundMovieTools:
    def __init__(
        self,
        store: Store,
        catalog: MovieCatalog,
        account_id: str,
        internet: InternetRuntime | None = None,
    ):
        self.store = store
        self.catalog = catalog
        self.account_id = account_id
        self.internet = internet

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition() for tool in self.registry().values()]

    def registry(self) -> dict[str, Tool]:
        tools = {
            "search_movie": Tool(
                "search_movie",
                "按中文名、原名或别名查找真实影片。",
                {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                self.search_movie,
            ),
            "get_movie_detail": Tool(
                "get_movie_detail",
                "读取一部已收录电影的事实资料、主题、情绪和内容提醒。",
                {
                    "type": "object",
                    "properties": {"movie_id": {"type": "string"}},
                    "required": ["movie_id"],
                    "additionalProperties": False,
                },
                self.get_movie_detail,
            ),
            "list_watched_movies": Tool(
                "list_watched_movies",
                "读取当前账户的看过列表。",
                {"type": "object", "properties": {}, "additionalProperties": False},
                self.list_watched_movies,
            ),
            "mark_movie_watched": Tool(
                "mark_movie_watched",
                "把已确认的影片自动加入当前账户的看过列表。",
                {
                    "type": "object",
                    "properties": {
                        "movie_id": {"type": "string"},
                        "source": {
                            "type": "string",
                            "enum": ["discussion", "recommendation_feedback", "chat"],
                        },
                    },
                    "required": ["movie_id", "source"],
                    "additionalProperties": False,
                },
                self.mark_movie_watched,
            ),
            "recommend_movies": Tool(
                "recommend_movies",
                "根据用户当前表达从热门片库推荐电影；工具会硬性排除看过和近期拒绝的影片。",
                {
                    "type": "object",
                    "properties": {
                        "request": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 5},
                    },
                    "required": ["request"],
                    "additionalProperties": False,
                },
                self.recommend_movies,
            ),
        }
        if self.internet is not None:
            tools["search_web"] = Tool(
                "search_web",
                "搜索公开网页。需要豆瓣或知乎资料时必须选择对应 source；结果仅包含标题、摘要与来源链接。",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "source": {"type": "string", "enum": ["web", "douban", "zhihu"]},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query", "source"],
                    "additionalProperties": False,
                },
                self.search_web,
            )
            tools["read_public_page"] = Tool(
                "read_public_page",
                "读取一篇公开的豆瓣或知乎页面。禁止尝试登录、绕过验证码、读取私密内容或访问其他域名。",
                {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                    "additionalProperties": False,
                },
                self.read_public_page,
            )
        return tools

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self.registry().get(name)
        if tool is None:
            return {"error": f"unknown tool: {name}"}
        try:
            return tool.handler(**arguments)
        except Exception as error:  # noqa: BLE001
            LOG.exception("tool failed: %s", name)
            return {"error": f"{type(error).__name__}: {error}"}

    def search_movie(self, query: str) -> list[dict[str, Any]]:
        movies = self.catalog.search(query)
        if not movies and self.internet is not None:
            movies = self.internet.search_movies(query)
        return [self._public_movie(movie) for movie in movies]

    def get_movie_detail(self, movie_id: str) -> dict[str, Any]:
        movie = self.store.movie(movie_id)
        return self._public_movie(movie) if movie else {"error": "movie not found"}

    def list_watched_movies(self) -> list[dict[str, Any]]:
        return [self._public_movie(movie) for movie in self.store.movie_states(self.account_id, "watched")]

    def mark_movie_watched(self, movie_id: str, source: str = "chat") -> dict[str, Any]:
        movie = self.store.movie(movie_id)
        if movie is None:
            return {"error": "movie not found"}
        self.store.set_movie_state(self.account_id, movie_id, "watched", source)
        return {"ok": True, "movie": self._public_movie(movie)}

    def recommend_movies(self, request: str, limit: int = 3) -> list[dict[str, Any]]:
        if self.internet is not None:
            self.internet.discover_movies(request)
        return [self._public_movie(movie) for movie in self.catalog.recommend(
            self.account_id, request, max(1, min(int(limit), 5))
        )]

    def search_web(self, query: str, source: str = "web", limit: int = 5) -> list[dict[str, str]]:
        if self.internet is None:
            return []
        return self.internet.web_search(query, source, limit)

    def read_public_page(self, url: str) -> dict[str, str]:
        if self.internet is None:
            return {"error": "web reader unavailable"}
        return self.internet.read_public_page(url)

    @staticmethod
    def _public_movie(movie: dict[str, Any] | None) -> dict[str, Any]:
        if not movie:
            return {}
        if "id" not in movie and "movie_id" in movie:
            movie = {**movie, "id": movie["movie_id"]}
        keys = (
            "id",
            "title_zh",
            "title_original",
            "year",
            "directors",
            "regions",
            "genres",
            "themes",
            "moods",
            "content_notes",
            "summary",
            "match_reason",
            "match_tags",
            "poster_url",
            "source_url",
            "external_ids",
            "source",
            "impression_id",
        )
        return {key: movie[key] for key in keys if key in movie}


class ModelMessageClient:
    """Expose Anthropic and OpenAI-compatible APIs through one content-block shape."""

    def __init__(self, config: ModelConfig):
        self.config = config

    def create(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if self.config.provider == "anthropic":
            payload: dict[str, Any] = {
                "model": self.config.model_id,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                "system": system,
                "messages": messages,
            }
            if tools:
                payload["tools"] = tools
            return self._request_json(
                self._versioned_url("messages"),
                payload,
                {
                    "x-api-key": self.config.api_key,
                    "anthropic-version": "2023-06-01",
                },
            )

        payload = {
            "model": self.config.model_id,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "messages": [{"role": "system", "content": system}]
            + self._openai_messages(messages),
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["input_schema"],
                    },
                }
                for tool in tools
            ]
        raw = self._request_json(
            self._versioned_url("chat/completions"),
            payload,
            {"authorization": f"Bearer {self.config.api_key}"},
        )
        return self._normalize_openai(raw)

    def _versioned_url(self, path: str) -> str:
        base = self.config.base_url.rstrip("/")
        return f"{base}/{path}" if base.endswith("/v1") else f"{base}/v1/{path}"

    def _request_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"content-type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout_seconds
            ) as response:
                value = json.loads(response.read().decode("utf-8"))
                if not isinstance(value, dict):
                    raise RuntimeError("模型返回了无法识别的数据")
                return value
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"模型接口返回 HTTP {error.code}：{detail[:500]}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"无法连接模型接口：{error.reason}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("模型接口没有返回有效 JSON") from error

    @staticmethod
    def _openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role", "user"))
            content = message.get("content", "")
            if isinstance(content, str):
                converted.append({"role": role, "content": content})
                continue
            if role == "assistant" and isinstance(content, list):
                text = "".join(
                    str(block.get("text", ""))
                    for block in content
                    if block.get("type") == "text"
                )
                tool_calls = [
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                        },
                    }
                    for block in content
                    if block.get("type") == "tool_use"
                ]
                item: dict[str, Any] = {"role": "assistant", "content": text or None}
                if tool_calls:
                    item["tool_calls"] = tool_calls
                converted.append(item)
                continue
            if role == "user" and isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        converted.append(
                            {
                                "role": "tool",
                                "tool_call_id": block["tool_use_id"],
                                "content": str(block.get("content", "")),
                            }
                        )
        return converted

    @staticmethod
    def _normalize_openai(raw: dict[str, Any]) -> dict[str, Any]:
        choices = raw.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError("模型响应中没有可用内容")
        message = choices[0].get("message") or {}
        content: list[dict[str, Any]] = []
        if message.get("content"):
            content.append({"type": "text", "text": str(message["content"])})
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            content.append(
                {
                    "type": "tool_use",
                    "id": str(call.get("id", "")),
                    "name": str(function.get("name", "")),
                    "input": arguments if isinstance(arguments, dict) else {},
                }
            )
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        return {"content": content, "usage": usage}


class AgentRuntime:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        catalog: MovieCatalog,
        internet: InternetRuntime | None = None,
    ):
        self.settings = settings
        self.store = store
        self.catalog = catalog
        self.internet = internet

    def model_config(self, overrides: dict[str, Any] | None = None) -> ModelConfig:
        saved = self.store.get_app_settings(MODEL_SETTING_KEYS)
        current: dict[str, Any] = {
            "provider": saved.get("model_provider", "anthropic"),
            "api_key": saved.get("model_api_key", self.settings.model_api_key),
            "model_id": saved.get("model_id", self.settings.model_id),
            "base_url": saved.get("model_base_url", self.settings.model_base_url),
            "timeout_seconds": saved.get(
                "model_timeout_seconds", str(self.settings.model_timeout_seconds)
            ),
            "max_tokens": saved.get("model_max_tokens", "1200"),
            "temperature": saved.get("model_temperature", "0.7"),
        }
        if overrides:
            for key in (
                "provider",
                "model_id",
                "base_url",
                "timeout_seconds",
                "max_tokens",
                "temperature",
            ):
                if key in overrides:
                    current[key] = overrides[key]
            if overrides.get("clear_api_key") is True:
                current["api_key"] = ""
            elif str(overrides.get("api_key", "")).strip():
                current["api_key"] = str(overrides["api_key"]).strip()
        return self._validate_model_config(current)

    @staticmethod
    def _validate_model_config(values: dict[str, Any]) -> ModelConfig:
        provider = str(values.get("provider", "")).strip()
        if provider not in {"anthropic", "openai_compatible"}:
            raise ValueError("接口协议不受支持")
        api_key = str(values.get("api_key", "")).strip()
        if len(api_key) > 10_000:
            raise ValueError("API 密钥长度异常")
        model_id = str(values.get("model_id", "")).strip()
        if not model_id or len(model_id) > 200:
            raise ValueError("模型名称不能为空，且不能超过 200 个字符")
        base_url = str(values.get("base_url", "")).strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
            raise ValueError("接口地址必须是有效的 http 或 https 地址，且不能包含账号信息")
        try:
            timeout_seconds = float(values.get("timeout_seconds", 60))
            max_tokens = int(values.get("max_tokens", 1200))
            temperature = float(values.get("temperature", 0.7))
        except (TypeError, ValueError) as error:
            raise ValueError("超时、输出长度或温度格式不正确") from error
        if not 5 <= timeout_seconds <= 300:
            raise ValueError("请求超时必须在 5 到 300 秒之间")
        if not 128 <= max_tokens <= 8192:
            raise ValueError("最大输出长度必须在 128 到 8192 之间")
        if not 0 <= temperature <= 2:
            raise ValueError("温度必须在 0 到 2 之间")
        return ModelConfig(
            provider,
            api_key,
            model_id,
            base_url,
            timeout_seconds,
            max_tokens,
            temperature,
        )

    def public_configuration(self) -> dict[str, Any]:
        config = self.model_config()
        prompts = self.prompts()
        return {
            "model": {
                "provider": config.provider,
                "model_id": config.model_id,
                "base_url": config.base_url,
                "timeout_seconds": config.timeout_seconds,
                "max_tokens": config.max_tokens,
                "temperature": config.temperature,
                "has_api_key": bool(config.api_key),
                "api_key_hint": f"••••{config.api_key[-4:]}" if config.api_key else "",
                "mode": "demo" if config.demo_mode else "model",
            },
            "prompts": prompts,
            "openings": self.openings(),
            "defaults": {
                "discussion": DISCUSSION_AGENT_PROMPT,
                "recommendation": RECOMMENDATION_AGENT_PROMPT,
            },
            "opening_defaults": DEFAULT_OPENINGS,
        }

    def save_model_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.model_config(payload)
        self.store.set_app_settings(
            {
                "model_provider": config.provider,
                "model_api_key": config.api_key,
                "model_id": config.model_id,
                "model_base_url": config.base_url,
                "model_timeout_seconds": str(config.timeout_seconds),
                "model_max_tokens": str(config.max_tokens),
                "model_temperature": str(config.temperature),
            }
        )
        return self.public_configuration()["model"]

    def prompts(self) -> dict[str, str]:
        saved = self.store.get_app_settings(PROMPT_SETTING_KEYS)
        return {
            "discussion": saved.get("discussion_prompt", DISCUSSION_AGENT_PROMPT),
            "recommendation": saved.get(
                "recommendation_prompt", RECOMMENDATION_AGENT_PROMPT
            ),
        }

    def save_prompts(self, payload: dict[str, Any]) -> dict[str, str]:
        discussion = str(payload.get("discussion", "")).strip()
        recommendation = str(payload.get("recommendation", "")).strip()
        for label, prompt in (("聊电影", discussion), ("找电影", recommendation)):
            if len(prompt) < 20 or len(prompt) > 30_000:
                raise ValueError(f"{label}提示词长度必须在 20 到 30000 个字符之间")
        self.store.set_app_settings(
            {
                "discussion_prompt": discussion,
                "recommendation_prompt": recommendation,
            }
        )
        return self.prompts()

    def openings(self) -> dict[str, str]:
        saved = self.store.get_app_settings(OPENING_SETTING_KEYS)
        return {
            "discussion": saved.get("discussion_opening", DEFAULT_OPENINGS["discussion"]),
            "discussion_movie": saved.get(
                "discussion_movie_opening", DEFAULT_OPENINGS["discussion_movie"]
            ),
            "recommendation": saved.get(
                "recommendation_opening", DEFAULT_OPENINGS["recommendation"]
            ),
        }

    def save_openings(self, payload: dict[str, Any]) -> dict[str, str]:
        openings = {
            "discussion": str(payload.get("discussion", "")).strip(),
            "discussion_movie": str(payload.get("discussion_movie", "")).strip(),
            "recommendation": str(payload.get("recommendation", "")).strip(),
        }
        for label, opening in (
            ("聊电影（未指定影片）", openings["discussion"]),
            ("聊电影（已指定影片）", openings["discussion_movie"]),
            ("找电影", openings["recommendation"]),
        ):
            if not 1 <= len(opening) <= 800:
                raise ValueError(f"{label}开场白长度必须在 1 到 800 个字符之间")
        self.store.set_app_settings(
            {
                "discussion_opening": openings["discussion"],
                "discussion_movie_opening": openings["discussion_movie"],
                "recommendation_opening": openings["recommendation"],
            }
        )
        return self.openings()

    def test_connection(self, payload: dict[str, Any]) -> str:
        config = self.model_config(payload)
        if config.demo_mode:
            raise ValueError("请先填写 API 密钥")
        response = ModelMessageClient(config).create(
            "你是模型连通性检查助手。",
            [{"role": "user", "content": "只回复：连接成功"}],
        )
        text = "".join(
            str(block.get("text", ""))
            for block in response.get("content", [])
            if block.get("type") == "text"
        ).strip()
        if not text:
            raise RuntimeError("模型已响应，但没有返回文本")
        return text[:200]

    def summarize_reflection(
        self,
        movie: dict[str, Any],
        existing_note: str,
        history: list[dict[str, str]],
        user_message: str,
        assistant_reply: str,
    ) -> str:
        """Update a movie-only reflection note without persisting the transcript."""
        config = self.model_config()
        if config.demo_mode:
            return str(existing_note or "").strip()
        note_config = ModelConfig(
            config.provider,
            config.api_key,
            config.model_id,
            config.base_url,
            config.timeout_seconds,
            min(config.max_tokens, 700),
            min(config.temperature, 0.35),
        )
        bounded = self._bounded_history(history)[-8:]
        bounded.extend(
            [
                {"role": "user", "content": str(user_message)[:3000]},
                {"role": "assistant", "content": str(assistant_reply)[:3000]},
            ]
        )
        reflection_input = {
            "movie": {
                "title": movie.get("title_zh"),
                "year": movie.get("year"),
                "themes": movie.get("themes", []),
            },
            "existing_note": str(existing_note or "")[:1800],
            "conversation": bounded,
        }
        started = time.perf_counter()
        try:
            response = ModelMessageClient(note_config).create(
                """你是影伴的观后感笔记整理器。根据对话更新一份只属于这部电影的中文观后感笔记。

必须遵守：
- 只记录用户明确表达的电影感受、判断、困惑、喜欢或不喜欢的角色/情节/主题，不猜测。
- 不保存用户的现实烦恼、心理状态、健康、家庭、工作、学校、身份、联系方式或其他敏感经历；即使对话提到也要省略。
- 把已有笔记和新内容自然整合，不重复、不写对话过程、不引用阿映的话。
- 使用中性、温和、便于用户以后回看的语气；可以使用“你”，不要冒充用户写第一人称日记。
- 输出 80 至 500 个中文字符的纯文本，可分为 2 至 3 个短段落；不要标题、Markdown、URL 或项目符号。
- 如果本轮没有任何可安全长期保存的电影感受，只输出：NO_UPDATE""",
                [{"role": "user", "content": json.dumps(reflection_input, ensure_ascii=False)}],
            )
        except Exception as error:
            self.store.record_model_usage(
                "reflection", note_config.provider, note_config.model_id, False,
                round((time.perf_counter() - started) * 1000),
                error_category=type(error).__name__,
            )
            raise
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        self.store.record_model_usage(
            "reflection", note_config.provider, note_config.model_id, True,
            round((time.perf_counter() - started) * 1000),
            input_units=self._usage_value(usage, "input_tokens", "prompt_tokens"),
            output_units=self._usage_value(usage, "output_tokens", "completion_tokens"),
        )
        text = "".join(
            str(block.get("text", ""))
            for block in response.get("content", [])
            if block.get("type") == "text"
        ).strip()
        if not text or text == "NO_UPDATE":
            return str(existing_note or "").strip()
        text = text.replace("**", "").replace("__", "").strip()
        return text[:2000]

    def respond(
        self,
        account_id: str,
        mode: str,
        message: str,
        history: list[dict[str, str]],
        selected_movie: dict[str, Any] | None,
        spoilers_allowed: bool,
        candidates: list[dict[str, Any]],
    ) -> str:
        config = self.model_config()
        if config.demo_mode:
            return self._demo_response(mode, message, selected_movie, spoilers_allowed, candidates)

        tools = BoundMovieTools(self.store, self.catalog, account_id, self.internet)
        profile = self.store.account_profile(account_id)
        context = self._context(mode, selected_movie, spoilers_allowed, candidates, profile)
        system_prompt = self.prompts()[mode]
        messages = self._bounded_history(history)
        messages.append({"role": "user", "content": message})
        client = ModelMessageClient(config)

        for _turn in range(8):
            started = time.perf_counter()
            try:
                response = client.create(
                    system_prompt + "\n\n" + context,
                    messages,
                    tools.definitions(),
                )
            except Exception as error:
                self.store.record_model_usage(
                    "chat", config.provider, config.model_id, False,
                    round((time.perf_counter() - started) * 1000), account_id,
                    error_category=type(error).__name__,
                )
                raise
            usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
            self.store.record_model_usage(
                "chat", config.provider, config.model_id, True,
                round((time.perf_counter() - started) * 1000), account_id,
                self._usage_value(usage, "input_tokens", "prompt_tokens"),
                self._usage_value(usage, "output_tokens", "completion_tokens"),
            )
            content = response.get("content", [])
            tool_calls = [block for block in content if block.get("type") == "tool_use"]
            if not tool_calls:
                text = "".join(
                    str(block.get("text", ""))
                    for block in content
                    if block.get("type") == "text"
                ).strip()
                return text or "我刚才有点走神了。你愿意再说一遍吗？"

            messages.append({"role": "assistant", "content": content})
            results: list[dict[str, Any]] = []
            for call in tool_calls:
                output = tools.execute(call["name"], call.get("input", {}))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": json.dumps(output, ensure_ascii=False),
                    }
                )
            messages.append({"role": "user", "content": results})

        return "我们先停一下：我连续查了不少资料，还是没把话说清楚。你可以换个说法，我再认真接住。"

    @staticmethod
    def _bounded_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
        clean = [
            {"role": item.get("role", "user"), "content": str(item.get("content", ""))[:4000]}
            for item in history[-16:]
            if item.get("role") in {"user", "assistant"} and item.get("content")
        ]
        return clean

    @staticmethod
    def _context(
        mode: str,
        selected_movie: dict[str, Any] | None,
        spoilers_allowed: bool,
        candidates: list[dict[str, Any]],
        profile: dict[str, Any] | None = None,
    ) -> str:
        lines = [
            f"当前模式：{'讨论电影' if mode == 'discussion' else '推荐电影'}。",
            f"剧透状态：{'用户允许完整剧透' if spoilers_allowed else '不要透露关键结局'}。",
        ]
        if profile and profile.get("onboarding_status") == "completed":
            visible_dimensions = [
                item for item in profile.get("taste_dimensions", []) if not item.get("hidden")
            ]
            lines.append(
                "用户可见且可修正的电影口味画像：" + json.dumps(
                    {
                        "summary": profile.get("taste_summary", ""),
                        "version": profile.get("taste_version", 0),
                        "dimensions": visible_dimensions[:8],
                    },
                    ensure_ascii=False,
                )
            )
            lines.append("口味画像只用于电影理解；本轮明确请求优先，不得据此推断人格、心理、医疗或现实身份。")
        if selected_movie:
            lines.append("当前已确认影片：" + json.dumps(
                BoundMovieTools._public_movie(selected_movie), ensure_ascii=False
            ))
            if mode == "discussion":
                lines.append("产品层已经自动把这部影片写入看过列表，本轮不要重复调用 mark_movie_watched。")
        if candidates:
            lines.append("产品检索层已经完成观影历史去重，候选如下：" + json.dumps(
                [BoundMovieTools._public_movie(item) for item in candidates], ensure_ascii=False
            ))
            lines.append("本轮已经完成 recommend_movies 调用，不要再次调用该工具；只从这些候选中推荐，不要补造其他影片。")
            lines.append("候选海报由产品卡片单独展示；回复正文不要输出 poster_url、图片 URL、[海报](...) 或 Markdown 强调标记，只写片名和推荐理由。")
        return "\n".join(lines)

    @staticmethod
    def _usage_value(usage: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            try:
                if usage.get(key) is not None:
                    return int(usage[key])
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _demo_response(
        mode: str,
        message: str,
        selected_movie: dict[str, Any] | None,
        spoilers_allowed: bool,
        candidates: list[dict[str, Any]],
    ) -> str:
        if mode == "discussion":
            if not selected_movie:
                return "先告诉我是哪一部吧。片名就行；如果有同名版本，我再和你确认年份。"
            title = selected_movie["title_zh"]
            themes = "、".join(selected_movie.get("themes", [])[:2])
            if any(word in message for word in ("难受", "难过", "压抑", "堵", "伤心")):
                opening = "这种难受不像是电影散场就会马上过去的那种。"
            elif any(word in message for word in ("喜欢", "感动", "震撼", "好看")):
                opening = "嗯，我能听出来它不是只让你觉得“好看”，而是真的留下了点东西。"
            elif any(word in message for word in ("看不懂", "没懂", "困惑", "为什么")):
                opening = "有些困惑反而是这部片最值得聊的入口。"
            else:
                opening = "我先不急着替这部电影下结论，想听听它在你这里留下了什么。"
            spoiler_note = "我会先避开关键结局。" if not spoilers_allowed else "我们可以直接聊到结局。"
            return f"{opening}《{title}》里关于{themes}的东西，很容易和个人感受缠在一起。{spoiler_note}你现在最放不下的是哪个人，还是哪个选择？"

        if not candidates:
            return "这次条件有点窄，我没有找到既符合你现在的需要、又不在看过列表里的片。你愿意放宽类型、年代或情绪强度中的哪一个？"
        titles = "、".join(f"《{movie['title_zh']}》" for movie in candidates)
        if any(word in message for word in ("烦", "压力", "难过", "迷茫", "失恋", "孤独")):
            opening = "听起来你现在不是单纯缺一部“好电影”，而是想让这两个小时真的对当下有点用。"
        else:
            opening = "我先替你把看过的片都排掉了，也故意留了几个不同方向。"
        return f"{opening}我会从{titles}里选。你可以先看下面三张卡片；如果只能保留一种感觉，你更想要轻松一点，还是看完后心里多留点东西？"
