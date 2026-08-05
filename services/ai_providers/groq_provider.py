from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .base import AIProviderError, AIProviderResult


class GroqProvider:
    """Adaptador do executor Groq legado ao contrato comum de providers."""

    name = "groq"

    def __init__(self, executor: Callable[..., str], model: str) -> None:
        self._executor = executor
        self.model = model

    def generate(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        response_schema: dict[str, Any] | None = None,
        temperature: float = 0.4,
        max_output_tokens: int = 500,
    ) -> AIProviderResult:
        del temperature, max_output_tokens
        try:
            content = self._executor(
                messages,
                modelo=self.model,
                response_format_json=response_schema is not None,
            )
        except Exception as exc:
            raise exc

        texto = str(content or "").strip()
        if not texto:
            raise AIProviderError("provider retornou resposta vazia", error_code="content_empty")
        parsed = _parse_json_object(texto)
        return AIProviderResult(
            content=texto,
            parsed_json=parsed,
            provider=self.name,
            model=self.model,
        )


def _parse_json_object(texto: str) -> dict[str, Any] | None:
    try:
        valor = json.loads(texto)
    except json.JSONDecodeError:
        return None
    return dict(valor) if isinstance(valor, Mapping) else None
