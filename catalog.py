from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from storage import Store


INTENT_TAGS: dict[str, list[str]] = {
    "放松": ["轻松", "温暖", "治愈", "喜剧", "轻盈"],
    "轻松": ["轻松", "喜剧", "热闹", "轻盈"],
    "治愈": ["治愈", "温暖", "接纳", "陪伴", "家庭"],
    "难过": ["温暖", "治愈", "共鸣", "陪伴", "告别"],
    "孤独": ["孤独", "连接", "友谊", "温暖", "陪伴"],
    "迷茫": ["成长", "选择", "人生意义", "勇气", "自由"],
    "没方向": ["成长", "选择", "人生意义", "勇气"],
    "压力": ["轻松", "治愈", "鼓舞", "坚持", "有力量"],
    "考试": ["鼓舞", "坚持", "成长", "有力量", "轻松"],
    "学习": ["坚持", "成长", "鼓舞", "有力量"],
    "工作": ["工作", "坚持", "选择", "有力量", "轻松"],
    "失恋": ["关系", "告别", "成长", "接纳", "温暖"],
    "家庭": ["家庭", "亲情", "接纳", "温暖"],
    "烧脑": ["烧脑", "悬疑", "梦境", "证据", "思考"],
    "刺激": ["紧张", "动作", "悬疑", "冒险", "震撼"],
    "科幻": ["科幻", "宏大", "人类未来", "梦境"],
    "喜剧": ["喜剧", "轻松", "热闹", "荒诞"],
    "感动": ["感动", "温暖", "亲情", "友谊", "希望"],
    "力量": ["有力量", "鼓舞", "坚持", "希望", "勇气"],
    "新片": ["新片"],
    "最近上映": ["新片"],
    "最近的电影": ["新片"],
}


def normalize(value: str) -> str:
    return re.sub(r"[\s·:：,，。.!！?？'\"《》〈〉()（）\-_]", "", value).lower()


class MovieCatalog:
    def __init__(self, store: Store, seed_path: Path):
        self.store = store
        self.seed_path = Path(seed_path)

    def load_seed(self) -> int:
        records = json.loads(self.seed_path.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError("movie seed must be a JSON array")
        self.store.upsert_movies(records)
        return len(records)

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        needle = normalize(query)
        if not needle:
            return []
        scored: list[tuple[int, dict[str, Any]]] = []
        for movie in self.store.all_movies():
            names = [movie["title_zh"], movie["title_original"], *movie["aliases"]]
            normalized_names = [normalize(str(name)) for name in names]
            if needle in normalized_names:
                score = 100
            elif any(needle in name or name in needle for name in normalized_names):
                score = 70
            else:
                tokens = [part for part in re.split(r"\s+", query.strip()) if part]
                score = sum(
                    10 for token in tokens if any(normalize(token) in name for name in normalized_names)
                )
            if score:
                score += max(0, 20 - int(movie["popularity_rank"]))
                scored.append((score, movie))
        scored.sort(key=lambda item: (-item[0], item[1]["popularity_rank"]))
        return [movie for _score, movie in scored[:limit]]

    def detect_in_text(self, text: str) -> list[dict[str, Any]]:
        haystack = normalize(text)
        matches: list[dict[str, Any]] = []
        for movie in self.store.all_movies():
            names = [movie["title_zh"], movie["title_original"], *movie["aliases"]]
            if any(len(normalize(name)) >= 2 and normalize(name) in haystack for name in names):
                matches.append(movie)
        return matches

    def recommend(
        self,
        account_id: str,
        request: str,
        limit: int = 3,
        include_recent: bool = False,
    ) -> list[dict[str, Any]]:
        watched = self.store.watched_ids(account_id)
        persistently_excluded = self.store.persistently_excluded_ids(account_id)
        recent = set() if include_recent else self.store.recent_recommended_ids(account_id)
        wanted_tags = self._wanted_tags(request)
        profile = self.store.account_profile(account_id)
        for dimension in profile.get("taste_dimensions", []):
            if dimension.get("direction") == "prefer" and not dimension.get("hidden"):
                wanted_tags.add(str(dimension.get("label", "")).split(" · ", 1)[-1])
        excluded_tags = self._excluded_tags(request)

        ranked: list[tuple[float, dict[str, Any], list[str]]] = []
        for movie in self.store.all_movies():
            if movie["id"] in watched or movie["id"] in persistently_excluded or movie["id"] in recent:
                continue
            searchable_tags = {
                *movie["genres"],
                *movie["themes"],
                *movie["moods"],
                *movie["content_notes"],
            }
            if excluded_tags.intersection(searchable_tags):
                continue
            matched = sorted(wanted_tags.intersection(searchable_tags))
            score = len(matched) * 12 + max(0, 30 - int(movie["popularity_rank"])) / 10
            if not wanted_tags:
                score += max(0, 30 - int(movie["popularity_rank"])) / 4
            ranked.append((score, movie, matched))

        ranked.sort(key=lambda item: (-item[0], item[1]["popularity_rank"]))
        selected: list[dict[str, Any]] = []
        used_primary_moods: set[str] = set()
        for score, movie, matched in ranked:
            primary_mood = movie["moods"][0] if movie["moods"] else ""
            if primary_mood in used_primary_moods and len(selected) < 2:
                continue
            result = dict(movie)
            result["match_tags"] = matched[:4]
            result["match_reason"] = self._reason(movie, matched, request)
            result["match_score"] = round(score, 2)
            selected.append(result)
            if primary_mood:
                used_primary_moods.add(primary_mood)
            if len(selected) >= limit:
                break

        if len(selected) < limit:
            selected_ids = {item["id"] for item in selected}
            for _score, movie, matched in ranked:
                if movie["id"] in selected_ids:
                    continue
                result = dict(movie)
                result["match_tags"] = matched[:4]
                result["match_reason"] = self._reason(movie, matched, request)
                result["match_score"] = round(_score, 2)
                selected.append(result)
                if len(selected) >= limit:
                    break

        impression_ids = self.store.record_recommendations(account_id, selected, request)
        for item in selected:
            item["impression_id"] = impression_ids.get(str(item["id"]), "")
        return selected

    @staticmethod
    def _wanted_tags(request: str) -> set[str]:
        tags: set[str] = set()
        lowered = request.lower()
        for phrase, mapped in INTENT_TAGS.items():
            if phrase in lowered:
                tags.update(mapped)
        return tags

    @staticmethod
    def _excluded_tags(request: str) -> set[str]:
        tags: set[str] = set()
        lowered = request.lower()
        known = {
            "爱情",
            "暴力",
            "死亡",
            "战争",
            "疾病",
            "恐怖",
            "悬疑",
            "沉重",
            "催泪",
            "犯罪",
        }
        for tag in known:
            patterns = (f"不想看{tag}", f"不要{tag}", f"不喜欢{tag}", f"避开{tag}")
            if any(pattern in lowered for pattern in patterns):
                tags.add(tag)
        return tags

    @staticmethod
    def _reason(movie: dict[str, Any], matched: list[str], request: str) -> str:
        if matched:
            joined = "、".join(matched[:3])
            return f"它和你刚才提到的需要，在{joined}这些方向上比较接近。"
        if request.strip():
            return f"它在热门片库里辨识度很高，也保留了{movie['moods'][0]}的观看感受。"
        return "它是片库中较受欢迎、也适合作为下一部选择的影片。"
