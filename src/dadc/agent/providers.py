"""Parameter-selection providers. Providers return data; they never execute code."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class ParameterSelectionProvider(Protocol):
    provider_id: str
    model: str

    def select(self, context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]: ...


@dataclass
class DeterministicFixtureProvider:
    """Portable provider used to prove the agent boundary without an LLM."""

    provider_id: str = "deterministic_fixture"
    model: str = "bounded_selection_fixture_v1"

    def select(self, context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        selected = []
        for policy in context["request"]["parameter_policy"]:
            count = min(int(policy["max_selected_values"]), len(policy["allowed_values"]))
            selected.append({"name": policy["name"], "values": policy["allowed_values"][:count]})
        chunk_ids = [item["chunk_id"] for item in context["knowledge_evidence"][:2]]
        value = {
            "agent_parameter_selection_version": "1.0",
            "selected_values": selected,
            "knowledge_chunk_ids": chunk_ids,
            "rationale": (
                "Deterministic contract fixture selected the first allow-listed values. "
                "This proves orchestration only and is not an AI or physical recommendation."
            ),
        }
        return value, {
            "provider_id": self.provider_id,
            "model": self.model,
            "network_call": False,
            "raw_response": value,
        }


@dataclass
class DeepSeekJSONProvider:
    """DeepSeek JSON-output provider using the official OpenAI-compatible endpoint."""

    model: str = "deepseek-v4-flash"
    timeout_seconds: int = 120
    api_key_env: str = "DEEPSEEK_API_KEY"
    provider_id: str = "deepseek_json"
    endpoint: str = "https://api.deepseek.com/chat/completions"

    @staticmethod
    def _messages(context: dict[str, Any]) -> list[dict[str, str]]:
        response_shape = {
            "agent_parameter_selection_version": "1.0",
            "selected_values": [
                {"name": "parameter_name", "values": [1.0, 2.0]}
            ],
            "knowledge_chunk_ids": ["chunk_id_used"],
            "rationale": "short evidence-grounded explanation",
        }
        system = (
            "You are a bounded engineering parameter-planning assistant. Return JSON only. "
            "Select values only from each parameter's allowed_values. Do not change the solver, "
            "objective, units, budget, files, commands, or code. Do not claim that a target is met; "
            "only the independent solver verification can decide acceptance. Use only supplied "
            "knowledge chunk IDs and DADC history. The JSON must match this example shape: "
            + json.dumps(response_shape, ensure_ascii=False)
        )
        user = json.dumps(
            {
                "instruction": "Propose a small parameter grid within the declared constraints.",
                "context": context,
            },
            ensure_ascii=False,
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def select(self, context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Missing {self.api_key_env}; set it in the current PowerShell session without "
                "putting the key in a repository file"
            )
        messages = self._messages(context)
        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 1600,
            "stream": False,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_bytes = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"DeepSeek API HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DeepSeek API connection failed: {exc.reason}") from exc
        envelope = json.loads(response_bytes.decode("utf-8"))
        try:
            message = envelope["choices"][0]["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("DeepSeek response does not contain choices[0].message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("DeepSeek returned empty JSON content")
        try:
            selection = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError("DeepSeek returned content that is not valid JSON") from exc
        usage = envelope.get("usage", {})
        return selection, {
            "provider_id": self.provider_id,
            "model": envelope.get("model", self.model),
            "network_call": True,
            "finish_reason": envelope.get("choices", [{}])[0].get("finish_reason"),
            "usage": usage if isinstance(usage, dict) else {},
            "raw_response": selection,
        }


def create_provider(provider: str, *, model: str | None = None) -> ParameterSelectionProvider:
    if provider == "deterministic_fixture":
        return DeterministicFixtureProvider()
    if provider == "deepseek":
        return DeepSeekJSONProvider(model=model or "deepseek-v4-flash")
    raise ValueError(f"Unsupported agent provider: {provider!r}")
