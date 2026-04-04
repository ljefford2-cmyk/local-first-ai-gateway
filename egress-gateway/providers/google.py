"""Google (Gemini) provider adapter."""

from __future__ import annotations

from typing import Any


class GoogleAdapter:
    """Formats requests and parses responses for Google's Gemini API."""

    @staticmethod
    def build_request(
        endpoint_url: str,
        model: str,
        prompt: str,
        api_key: str,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        # Google uses API key as query parameter
        url = f"{endpoint_url}?key={api_key}"
        headers = {"Content-Type": "application/json"}
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
        }
        return url, headers, body

    @staticmethod
    def parse_response(data: dict[str, Any]) -> dict[str, Any]:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        usage = data.get("usageMetadata", {})
        return {
            "response_text": text,
            "token_count_in": usage.get("promptTokenCount", 0),
            "token_count_out": usage.get("candidatesTokenCount", 0),
            "finish_reason": data["candidates"][0].get("finishReason", "STOP"),
        }
