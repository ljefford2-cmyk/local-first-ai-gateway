"""Cloud provider adapters for the egress gateway."""

from .anthropic import AnthropicAdapter
from .google import GoogleAdapter
from .ollama import OllamaAdapter
from .openai import OpenAIAdapter

ADAPTERS = {
    "anthropic": AnthropicAdapter,
    "openai": OpenAIAdapter,
    "google": GoogleAdapter,
    "ollama": OllamaAdapter,
}

__all__ = ["ADAPTERS", "AnthropicAdapter", "OpenAIAdapter", "GoogleAdapter", "OllamaAdapter"]
