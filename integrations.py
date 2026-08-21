from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from settings import Settings
from storage import Store


INTERNET_SETTING_KEYS = (
    "tmdb_api_key",
    "tmdb_base_url",
    "tmdb_image_base_url",
    "web_search_provider",
    "web_search_api_key",
    "web_search_base_url",
    "web_reader_enabled",
    "web_reader_base_url",
)

WEB_SEARCH_PROVIDERS = {"tavily", "brave"}

TMDB_GENRES = {
    12: "冒险",
    14: "奇幻",
    16: "动画",
    18: "剧情",
    27: "恐怖",
    28: "动作",
    35: "喜剧",
    36: "历史",
    37: "西部",
    53: "惊悚",
    80: "犯罪",
    99: "纪录片",
    878: "科幻",
    9648: "悬疑",
    10402: "音乐",
    10749: "爱情",
    10751: "家庭",
    10752: "战争",
}

DISCOVERY_GENRES = {
    "动作": 28,
    "冒险": 12,
    "动画": 16,
    "喜剧": 35,
    "搞笑": 35,
    "犯罪": 80,
    "纪录片": 99,
    "剧情": 18,
    "家庭": 10751,
    "奇幻": 14,
    "历史": 36,
    "恐怖": 27,
    "音乐": 10402,
    "悬疑": 9648,
    "爱情": 10749,
    "科幻": 878,
    "惊悚": 53,
    "战争": 10752,
}

MOOD_HINTS = {
    "放松": ["轻松", "放松"],
    "轻松": ["轻松"],
    "治愈": ["治愈", "温暖"],
    "温暖": ["温暖"],
    "感动": ["感动"],
    "力量": ["有力量", "鼓舞"],
    "烧脑": ["烧脑", "思考"],
    "刺激": ["紧张", "刺激"],
    "孤独": ["孤独", "陪伴"],
    "压力": ["放松", "陪伴"],
    "新片": ["新片"],
    "最近上映": ["新片"],
    "最近的电影": ["新片"],
}


def _secret_hint(value: str) -> str:
    return f"••••{value[-4:]}" if value else ""


def _url(value: Any, label: str) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        raise ValueError(f"{label}必须是有效的 http 或 https 地址")
    return candidate


def _search_provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    if provider not in WEB_SEARCH_PROVIDERS:
        raise ValueError("Web Search 服务商只允许 tavily 或 brave")
    return provider


def _normalized_movie_title(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


@dataclass(frozen=True)
class InternetConfig:
    tmdb_api_key: str
    tmdb_base_url: str
    tmdb_image_base_url: str
    web_search_provider: str
    web_search_api_key: str
    web_search_base_url: str
    web_reader_enabled: bool
    web_reader_base_url: str

    @property
    def movie_enabled(self) -> bool:
        return bool(self.tmdb_api_key)

    @property
    def search_enabled(self) -> bool:
        return bool(self.web_search_api_key)


class InternetRuntime:
    """Structured movie discovery plus constrained public web research."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    def config(self, overrides: dict[str, Any] | None = None) -> InternetConfig:
        saved = self.store.get_app_settings(INTERNET_SETTING_KEYS)
        saved_search_base_url = saved.get(
            "web_search_base_url", self.settings.web_search_base_url
        )
        saved_search_provider = saved.get("web_search_provider", "")
        if not saved_search_provider:
            if "api.search.brave.com" in saved_search_base_url:
                saved_search_provider = "brave"
            elif "api.tavily.com" in saved_search_base_url:
                saved_search_provider = "tavily"
            else:
                saved_search_provider = self.settings.web_search_provider
        current: dict[str, Any] = {
            "tmdb_api_key": saved.get("tmdb_api_key", self.settings.tmdb_api_key),
            "tmdb_base_url": saved.get("tmdb_base_url", self.settings.tmdb_base_url),
            "tmdb_image_base_url": saved.get(
                "tmdb_image_base_url", self.settings.tmdb_image_base_url
            ),
            "web_search_provider": saved_search_provider,
            "web_search_api_key": saved.get(
                "web_search_api_key", self.settings.web_search_api_key
            ),
            "web_search_base_url": saved_search_base_url,
            "web_reader_enabled": saved.get(
                "web_reader_enabled", "true" if self.settings.web_reader_enabled else "false"
            ),
            "web_reader_base_url": saved.get(
                "web_reader_base_url", self.settings.web_reader_base_url
            ),
        }
        if overrides:
            if "web_search_provider" not in overrides and "web_search_base_url" in overrides:
                override_search_url = str(overrides["web_search_base_url"])
                if "api.search.brave.com" in override_search_url:
                    overrides = {**overrides, "web_search_provider": "brave"}
                elif "api.tavily.com" in override_search_url:
                    overrides = {**overrides, "web_search_provider": "tavily"}
            if "web_search_provider" in overrides:
                next_provider = _search_provider(overrides["web_search_provider"])
                if (
                    next_provider != _search_provider(current["web_search_provider"])
                    and not str(overrides.get("web_search_api_key", "")).strip()
                ):
                    current["web_search_api_key"] = ""
                current["web_search_provider"] = next_provider
            for key in ("tmdb_api_key", "web_search_api_key"):
                if overrides.get(f"clear_{key}") is True:
                    current[key] = ""
                elif str(overrides.get(key, "")).strip():
                    current[key] = str(overrides[key]).strip()
            for key in (
                "tmdb_base_url",
                "tmdb_image_base_url",
                "web_search_base_url",
                "web_reader_base_url",
                "web_reader_enabled",
            ):
                if key in overrides:
                    current[key] = overrides[key]

        tmdb_key = str(current["tmdb_api_key"]).strip()
        search_key = str(current["web_search_api_key"]).strip()
        if len(tmdb_key) > 10_000 or len(search_key) > 10_000:
            raise ValueError("API 密钥过长")
        reader_raw = current["web_reader_enabled"]
        reader_enabled = reader_raw if isinstance(reader_raw, bool) else str(reader_raw).lower() == "true"
        return InternetConfig(
            tmdb_api_key=tmdb_key,
            tmdb_base_url=_url(current["tmdb_base_url"], "TMDB 接口地址"),
            tmdb_image_base_url=_url(current["tmdb_image_base_url"], "海报图片地址"),
            web_search_provider=_search_provider(current["web_search_provider"]),
            web_search_api_key=search_key,
            web_search_base_url=_url(current["web_search_base_url"], "Web Search 接口地址"),
            web_reader_enabled=reader_enabled,
            web_reader_base_url=_url(current["web_reader_base_url"], "公开页面阅读地址"),
        )

    def public_config(self) -> dict[str, Any]:
        config = self.config()
        return {
            "tmdb": {
                "base_url": config.tmdb_base_url,
                "image_base_url": config.tmdb_image_base_url,
                "has_api_key": bool(config.tmdb_api_key),
                "api_key_hint": _secret_hint(config.tmdb_api_key),
                "enabled": config.movie_enabled,
            },
            "web_search": {
                "provider": config.web_search_provider,
                "base_url": config.web_search_base_url,
                "has_api_key": bool(config.web_search_api_key),
                "api_key_hint": _secret_hint(config.web_search_api_key),
                "enabled": config.search_enabled,
            },
            "web_reader": {
                "enabled": config.web_reader_enabled,
                "base_url": config.web_reader_base_url,
                "allowed_domains": ["movie.douban.com", "douban.com", "zhihu.com"],
            },
        }

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.config(payload)
        self.store.set_app_settings(
            {
                "tmdb_api_key": config.tmdb_api_key,
                "tmdb_base_url": config.tmdb_base_url,
                "tmdb_image_base_url": config.tmdb_image_base_url,
                "web_search_provider": config.web_search_provider,
                "web_search_api_key": config.web_search_api_key,
                "web_search_base_url": config.web_search_base_url,
                "web_reader_enabled": "true" if config.web_reader_enabled else "false",
                "web_reader_base_url": config.web_reader_base_url,
            }
        )
        return self.public_config()

    def test_connection(self, payload: dict[str, Any], target: str) -> dict[str, Any]:
        config = self.config(payload)
        if target == "tmdb":
            if not config.movie_enabled:
                raise ValueError("请先填写 TMDB API 凭据")
            result = self._tmdb_get("search/movie", {"query": "星际穿越", "language": "zh-CN"}, config)
            return {"target": target, "items": len(result.get("results") or [])}
        if target == "web_search":
            if not config.search_enabled:
                raise ValueError("请先填写 Web Search API 密钥")
            return {"target": target, "items": len(self.web_search("电影", "web", 1, config))}
        raise ValueError("未知的连接测试目标")

    def search_movies(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        config = self.config()
        if not config.movie_enabled or not query.strip():
            return []
        payload = self._tmdb_get(
            "search/movie",
            {"query": query[:160], "language": "zh-CN", "include_adult": "false"},
            config,
        )
        records = [self._movie_record(item, config, []) for item in payload.get("results") or []]
        records = [record for record in records if record][: max(1, min(limit, 12))]
        self.store.upsert_movies(records)
        return [self.store.movie(record["id"]) for record in records if self.store.movie(record["id"])]

    def discover_movies(self, request: str, limit: int = 15) -> list[dict[str, Any]]:
        config = self.config()
        if not config.movie_enabled:
            return []
        genres = [
            genre
            for word, genre in DISCOVERY_GENRES.items()
            if word in request
            and not any(phrase in request for phrase in (f"不要{word}", f"不想看{word}", f"避开{word}", f"不喜欢{word}"))
        ]
        params: dict[str, Any] = {
            "language": "zh-CN",
            "include_adult": "false",
            "include_video": "false",
            "sort_by": "popularity.desc",
            "primary_release_date.lte": datetime.now(UTC).date().isoformat(),
        }
        if genres:
            params["with_genres"] = "|".join(str(item) for item in sorted(set(genres)))
        payload = self._tmdb_get("discover/movie", params, config)
        hints = sorted({tag for word, tags in MOOD_HINTS.items() if word in request for tag in tags})
        records = [self._movie_record(item, config, hints) for item in payload.get("results") or []]
        records = [record for record in records if record][: max(1, min(limit, 20))]
        self.store.upsert_movies(records)
        return [self.store.movie(record["id"]) for record in records if self.store.movie(record["id"])]

    def hydrate_movie_posters(
        self, movies: list[dict[str, Any]], limit: int = 3
    ) -> list[dict[str, Any]]:
        """Persist missing TMDB posters while preserving curated movie IDs and metadata."""
        config = self.config()
        if not config.movie_enabled:
            return movies
        hydrated: list[dict[str, Any]] = []
        remaining = max(0, min(int(limit), 5))
        for movie in movies:
            current = dict(movie)
            if current.get("poster_url") or remaining <= 0:
                hydrated.append(current)
                continue
            remaining -= 1
            try:
                payload = self._tmdb_get(
                    "search/movie",
                    {
                        "query": str(current.get("title_zh") or current.get("title_original") or "")[:160],
                        "language": "zh-CN",
                        "include_adult": "false",
                    },
                    config,
                )
            except RuntimeError:
                hydrated.append(current)
                continue
            candidates = [
                item for item in payload.get("results") or []
                if str(item.get("poster_path") or "").startswith("/")
            ]
            if not candidates:
                hydrated.append(current)
                continue
            expected_titles = {
                _normalized_movie_title(current.get("title_zh")),
                _normalized_movie_title(current.get("title_original")),
                *(_normalized_movie_title(alias) for alias in current.get("aliases") or []),
            }
            expected_titles.discard("")
            expected_year = int(current.get("year") or 0)

            def match_score(item: dict[str, Any]) -> tuple[int, float]:
                titles = {
                    _normalized_movie_title(item.get("title")),
                    _normalized_movie_title(item.get("original_title")),
                }
                release_year = str(item.get("release_date") or "")[:4]
                score = 8 if expected_titles.intersection(titles) else 0
                if expected_year and release_year == str(expected_year):
                    score += 5
                return score, float(item.get("popularity") or 0)

            match = max(candidates, key=match_score)
            if match_score(match)[0] < 5:
                hydrated.append(current)
                continue
            external_id = str(match.get("id") or "")
            poster_path = str(match["poster_path"])
            current["poster_url"] = config.tmdb_image_base_url + poster_path
            if external_id:
                current["source_url"] = f"https://www.themoviedb.org/movie/{external_id}"
                current["external_ids"] = {
                    **(current.get("external_ids") or {}),
                    "tmdb": external_id,
                }
            self.store.upsert_movies([current])
            refreshed = {
                **current,
                **(self.store.movie(str(current["id"])) or {}),
            }
            hydrated.append(refreshed)
        return hydrated

    def web_search(
        self,
        query: str,
        source: str = "web",
        limit: int = 5,
        config: InternetConfig | None = None,
    ) -> list[dict[str, str]]:
        config = config or self.config()
        if not config.search_enabled:
            raise RuntimeError("管理员尚未配置 Web Search API")
        domain = {
            "douban": "movie.douban.com",
            "zhihu": "zhihu.com",
            "web": "",
        }.get(source)
        if domain is None:
            raise ValueError("source 只允许 web、douban 或 zhihu")
        text = query.strip()[:300]
        if not text:
            return []
        result_limit = max(1, min(int(limit), 10))
        if config.web_search_provider == "tavily":
            body: dict[str, Any] = {
                "query": text,
                "topic": "general",
                "search_depth": "basic",
                "max_results": result_limit,
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
            }
            if domain:
                body["include_domains"] = [domain]
            payload = self._json_request(
                config.web_search_base_url + "/search",
                {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {config.web_search_api_key}",
                    "User-Agent": "Yingban/0.5",
                },
                body,
            )
            source_items = payload.get("results") or []
            snippet_key = "content"
        else:
            if domain:
                text = f"site:{domain} {text}"
            url = config.web_search_base_url + "/web/search?" + urllib.parse.urlencode(
                {"q": text, "count": result_limit, "search_lang": "zh-hans"}
            )
            payload = self._json_request(
                url,
                {
                    "Accept": "application/json",
                    "X-Subscription-Token": config.web_search_api_key,
                    "User-Agent": "Yingban/0.5",
                },
            )
            source_items = (payload.get("web") or {}).get("results") or []
            snippet_key = "description"
        results = []
        for item in source_items:
            results.append(
                {
                    "title": str(item.get("title", ""))[:300],
                    "url": str(item.get("url", ""))[:2000],
                    "snippet": re.sub(r"<[^>]+>", "", str(item.get(snippet_key, "")))[:1200],
                    "source": source,
                }
            )
        return results

    def read_public_page(self, url: str, max_chars: int = 12_000) -> dict[str, str]:
        config = self.config()
        if not config.web_reader_enabled:
            raise RuntimeError("管理员已关闭公开页面阅读")
        parsed = urllib.parse.urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        allowed = host == "douban.com" or host.endswith(".douban.com") or host == "zhihu.com" or host.endswith(".zhihu.com")
        if parsed.scheme not in {"http", "https"} or not allowed or parsed.username:
            raise ValueError("只允许读取公开的豆瓣或知乎 http/https 页面")
        target = config.web_reader_base_url + "/http://" + host + (parsed.path or "/")
        if parsed.query:
            target += "?" + parsed.query
        request = urllib.request.Request(
            target,
            headers={"Accept": "text/plain", "User-Agent": "Yingban/0.3 (+public-research)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                text = response.read(max(1, min(max_chars, 30_000)) * 4).decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"公开页面读取失败（HTTP {error.code}）") from error
        return {"url": url, "content": text[: max(1, min(max_chars, 30_000))]}

    def _tmdb_get(self, path: str, params: dict[str, Any], config: InternetConfig) -> dict[str, Any]:
        query = dict(params)
        headers = {"Accept": "application/json", "User-Agent": "Yingban/0.3"}
        if config.tmdb_api_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {config.tmdb_api_key}"
        else:
            query["api_key"] = config.tmdb_api_key
        url = f"{config.tmdb_base_url}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"
        return self._json_request(url, headers)

    @staticmethod
    def _json_request(
        url: str, headers: dict[str, str], body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request_headers = dict(headers)
        encoded_body = None
        if body is not None:
            encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=encoded_body,
            headers=request_headers,
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read(2_000_000)
        except urllib.error.HTTPError as error:
            detail = error.read(1000).decode("utf-8", "replace")
            raise RuntimeError(f"外部服务返回 HTTP {error.code}: {detail[:240]}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"无法连接外部服务：{error.reason}") from error
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("外部服务没有返回有效 JSON") from error
        if not isinstance(value, dict):
            raise RuntimeError("外部服务响应格式不正确")
        return value

    @staticmethod
    def _movie_record(item: dict[str, Any], config: InternetConfig, mood_hints: list[str]) -> dict[str, Any] | None:
        try:
            external_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            return None
        title = str(item.get("title") or item.get("original_title") or "").strip()
        if not title:
            return None
        original = str(item.get("original_title") or title).strip()
        release_date = str(item.get("release_date") or "")
        year_match = re.match(r"(\d{4})", release_date)
        year = int(year_match.group(1)) if year_match else 0
        genres = [TMDB_GENRES[genre] for genre in item.get("genre_ids") or [] if genre in TMDB_GENRES]
        poster_path = str(item.get("poster_path") or "")
        return {
            "id": f"tmdb-{external_id}",
            "title_zh": title,
            "title_original": original,
            "aliases": [] if title == original else [original],
            "year": year,
            "directors": [],
            "regions": [str(value) for value in item.get("origin_country") or []],
            "genres": genres,
            "themes": [],
            "moods": mood_hints,
            "content_notes": [],
            "summary": str(item.get("overview") or "暂无简介").strip()[:4000],
            "popularity_rank": max(1, 1000 - int(float(item.get("popularity") or 0))),
            "source": "tmdb",
            "poster_url": config.tmdb_image_base_url + poster_path if poster_path.startswith("/") else "",
            "source_url": f"https://www.themoviedb.org/movie/{external_id}",
            "external_ids": {"tmdb": str(external_id)},
        }
