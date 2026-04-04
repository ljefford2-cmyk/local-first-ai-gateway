"""Local classifier using Ollama (llama3.1:8b).

Classifies requests — does NOT answer them. "Route, don't reason."
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")

CLASSIFICATION_PROMPT = """You are a request classifier for an AI routing system. Your ONLY job is to classify the user's request. Do NOT answer the request.

Classify into exactly one category:
- quick_lookup: Simple factual questions, basic math, definitions, common knowledge that a small model can answer confidently in one sentence
- analysis: Comparison, evaluation, pros/cons, recommendations, research, legal/technical analysis, anything requiring depth or multi-step reasoning
- creative: Writing, brainstorming, storytelling, poetry, content creation
- code: Programming, debugging, technical implementation, writing functions
- summarization: Condensing documents, articles, conversations
- conversation: Casual chat, greetings, personal questions

Also determine:
- local_capable: true ONLY for quick_lookup where the answer is a well-known fact, simple math, or a short definition. false for EVERYTHING else — including code, creative, analysis, and summarization.
- routing: "local" if local_capable is true, "cloud" otherwise
- recommended_model: For cloud routing, pick the best fit:
  - "claude" for analysis, reasoning, safety-sensitive topics
  - "chatgpt" for creative writing, conversation, general tasks
  - "gemini" for summarization, research synthesis, multi-source comparison
  For local routing, use "local".

Here are examples of correct classifications:

User: "What is the capital of France?"
{{"category": "quick_lookup", "local_capable": true, "routing": "local", "recommended_model": "local", "confidence": 0.95}}

User: "What is 2 plus 2?"
{{"category": "quick_lookup", "local_capable": true, "routing": "local", "recommended_model": "local", "confidence": 0.99}}

User: "Compare the architectural trade-offs between microservices and monolith patterns"
{{"category": "analysis", "local_capable": false, "routing": "cloud", "recommended_model": "claude", "confidence": 0.9}}

User: "Write a legal analysis of the EU AI Act"
{{"category": "analysis", "local_capable": false, "routing": "cloud", "recommended_model": "claude", "confidence": 0.9}}

User: "Help me write a Python function to sort a list"
{{"category": "code", "local_capable": false, "routing": "cloud", "recommended_model": "claude", "confidence": 0.9}}

User: "Write a poem about the ocean"
{{"category": "creative", "local_capable": false, "routing": "cloud", "recommended_model": "chatgpt", "confidence": 0.9}}

Now classify this request:

User: "{raw_input}"

Respond with ONLY valid JSON, no explanation:
{{"category": "...", "local_capable": true/false, "routing": "local|cloud", "recommended_model": "claude|chatgpt|gemini|local", "confidence": 0.0-1.0}}"""

VALID_CATEGORIES = {
    "quick_lookup", "analysis", "creative", "code", "summarization", "conversation",
}
VALID_MODELS = {"claude", "chatgpt", "gemini", "local"}

# Mapping from Ollama recommendation to orchestrator values
MODEL_MAP = {
    "local": {
        "capability_id": "route.local",
        "candidate_model": "llama3.1:8b",
        "route_id": "ollama-llama3-local",
    },
    "claude": {
        "capability_id": "route.cloud.claude",
        "candidate_model": "claude-sonnet-4-20250514",
        "route_id": "claude-sonnet-default",
    },
    "chatgpt": {
        "capability_id": "route.cloud.openai",
        "candidate_model": "gpt-4o",
        "route_id": "openai-gpt4o-default",
    },
    "gemini": {
        "capability_id": "route.cloud.gemini",
        "candidate_model": "gemini-2.0-flash",
        "route_id": "google-gemini-default",
    },
}

FALLBACK = {
    "category": "analysis",
    "local_capable": False,
    "routing": "cloud",
    "recommended_model": "claude",
    "confidence": 0.5,
}


@dataclass
class ClassificationResult:
    category: str
    local_capable: bool
    routing: str  # "local" or "cloud"
    recommended_model: str  # "claude", "chatgpt", "gemini", "local"
    confidence: float
    capability_id: str
    candidate_models: list[str]
    route_id: str


@dataclass
class LocalResponseResult:
    text: str
    token_count_in: int
    token_count_out: int


def _parse_classification(raw: str) -> dict[str, Any]:
    """Parse Ollama JSON response. Returns fallback on any failure."""
    try:
        # Strip markdown code fences if present
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last lines (fences)
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        data = json.loads(text)

        category = data.get("category", "")
        if category not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category: {category}")

        recommended = data.get("recommended_model", "")
        if recommended not in VALID_MODELS:
            raise ValueError(f"Invalid recommended_model: {recommended}")

        local_capable = bool(data.get("local_capable", False))
        routing = data.get("routing", "cloud")
        if routing not in ("local", "cloud"):
            routing = "cloud"

        confidence = float(data.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        return {
            "category": category,
            "local_capable": local_capable,
            "routing": routing,
            "recommended_model": recommended,
            "confidence": confidence,
        }
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.warning("Classification parse failed: %s — raw: %s", exc, raw[:200])
        return dict(FALLBACK)


async def classify(raw_input: str) -> ClassificationResult:
    """Classify a request using Ollama llama3.1:8b."""
    prompt = CLASSIFICATION_PROMPT.format(raw_input=raw_input)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": "llama3.1:8b",
                    "prompt": prompt,
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 100,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            raw_response = data.get("response", "")
    except Exception as exc:
        logger.error("Ollama classification call failed: %s", exc)
        raw_response = ""

    parsed = _parse_classification(raw_response)

    model_key = parsed["recommended_model"]
    mapping = MODEL_MAP.get(model_key, MODEL_MAP["claude"])

    return ClassificationResult(
        category=parsed["category"],
        local_capable=parsed["local_capable"],
        routing=parsed["routing"],
        recommended_model=parsed["recommended_model"],
        confidence=parsed["confidence"],
        capability_id=mapping["capability_id"],
        candidate_models=[mapping["candidate_model"]],
        route_id=mapping["route_id"],
    )


async def generate_local_response(raw_input: str) -> LocalResponseResult:
    """Use Ollama to generate a substantive response for local-capable requests."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": "llama3.1:8b",
                    "prompt": raw_input,
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {
                        "temperature": 0.7,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return LocalResponseResult(
                text=data.get("response", ""),
                token_count_in=data.get("prompt_eval_count", 0),
                token_count_out=data.get("eval_count", 0),
            )
    except Exception as exc:
        logger.error("Ollama local response failed: %s", exc)
        raise


async def check_ollama_health() -> str:
    """Check Ollama connectivity. Returns 'healthy', 'degraded', or 'down'."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("name", "") for m in data.get("models", [])]
                if any("llama3.1:8b" in m for m in models):
                    return "healthy"
                return "degraded"
            return "degraded"
    except Exception:
        return "down"
