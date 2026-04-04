"""Ollama (local model) provider adapter."""

from __future__ import annotations

from typing import Any


class OllamaAdapter:
    """Formats requests and parses responses for Ollama's generate API."""

    @staticmethod
    def build_request(
        endpoint_url: str,
        model: str,
        prompt: str,
        api_key: str,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        body = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        return endpoint_url, headers, body

    @staticmethod
    def parse_response(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "response_text": data.get("response", ""),
            "token_count_in": 0,
            "token_count_out": 0,
            "finish_reason": "stop",
        }
