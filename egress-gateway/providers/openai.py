"""OpenAI (ChatGPT) provider adapter."""

from __future__ import annotations

from typing import Any


class OpenAIAdapter:
    """Formats requests and parses responses for OpenAI's Chat Completions API."""

    @staticmethod
    def build_request(
        endpoint_url: str,
        model: str,
        prompt: str,
        api_key: str,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        return endpoint_url, headers, body

    @staticmethod
    def parse_response(data: dict[str, Any]) -> dict[str, Any]:
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return {
            "response_text": text,
            "token_count_in": usage.get("prompt_tokens", 0),
            "token_count_out": usage.get("completion_tokens", 0),
            "finish_reason": data["choices"][0].get("finish_reason", "stop"),
        }
