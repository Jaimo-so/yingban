from __future__ import annotations

import json
import base64
import secrets
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from model_catalog import stepfun_model_ids
from settings import Settings
from storage import Store


VOICE_SETTING_KEYS = (
    "voice_provider",
    "voice_api_key",
    "voice_app_id",
    "voice_access_token",
    "voice_base_url",
    "voice_stt_model",
    "voice_tts_model",
    "voice_chat_model",
    "voice_realtime_model",
    "voice_name",
    "voice_audio_format",
    "voice_timeout_seconds",
)

ALLOWED_AUDIO_FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav", "opus": "audio/ogg", "aac": "audio/aac"}


def _url(value: Any) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        raise ValueError("语音接口根地址必须是有效的 http 或 https 地址")
    return candidate


@dataclass(frozen=True)
class VoiceConfig:
    provider: str
    api_key: str
    app_id: str
    access_token: str
    base_url: str
    stt_model: str
    tts_model: str
    chat_model: str
    realtime_model: str
    voice_name: str
    audio_format: str
    timeout_seconds: float

    @property
    def enabled(self) -> bool:
        credentials_ready = bool(self.api_key) if self.provider in {"openai_compatible", "stepfun"} else bool(
            self.api_key or (self.app_id and self.access_token)
        )
        return bool(credentials_ready and self.stt_model and self.tts_model and self.voice_name)


class VoiceRuntime:
    """StepFun, OpenAI-compatible, and Doubao speech adapters."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    def config(self, overrides: dict[str, Any] | None = None) -> VoiceConfig:
        saved = self.store.get_app_settings(VOICE_SETTING_KEYS)
        saved_provider = saved.get("voice_provider")
        if not saved_provider and saved.get("voice_base_url", "").startswith("https://api.openai.com"):
            saved_provider = "openai_compatible"
        current: dict[str, Any] = {
            "provider": saved_provider or self.settings.voice_provider,
            "api_key": saved.get("voice_api_key", self.settings.voice_api_key),
            "app_id": saved.get("voice_app_id", self.settings.voice_app_id),
            "access_token": saved.get("voice_access_token", self.settings.voice_access_token),
            "base_url": saved.get("voice_base_url", self.settings.voice_base_url),
            "stt_model": saved.get("voice_stt_model", self.settings.voice_stt_model),
            "tts_model": saved.get("voice_tts_model", self.settings.voice_tts_model),
            "chat_model": saved.get("voice_chat_model", self.settings.voice_chat_model),
            "realtime_model": saved.get(
                "voice_realtime_model", self.settings.voice_realtime_model
            ),
            "voice_name": saved.get("voice_name", self.settings.voice_name),
            "audio_format": saved.get("voice_audio_format", self.settings.voice_audio_format),
            "timeout_seconds": saved.get("voice_timeout_seconds", self.settings.voice_timeout_seconds),
        }
        if overrides:
            if overrides.get("clear_api_key") is True:
                current["api_key"] = ""
            elif str(overrides.get("api_key", "")).strip():
                current["api_key"] = str(overrides["api_key"]).strip()
            if overrides.get("clear_access_token") is True:
                current["access_token"] = ""
            elif str(overrides.get("access_token", "")).strip():
                current["access_token"] = str(overrides["access_token"]).strip()
            for key in (
                "provider", "app_id", "base_url", "stt_model", "tts_model",
                "chat_model", "realtime_model", "voice_name", "audio_format",
                "timeout_seconds",
            ):
                if key in overrides:
                    current[key] = overrides[key]
        provider = str(current["provider"]).strip()
        if provider not in {"openai_compatible", "doubao", "stepfun"}:
            raise ValueError("语音服务商只支持阶跃星辰、OpenAI 兼容或豆包")
        api_key = str(current["api_key"]).strip()
        app_id = str(current["app_id"]).strip()
        access_token = str(current["access_token"]).strip()
        if any(len(value) > 10_000 for value in (api_key, app_id, access_token)):
            raise ValueError("语音服务凭据过长")
        values = {
            name: str(current[name]).strip()
            for name in (
                "stt_model", "tts_model", "chat_model", "realtime_model",
                "voice_name", "audio_format",
            )
        }
        if any(not value or len(value) > 200 for value in values.values()):
            raise ValueError("语音模型、音色和格式不能为空，且不能超过 200 个字符")
        if values["audio_format"] not in ALLOWED_AUDIO_FORMATS:
            raise ValueError("语音格式只支持 mp3、wav、opus 或 aac")
        if provider == "doubao" and values["audio_format"] not in {"mp3", "opus"}:
            raise ValueError("豆包单向流式语音合成请选择 MP3 或 Opus 格式")
        if provider == "stepfun":
            expected_models = {
                "stt_model": stepfun_model_ids("asr"),
                "tts_model": stepfun_model_ids("tts"),
                "chat_model": stepfun_model_ids("audio_chat"),
                "realtime_model": stepfun_model_ids("realtime"),
            }
            if any(values[name] not in allowed for name, allowed in expected_models.items()):
                raise ValueError("阶跃语音模型与所选能力不匹配")
            if values["audio_format"] not in {"mp3", "wav", "opus"}:
                raise ValueError("StepAudio 2.5 TTS 请选择 MP3、WAV 或 Opus 格式")
        timeout = float(current["timeout_seconds"])
        if not 5 <= timeout <= 300:
            raise ValueError("语音请求超时必须在 5 到 300 秒之间")
        return VoiceConfig(
            provider=provider,
            api_key=api_key,
            app_id=app_id,
            access_token=access_token,
            base_url=_url(current["base_url"]),
            stt_model=values["stt_model"],
            tts_model=values["tts_model"],
            chat_model=values["chat_model"],
            realtime_model=values["realtime_model"],
            voice_name=values["voice_name"],
            audio_format=values["audio_format"],
            timeout_seconds=timeout,
        )

    def public_config(self) -> dict[str, Any]:
        config = self.config()
        return {
            "provider": config.provider,
            "base_url": config.base_url,
            "stt_model": config.stt_model,
            "tts_model": config.tts_model,
            "chat_model": config.chat_model,
            "realtime_model": config.realtime_model,
            "voice_name": config.voice_name,
            "audio_format": config.audio_format,
            "timeout_seconds": config.timeout_seconds,
            "has_api_key": bool(config.api_key),
            "api_key_hint": f"••••{config.api_key[-4:]}" if config.api_key else "",
            "app_id": config.app_id,
            "has_access_token": bool(config.access_token),
            "access_token_hint": f"••••{config.access_token[-4:]}" if config.access_token else "",
            "enabled": config.enabled,
        }

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.config(payload)
        self.store.set_app_settings(
            {
                "voice_provider": config.provider,
                "voice_api_key": config.api_key,
                "voice_app_id": config.app_id,
                "voice_access_token": config.access_token,
                "voice_base_url": config.base_url,
                "voice_stt_model": config.stt_model,
                "voice_tts_model": config.tts_model,
                "voice_chat_model": config.chat_model,
                "voice_realtime_model": config.realtime_model,
                "voice_name": config.voice_name,
                "voice_audio_format": config.audio_format,
                "voice_timeout_seconds": str(config.timeout_seconds),
            }
        )
        return self.public_config()

    def transcribe(self, audio: bytes, mime_type: str) -> str:
        config = self.config()
        if not config.enabled:
            raise RuntimeError("管理员尚未配置语音服务")
        if not audio or len(audio) > 10_000_000:
            raise ValueError("录音不能为空，且不能超过 10 MB")
        if config.provider == "doubao":
            return self._doubao_transcribe(self._doubao_audio(audio, mime_type), config)
        if config.provider == "stepfun":
            return self._stepfun_transcribe(audio, mime_type, config)
        extension = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/wav": "wav"}.get(mime_type.split(";")[0], "webm")
        boundary = "----yingban" + secrets.token_hex(12)
        parts = [
            self._form_field(boundary, "model", config.stt_model),
            self._form_field(boundary, "language", "zh"),
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.{extension}\"\r\n"
                f"Content-Type: {mime_type}\r\n\r\n"
            ).encode() + audio + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        raw = self._request(
            self._versioned_url(config.base_url, "audio/transcriptions"),
            b"".join(parts),
            {"Content-Type": f"multipart/form-data; boundary={boundary}", "Authorization": f"Bearer {config.api_key}"},
            config.timeout_seconds,
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("语音识别接口没有返回有效 JSON") from error
        text = str(payload.get("text", "")).strip()
        if not text:
            raise RuntimeError("没有识别到可发送的文字")
        return text[:6000]

    def synthesize(self, text: str) -> tuple[bytes, str]:
        config = self.config()
        return self._synthesize_with_config(text, config)

    def _synthesize_with_config(self, text: str, config: VoiceConfig) -> tuple[bytes, str]:
        if not config.enabled:
            raise RuntimeError("管理员尚未配置语音服务")
        clean = text.strip()
        if not clean or len(clean) > 3000:
            raise ValueError("语音回复文字不能为空，且不能超过 3000 个字符")
        if config.provider == "stepfun" and len(clean) > 1000:
            raise ValueError("StepAudio 2.5 TTS 单次最多合成 1000 个字符")
        if config.provider == "doubao":
            return self._doubao_synthesize(clean, config)
        body = json.dumps(
            {
                "model": config.tts_model,
                "voice": config.voice_name,
                "input": clean,
                "response_format": config.audio_format,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        audio = self._request(
            self._versioned_url(config.base_url, "audio/speech"),
            body,
            {"Content-Type": "application/json", "Authorization": f"Bearer {config.api_key}"},
            config.timeout_seconds,
        )
        if not audio:
            raise RuntimeError("语音合成接口返回了空音频")
        return audio, ALLOWED_AUDIO_FORMATS[config.audio_format]

    def test_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.config(payload)
        if not config.enabled:
            raise ValueError("请先填写语音服务凭据、模型和音色")
        audio, content_type = self._synthesize_with_config("连接成功", config)
        return {"bytes": len(audio), "content_type": content_type}

    def _stepfun_transcribe(
        self, audio: bytes, mime_type: str, config: VoiceConfig
    ) -> str:
        normalized, audio_type = self._stepfun_audio(audio, mime_type)
        body = json.dumps(
            {
                "audio": {
                    "data": base64.b64encode(normalized).decode("ascii"),
                    "input": {
                        "transcription": {
                            "language": "zh",
                            "model": config.stt_model,
                            "enable_itn": True,
                        },
                        "format": {"type": audio_type},
                    },
                }
            },
            ensure_ascii=False,
        ).encode("utf-8")
        raw = self._request(
            self._versioned_url(config.base_url, "audio/asr/sse"),
            body,
            {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {config.api_key}",
            },
            config.timeout_seconds,
        )
        return self._decode_stepfun_asr(raw)

    @staticmethod
    def _decode_stepfun_asr(raw: bytes) -> str:
        try:
            stream = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError("阶跃语音识别返回格式无法解析") from error
        deltas: list[str] = []
        completed = ""
        for line in stream.splitlines():
            if not line.startswith("data:"):
                continue
            value = line.removeprefix("data:").strip()
            if not value or value == "[DONE]":
                continue
            try:
                event = json.loads(value)
            except json.JSONDecodeError as error:
                raise RuntimeError("阶跃语音识别返回了无效 SSE 数据") from error
            if event.get("type") == "error":
                raise RuntimeError(
                    f"阶跃语音识别失败：{str(event.get('message', '未知错误'))[:180]}"
                )
            if event.get("type") == "transcript.text.delta":
                deltas.append(str(event.get("delta", "")))
            if event.get("type") == "transcript.text.done":
                completed = str(event.get("text", "")).strip()
        text = completed or "".join(deltas).strip()
        if not text:
            raise RuntimeError("没有识别到可发送的文字")
        return text[:6000]

    @classmethod
    def _stepfun_audio(cls, audio: bytes, mime_type: str) -> tuple[bytes, str]:
        media_type = mime_type.split(";", 1)[0].lower()
        supported = {
            "audio/ogg": "ogg",
            "audio/mpeg": "mp3",
            "audio/mp3": "mp3",
            "audio/wav": "wav",
            "audio/x-wav": "wav",
            "audio/mp4": "m4a",
            "audio/x-m4a": "m4a",
        }
        if media_type in supported:
            return audio, supported[media_type]
        return cls._convert_audio_to_wav(audio), "wav"

    def _doubao_headers(self, config: VoiceConfig, resource_id: str, *, asr: bool = False) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
        }
        if config.api_key:
            headers["X-Api-Key"] = config.api_key
        else:
            headers["X-Api-App-Key"] = config.app_id
            headers["X-Api-Access-Key"] = config.access_token
        if asr:
            headers["X-Api-Sequence"] = "-1"
        return headers

    def _doubao_transcribe(self, audio: bytes, config: VoiceConfig) -> str:
        body = json.dumps(
            {
                "user": {"uid": config.app_id or "yingban"},
                "audio": {"data": base64.b64encode(audio).decode("ascii")},
                "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        raw = self._request(
            f"{config.base_url.rstrip('/')}/api/v3/auc/bigmodel/recognize/flash",
            body,
            self._doubao_headers(config, config.stt_model, asr=True),
            config.timeout_seconds,
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("豆包语音识别没有返回有效 JSON") from error
        text = str(payload.get("result", {}).get("text", "")).strip()
        if not text:
            raise RuntimeError("没有识别到可发送的文字")
        return text[:6000]

    @staticmethod
    def _doubao_audio(audio: bytes, mime_type: str) -> bytes:
        media_type = mime_type.split(";", 1)[0].lower()
        if media_type in {"audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3", "audio/ogg"}:
            return audio
        return VoiceRuntime._convert_audio_to_wav(audio)

    @staticmethod
    def _convert_audio_to_wav(audio: bytes) -> bytes:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("当前录音格式需要后端安装 ffmpeg 后才能交给豆包识别")
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
                input=audio,
                capture_output=True,
                check=False,
                timeout=20,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("录音格式转换超时，请缩短录音后重试") from error
        if result.returncode != 0 or not result.stdout:
            raise RuntimeError("当前录音格式无法转换，请改用最新版浏览器重试")
        if len(result.stdout) > 10_000_000:
            raise ValueError("转换后的录音超过 10 MB")
        return result.stdout

    def _doubao_synthesize(self, text: str, config: VoiceConfig) -> tuple[bytes, str]:
        doubao_format = "ogg_opus" if config.audio_format == "opus" else config.audio_format
        body = json.dumps(
            {
                "user": {"uid": config.app_id or "yingban"},
                "req_params": {
                    "text": text,
                    "speaker": config.voice_name,
                    "audio_params": {"format": doubao_format, "sample_rate": 24000},
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        raw = self._request(
            f"{config.base_url.rstrip('/')}/api/v3/tts/unidirectional",
            body,
            self._doubao_headers(config, config.tts_model),
            config.timeout_seconds,
        )
        audio = self._decode_doubao_tts(raw)
        if not audio:
            raise RuntimeError("豆包语音合成返回了空音频")
        return audio, ALLOWED_AUDIO_FORMATS[config.audio_format]

    @staticmethod
    def _decode_doubao_tts(raw: bytes) -> bytes:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError("豆包语音合成返回格式无法解析") from error
        decoder = json.JSONDecoder()
        index = 0
        chunks: list[bytes] = []
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text):
                break
            try:
                frame, index = decoder.raw_decode(text, index)
            except json.JSONDecodeError as error:
                raise RuntimeError("豆包语音合成返回了不完整的数据流") from error
            if frame.get("code") not in {None, 0, 20000000}:
                raise RuntimeError(f"豆包语音合成失败：{str(frame.get('message', frame.get('code')))[:180]}")
            if isinstance(frame.get("data"), str) and frame["data"]:
                try:
                    chunks.append(base64.b64decode(frame["data"], validate=True))
                except (ValueError, base64.binascii.Error) as error:
                    raise RuntimeError("豆包语音合成返回了无效音频数据") from error
        return b"".join(chunks)

    @staticmethod
    def _form_field(boundary: str, name: str, value: str) -> bytes:
        return f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()

    @staticmethod
    def _versioned_url(base_url: str, path: str) -> str:
        base = base_url.rstrip("/")
        return f"{base}/{path}" if base.endswith("/v1") else f"{base}/v1/{path}"

    @staticmethod
    def _request(url: str, body: bytes, headers: dict[str, str], timeout: float) -> bytes:
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(12_000_000)
        except urllib.error.HTTPError as error:
            detail = error.read(1000).decode("utf-8", "replace")
            raise RuntimeError(f"语音服务返回 HTTP {error.code}: {detail[:240]}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"无法连接语音服务：{error.reason}") from error
