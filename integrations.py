from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from settings import Settings
from storage import Store
from catalog import movie_recency_key


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

WEB_SEARCH_PROVIDERS = {"bocha", "tavily", "brave"}

BOX_OFFICE_SOURCE_URL = "https://www.zgdypw.cn/"
BOX_OFFICE_DATA_URL = "https://www.zgdypw.cn/data/searchDayBoxOffice.json"
BOX_OFFICE_CACHE_KEY = "daily_box_office_snapshot_v1"
BOX_OFFICE_CACHE_TTL = timedelta(hours=6)
BOX_OFFICE_TIMEZONE = ZoneInfo("Asia/Shanghai")
DOUBAN_MOVIE_URL_RE = re.compile(
    r"^https?://movie\.douban\.com/subject/(?P<id>[0-9]+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)
DOUBAN_POSTER_PATH_RE = re.compile(
    r"^/view/photo/s_ratio_poster/public/(?P<filename>p[0-9]{6,20}\.jpg)$",
    re.IGNORECASE,
)
DOUBAN_POSTER_FILENAME_RE = re.compile(r"^p[0-9]{6,20}\.jpg$", re.IGNORECASE)
DOUBAN_SUGGEST_URL = "https://movie.douban.com/j/subject_suggest"
DOUBAN_POSTER_ORIGIN = "https://img1.doubanio.com/view/photo/s_ratio_poster/public"
ADULT_SEARCH_MARKERS_RE = re.compile(
    r"成人视频|成人影片|成人电影|成人片|色情|情色|真刀实枪|真刀真枪",
    re.IGNORECASE,
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


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
        raise ValueError("Web Search 服务商只允许 bocha、tavily 或 brave")
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
        self._box_office_lock = threading.Lock()
        self._suggest_lock = threading.Lock()
        self._suggest_cache: OrderedDict[str, tuple[float, Future]] = OrderedDict()
        self._tmdb_search_retry_at = 0.0

    def config(self, overrides: dict[str, Any] | None = None) -> InternetConfig:
        saved = self.store.get_app_settings(INTERNET_SETTING_KEYS)
        saved_search_base_url = saved.get(
            "web_search_base_url", self.settings.web_search_base_url
        )
        saved_search_provider = saved.get("web_search_provider", "")
        if not saved_search_provider:
            if "api.bochaai.com" in saved_search_base_url:
                saved_search_provider = "bocha"
            elif "api.search.brave.com" in saved_search_base_url:
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
                if "api.bochaai.com" in override_search_url:
                    overrides = {**overrides, "web_search_provider": "bocha"}
                elif "api.search.brave.com" in override_search_url:
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
        self._tmdb_search_retry_at = 0.0
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
        text = query.strip()
        if not text:
            return []
        result_limit = max(1, min(limit, 12))
        tmdb_error: RuntimeError | None = None
        if config.movie_enabled and time.monotonic() >= self._tmdb_search_retry_at:
            try:
                payload = self._tmdb_get(
                    "search/movie",
                    {
                        "query": text[:160],
                        "language": "zh-CN",
                        "include_adult": "false",
                    },
                    config,
                    timeout_seconds=2.0 if config.search_enabled else 6.0,
                )
                records = [
                    self._movie_record(item, config, [])
                    for item in payload.get("results") or []
                ]
                records = sorted(
                    [record for record in records if record], key=movie_recency_key
                )[:result_limit]
                if records:
                    self.store.upsert_movies(records)
                    return [
                        movie
                        for record in records
                        if (movie := self.store.movie(record["id"])) is not None
                    ]
            except RuntimeError as error:
                tmdb_error = error
                self._tmdb_search_retry_at = time.monotonic() + 60

        elif config.movie_enabled:
            tmdb_error = RuntimeError("TMDB 搜索正在短暂冷却")

        if config.search_enabled:
            try:
                results = self.web_search(
                    f"{text[:240]} 电影",
                    "douban",
                    min(10, max(result_limit, result_limit * 2)),
                    config,
                )
                records = self._douban_movie_records(results, 10)
                if records:
                    self.store.upsert_movies(records)
                    stored = [
                        movie
                        for record in records
                        if (movie := self.store.movie(record["id"])) is not None
                    ]
                    return sorted(
                        self.hydrate_douban_posters(stored, limit=8),
                        key=movie_recency_key,
                    )[:result_limit]
            except RuntimeError as error:
                if tmdb_error is not None:
                    raise RuntimeError("电影搜索服务暂时不可用，请稍后重试") from error
                raise

        if tmdb_error is not None:
            raise RuntimeError("电影搜索服务暂时不可用，请稍后重试") from tmdb_error
        return []

    @staticmethod
    def _douban_movie_records(
        results: list[dict[str, str]], limit: int
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for position, item in enumerate(results):
            url_match = DOUBAN_MOVIE_URL_RE.match(str(item.get("url") or "").strip())
            if url_match is None:
                continue
            external_id = url_match.group("id")
            if external_id in seen_ids:
                continue
            title = str(item.get("title") or "").strip()
            title = re.sub(r"(?:[_\s-]*影视)?[_\s-]*豆瓣$", "", title).strip(" _-")
            title = re.sub(r"\s*[（(]豆瓣[）)]\s*$", "", title).strip()
            if not title or re.search(r"(?:影评|短评|讨论|图片|预告片)$", title):
                continue

            snippet = re.sub(r"\s+", " ", str(item.get("snippet") or "")).strip()
            if ADULT_SEARCH_MARKERS_RE.search(f"{title} {snippet}"):
                continue
            directors = InternetRuntime._metadata_values(
                snippet, "导演", ("编剧", "主演", "类型")
            )
            genres = InternetRuntime._metadata_values(
                snippet,
                "类型",
                ("制片国家/地区", "语言", "上映日期", "首播", "片长", "集数", "官方网站"),
            )
            if {"情色", "成人"}.intersection(genres):
                continue
            regions = InternetRuntime._metadata_values(
                snippet, "制片国家/地区", ("语言", "上映日期", "首播", "片长", "集数")
            )
            aliases = InternetRuntime._metadata_values(
                snippet, "又名", ("IMDb", "豆瓣评分", "评价", "剧情简介")
            )
            aliases = [alias for alias in aliases if alias != title]
            release_match = re.search(
                r"(?:上映日期|首播)\s*[:：]\s*((?:19|20)\d{2}(?:-\d{2}-\d{2})?)",
                snippet,
            )
            release_date = release_match.group(1) if release_match else ""
            year = int(release_date[:4]) if release_date else 0
            summary = InternetRuntime._douban_summary(snippet)
            records.append(
                {
                    "id": f"douban-{external_id}",
                    "title_zh": title,
                    "title_original": aliases[0] if aliases else title,
                    "aliases": aliases,
                    "year": year,
                    "directors": directors,
                    "regions": regions,
                    "genres": genres,
                    "themes": [],
                    "moods": [],
                    "content_notes": [],
                    "summary": summary or "暂无简介",
                    "popularity_rank": 2000 + position,
                    "source": "douban-web-search",
                    "poster_url": "",
                    "source_url": f"https://movie.douban.com/subject/{external_id}/",
                    "external_ids": {"douban": external_id},
                    "release_date": release_date,
                }
            )
            seen_ids.add(external_id)
            if len(records) >= limit:
                break
        return records

    def hydrate_douban_posters(
        self, movies: list[dict[str, Any]], limit: int = 8
    ) -> list[dict[str, Any]]:
        """Fill cached Douban search records without turning poster failure into search failure."""
        hydrated = [dict(movie) for movie in movies]
        candidates: list[tuple[int, dict[str, Any]]] = []
        remaining = max(0, min(int(limit), 8))
        for index, movie in enumerate(hydrated):
            external_ids = movie.get("external_ids") or {}
            if (
                remaining <= 0
                or (movie.get("poster_url") and movie.get("year"))
                or not isinstance(external_ids, dict)
                or re.fullmatch(
                    r"[0-9]+", str(external_ids.get("douban") or "")
                ) is None
            ):
                continue
            candidates.append((index, movie))
            remaining -= 1

        def resolve(candidate: tuple[int, dict[str, Any]]) -> tuple[int, str]:
            index, movie = candidate
            try:
                # The resolver may also fill an unknown year from an exact subject match.
                return index, self._douban_poster_url(movie)
            except Exception:  # noqa: BLE001 - poster enrichment must stay fail-soft
                return index, ""

        if len(candidates) == 1:
            resolved = [resolve(candidates[0])]
        elif candidates:
            with ThreadPoolExecutor(
                max_workers=min(4, len(candidates)),
                thread_name_prefix="yingban-douban-poster",
            ) as executor:
                resolved = list(executor.map(resolve, candidates))
        else:
            resolved = []

        updates = []
        for index, poster_url in resolved:
            current = dict(hydrated[index])
            if poster_url and not current.get("poster_url"):
                current["poster_url"] = poster_url
            if current == movies[index]:
                continue
            hydrated[index] = current
            updates.append(current)
        if updates:
            self.store.upsert_movies(updates)
        return hydrated

    @staticmethod
    def _fetch_douban_suggestions(title: str) -> list[dict[str, Any]]:
        url = DOUBAN_SUGGEST_URL + "?" + urllib.parse.urlencode({"q": title[:160]})
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Referer": "https://movie.douban.com/",
                "User-Agent": "Yingban/0.7 (+movie-search)",
            },
        )
        opener = urllib.request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=5) as response:
                raw = response.read(300_000)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"豆瓣电影建议返回 HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError("无法连接豆瓣电影建议") from error
        try:
            suggestions = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("豆瓣电影建议没有返回有效 JSON") from error
        if not isinstance(suggestions, list):
            raise RuntimeError("豆瓣电影建议响应格式不正确")
        return suggestions

    def _douban_suggestions(self, title: str) -> list[dict[str, Any]]:
        """Share concurrent title lookups; briefly cache misses and upstream failures."""
        key = title.strip()[:160]
        with self._suggest_lock:
            cached = self._suggest_cache.get(key)
            if cached and (not cached[1].done() or cached[0] > time.monotonic()):
                future = cached[1]
                owner = False
            else:
                future = Future()
                self._suggest_cache[key] = (float("inf"), future)
                self._suggest_cache.move_to_end(key)
                owner = True
        if not owner:
            return future.result()
        try:
            suggestions = self._fetch_douban_suggestions(key)
        except Exception:  # noqa: BLE001 - failed enrichment remains retryable
            suggestions = []
        with self._suggest_lock:
            self._suggest_cache[key] = (
                time.monotonic() + (900 if suggestions else 60), future
            )
            future.set_result(suggestions)
            for old_key in list(self._suggest_cache):
                if len(self._suggest_cache) <= 128:
                    break
                if self._suggest_cache[old_key][1].done():
                    del self._suggest_cache[old_key]
        return suggestions

    def _douban_poster_url(self, movie: dict[str, Any]) -> str:
        external_ids = movie.get("external_ids") or {}
        expected_id = str(external_ids.get("douban") or "") if isinstance(external_ids, dict) else ""
        title = str(movie.get("title_zh") or movie.get("title_original") or "").strip()
        if re.fullmatch(r"[0-9]+", expected_id) is None or not title:
            return ""

        for item in self._douban_suggestions(title):
            if (
                not isinstance(item, dict)
                or str(item.get("type") or "") != "movie"
                or str(item.get("id") or "") != expected_id
            ):
                continue
            year = str(item.get("year") or "")
            if not movie.get("year") and re.fullmatch(r"(?:18|19|20)[0-9]{2}", year):
                movie["year"] = int(year)
                if not movie.get("release_date"):
                    movie["release_date"] = year
            image_url = str(item.get("img") or "").strip()
            try:
                parsed = urllib.parse.urlparse(image_url)
                port = parsed.port
            except ValueError:
                continue
            host = (parsed.hostname or "").lower()
            path_match = DOUBAN_POSTER_PATH_RE.fullmatch(parsed.path)
            if (
                parsed.scheme != "https"
                or re.fullmatch(r"img[0-9]+\.doubanio\.com", host) is None
                or parsed.username
                or parsed.password
                or port not in (None, 443)
                or parsed.query
                or parsed.fragment
                or path_match is None
            ):
                continue
            return "/api/posters/douban/" + path_match.group("filename").lower()
        return ""

    @staticmethod
    def fetch_douban_poster(filename: str) -> tuple[bytes, str]:
        """Fetch one allowlisted Douban poster for the same-origin proxy."""
        if DOUBAN_POSTER_FILENAME_RE.fullmatch(filename) is None:
            raise ValueError("invalid Douban poster filename")
        last_error = None
        for origin in (
            DOUBAN_POSTER_ORIGIN,
            "https://img3.doubanio.com/view/photo/s_ratio_poster/public",
        ):
            try:
                return InternetRuntime._fetch_douban_poster_from_origin(filename, origin)
            except RuntimeError as error:
                last_error = error
        raise last_error

    @staticmethod
    def _fetch_douban_poster_from_origin(filename: str, origin: str) -> tuple[bytes, str]:
        request = urllib.request.Request(
            f"{origin}/{filename.lower()}",
            headers={
                "Accept": "image/jpeg",
                "Referer": "https://movie.douban.com/",
                "User-Agent": "Yingban/0.7 (+movie-poster-proxy)",
            },
        )
        opener = urllib.request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=4) as response:
                final_host = (
                    urllib.parse.urlparse(response.geturl()).hostname or ""
                ).lower()
                if re.fullmatch(r"img[0-9]+\.doubanio\.com", final_host) is None:
                    raise RuntimeError("豆瓣海报重定向到了未允许的地址")
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
                content = response.read(2_000_001)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"豆瓣海报返回 HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError("无法连接豆瓣海报服务") from error
        except RuntimeError:
            raise
        except Exception as error:  # noqa: BLE001 - normalize upstream read failures
            raise RuntimeError("无法读取豆瓣海报响应") from error
        if content_type != "image/jpeg" or not content.startswith(b"\xff\xd8"):
            raise RuntimeError("豆瓣海报响应不是有效的 JPEG 图片")
        if len(content) > 2_000_000:
            raise RuntimeError("豆瓣海报超过 2 MB 限制")
        return content, content_type

    @staticmethod
    def _metadata_values(
        snippet: str, label: str, next_labels: tuple[str, ...]
    ) -> list[str]:
        boundary = "|".join(re.escape(item) for item in next_labels)
        match = re.search(
            rf"{re.escape(label)}\s*[:：]\s*(.*?)(?=\s+(?:{boundary})\s*[:：]|$)",
            snippet,
        )
        if match is None:
            return []
        return [
            value.strip()
            for value in re.split(r"\s*/\s*", match.group(1))
            if value.strip()
        ]

    @staticmethod
    def _douban_summary(snippet: str) -> str:
        match = re.search(r"剧情简介\s*·?\s*(.+)", snippet)
        if match is None:
            return ""
        summary = re.split(
            r"\s+(?:.+?的演职员|演职员|获奖情况|喜欢这部电影的人也喜欢|.+?的短评|讨论区)\s*·?",
            match.group(1),
            maxsplit=1,
        )[0]
        return summary.strip()[:1200]

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

    def discover_now_playing(
        self, request: str, region: str = "CN", limit: int = 20
    ) -> list[dict[str, Any]]:
        """Load a dated theatrical candidate pool instead of treating old popularity as newness."""
        config = self.config()
        if not config.movie_enabled:
            return []
        payload = self._tmdb_get(
            "movie/now_playing",
            {
                "language": "zh-CN",
                "region": str(region or "CN").upper()[:2],
                "page": 1,
            },
            config,
        )
        hints = sorted({tag for word, tags in MOOD_HINTS.items() if word in request for tag in tags})
        records = [self._movie_record(item, config, hints) for item in payload.get("results") or []]
        records = [record for record in records if record][: max(1, min(limit, 20))]
        self.store.upsert_movies(records)
        return [self.store.movie(record["id"]) for record in records if self.store.movie(record["id"])]

    def daily_box_office(
        self, limit: int = 3, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """Return the official mainland China daily box-office ranking with a durable cache."""
        result_limit = max(1, min(int(limit), 10))
        current = (now or datetime.now(UTC)).astimezone(UTC)
        cached = self._cached_box_office_snapshot()
        if self._box_office_cache_is_fresh(cached, current):
            return self._box_office_result(cached["snapshot"], result_limit, refreshed=False)

        with self._box_office_lock:
            cached = self._cached_box_office_snapshot()
            if self._box_office_cache_is_fresh(cached, current):
                return self._box_office_result(cached["snapshot"], result_limit, refreshed=False)
            try:
                payload = self._json_request(
                    BOX_OFFICE_DATA_URL
                    + "?"
                    + urllib.parse.urlencode({"timestamp": int(current.timestamp() * 1000)}),
                    {
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "User-Agent": "Yingban/0.7 (+daily-box-office)",
                    },
                )
                snapshot = self._normalize_box_office_payload(payload, current)
            except (RuntimeError, TypeError, ValueError):
                if cached and isinstance(cached.get("snapshot"), dict):
                    stale = self._box_office_result(
                        cached["snapshot"], result_limit, refreshed=False
                    )
                    return {**stale, "status": "stale", "stale": True}
                return {
                    "status": "unavailable",
                    "stale": False,
                    "refreshed": False,
                    "business_date": "",
                    "updated_at": "",
                    "source": {
                        "name": "中国电影数据信息网",
                        "url": BOX_OFFICE_SOURCE_URL,
                        "metric": "中国内地当日票房（万元）",
                    },
                    "rankings": [],
                }

            self.store.set_app_settings(
                {
                    BOX_OFFICE_CACHE_KEY: json.dumps(
                        {"fetched_at": snapshot["updated_at"], "snapshot": snapshot},
                        ensure_ascii=False,
                    )
                }
            )
            return self._box_office_result(snapshot, result_limit, refreshed=True)

    def _cached_box_office_snapshot(self) -> dict[str, Any] | None:
        raw = self.store.get_app_settings((BOX_OFFICE_CACHE_KEY,)).get(
            BOX_OFFICE_CACHE_KEY, ""
        )
        if not raw:
            return None
        try:
            cached = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(cached, dict) or not isinstance(cached.get("snapshot"), dict):
            return None
        return cached

    @staticmethod
    def _box_office_cache_is_fresh(
        cached: dict[str, Any] | None, current: datetime
    ) -> bool:
        if not cached:
            return False
        try:
            fetched_at = datetime.fromisoformat(str(cached.get("fetched_at") or ""))
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=UTC)
        except ValueError:
            return False
        age = current - fetched_at.astimezone(UTC)
        same_china_date = (
            current.astimezone(BOX_OFFICE_TIMEZONE).date()
            == fetched_at.astimezone(BOX_OFFICE_TIMEZONE).date()
        )
        return same_china_date and timedelta(0) <= age < BOX_OFFICE_CACHE_TTL

    @staticmethod
    def _box_office_result(
        snapshot: dict[str, Any], limit: int, *, refreshed: bool
    ) -> dict[str, Any]:
        rankings = snapshot.get("rankings")
        current_business_date = snapshot.get("is_current_business_date") is not False
        return {
            **{
                key: value
                for key, value in snapshot.items()
                if key != "is_current_business_date"
            },
            "status": "ready" if current_business_date else "stale",
            "stale": not current_business_date,
            "refreshed": refreshed,
            "rankings": list(rankings[:limit]) if isinstance(rankings, list) else [],
        }

    @staticmethod
    def _normalize_box_office_payload(
        payload: dict[str, Any], fetched_at: datetime
    ) -> dict[str, Any]:
        if str(payload.get("code")) != "200" or payload.get("status") != "success":
            raise RuntimeError("官方票房数据暂时不可用")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("官方票房数据格式不正确")
        business_date = str(data.get("businessDay") or "")[:10]
        try:
            datetime.strptime(business_date, "%Y-%m-%d")
        except ValueError as error:
            raise RuntimeError("官方票房数据缺少有效日期") from error
        films = data.get("top10Films")
        if not isinstance(films, list):
            raise RuntimeError("官方票房数据缺少影片榜单")

        rankings: list[dict[str, Any]] = []
        seen_titles: set[str] = set()
        for item in films:
            if not isinstance(item, dict):
                continue
            title = str(item.get("filmName") or "").strip()
            if not title or title in seen_titles:
                continue
            try:
                rank = int(item.get("rank"))
                day_box_office = float(item.get("daySales"))
                sessions = int(item.get("daySession") or 0)
                audience = int(item.get("dayAudience") or 0)
                cumulative = float(item.get("filmTotalSales") or 0)
            except (TypeError, ValueError):
                continue
            if rank < 1 or day_box_office < 0 or sessions <= 0 or audience < 0:
                continue
            seen_titles.add(title)
            rankings.append(
                {
                    "rank": rank,
                    "title": title,
                    "day_box_office_wan": round(day_box_office, 2),
                    "cumulative_box_office_wan": round(cumulative, 2),
                    "sessions": sessions,
                    "audience": audience,
                }
            )
        rankings.sort(key=lambda item: item["rank"])
        if not rankings:
            raise RuntimeError("官方票房榜单为空")
        return {
            "business_date": business_date,
            "updated_at": fetched_at.astimezone(UTC).isoformat(),
            "is_current_business_date": (
                business_date
                == fetched_at.astimezone(BOX_OFFICE_TIMEZONE).date().isoformat()
            ),
            "source": {
                "name": "中国电影数据信息网",
                "url": BOX_OFFICE_SOURCE_URL,
                "metric": "中国内地当日票房（万元）",
            },
            "rankings": rankings[:10],
        }

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
        if config.web_search_provider == "bocha":
            body = {
                "query": text,
                "freshness": "noLimit",
                "summary": True,
                "count": result_limit,
            }
            if domain:
                body["include"] = domain
            try:
                payload = self._json_request(
                    config.web_search_base_url + "/web-search",
                    {
                        "Accept": "application/json",
                        "Authorization": f"Bearer {config.web_search_api_key}",
                        "User-Agent": "Yingban/0.6",
                    },
                    body,
                )
            except RuntimeError as error:
                raise RuntimeError(self._bocha_error_message(error)) from error
            response_code = payload.get("code")
            if response_code not in (None, 0, 200):
                raise RuntimeError(
                    self._bocha_error_message(
                        RuntimeError(f"博查返回错误 {response_code}: {payload.get('msg', '')}")
                    )
                )
            source_items = (
                ((payload.get("data") or {}).get("webPages") or {}).get("value")
                or []
            )
            title_key = "name"
            snippet_keys = ("summary", "snippet")
        elif config.web_search_provider == "tavily":
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
            title_key = "title"
            snippet_keys = ("content",)
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
            title_key = "title"
            snippet_keys = ("description",)
        results = []
        for item in source_items:
            snippet = next(
                (str(item.get(key) or "") for key in snippet_keys if item.get(key)),
                "",
            )
            results.append(
                {
                    "title": str(item.get(title_key, ""))[:300],
                    "url": str(item.get("url", ""))[:2000],
                    "snippet": re.sub(r"<[^>]+>", "", snippet)[:1200],
                    "source": source,
                }
            )
        return results

    @staticmethod
    def _bocha_error_message(error: Exception) -> str:
        detail = str(error)
        if re.search(r"(?:HTTP|错误)\s*401\b", detail):
            return "博查 API 密钥无效，请在博查开放平台重新复制密钥后保存。"
        if re.search(r"(?:HTTP|错误)\s*403\b", detail):
            return "博查账户余额或可用额度不足，请在博查开放平台检查账户后重试。"
        if re.search(r"(?:HTTP|错误)\s*429\b", detail):
            return "博查请求过于频繁，请稍后再试。"
        return "博查搜索暂时不可用，请检查接口地址和网络后重试。"

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

    def _tmdb_get(
        self,
        path: str,
        params: dict[str, Any],
        config: InternetConfig,
        *,
        timeout_seconds: float = 25,
    ) -> dict[str, Any]:
        query = dict(params)
        headers = {"Accept": "application/json", "User-Agent": "Yingban/0.3"}
        if config.tmdb_api_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {config.tmdb_api_key}"
        else:
            query["api_key"] = config.tmdb_api_key
        url = f"{config.tmdb_base_url}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"
        tmdb_host = (urllib.parse.urlparse(config.tmdb_base_url).hostname or "").lower()
        return self._json_request(
            url,
            headers,
            direct_fallback=tmdb_host == "api.themoviedb.org",
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _json_request(
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
        *,
        direct_fallback: bool = False,
        timeout_seconds: float = 25,
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
            try:
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    raw = response.read(2_000_000)
            except urllib.error.URLError as error:
                reason = str(error.reason).lower()
                if not direct_fallback or "tunnel connection failed" not in reason:
                    raise
                direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with direct_opener.open(request, timeout=timeout_seconds) as response:
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
            "release_date": release_date[:10],
        }
