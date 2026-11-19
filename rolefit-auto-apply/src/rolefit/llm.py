"""Provider-agnostic JSON completions for OpenAI, Anthropic, and Gemini."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Protocol

from rolefit.exceptions import LLMError

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from a model reply, including fenced blocks.

    Args:
        text: Raw model text.

    Returns:
        The decoded object.

    Raises:
        ValueError: The reply is not a JSON object.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = _JSON_FENCE.sub("", cleaned).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise ValueError("The model did not return a JSON object.") from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ValueError(f"The model returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("The model JSON root must be an object.")
    return data


def _transient(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__} {exc}".lower()
    markers = (
        "timeout",
        "429",
        "500",
        "502",
        "503",
        "504",
        "rate limit",
        "overloaded",
        "temporarily",
    )
    return any(marker in text for marker in markers)


class LLMClient(Protocol):
    """The completion surface used by tailoring, scoring, and screening answers."""

    provider: str
    model: str

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Return one JSON object."""


class ChatLLM:
    """Call one chat provider and always return a single JSON object.

    Transient HTTP failures are retried. A non-JSON reply is requested once more.
    """

    def __init__(self, provider: str, api_key: str, model: str) -> None:
        self.provider = provider
        self.api_key = api_key
        self.model = model

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        """Return the raw text of one completion.

        Raises:
            LLMError: The provider is unknown or the request failed.
        """
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                if self.provider == "openai":
                    return self._openai(system, user, temperature, max_tokens)
                if self.provider == "anthropic":
                    return self._anthropic(system, user, temperature, max_tokens)
                if self.provider == "gemini":
                    return self._gemini(system, user, temperature, max_tokens)
                raise LLMError(f"Unsupported LLM provider: {self.provider}")
            except LLMError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt == 2 or not _transient(exc):
                    raise LLMError(f"{self.provider} request failed: {exc}") from exc
                time.sleep(2**attempt)
        raise LLMError(f"{self.provider} request failed: {last_error}")

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Return one JSON object, retrying once when the first reply is not JSON."""
        prompt = user
        last_error: Exception | None = None
        for _ in range(2):
            text = self.complete(system, prompt, temperature=temperature, max_tokens=max_tokens)
            try:
                return parse_json_object(text)
            except ValueError as exc:
                last_error = exc
                prompt = (
                    user
                    + "\n\nThe previous reply was not valid JSON ("
                    + str(exc)
                    + "). Return one JSON object and no other text."
                )
        raise LLMError(f"The model did not return valid JSON: {last_error}")

    def _openai(self, system: str, user: str, temperature: float, max_tokens: int) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError("The openai package is not installed. pip install -e '.[llm]'") from exc

        client = OpenAI(api_key=self.api_key, timeout=90)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        response = None
        last_error: Exception | None = None
        for _ in range(4):
            try:
                response = client.chat.completions.create(**kwargs)
                break
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                changed = False
                if "response_format" in kwargs and (
                    "response_format" in message or "json_object" in message
                ):
                    kwargs.pop("response_format", None)
                    changed = True
                if "temperature" in kwargs and "temperature" in message:
                    kwargs.pop("temperature", None)
                    changed = True
                if "max_tokens" in kwargs and "max_tokens" in message:
                    kwargs.pop("max_tokens", None)
                    kwargs["max_completion_tokens"] = max_tokens
                    changed = True
                if not changed:
                    raise
        if response is None:
            raise LLMError(f"OpenAI request failed: {last_error}")
        content = response.choices[0].message.content if response.choices else ""
        if not content or not content.strip():
            raise LLMError("OpenAI returned an empty response.")
        return content

    def _anthropic(self, system: str, user: str, _temperature: float, max_tokens: int) -> str:
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise LLMError(
                "The anthropic package is not installed. pip install -e '.[llm]'"
            ) from exc

        client = Anthropic(api_key=self.api_key, timeout=90)
        response = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system + "\nReturn one JSON object and no other text.",
            messages=[{"role": "user", "content": user}],
        )
        parts: list[str] = []
        for block in response.content:
            text = getattr(block, "text", "")
            if text:
                parts.append(text)
        content = "".join(parts).strip()
        if not content:
            raise LLMError("Anthropic returned an empty response.")
        return content

    def _gemini(self, system: str, user: str, temperature: float, max_tokens: int) -> str:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise LLMError(
                "The google-genai package is not installed. pip install -e '.[llm]'"
            ) from exc

        client = genai.Client(api_key=self.api_key)
        response = client.models.generate_content(
            model=self.model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system + "\nReturn one JSON object and no other text.",
                temperature=temperature,
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
            ),
        )
        content = (getattr(response, "text", None) or "").strip()
        if not content:
            raise LLMError("Gemini returned an empty response.")
        return content


def build_llm(provider: str, api_key: str, model: str) -> ChatLLM:
    """Construct the chat client for a configured provider."""
    return ChatLLM(provider=provider, api_key=api_key, model=model)
