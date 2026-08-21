from __future__ import annotations

import base64
import http.cookiejar
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

from agent import AgentRuntime  # noqa: E402
from catalog import MovieCatalog  # noqa: E402
from integrations import InternetRuntime  # noqa: E402
from server import AppContext, YingbanHTTPServer, sanitize_agent_reply  # noqa: E402
from settings import Settings  # noqa: E402
from storage import InviteError, Store  # noqa: E402
from voice import VoiceRuntime  # noqa: E402


class ProductFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(
            database_path=root / "test.db",
            movie_seed_path=PRODUCT_DIR / "data" / "movies.json",
            invite_pepper="test-invite-pepper",
            session_secret="test-session-secret",
            admin_token="test-admin-token",
            cookie_secure=False,
            model_api_key="",
        )
        self.store = Store(
            self.settings.database_path,
            self.settings.invite_pepper,
            self.settings.session_secret,
        )
        self.catalog = MovieCatalog(self.store, self.settings.movie_seed_path)
        self.catalog.load_seed()

    def tearDown(self) -> None:
        self.temporary.cleanup()


class InviteAndMemoryTests(ProductFixture):
    def test_same_invite_always_opens_the_same_account(self) -> None:
        code = self.store.generate_invites(1)[0]
        first_account, first_token = self.store.login_with_invite(code, 30)
        second_account, second_token = self.store.login_with_invite(code, 30)

        self.assertEqual(first_account, second_account)
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(self.store.account_for_session(first_token), first_account)
        self.assertEqual(self.store.account_for_session(second_token), first_account)

    def test_revoked_invite_cannot_login(self) -> None:
        code = self.store.generate_invites(1)[0]
        invite_id = self.store.list_invites()[0]["id"]
        self.store.revoke_invite(invite_id)
        with self.assertRaises(InviteError):
            self.store.login_with_invite(code, 30)

    def test_rotation_preserves_account_and_invalidates_old_code(self) -> None:
        code = self.store.generate_invites(1)[0]
        account_id, old_session = self.store.login_with_invite(code, 30)
        invite_id = self.store.list_invites()[0]["id"]
        new_code = self.store.rotate_invite(invite_id)

        self.assertIsNone(self.store.account_for_session(old_session))
        with self.assertRaises(InviteError):
            self.store.login_with_invite(code, 30)
        rotated_account, _token = self.store.login_with_invite(new_code, 30)
        self.assertEqual(rotated_account, account_id)

    def test_recommendations_hard_exclude_watched_movies(self) -> None:
        code = self.store.generate_invites(1)[0]
        account_id, _token = self.store.login_with_invite(code, 30)
        watched = self.store.all_movies()[:5]
        for movie in watched:
            self.store.set_movie_state(account_id, movie["id"], "watched", "test")

        recommendations = self.catalog.recommend(account_id, "最近压力很大，想轻松治愈一点", 3)
        recommended_ids = {movie["id"] for movie in recommendations}
        self.assertTrue(recommendations)
        self.assertFalse(recommended_ids.intersection({movie["id"] for movie in watched}))

    def test_accounts_have_isolated_movie_history(self) -> None:
        first_code, second_code = self.store.generate_invites(2)
        first_account, _ = self.store.login_with_invite(first_code, 30)
        second_account, _ = self.store.login_with_invite(second_code, 30)
        movie_id = "us-interstellar-2014"
        self.store.set_movie_state(first_account, movie_id, "watched", "test")

        self.assertIn(movie_id, self.store.watched_ids(first_account))
        self.assertNotIn(movie_id, self.store.watched_ids(second_account))

    def test_reflection_note_is_scoped_to_account_and_watched_movie(self) -> None:
        first_code, second_code = self.store.generate_invites(2)
        first_account, _ = self.store.login_with_invite(first_code, 30)
        second_account, _ = self.store.login_with_invite(second_code, 30)
        movie_id = "us-interstellar-2014"
        note = "你被亲情与时间的拉扯打动，也对结尾的选择仍有一点复杂感受。"
        self.store.save_movie_reflection(first_account, movie_id, note)
        first = self.store.get_movie_state(first_account, movie_id)
        second = self.store.get_movie_state(second_account, movie_id)
        self.assertEqual(first["state"], "watched")
        self.assertEqual(first["note"], note)
        self.assertIsNone(second)


class AgentConfigurationTests(ProductFixture):
    def test_model_configuration_is_persistent_and_secret_is_masked(self) -> None:
        runtime = AgentRuntime(self.settings, self.store, self.catalog)
        saved = runtime.save_model_config(
            {
                "provider": "openai_compatible",
                "api_key": "sk-test-secret-1234",
                "model_id": "test-chat-model",
                "base_url": "https://models.example.com/v1",
                "timeout_seconds": 45,
                "max_tokens": 1600,
                "temperature": 0.4,
            }
        )

        self.assertTrue(saved["has_api_key"])
        self.assertEqual(saved["api_key_hint"], "••••1234")
        self.assertNotIn("sk-test-secret", json.dumps(saved, ensure_ascii=False))

        reloaded = AgentRuntime(self.settings, self.store, self.catalog).model_config()
        self.assertEqual(reloaded.provider, "openai_compatible")
        self.assertEqual(reloaded.api_key, "sk-test-secret-1234")
        self.assertEqual(reloaded.model_id, "test-chat-model")

    def test_discussion_and_recommendation_prompts_are_independent(self) -> None:
        runtime = AgentRuntime(self.settings, self.store, self.catalog)
        prompts = runtime.save_prompts(
            {
                "discussion": "你是聊电影 Agent。先回应用户的具体观后感，再提出一个自然的问题。",
                "recommendation": "你是找电影 Agent。只从候选片单推荐，并清楚说明选择理由和内容提醒。",
            }
        )
        self.assertIn("聊电影", prompts["discussion"])
        self.assertIn("找电影", prompts["recommendation"])
        self.assertNotEqual(prompts["discussion"], prompts["recommendation"])

    def test_three_openings_are_persistent_and_support_movie_placeholder(self) -> None:
        runtime = AgentRuntime(self.settings, self.store, self.catalog)
        saved = runtime.save_openings(
            {
                "discussion": "先告诉我刚看完哪部电影。",
                "discussion_movie": "《{movie}》散场后，最先留下的感觉是什么？",
                "recommendation": "今晚想找一部什么样的电影？",
            }
        )
        self.assertIn("{movie}", saved["discussion_movie"])
        reloaded = AgentRuntime(self.settings, self.store, self.catalog).openings()
        self.assertEqual(reloaded, saved)


class IntegrationConfigurationTests(ProductFixture):
    def test_internet_and_voice_secrets_are_persistent_and_masked(self) -> None:
        internet = InternetRuntime(self.settings, self.store)
        public_internet = internet.save_config(
            {
                "tmdb_api_key": "tmdb-secret-1234",
                "tmdb_base_url": "https://api.themoviedb.org/3",
                "tmdb_image_base_url": "https://image.tmdb.org/t/p/w500",
                "web_search_provider": "brave",
                "web_search_api_key": "brave-secret-5678",
                "web_search_base_url": "https://api.search.brave.com/res/v1",
                "web_reader_enabled": True,
                "web_reader_base_url": "https://r.jina.ai",
            }
        )
        self.assertEqual(public_internet["tmdb"]["api_key_hint"], "••••1234")
        self.assertEqual(public_internet["web_search"]["api_key_hint"], "••••5678")
        self.assertEqual(public_internet["web_search"]["provider"], "brave")
        self.assertNotIn("tmdb-secret", json.dumps(public_internet, ensure_ascii=False))

        voice = VoiceRuntime(self.settings, self.store)
        public_voice = voice.save_config(
            {
                "provider": "openai_compatible",
                "api_key": "voice-secret-9999",
                "base_url": "https://voice.example.com/v1",
                "stt_model": "speech-to-text",
                "tts_model": "text-to-speech",
                "voice_name": "warm",
                "audio_format": "mp3",
                "timeout_seconds": 45,
            }
        )
        self.assertTrue(public_voice["enabled"])
        self.assertEqual(public_voice["api_key_hint"], "••••9999")
        self.assertNotIn("voice-secret", json.dumps(public_voice, ensure_ascii=False))

    def test_doubao_voice_uses_resource_headers_and_decodes_audio_stream(self) -> None:
        voice = VoiceRuntime(self.settings, self.store)
        public_voice = voice.save_config(
            {
                "provider": "doubao",
                "api_key": "doubao-app-key-4321",
                "base_url": "https://openspeech.bytedance.com",
                "stt_model": "volc.bigasr.auc_turbo",
                "tts_model": "seed-tts-2.0",
                "voice_name": "zh_female_xiaohe_uranus_bigtts",
                "audio_format": "mp3",
                "timeout_seconds": 45,
            }
        )
        self.assertEqual(public_voice["provider"], "doubao")
        self.assertEqual(public_voice["api_key_hint"], "••••4321")
        requests: list[tuple[str, dict, dict[str, str]]] = []

        def fake_request(url: str, body: bytes, headers: dict[str, str], _timeout: float) -> bytes:
            payload = json.loads(body.decode("utf-8"))
            requests.append((url, payload, headers))
            if url.endswith("/recognize/flash"):
                return json.dumps({"result": {"text": "我刚看完一部电影。"}}).encode()
            first = json.dumps({"data": base64.b64encode(b"first").decode()})
            second = json.dumps({"data": base64.b64encode(b"second").decode(), "code": 20000000})
            return (first + second).encode()

        voice._request = fake_request  # type: ignore[method-assign]
        self.assertEqual(voice.transcribe(b"RIFF-test", "audio/wav"), "我刚看完一部电影。")
        audio, content_type = voice.synthesize("我在听。")
        self.assertEqual(audio, b"firstsecond")
        self.assertEqual(content_type, "audio/mpeg")
        self.assertEqual(requests[0][2]["X-Api-Key"], "doubao-app-key-4321")
        self.assertEqual(requests[0][2]["X-Api-Resource-Id"], "volc.bigasr.auc_turbo")
        self.assertEqual(requests[0][2]["X-Api-Sequence"], "-1")
        self.assertEqual(requests[1][2]["X-Api-Resource-Id"], "seed-tts-2.0")
        self.assertEqual(requests[1][1]["req_params"]["speaker"], "zh_female_xiaohe_uranus_bigtts")

    def test_microphone_entry_is_not_hidden_when_voice_is_unconfigured(self) -> None:
        html = (PRODUCT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        microphone = html.split('id="voice-mode-button"', 1)[1].split(">", 1)[0]
        self.assertNotIn("hidden", microphone)

    def test_tmdb_result_is_persisted_with_a_real_poster_url(self) -> None:
        internet = InternetRuntime(self.settings, self.store)
        internet.save_config(
            {
                "tmdb_api_key": "test-key",
                "tmdb_base_url": "https://api.themoviedb.org/3",
                "tmdb_image_base_url": "https://image.tmdb.org/t/p/w500",
                "web_search_base_url": "https://api.search.brave.com/res/v1",
                "web_reader_base_url": "https://r.jina.ai",
            }
        )
        internet._tmdb_get = lambda path, params, config: {  # type: ignore[method-assign]
            "results": [
                {
                    "id": 12345,
                    "title": "测试新片",
                    "original_title": "Test New Movie",
                    "release_date": "2026-08-01",
                    "genre_ids": [878],
                    "origin_country": ["CN"],
                    "overview": "用于验证实时电影入库。",
                    "poster_path": "/poster.jpg",
                    "popularity": 100,
                }
            ]
        }
        movies = internet.search_movies("测试新片")
        self.assertEqual(movies[0]["id"], "tmdb-12345")
        self.assertEqual(
            movies[0]["poster_url"], "https://image.tmdb.org/t/p/w500/poster.jpg"
        )
        self.assertEqual(self.store.movie("tmdb-12345")["external_ids"]["tmdb"], "12345")

    def test_tavily_search_uses_basic_search_and_domain_filter(self) -> None:
        internet = InternetRuntime(self.settings, self.store)
        public = internet.save_config(
            {
                "web_search_provider": "tavily",
                "web_search_api_key": "tvly-test-secret-2468",
                "web_search_base_url": "https://api.tavily.com",
                "web_reader_base_url": "https://r.jina.ai",
            }
        )
        self.assertEqual(public["web_search"]["provider"], "tavily")
        requests: list[tuple[str, dict[str, str], dict]] = []

        def fake_json_request(url: str, headers: dict[str, str], body: dict | None = None) -> dict:
            requests.append((url, headers, body or {}))
            return {
                "results": [
                    {
                        "title": "测试电影（豆瓣）",
                        "url": "https://movie.douban.com/subject/123/",
                        "content": "公开页面的搜索摘要。",
                    }
                ]
            }

        internet._json_request = fake_json_request  # type: ignore[method-assign]
        results = internet.web_search("测试电影", "douban", 3)
        self.assertEqual(requests[0][0], "https://api.tavily.com/search")
        self.assertEqual(requests[0][1]["Authorization"], "Bearer tvly-test-secret-2468")
        self.assertEqual(requests[0][2]["search_depth"], "basic")
        self.assertEqual(requests[0][2]["max_results"], 3)
        self.assertEqual(requests[0][2]["include_domains"], ["movie.douban.com"])
        self.assertEqual(results[0]["snippet"], "公开页面的搜索摘要。")

    def test_missing_seed_poster_is_hydrated_from_tmdb_and_persisted(self) -> None:
        internet = InternetRuntime(self.settings, self.store)
        internet.save_config(
            {
                "tmdb_api_key": "test-key",
                "tmdb_base_url": "https://api.themoviedb.org/3",
                "tmdb_image_base_url": "https://image.tmdb.org/t/p/w500",
                "web_search_base_url": "https://api.tavily.com",
                "web_reader_base_url": "https://r.jina.ai",
            }
        )
        movie = self.store.movie("us-interstellar-2014")
        self.assertIsNotNone(movie)
        movie = {**movie, "poster_url": "", "match_reason": "测试匹配理由"}
        internet._tmdb_get = lambda path, params, config: {  # type: ignore[method-assign]
            "results": [
                {
                    "id": 157336,
                    "title": "星际穿越",
                    "original_title": "Interstellar",
                    "release_date": "2014-11-05",
                    "poster_path": "/interstellar.jpg",
                    "popularity": 99,
                }
            ]
        }

        hydrated = internet.hydrate_movie_posters([movie])
        self.assertEqual(hydrated[0]["id"], "us-interstellar-2014")
        self.assertEqual(hydrated[0]["match_reason"], "测试匹配理由")
        self.assertEqual(
            hydrated[0]["poster_url"],
            "https://image.tmdb.org/t/p/w500/interstellar.jpg",
        )
        persisted = self.store.movie("us-interstellar-2014")
        self.assertEqual(persisted["external_ids"]["tmdb"], "157336")

    def test_voice_reply_uses_compact_player_and_autoplay_preference(self) -> None:
        html = (PRODUCT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        script = (PRODUCT_DIR / "web" / "app.js").read_text(encoding="utf-8")
        styles = (PRODUCT_DIR / "web" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="voice-autoplay-toggle"', html)
        self.assertIn('aria-pressed="true"', html)
        self.assertIn("yingban.voiceAutoPlay", script)
        self.assertIn("voice-reply-button", script)
        self.assertIn("await audio.play()", script)
        self.assertIn("aspect-ratio: 2 / 3", styles)

    def test_agent_reply_removes_poster_markdown_and_plain_tmdb_image_urls(self) -> None:
        reply = """**《流浪地球》**（2019）
[海报]
(https://image.tmdb.org/t/p/w500/poster.jpg)

值得一看。"""
        cleaned = sanitize_agent_reply(reply)
        self.assertEqual(cleaned, "《流浪地球》（2019）\n\n值得一看。")
        self.assertNotIn("image.tmdb.org", cleaned)
        self.assertNotIn("**", cleaned)

    def test_opening_index_as_a_file_redirects_to_the_local_service(self) -> None:
        html = (PRODUCT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('window.location.protocol === "file:"', html)
        self.assertIn('window.location.replace("http://127.0.0.1:8765/")', html)

    def test_reflection_dialog_and_themed_voice_player_are_present(self) -> None:
        html = (PRODUCT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        script = (PRODUCT_DIR / "web" / "app.js").read_text(encoding="utf-8")
        styles = (PRODUCT_DIR / "web" / "styles.css").read_text(encoding="utf-8")
        admin = (PRODUCT_DIR / "web" / "admin.html").read_text(encoding="utf-8")
        self.assertIn('id="reflection-dialog"', html)
        self.assertIn("history-poster-button", script)
        self.assertIn("name.after(wrap)", script)
        self.assertIn("background: var(--wine)", styles)
        self.assertIn('id="opening-config-form"', admin)


class HTTPFlowTests(ProductFixture):
    def setUp(self) -> None:
        super().setUp()
        internet = InternetRuntime(self.settings, self.store)
        voice = VoiceRuntime(self.settings, self.store)
        runtime = AgentRuntime(self.settings, self.store, self.catalog, internet)
        context = AppContext(self.settings, self.store, self.catalog, runtime, internet, voice)
        self.server = YingbanHTTPServer(("127.0.0.1", 0), context)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        cookie_jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with self.client.open(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def login(self) -> str:
        code = self.store.generate_invites(1)[0]
        result = self.request("/api/auth/login", "POST", {"invite_code": code})
        self.assertTrue(result["ok"])
        return code

    def test_discussion_auto_marks_movie_watched(self) -> None:
        self.login()
        response = self.request(
            "/api/chat",
            "POST",
            {
                "mode": "discussion",
                "message": "我刚看完星际穿越，心里有点堵",
                "history": [],
                "spoilers_allowed": True,
            },
        )
        self.assertEqual(response["selected_movie"]["id"], "us-interstellar-2014")
        self.assertEqual(response["memory_event"]["type"], "watched")
        history = self.request("/api/history?state=watched")
        self.assertEqual([item["movie_id"] for item in history["items"]], ["us-interstellar-2014"])

    def test_discussion_updates_reflection_note_visible_in_history(self) -> None:
        self.login()
        self.server.app.agent.summarize_reflection = lambda *args: (
            "你被库珀与女儿之间跨越时间的牵挂打动，也觉得结尾同时带着希望和遗憾。"
        )
        response = self.request(
            "/api/chat",
            "POST",
            {
                "mode": "discussion",
                "message": "我刚看完星际穿越，父女告别让我很难受，但最后又有希望。",
                "history": [],
                "spoilers_allowed": True,
            },
        )
        self.assertTrue(response["reflection_updated"])
        history = self.request("/api/history?state=watched")
        self.assertIn("跨越时间", history["items"][0]["note"])

    def test_recommendation_does_not_return_watched_movie(self) -> None:
        self.login()
        self.request(
            "/api/history",
            "POST",
            {"movie_id": "us-inside-out-2015", "state": "watched", "source": "test"},
        )
        response = self.request(
            "/api/chat",
            "POST",
            {
                "mode": "recommendation",
                "message": "最近压力很大，想看点轻松治愈的",
                "history": [],
            },
        )
        ids = {movie["id"] for movie in response["recommendations"]}
        self.assertNotIn("us-inside-out-2015", ids)
        self.assertTrue(ids)

    def test_replacement_recommendation_keeps_previous_intent(self) -> None:
        self.login()
        original = "最近学习压力很大，想看点轻松治愈的，不要爱情"
        first = self.request(
            "/api/chat",
            "POST",
            {"mode": "recommendation", "message": original, "history": []},
        )
        watched = first["recommendations"][0]
        self.request(
            "/api/history",
            "POST",
            {"movie_id": watched["id"], "state": "watched", "source": "feedback"},
        )
        second = self.request(
            "/api/chat",
            "POST",
            {
                "mode": "recommendation",
                "message": f"《{watched['title_zh']}》我看过了，换一部",
                "history": [
                    {"role": "user", "content": original},
                    {"role": "assistant", "content": first["reply"]},
                ],
            },
        )
        self.assertNotIn(watched["id"], {movie["id"] for movie in second["recommendations"]})
        self.assertTrue(
            any(
                {"轻松", "治愈", "温暖", "喜剧"}.intersection(
                    set(movie.get("moods", [])) | set(movie.get("genres", []))
                )
                for movie in second["recommendations"]
            )
        )

    def test_persona_is_transparent_when_asked(self) -> None:
        self.login()
        response = self.request(
            "/api/chat",
            "POST",
            {"mode": "discussion", "message": "你是真人吗？", "history": []},
        )
        self.assertIn("AI", response["reply"])
        self.assertIn("不是真人", response["reply"])

    def test_admin_can_generate_invites(self) -> None:
        login = self.request(
            "/api/admin/login", "POST", {"admin_token": "test-admin-token"}
        )
        self.assertTrue(login["ok"])
        generated = self.request("/api/admin/invites/generate", "POST", {"count": 2})
        self.assertEqual(len(generated["codes"]), 2)
        listing = self.request("/api/admin/invites")
        self.assertEqual(len(listing["items"]), 2)

    def test_admin_can_manage_model_and_independent_prompts(self) -> None:
        self.request("/api/admin/login", "POST", {"admin_token": "test-admin-token"})
        initial = self.request("/api/admin/agent-config")
        self.assertEqual(initial["model"]["mode"], "demo")
        self.assertNotEqual(
            initial["prompts"]["discussion"], initial["prompts"]["recommendation"]
        )

        saved = self.request(
            "/api/admin/agent-config/model",
            "POST",
            {
                "provider": "anthropic",
                "api_key": "admin-secret-5678",
                "model_id": "claude-test",
                "base_url": "https://api.example.com",
                "timeout_seconds": 30,
                "max_tokens": 900,
                "temperature": 0.6,
            },
        )
        self.assertEqual(saved["model"]["mode"], "model")
        self.assertEqual(saved["model"]["api_key_hint"], "••••5678")
        self.assertNotIn("admin-secret", json.dumps(saved, ensure_ascii=False))
        self.assertEqual(self.request("/api/health")["mode"], "model")

        prompt_result = self.request(
            "/api/admin/agent-config/prompts",
            "POST",
            {
                "discussion": "聊电影提示词：认真回应具体感受，遵守剧透设置，并自然地继续对话。",
                "recommendation": "找电影提示词：理解用户需求，只选择真实候选，并解释适合与不适合之处。",
            },
        )
        self.assertIn("聊电影提示词", prompt_result["prompts"]["discussion"])
        self.assertIn("找电影提示词", prompt_result["prompts"]["recommendation"])

        opening_result = self.request(
            "/api/admin/agent-config/openings",
            "POST",
            {
                "discussion": "刚看完哪部电影？",
                "discussion_movie": "《{movie}》最让你放不下的是什么？",
                "recommendation": "今晚想让电影带来什么感觉？",
            },
        )
        self.assertIn("{movie}", opening_result["openings"]["discussion_movie"])
        self.assertEqual(
            self.request("/api/admin/agent-config")["openings"]["recommendation"],
            "今晚想让电影带来什么感觉？",
        )

    def test_admin_can_manage_internet_and_voice_configuration(self) -> None:
        self.request("/api/admin/login", "POST", {"admin_token": "test-admin-token"})
        internet = self.request(
            "/api/admin/internet-config",
            "POST",
            {
                "tmdb_api_key": "http-tmdb-1234",
                "tmdb_base_url": "https://api.themoviedb.org/3",
                "tmdb_image_base_url": "https://image.tmdb.org/t/p/w500",
                "web_search_provider": "brave",
                "web_search_api_key": "http-brave-5678",
                "web_search_base_url": "https://api.search.brave.com/res/v1",
                "web_reader_enabled": True,
                "web_reader_base_url": "https://r.jina.ai",
            },
        )
        self.assertEqual(internet["internet"]["tmdb"]["api_key_hint"], "••••1234")
        self.assertEqual(internet["internet"]["web_search"]["provider"], "brave")
        self.assertNotIn("http-tmdb", json.dumps(internet, ensure_ascii=False))

        voice = self.request(
            "/api/admin/voice-config",
            "POST",
            {
                "provider": "openai_compatible",
                "api_key": "http-voice-9999",
                "base_url": "https://voice.example.com",
                "stt_model": "stt-test",
                "tts_model": "tts-test",
                "voice_name": "warm",
                "audio_format": "mp3",
                "timeout_seconds": 30,
            },
        )
        self.assertTrue(voice["voice"]["enabled"])
        self.assertEqual(voice["voice"]["api_key_hint"], "••••9999")
        self.assertNotIn("http-voice", json.dumps(voice, ensure_ascii=False))

        public = self.request("/api/admin/agent-config")
        self.assertTrue(public["internet"]["tmdb"]["enabled"])
        self.assertTrue(public["voice"]["enabled"])


if __name__ == "__main__":
    unittest.main()
