from __future__ import annotations

import base64
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from model_catalog import stepfun_model_ids
from settings import Settings
from storage import Store


IMAGE_SETTING_KEYS = (
    "image_api_key",
    "image_base_url",
    "image_model",
    "image_timeout_seconds",
)
ALLOWED_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def _url(value: Any) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        raise ValueError("图像接口根地址必须是有效的 http 或 https 地址")
    return candidate


@dataclass(frozen=True)
class ImageConfig:
    api_key: str
    base_url: str
    model: str
    timeout_seconds: float

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model)


class ImageRuntime:
    """StepFun image generation and single-image editing adapter."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    def config(self, overrides: dict[str, Any] | None = None) -> ImageConfig:
        saved = self.store.get_app_settings(IMAGE_SETTING_KEYS)
        current: dict[str, Any] = {
            "api_key": saved.get("image_api_key", self.settings.image_api_key),
            "base_url": saved.get("image_base_url", self.settings.image_base_url),
            "model": saved.get("image_model", self.settings.image_model),
            "timeout_seconds": saved.get(
                "image_timeout_seconds", self.settings.image_timeout_seconds
            ),
        }
        if overrides:
            if overrides.get("clear_api_key") is True:
                current["api_key"] = ""
            elif str(overrides.get("api_key", "")).strip():
                current["api_key"] = str(overrides["api_key"]).strip()
            for key in ("base_url", "model", "timeout_seconds"):
                if key in overrides:
                    current[key] = overrides[key]
        api_key = str(current["api_key"]).strip()
        if len(api_key) > 10_000:
            raise ValueError("图像服务 API 密钥长度异常")
        model = str(current["model"]).strip()
        if model not in stepfun_model_ids("image"):
            raise ValueError("请选择可用于图像生成或编辑的阶跃星辰模型")
        try:
            timeout = float(current["timeout_seconds"])
        except (TypeError, ValueError) as error:
            raise ValueError("图像请求超时格式不正确") from error
        if not 5 <= timeout <= 300:
            raise ValueError("图像请求超时必须在 5 到 300 秒之间")
        return ImageConfig(api_key, _url(current["base_url"]), model, timeout)

    def public_config(self) -> dict[str, Any]:
        config = self.config()
        return {
            "provider": "stepfun",
            "base_url": config.base_url,
            "model": config.model,
            "timeout_seconds": config.timeout_seconds,
            "has_api_key": bool(config.api_key),
            "api_key_hint": f"••••{config.api_key[-4:]}" if config.api_key else "",
            "enabled": config.enabled,
        }

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.config(payload)
        self.store.set_app_settings(
            {
                "image_api_key": config.api_key,
                "image_base_url": config.base_url,
                "image_model": config.model,
                "image_timeout_seconds": str(config.timeout_seconds),
            }
        )
        return self.public_config()

    def test_connection(self, payload: dict[str, Any]) -> dict[str, str]:
        config = self.config(payload)
        if not config.enabled:
            raise ValueError("请先填写阶跃星辰 API 密钥")
        raw = self._request(
            self._versioned_url(config.base_url, f"models/{config.model}"),
            None,
            {"Authorization": f"Bearer {config.api_key}"},
            config.timeout_seconds,
            method="GET",
        )
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("图像模型查询没有返回有效 JSON") from error
        returned_model = str(result.get("id", "")).strip()
        if returned_model != config.model:
            raise RuntimeError("图像模型查询没有返回所选模型")
        return {"model": returned_model}

    def generate(self, prompt: str, size: str = "1024x1024") -> tuple[bytes, str]:
        config = self.config()
        clean = self._prompt(prompt)
        if not config.enabled:
            raise RuntimeError("管理员尚未配置图像模型")
        if size not in {"1024x1024", "768x1360", "896x1184", "1360x768", "1184x896"}:
            raise ValueError("所选图片尺寸不受 Step Image Edit 2 支持")
        body = json.dumps(
            {
                "model": config.model,
                "prompt": clean,
                "size": size,
                "response_format": "b64_json",
                "cfg_scale": 1.0,
                "steps": 8,
                "text_mode": True,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        raw = self._request(
            self._versioned_url(config.base_url, "images/generations"),
            body,
            {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            config.timeout_seconds,
        )
        return self._image_result(raw)

    def edit(self, image: bytes, mime_type: str, prompt: str) -> tuple[bytes, str]:
        config = self.config()
        clean = self._prompt(prompt)
        media_type = mime_type.split(";", 1)[0].lower()
        if not config.enabled:
            raise RuntimeError("管理员尚未配置图像模型")
        if not image or len(image) > 10_000_000:
            raise ValueError("待编辑图片不能为空，且不能超过 10 MB")
        if media_type not in ALLOWED_IMAGE_TYPES:
            raise ValueError("图像编辑只支持 JPG、PNG 或 WebP")
        boundary = "----yingban-image-" + secrets.token_hex(12)
        fields = {
            "model": config.model,
            "prompt": clean,
            "response_format": "b64_json",
            "cfg_scale": "1.0",
            "steps": "8",
            "text_mode": "true",
        }
        parts = [self._form_field(boundary, name, value) for name, value in fields.items()]
        filename = f"input.{ALLOWED_IMAGE_TYPES[media_type]}"
        parts.extend(
            [
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                    f"filename=\"{filename}\"\r\nContent-Type: {media_type}\r\n\r\n"
                ).encode()
                + image
                + b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        raw = self._request(
            self._versioned_url(config.base_url, "images/edits"),
            b"".join(parts),
            {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            config.timeout_seconds,
        )
        return self._image_result(raw)

    @staticmethod
    def _prompt(value: str) -> str:
        clean = str(value or "").strip()
        if not clean or len(clean) > 512:
            raise ValueError("图像提示词不能为空，且不能超过 512 个字符")
        return clean

    @staticmethod
    def _image_result(raw: bytes) -> tuple[bytes, str]:
        try:
            payload = json.loads(raw.decode("utf-8"))
            item = payload["data"][0]
            encoded = str(item["b64_json"])
            image = base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("图像接口没有返回有效的 Base64 图片") from error
        if not image:
            raise RuntimeError("图像接口返回了空图片")
        return image, "image/png"

    @staticmethod
    def _form_field(boundary: str, name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n"
        ).encode()

    @staticmethod
    def _versioned_url(base_url: str, path: str) -> str:
        base = base_url.rstrip("/")
        return f"{base}/{path}" if base.endswith("/v1") else f"{base}/v1/{path}"

    @staticmethod
    def _request(
        url: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: float,
        *,
        method: str = "POST",
    ) -> bytes:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(16_000_000)
        except urllib.error.HTTPError as error:
            detail = error.read(1000).decode("utf-8", "replace")
            raise RuntimeError(f"图像服务返回 HTTP {error.code}: {detail[:240]}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"无法连接图像服务：{error.reason}") from error
