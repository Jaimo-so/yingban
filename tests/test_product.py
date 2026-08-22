from __future__ import annotations

import base64
import http.cookiejar
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

from agent import AgentRuntime, ModelMessageClient  # noqa: E402
from catalog import MovieCatalog  # noqa: E402
from integrations import InternetRuntime  # noqa: E402
from server import (  # noqa: E402
    AppContext,
    YingbanHTTPServer,
    has_reflection_signal,
    sanitize_agent_reply,
)
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


class StageNineProductTests(ProductFixture):
    def account(self) -> str:
        code = self.store.generate_invites(1)[0]
        account_id, _ = self.store.login_with_invite(code, 30)
        return account_id

    def test_onboarding_requires_exactly_five_and_rejects_sixth(self) -> None:
        account_id = self.account()
        movies = self.store.all_movies()[:6]
        for movie in movies[:4]:
            self.store.add_onboarding_movie(account_id, movie["id"], "positive")
        with self.assertRaises(ValueError):
            self.store.complete_onboarding(account_id)
        self.store.add_onboarding_movie(account_id, movies[4]["id"], "neutral")
        with self.assertRaises(ValueError):
            self.store.add_onboarding_movie(account_id, movies[5]["id"], "negative")
        profile = self.store.complete_onboarding(account_id)
        self.assertEqual(profile["onboarding_status"], "completed")
        self.assertEqual(profile["taste_version"], 1)

    def test_negative_onboarding_feedback_keeps_watched_fact(self) -> None:
        account_id = self.account()
        movie = self.store.all_movies()[0]
        self.store.add_onboarding_movie(account_id, movie["id"], "negative")
        state = self.store.get_movie_state(account_id, movie["id"])
        self.assertEqual(state["state"], "watched")
        self.assertEqual(self.store.onboarding_movies(account_id)[0]["sentiment"], "negative")

    def test_profile_uses_only_current_account_evidence_and_increments_version(self) -> None:
        first = self.account()
        second = self.account()
        movies = self.store.all_movies()[:7]
        for movie in movies[:5]:
            self.store.add_onboarding_movie(first, movie["id"], "positive")
        profile = self.store.complete_onboarding(first)
        evidence_ids = {
            evidence["movie_id"]
            for dimension in profile["taste_dimensions"]
            for evidence in dimension["evidence"]
        }
        self.assertTrue(evidence_ids.issubset({movie["id"] for movie in movies[:5]}))
        self.assertEqual(self.store.account_profile(second)["taste_version"], 0)
        self.store.remove_onboarding_movie(first, movies[4]["id"])
        self.store.add_onboarding_movie(first, movies[5]["id"], "negative")
        updated = self.store.complete_onboarding(first)
        self.assertEqual(updated["taste_version"], 2)

    def test_recommendation_feedback_is_scoped_and_action_semantics_are_distinct(self) -> None:
        first = self.account()
        second = self.account()
        recommendation = self.catalog.recommend(first, "想看轻松一点的", 1)[0]
        impression_id = recommendation["impression_id"]
        self.assertTrue(impression_id.startswith("rec_"))
        with self.assertRaises(ValueError):
            self.store.record_recommendation_feedback(second, impression_id, "not_now")
        feedback = self.store.record_recommendation_feedback(first, impression_id, "not_now")
        self.assertEqual(feedback["action"], "not_now")
        self.assertIsNone(self.store.get_movie_state(first, recommendation["id"]))
        second_recommendation = self.catalog.recommend(first, "想看轻松一点的", 1)[0]
        self.store.record_recommendation_feedback(
            first, second_recommendation["impression_id"], "not_interested"
        )
        self.assertEqual(
            self.store.get_movie_state(first, second_recommendation["id"])["state"],
            "disliked",
        )

    def test_reflection_versions_confirm_lock_edit_and_delete_keep_watched(self) -> None:
        account_id = self.account()
        movie_id = self.store.all_movies()[0]["id"]
        self.store.set_movie_state(account_id, movie_id, "watched", "test")
        draft = self.store.create_reflection_version(
            account_id, movie_id, "这是一份只围绕电影本身的 AI 草稿。", "ai", "draft"
        )
        self.assertEqual(draft["status"], "draft")
        confirmed = self.store.set_reflection_status(
            account_id, movie_id, draft["version"], "confirm"
        )["current"]
        self.assertEqual(confirmed["status"], "confirmed")
        edited = self.store.create_reflection_version(
            account_id, movie_id, "这是用户编辑后确认保存的新版本。", "ai_then_user", "confirmed",
            confirmed["version"],
        )
        locked = self.store.set_reflection_status(
            account_id, movie_id, edited["version"], "lock"
        )["current"]
        self.assertEqual(locked["status"], "locked")
        ai_suggestion = self.store.create_reflection_version(
            account_id, movie_id, "锁定后只能产生独立草稿，不能覆盖确认快照。", "ai", "draft"
        )
        self.assertEqual(ai_suggestion["status"], "draft")
        self.assertEqual(
            self.store.get_movie_state(account_id, movie_id)["note"], locked["content"]
        )
        self.assertTrue(self.store.delete_reflections(account_id, movie_id))
        self.assertEqual(self.store.get_movie_state(account_id, movie_id)["state"], "watched")
        self.assertIsNone(self.store.reflection_bundle(account_id, movie_id)["current"])

    def test_legacy_note_migrates_once_without_fabricating_empty_note(self) -> None:
        account_id = self.account()
        first_movie, second_movie = self.store.all_movies()[:2]
        self.store.set_movie_state(account_id, first_movie["id"], "watched", "legacy")
        self.store.set_movie_state(account_id, second_movie["id"], "watched", "legacy")
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE user_movie_states SET note = ? WHERE account_id = ? AND movie_id = ?",
                ("旧版本中已经存在的观后感。", account_id, first_movie["id"]),
            )
        Store(self.settings.database_path, self.settings.invite_pepper, self.settings.session_secret)
        migrated = self.store.reflection_bundle(account_id, first_movie["id"])
        empty = self.store.reflection_bundle(account_id, second_movie["id"])
        self.assertEqual(migrated["current"]["status"], "confirmed")
        self.assertEqual(migrated["current"]["version"], 1)
        self.assertIsNone(empty["current"])
        Store(self.settings.database_path, self.settings.invite_pepper, self.settings.session_secret)
        self.assertEqual(len(self.store.reflection_bundle(account_id, first_movie["id"])["history"]), 1)

    def test_product_and_usage_events_exclude_private_content(self) -> None:
        account_id = self.account()
        self.store.record_product_event(
            "chat_turn_succeeded", account_id,
            {"mode": "discussion", "message": "不应保存的原话", "reflection_content": "隐私正文", "latency_ms": 12},
        )
        self.store.save_usage_pricing({
            "version": "test-pricing-v1",
            "rates": {"chat": {"input_per_million": 2, "output_per_million": 4, "call_cost": 0}},
        })
        self.store.record_model_usage(
            "chat", "demo", "demo-v1", True, 12, account_id,
            input_units=500_000, output_units=250_000,
        )
        with self.store.connect() as connection:
            properties = connection.execute(
                "SELECT properties_json FROM product_events WHERE event_name = 'chat_turn_succeeded'"
            ).fetchone()[0]
        self.assertNotIn("不应保存", properties)
        self.assertNotIn("隐私正文", properties)
        self.assertIn("latency_ms", properties)
        metrics = self.store.metrics_summary()
        self.assertEqual(metrics["usage"][0]["operation"], "chat")
        self.assertEqual(metrics["usage"][0]["estimated_cost"], 2.0)
        self.assertEqual(metrics["pricing"]["version"], "test-pricing-v1")


class StageTenProductTests(ProductFixture):
    def test_short_explicit_movie_reactions_are_valid_reflection_signals(self) -> None:
        self.assertTrue(has_reflection_signal(["还好"]))
        self.assertTrue(has_reflection_signal(["感觉一般"]))
        self.assertFalse(has_reflection_signal(["是的", "好的"]))

    def account(self) -> str:
        code = self.store.generate_invites(1)[0]
        account_id, _ = self.store.login_with_invite(code, 30)
        return account_id

    def test_conversation_summary_requires_consent_data_and_is_independent_from_reflection(self) -> None:
        account_id = self.account()
        movie_id = self.store.all_movies()[0]["id"]
        self.store.set_movie_state(account_id, movie_id, "watched", "test")
        reflection = self.store.create_reflection_version(
            account_id, movie_id, "这是用户确认要留下的电影观后感。", "user", "confirmed"
        )
        summary = self.store.save_conversation_summary(
            account_id, movie_id, "我们聊到结尾的选择，还没有形成一致理解。",
            ["结尾", "人物选择"], ["这个选择是否改变了人物？"], True,
        )
        self.assertTrue(summary["spoilers_allowed"])
        self.assertEqual(summary["topics"], ["结尾", "人物选择"])
        with self.assertRaises(ValueError):
            self.store.save_conversation_summary(
                account_id,
                movie_id,
                "我的手机号是 13800138000，想把现实经历也存进去。",
                [],
                [],
                False,
            )
        self.assertTrue(self.store.delete_conversation_summary(account_id, movie_id))
        self.assertIsNone(self.store.conversation_summary(account_id, movie_id))
        self.assertEqual(
            self.store.reflection_bundle(account_id, movie_id)["current"]["id"], reflection["id"]
        )

    def test_weekly_recommendation_is_unique_per_week_and_hard_excludes_watched(self) -> None:
        account_id = self.account()
        watched, candidate = self.store.all_movies()[:2]
        self.store.set_movie_state(account_id, watched["id"], "watched", "test")
        with self.assertRaises(ValueError):
            self.store.save_weekly_recommendation(account_id, [watched], "不应保存", {})
        first = self.store.save_weekly_recommendation(
            account_id, [candidate], "来自想看列表", {"types": ["watchlist"]}
        )
        second = self.store.save_weekly_recommendation(
            account_id, self.store.all_movies()[2:3], "不会每次刷新重做", {}
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["movies"][0]["id"], candidate["id"])
        self.store.dismiss_weekly_recommendation(account_id, permanently=True)
        self.assertEqual(self.store.weekly_recommendation(account_id)["status"], "dismissed")
        self.assertFalse(self.store.account_profile(account_id)["weekly_recommendations_enabled"])

    def test_watchlist_follow_up_keeps_temporary_and_persistent_meanings_distinct(self) -> None:
        account_id = self.account()
        first, second = self.store.all_movies()[:2]
        self.store.set_movie_state(account_id, first["id"], "watchlist", "test")
        result = self.store.follow_up_watchlist(account_id, first["id"], "not_now")
        self.assertIsNone(result["item"]["state"])
        self.assertNotIn(first["id"], self.store.persistently_excluded_ids(account_id))
        self.store.set_movie_state(account_id, second["id"], "watchlist", "test")
        self.store.follow_up_watchlist(account_id, second["id"], "not_interested", "direction_changed")
        self.assertIn(second["id"], self.store.persistently_excluded_ids(account_id))

    def test_monthly_recap_uses_only_current_account_and_confirmed_reflections(self) -> None:
        first = self.account()
        second = self.account()
        movie = self.store.all_movies()[0]
        self.store.set_movie_state(first, movie["id"], "watched", "test")
        self.store.create_reflection_version(first, movie["id"], "第一账户确认的观后感。", "user", "confirmed")
        self.store.create_reflection_version(first, movie["id"], "尚未确认的 AI 草稿。", "ai", "draft")
        month = datetime.now(UTC).strftime("%Y-%m")
        recap = self.store.generate_monthly_recap(first, month)
        self.assertEqual(len(recap["content"]["watched"]), 1)
        self.assertEqual(len(recap["source_reflection_ids"]), 1)
        self.assertIsNone(self.store.monthly_recap(second, month))
        confirmed = self.store.confirm_monthly_recap(first, month, "下个月继续看科幻电影")
        self.assertEqual(confirmed["status"], "confirmed")

    def test_share_card_is_random_expiring_revocable_and_public_payload_has_no_account(self) -> None:
        account_id = self.account()
        card = self.store.create_share_card(
            account_id, "taste_dimension", "genre:科幻",
            {"title": "我的电影口味", "text": "我喜欢科幻电影。", "attribution": "由影伴 AI 协助整理"},
            7,
        )
        self.assertGreaterEqual(len(card["public_token"]), 24)
        public = self.store.public_share_card(card["public_token"])
        self.assertNotIn("account_id", public)
        self.assertNotIn("public_token", public)
        self.assertTrue(self.store.revoke_share_card(account_id, card["id"]))
        self.assertIsNone(self.store.public_share_card(card["public_token"]))

    def test_background_job_stores_only_references_and_is_account_isolated(self) -> None:
        first = self.account()
        second = self.account()
        job = self.store.create_background_job(
            first, "reflection", {"movie_id": self.store.all_movies()[0]["id"]}
        )
        self.assertIsNone(self.store.background_job(second, job["id"]))
        running = self.store.update_background_job(first, job["id"], "running")
        self.assertEqual(running["attempt_count"], 1)
        done = self.store.update_background_job(
            first, job["id"], "succeeded", result={"version": 1}
        )
        self.assertEqual(done["result"], {"version": 1})
        self.assertNotIn("content", json.dumps(done["input_reference"]))
        metrics = self.store.metrics_summary()
        self.assertEqual(metrics["continuity"]["background_jobs"]["succeeded"], 1)

        expiring = self.store.create_background_job(first, "poster_hydration", {"movie_ids": []})
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE background_jobs SET expires_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), expiring["id"]),
            )
        self.assertEqual(
            self.store.background_job(first, expiring["id"])["status"], "expired"
        )

    def test_recent_event_count_supports_persistent_weekly_refresh_limit(self) -> None:
        account_id = self.account()
        for _ in range(3):
            self.store.record_product_event(
                "weekly_recommendation_refreshed", account_id, {"movie_count": 3}
            )
        self.assertEqual(
            self.store.recent_product_event_count(
                account_id,
                "weekly_recommendation_refreshed",
                since=datetime.now(UTC) - timedelta(hours=1),
            ),
            3,
        )

    def test_history_pagination_is_stable_and_reports_total(self) -> None:
        account_id = self.account()
        movies = self.store.all_movies()[:5]
        for movie in movies:
            self.store.set_movie_state(account_id, movie["id"], "watched", "test")
        first = self.store.movie_states_page(account_id, "watched", 0, 2)
        second = self.store.movie_states_page(account_id, "watched", first["next_cursor"], 2)
        self.assertEqual(first["total"], 5)
        self.assertEqual(len(first["items"]), 2)
        self.assertFalse(
            {item["movie_id"] for item in first["items"]}
            & {item["movie_id"] for item in second["items"]}
        )

    def test_stage_ten_ui_exposes_local_retention_background_jobs_and_user_controls(self) -> None:
        html = (PRODUCT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        script = (PRODUCT_DIR / "web" / "app.js").read_text(encoding="utf-8")
        styles = (PRODUCT_DIR / "web" / "styles.css").read_text(encoding="utf-8")
        share_html = (PRODUCT_DIR / "web" / "share.html").read_text(encoding="utf-8")
        share_script = (PRODUCT_DIR / "web" / "share-card.js").read_text(encoding="utf-8")
        self.assertIn('id="weekly-card"', html)
        self.assertIn('id="conversation-summary-dialog"', html)
        self.assertIn('id="monthly-recap-dialog"', html)
        self.assertIn('id="share-revoke"', html)
        self.assertIn("生成精美分享卡", html)
        self.assertIn("retentionMs: 7 * 24 * 60 * 60 * 1000", script)
        self.assertIn('storage: "local-browser"', script)
        self.assertIn('/api/jobs/voice', script)
        self.assertIn("async: true", script)
        self.assertIn("正在生成分享卡…", script)
        self.assertIn("window.location.assign(data.share_path)", script)
        self.assertIn('/share-card.js?v=12', share_html)
        self.assertIn('class="share-back-button" href="/"', share_html)
        self.assertIn("返回影伴", share_html)
        self.assertIn("share-film-grid", share_script)
        self.assertIn("visual.movies", share_script)
        self.assertIn(".reflection-paper .inline-check input", styles)
        self.assertIn("flex: 0 0 16px", styles)
        self.assertIn("width: fit-content", styles)

    def test_stage_eleven_discussion_entry_is_new_and_history_restore_is_explicit(self) -> None:
        html = (PRODUCT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        script = (PRODUCT_DIR / "web" / "app.js").read_text(encoding="utf-8")
        styles = (PRODUCT_DIR / "web" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="chat-history-button"', html)
        self.assertIn('id="conversation-history-dialog"', html)
        self.assertIn('id="conversation-history-delete-dialog"', html)
        self.assertIn('prefix: "yingban.conversation.v2."', script)
        self.assertIn('legacyPrefix: "yingban.chatDraft.v1."', script)
        self.assertIn("migrateLegacyDrafts()", script)
        self.assertIn("const restored = options.conversation || null", script)
        self.assertIn("restored?.id || localConversationStore.newId()", script)
        self.assertIn('openChat("discussion", conversation.movie || null, { conversation })', script)
        self.assertIn("history: state.chatHistory.slice(-40)", script)
        self.assertIn("value?.movie?.title_zh", script)
        self.assertIn("conversation-history-item", styles)
        self.assertIn("conversation-history-actions", styles)


class AgentConfigurationTests(ProductFixture):
    def test_reflection_keeps_early_short_user_feedback_without_using_ai_as_evidence(self) -> None:
        runtime = AgentRuntime(self.settings, self.store, self.catalog)
        runtime.save_model_config(
            {
                "provider": "openai_compatible",
                "api_key": "sk-reflection-test",
                "model_id": "reflection-test-model",
                "base_url": "https://models.example.com/v1",
                "timeout_seconds": 30,
                "max_tokens": 1200,
                "temperature": 0.4,
            }
        )
        history = [
            {"role": "assistant", "content": "你对《流浪地球》的整体感受怎么样？"},
            {"role": "user", "content": "还行"},
        ] + [
            {"role": "assistant" if index % 2 else "user", "content": f"后来转到其他话题 {index}"}
            for index in range(28)
        ]
        captured: dict[str, Any] = {}

        def fake_create(
            _client: ModelMessageClient,
            system: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
        ) -> dict[str, Any]:
            captured["system"] = system
            captured["input"] = json.loads(messages[0]["content"])
            self.assertIsNone(tools)
            return {
                "content": [{"type": "text", "text": "你觉得《流浪地球》整体还行，没有特别强烈的感受。"}],
                "usage": {},
            }

        with patch.object(ModelMessageClient, "create", autospec=True, side_effect=fake_create):
            draft = runtime.summarize_reflection(
                self.store.movie("cn-wandering-earth-2019") or {},
                "",
                history,
                "后来聊到另一部电影",
                "这是阿映的观点，不能写成用户观点。",
            )

        context = captured["input"]["conversation_context"]
        self.assertIn({"role": "user", "content": "还行"}, context)
        self.assertLessEqual(len(context), 24)
        self.assertIn("只有 `role=user` 的文字是用户观感证据", captured["system"])
        self.assertIn("20 至 500 个中文字符", captured["system"])
        self.assertIn("整体还行", draft)

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
        self.assertIn('id="usage-pricing-form"', admin)


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

    def test_new_account_completes_five_movie_onboarding_and_gets_visible_profile(self) -> None:
        self.login()
        me = self.request("/api/me")
        self.assertEqual(me["onboarding"]["status"], "not_started")
        candidates = self.request("/api/onboarding/candidates")["items"]
        self.assertGreaterEqual(len(candidates), 5)
        for index, movie in enumerate(candidates[:5]):
            result = self.request(
                "/api/onboarding/movies", "POST",
                {"movie_id": movie["id"], "sentiment": ["positive", "neutral", "negative"][index % 3]},
            )
            self.assertEqual(result["selected_count"], index + 1)
        completed = self.request("/api/onboarding/complete", "POST", {})
        self.assertEqual(completed["profile"]["onboarding_status"], "completed")
        self.assertEqual(completed["profile"]["taste_version"], 1)
        visible = self.request("/api/taste-profile")["profile"]
        self.assertTrue(visible["taste_summary"])
        self.assertEqual(self.request("/api/me")["onboarding"]["selected_count"], 5)

    def test_discussion_creates_reflection_only_after_explicit_request_and_confirmation(self) -> None:
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
        self.assertFalse(response["reflection_updated"])
        history = self.request("/api/history?state=watched")
        self.assertFalse(history["items"][0].get("note"))
        generated = self.request(
            "/api/reflections/us-interstellar-2014/generate",
            "POST",
            {
                "history": [
                    {"role": "user", "content": "父女告别让我很难受，但最后又有希望。"},
                    {"role": "assistant", "content": response["reply"]},
                ]
            },
        )
        self.assertEqual(generated["current"]["status"], "draft")
        confirmed = self.request(
            "/api/reflections/us-interstellar-2014/confirm",
            "POST",
            {"version": generated["current"]["version"]},
        )
        self.assertEqual(confirmed["current"]["status"], "confirmed")
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
        self.assertTrue(all(movie["impression_id"].startswith("rec_") for movie in response["recommendations"]))

    def test_recommendation_feedback_updates_state_through_exposure(self) -> None:
        self.login()
        response = self.request(
            "/api/chat", "POST",
            {"mode": "recommendation", "message": "想看一部轻松的电影", "history": []},
        )
        movie = response["recommendations"][0]
        feedback = self.request(
            f"/api/recommendations/{movie['impression_id']}/feedback", "POST",
            {"action": "watchlist"},
        )
        self.assertEqual(feedback["feedback"]["movie_id"], movie["id"])
        history = self.request("/api/history?state=watchlist")
        self.assertEqual(history["items"][0]["movie_id"], movie["id"])

    def test_stage_ten_http_continuity_weekly_follow_up_recap_and_share_flow(self) -> None:
        self.login()
        account_id = self.store.list_invites()[0]["account_id"]
        movie_id = "us-interstellar-2014"
        self.request(
            "/api/history", "POST",
            {"movie_id": movie_id, "state": "watched", "source": "test"},
        )
        saved = self.request(
            f"/api/conversations/{movie_id}/summary", "POST",
            {
                "summary": "我们聊到结尾与父女关系，还想继续讨论人物选择。",
                "topics": ["结尾", "父女关系"],
                "open_questions": ["结尾的选择意味着什么？"],
                "spoilers_allowed": True,
            },
        )
        self.assertEqual(saved["summary"]["movie_id"], movie_id)
        self.assertIsNotNone(self.request(f"/api/conversations/{movie_id}/summary")["summary"])

        watchlist_movie = "jp-spirited-away-2001"
        self.request(
            "/api/history", "POST",
            {"movie_id": watchlist_movie, "state": "watchlist", "source": "test"},
        )
        weekly = self.request("/api/weekly-recommendation")
        self.assertNotIn(movie_id, {movie["id"] for movie in weekly["recommendation"]["movies"]})
        followed = self.request(
            f"/api/watchlist/{watchlist_movie}/follow-up", "POST", {"action": "not_now"}
        )
        self.assertIsNone(followed["item"]["state"])

        reflection = self.store.create_reflection_version(
            account_id, movie_id, "这是一份由当前账户确认、可选择分享的电影观后感。", "user", "confirmed"
        )
        card = self.request(
            "/api/share-cards", "POST",
            {"source_type": "reflection", "source_id": movie_id, "text": reflection["content"]},
        )["card"]
        public = self.request(f"/api/public/share-cards/{card['public_token']}")
        self.assertNotIn("account_id", public["card"])
        self.assertTrue(self.request(f"/api/share-cards/{card['id']}", "DELETE")["ok"])

        month = datetime.now(UTC).strftime("%Y-%m")
        self.request(f"/api/recaps/monthly/{month}/generate", "POST", {})
        self.request(
            f"/api/recaps/monthly/{month}/confirm", "POST",
            {"next_direction": "下个月继续沿着科幻与亲情的方向看下去"},
        )
        monthly_card = self.request(
            "/api/share-cards", "POST",
            {"source_type": "monthly_recap", "source_id": month, "expires_days": 7},
        )["card"]
        visual = monthly_card["content"]["visual"]
        self.assertEqual(visual["variant"], "monthly_recap")
        self.assertEqual(visual["movie_count"], 1)
        self.assertEqual(visual["movies"][0]["title"], "星际穿越")
        self.assertIn("poster_url", visual["movies"][0])
        self.assertIn("和 1 部电影", visual["story_title"])
        monthly_public = self.request(
            f"/api/public/share-cards/{monthly_card['public_token']}"
        )["card"]
        self.assertEqual(monthly_public["content"]["visual"]["movies"][0]["title"], "星际穿越")
        self.assertTrue(self.request(f"/api/conversations/{movie_id}/summary", "DELETE")["ok"])

    def test_stage_ten_weekly_refresh_limit_and_background_retry(self) -> None:
        self.login()
        for index in range(3):
            result = self.request(
                "/api/weekly-recommendation/refresh",
                "POST",
                {"direction": f"第 {index + 1} 次换一批"},
            )
            self.assertEqual(result["status"], "ready")
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            self.request(
                "/api/weekly-recommendation/refresh",
                "POST",
                {"direction": "超过每小时限制"},
            )
        self.assertEqual(blocked.exception.code, 429)
        blocked.exception.close()

        self.request(
            "/api/weekly-recommendation/dismiss", "POST", {"permanently": True}
        )
        with self.assertRaises(urllib.error.HTTPError) as disabled:
            self.request(
                "/api/weekly-recommendation/refresh",
                "POST",
                {"direction": "停用后不能绕过设置直接刷新"},
            )
        self.assertEqual(disabled.exception.code, 409)
        disabled.exception.close()

        self.request(
            "/api/history",
            "POST",
            {"movie_id": "us-interstellar-2014", "state": "watched", "source": "test"},
        )

        attempts = 0

        def flaky_summary(*_args: object, **_kwargs: object) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary test failure")
            return "第二次尝试后成功整理出的观后感草稿。"

        self.server.app.agent.summarize_reflection = flaky_summary  # type: ignore[method-assign]
        created = self.request(
            "/api/reflections/us-interstellar-2014/generate",
            "POST",
            {
                "async": True,
                "history": [
                    {"role": "user", "content": "这部电影让我重新想了很久亲情和时间的关系。"}
                ],
            },
        )
        job_id = created["job"]["id"]
        job = created["job"]
        for _ in range(50):
            job = self.request(f"/api/jobs/{job_id}")["job"]
            if job["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["attempt_count"], 2)

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
        metrics = self.request("/api/admin/metrics/summary")
        self.assertIn("accounts", metrics)
        self.assertIn("usage", metrics)
        pricing = self.request(
            "/api/admin/usage-pricing", "POST",
            {"version": "provider-price-2026-08", "rates": {
                "chat": {"input_per_million": 1.5, "output_per_million": 6, "call_cost": 0}
            }},
        )["pricing"]
        self.assertEqual(pricing["version"], "provider-price-2026-08")
        self.assertEqual(self.request("/api/admin/usage-pricing")["currency"], "CNY")

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
