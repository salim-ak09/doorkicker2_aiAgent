import json
import socket
import unittest
from io import BytesIO
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from backend.providers import (
    InvalidProviderResponseError,
    ModelUnavailableError,
    OllamaProvider,
    ProviderStatus,
    ProviderTimeoutError,
)


class FakeResponse(BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def json_response(data: object) -> FakeResponse:
    return FakeResponse(json.dumps(data).encode("utf-8"))


class OllamaProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OllamaProvider(
            model="qwen2.5:3b",
            base_url="http://localhost:11434",
            timeout=1.5,
        )

    @patch("backend.providers.urlopen")
    def test_generate_sends_prompts_and_returns_actual_model_content(
        self, mock_urlopen
    ) -> None:
        mock_urlopen.side_effect = [
            json_response({"models": [{"name": "qwen2.5:3b"}]}),
            json_response({"message": {"content": "Model response"}}),
        ]

        result = self.provider.generate("System instructions", "User request")

        self.assertEqual(result, "Model response")
        status_request = mock_urlopen.call_args_list[0].args[0]
        chat_request = mock_urlopen.call_args_list[1].args[0]
        self.assertEqual(status_request.full_url, "http://localhost:11434/api/tags")
        self.assertEqual(chat_request.full_url, "http://localhost:11434/api/chat")
        self.assertEqual(chat_request.get_method(), "POST")
        request_body = json.loads(chat_request.data)
        self.assertEqual(request_body["model"], "qwen2.5:3b")
        self.assertEqual(
            request_body["messages"],
            [
                {"role": "system", "content": "System instructions"},
                {"role": "user", "content": "User request"},
            ],
        )
        self.assertEqual(request_body["format"], "json")
        self.assertFalse(request_body["stream"])
        self.assertEqual(request_body["options"]["num_gpu"], 0)

    @patch("backend.providers.urlopen")
    def test_structured_generation_sends_json_schema(self, mock_urlopen) -> None:
        response_schema = {
            "type": "object",
            "properties": {"summary": {"type": "string", "minLength": 1}},
            "required": ["summary"],
            "additionalProperties": False,
        }
        mock_urlopen.side_effect = [
            json_response({"models": [{"name": "qwen2.5:3b"}]}),
            json_response({"message": {"content": '{"summary":"Ready"}'}}),
        ]

        result = self.provider.generate_structured(
            "System instructions",
            "User request",
            response_schema,
        )

        self.assertEqual(result, '{"summary":"Ready"}')
        request_body = json.loads(mock_urlopen.call_args_list[1].args[0].data)
        self.assertEqual(request_body["format"], response_schema)

    @patch("backend.providers.urlopen")
    def test_status_distinguishes_server_and_missing_model(self, mock_urlopen) -> None:
        mock_urlopen.return_value = json_response({"models": []})

        status = self.provider.get_status()

        self.assertTrue(status.available)
        self.assertFalse(status.model_available)
        self.assertEqual(status.model, "qwen2.5:3b")

    @patch("backend.providers.urlopen")
    def test_missing_model_prevents_chat_request(self, mock_urlopen) -> None:
        mock_urlopen.return_value = json_response({"models": []})

        with self.assertRaises(ModelUnavailableError):
            self.provider.generate("System", "User")
        mock_urlopen.assert_called_once()

    @patch("backend.providers.urlopen")
    def test_unreachable_server_is_reported_without_claiming_connection(
        self, mock_urlopen
    ) -> None:
        mock_urlopen.side_effect = URLError("connection refused")

        status = self.provider.get_status()

        self.assertFalse(status.available)
        self.assertFalse(status.model_available)

    @patch("backend.providers.urlopen")
    def test_timeout_during_generation_is_explicit(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            json_response({"models": [{"name": "qwen2.5:3b"}]}),
            socket.timeout(),
        ]

        with self.assertRaises(ProviderTimeoutError):
            self.provider.generate("System", "User")

    @patch("backend.providers.urlopen")
    def test_model_removed_after_status_check_is_reported(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            json_response({"models": [{"name": "qwen2.5:3b"}]}),
            HTTPError(
                "http://localhost:11434/api/chat",
                404,
                "not found",
                {},
                None,
            ),
        ]

        with self.assertRaises(ModelUnavailableError):
            self.provider.generate("System", "User")

    @patch("backend.providers.urlopen")
    def test_invalid_provider_payloads_are_rejected(self, mock_urlopen) -> None:
        mock_urlopen.return_value = json_response({"models": "not-a-list"})
        with self.assertRaises(InvalidProviderResponseError):
            self.provider.get_status()

        mock_urlopen.return_value = json_response({"message": {"content": "  "}})
        with patch.object(
            self.provider,
            "get_status",
            return_value=ProviderStatus(
                "ollama", True, "qwen2.5:3b", True, "available"
            ),
        ):
            with self.assertRaises(InvalidProviderResponseError):
                self.provider.generate("System", "User")

    def test_invalid_configuration_and_prompts_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            OllamaProvider(base_url="file://not-http")
        with self.assertRaises(ValueError):
            OllamaProvider(timeout=0)
        with self.assertRaises(ValueError):
            OllamaProvider(timeout=float("inf"))
        with self.assertRaises(ValueError):
            OllamaProvider(base_url="http://localhost:invalid")
        with self.assertRaises(ValueError):
            self.provider.generate(" ", "User")
