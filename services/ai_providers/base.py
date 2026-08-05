from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence


@dataclass(slots=True)
class AIProviderResult:
    content: str
    parsed_json: dict[str, Any] | None
    provider: str
    model: str
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


class AIProviderError(RuntimeError):
    """Erro normalizado de um provider, sem expor credenciais."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.status_code = status_code


class AIProvider(Protocol):
    def generate(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        response_schema: dict[str, Any] | None = None,
        temperature: float = 0.4,
        max_output_tokens: int = 500,
    ) -> AIProviderResult:
        ...
