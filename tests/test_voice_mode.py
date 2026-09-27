from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import build_context
from fastapi_app import create_fastapi_app
from settings import Settings
from voice import VoiceRuntime


class VoiceModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.context = build_context(Settings(
            database_path=Path(self.directory.name) / "voice.db",
            movie_seed_path=Path(__file__).resolve().parents[1] / "data/movies.json",
            invite_pepper="test-pepper", session_secret="test-secret",
            admin_token="test-admin", cookie_secure=False,
            local_open_access=False, model_api_key="",
            voice_api_key="", voice_app_id="", voice_access_token="",
            sqlite_journal_mode="WAL", sqlite_vfs="", database_mount="",
        ))
        self.voice = self.context.voice
        self.client = TestClient(create_fastapi_app(self.context))

    def tearDown(self):
        self.client.close()
        for worker in threading.enumerate():
            if worker.name.startswith("yingban-"):
                worker.join(timeout=2)
        self.directory.cleanup()

    def login(self):
        code = self.context.store.generate_invites(1)[0]
        self.assertEqual(self.client.post("/api/auth/login", json={"invite_code": code}).status_code, 200)

    def configure(self):
        return self.voice.save_config({
            "provider": "openai_compatible", "api_key": "fixture-secret",
            "base_url": "https://voice.example.com/v1", "stt_model": "whisper-1",
            "tts_model": "gpt-4o-mini-tts", "voice_name": "alloy", "audio_format": "mp3",
        })

    def test_switch_persists_preserves_credentials_and_validates_boolean(self):
        self.assertTrue(self.configure()["enabled"])
        disabled = self.voice.save_config({"mode_enabled": False})
        self.assertFalse(disabled["enabled"])
        self.assertTrue(disabled["configured"])
        restarted = VoiceRuntime(self.context.settings, self.context.store)
        self.assertFalse(restarted.config().mode_enabled)
        self.assertEqual(restarted.config().api_key, "fixture-secret")
        restarted.save_config({"voice_name": "warm"})
        self.assertFalse(restarted.config().enabled)
        for invalid in ("false", 0, None, []):
            with self.assertRaises(ValueError):
                restarted.save_config({"mode_enabled": invalid})
        self.assertTrue(restarted.save_config({"mode_enabled": True})["enabled"])

    def test_off_or_unconfigured_requests_are_noops_and_text_chat_works(self):
        self.login()
        for configured in (False, True):
            if configured:
                self.configure()
                self.voice.save_config({"mode_enabled": False})
            with patch.object(self.voice, "transcribe") as stt, patch.object(self.voice, "synthesize") as tts:
                self.assertFalse(self.client.get("/api/me").json()["voice_available"])
                self.assertEqual(self.client.get("/api/voice/status").json(), {"voice_available": False})
                for endpoint in ("/api/jobs/voice", "/api/voice/transcribe", "/api/voice/synthesize"):
                    result = self.client.post(endpoint, json={"text": "测试回复", "audio_base64": "invalid"})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.json(), {"voice_available": False})
                stt.assert_not_called()
                tts.assert_not_called()
        with self.context.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0], 0)
        chat = self.client.post("/api/chat", json={"mode": "discussion", "message": "聊聊电影", "history": []})
        self.assertEqual(chat.status_code, 200)
        self.assertTrue(chat.json()["reply"])

    def test_only_admin_can_toggle_and_unconfigured_enable_is_safe(self):
        self.assertEqual(self.client.get("/api/voice/status").status_code, 401)
        self.login()
        self.assertEqual(self.client.post("/api/admin/voice-config", json={"mode_enabled": False}).status_code, 401)
        self.client.post("/api/admin/login", json={"admin_token": "test-admin"})
        for enabled in (False, True):
            response = self.client.post("/api/admin/voice-config", json={"mode_enabled": enabled})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["voice"]["mode_enabled"], enabled)
            self.assertFalse(response.json()["voice"]["enabled"])

    def test_runtime_blocks_off_but_explicit_admin_test_can_check_credentials(self):
        self.configure()
        self.voice.save_config({"mode_enabled": False})
        with patch.object(self.voice, "_request", return_value=b"audio") as request:
            with self.assertRaises(RuntimeError):
                self.voice.synthesize("测试")
            with self.assertRaises(RuntimeError):
                self.voice.transcribe(b"audio", "audio/wav")
            request.assert_not_called()
            self.assertEqual(self.voice.test_connection({})["bytes"], 5)
            self.assertFalse(self.voice.config().mode_enabled)

    def test_toggle_during_tts_finishes_without_failed_job_and_reenable_works(self):
        self.configure()
        self.login()
        entered, release = threading.Event(), threading.Event()
        def synthesize(text):
            entered.set()
            release.wait(timeout=3)
            return b"audio", "audio/mpeg"
        with patch.object(self.voice, "synthesize", side_effect=synthesize):
            response = self.client.post("/api/jobs/voice", json={"text": "测试回复"})
            self.assertEqual(response.status_code, 202)
            job_id = response.json()["job"]["id"]
            self.assertTrue(entered.wait(timeout=3))
            self.voice.save_config({"mode_enabled": False})
            release.set()
            for _ in range(100):
                job = self.client.get(f"/api/jobs/{job_id}").json()["job"]
                if job["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)
            self.assertEqual(job["status"], "succeeded")
            self.assertFalse(job["result"]["voice_available"])
            self.assertFalse(job["result"]["audio_available"])
        self.voice.save_config({"mode_enabled": True})
        self.assertTrue(self.client.get("/api/voice/status").json()["voice_available"])
        with patch.object(self.voice, "synthesize", return_value=(b"audio", "audio/mpeg")):
            result = self.client.post("/api/voice/synthesize", json={"text": "测试回复"})
            self.assertEqual(result.status_code, 200)
            self.assertIn("audio_base64", result.json())

    def test_switch_during_direct_voice_calls_returns_unavailable_without_error(self):
        self.configure()
        self.login()
        for method, endpoint, result in (
            ("transcribe", "/api/voice/transcribe", "识别文字"),
            ("synthesize", "/api/voice/synthesize", (b"audio", "audio/mpeg")),
        ):
            for failed in (False, True):
                self.voice.save_config({"mode_enabled": True})
                def disable(*args):
                    self.voice.save_config({"mode_enabled": False})
                    if failed:
                        raise RuntimeError("turned off during request")
                    return result
                with patch.object(self.voice, method, side_effect=disable):
                    response = self.client.post(endpoint, json={"text": "测试", "audio_base64": "YXVkaW8="})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"voice_available": False})
