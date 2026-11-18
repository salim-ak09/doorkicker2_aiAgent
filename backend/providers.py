"""Provider interface and an Ollama adapter using its local HTTP API."""

import json
import logging
import math
import os
import socket
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Base error for provider failures."""


class ProviderUnavailableError(ProviderError):
    """The configured provider cannot be reached or used."""


class ProviderTimeoutError(ProviderUnavailableError):
    """The provider did not respond before the configured timeout."""


class ModelUnavailableError(ProviderError):
    """The requested model is not available from the provider."""


class InvalidProviderResponseError(ProviderError):
    """The provider returned an invalid response."""


class ProviderHTTPError(ProviderUnavailableError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Provider returned HTTP status {status_code}")


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    available: bool
    model: str
    model_available: bool
    detail: str


class AIProvider(Protocol):
    """Common contract for providers that generate text responses."""

    def get_status(self) -> ProviderStatus:
        """Return provider and configured-model availability."""
        ...

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Generate a response from system and user prompts."""
        ...


@runtime_checkable
class StructuredAIProvider(Protocol):
    """Optional provider capability for schema-constrained JSON responses."""

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, object],
    ) -> str:
        ...


class OllamaProvider:
    """Generate text through a local Ollama server without external packages."""

    name = "ollama"

    def __init__(
        self,
        model: str = "qwen2.5:3b",
        base_url: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must not be empty")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be greater than zero")

        configured_url = (
            base_url
            if base_url is not None
            else os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        )
        parsed_url = urlsplit(configured_url)
        if (
            parsed_url.scheme not in ("http", "https")
            or not parsed_url.hostname
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("base_url must be an absolute HTTP or HTTPS URL")
        try:
            parsed_url.port
        except ValueError as exc:
            raise ValueError("base_url contains an invalid port") from exc
        if parsed_url.username or parsed_url.password:
            raise ValueError("base_url must not contain credentials")

        self.model = model.strip()
        self._base_url = configured_url.rstrip("/")
        self.timeout = timeout

    def get_status(self) -> ProviderStatus:
        try:
            response = self._request_json("GET", "/api/tags")
        except ProviderTimeoutError:
            logger.warning("Ollama status check timed out")
            return ProviderStatus(
                self.name, False, self.model, False, "Ollama status check timed out"
            )
        except ProviderUnavailableError:
            logger.warning("Ollama endpoint is unavailable")
            return ProviderStatus(
                self.name, False, self.model, False, "Ollama endpoint is unavailable"
            )

        models = response.get("models")
        if not isinstance(models, list):
            raise InvalidProviderResponseError(
                "Ollama tags response must contain a models list"
            )
        model_available = any(
            isinstance(item, dict) and item.get("name") == self.model
            for item in models
        )
        if model_available:
            logger.info("Ollama is available with the configured model")
            detail = "Ollama and the configured model are available"
        else:
            logger.info("Ollama is available; configured model is not installed")
            detail = "Configured Ollama model is not installed"
        return ProviderStatus(
            self.name, True, self.model, model_available, detail
        )

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        return self._generate(system_prompt, user_prompt, "json")

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, object],
    ) -> str:
        if not isinstance(response_schema, dict):
            raise ValueError("response_schema must be a JSON Schema object")
        return self._generate(system_prompt, user_prompt, response_schema)

    def _generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_format: str | dict[str, object],
    ) -> str:
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("system_prompt must not be empty")
        if not isinstance(user_prompt, str) or not user_prompt.strip():
            raise ValueError("user_prompt must not be empty")

        status = self.get_status()
        if not status.available:
            raise ProviderUnavailableError(status.detail)
        if not status.model_available:
            raise ModelUnavailableError(status.detail)

        payload = {
            "model": self.model,
            "format": response_format,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {
                "num_gpu": 0,
                "num_ctx": 2048,
                "num_predict": 512,
                "temperature": 0.2,
            },
        }
        try:
            response = self._request_json("POST", "/api/chat", payload)
        except ProviderHTTPError as exc:
            if exc.status_code == 404:
                raise ModelUnavailableError(
                    "Configured Ollama model is no longer available"
                ) from exc
            raise

        message = response.get("message")
        if not isinstance(message, dict):
            raise InvalidProviderResponseError(
                "Ollama chat response must contain a message object"
            )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise InvalidProviderResponseError(
                "Ollama chat response must contain non-empty message content"
            )
        return content

    def _request_json(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw_response = response.read()
        except HTTPError as exc:
            raise ProviderHTTPError(exc.code) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ProviderTimeoutError("Ollama request timed out") from exc
        except URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise ProviderTimeoutError("Ollama request timed out") from exc
            raise ProviderUnavailableError("Ollama endpoint is unavailable") from exc
        except OSError as exc:
            raise ProviderUnavailableError("Ollama endpoint is unavailable") from exc

        try:
            parsed_response = json.loads(raw_response)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvalidProviderResponseError(
                "Ollama returned invalid JSON"
            ) from exc
        if not isinstance(parsed_response, dict):
            raise InvalidProviderResponseError(
                "Ollama response must be a JSON object"
            )
        return parsed_response
