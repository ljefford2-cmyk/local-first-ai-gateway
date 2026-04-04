"""Anthropic (Claude) provider adapter."""

from __future__ import annotations

from typing import Any

import httpx


class AnthropicAdapter:
    """Formats requests and parses responses for Anthropic's Messages API."""

    @staticmethod
    def build_request(
        endpoint_url: str,
        model: str,
        prompt: str,
        api_key: str,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Return (url, headers, body) for the API call."""
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        return endpoint_url, headers, body

    @staticmethod
    def parse_response(data: dict[str, Any]) -> dict[str, Any]:
        """Extract text, token counts, and finish reason from response."""
        text = data["content"][0]["text"]
        usage = data.get("usage", {})
        return {
            "response_text": text,
            "token_count_in": usage.get("input_tokens", 0),
            "token_count_out": usage.get("output_tokens", 0),
            "finish_reason": data.get("stop_reason", "stop"),
        }
