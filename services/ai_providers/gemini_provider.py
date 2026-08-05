from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .base import AIProviderError, AIProviderResult


logger = logging.getLogger(__name__)
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = max(float(timeout_seconds), 1.0)
        self._client = client

    def generate(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        response_schema: dict[str, Any] | None = None,
        temperature: float = 0.4,
        max_output_tokens: int = 500,
    ) -> AIProviderResult:
        if not self.api_key:
            raise AIProviderError("GEMINI_API_KEY nao configurada", error_code="missing_api_key")

        client, types = self._client_and_types()
        contents, system_instruction = _convert_messages(messages, types)
        config_kwargs: dict[str, Any] = {
            "system_instruction": system_instruction or None,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        }
        if response_schema is not None:
            config_kwargs.update(
                {
                    "response_mime_type": "application/json",
                    "response_json_schema": response_schema,
                }
            )
        config = types.GenerateContentConfig(**config_kwargs)

        for tentativa in range(2):
            try:
                resposta = client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=config,
                )
                return self._result_from_response(resposta)
            except AIProviderError as exc:
                if not exc.retryable or tentativa:
                    raise
                logger.warning(
                    "ai_provider_retry provider=gemini model=%s erro_codigo=%s tentativa=%s",
                    self.model,
                    exc.error_code,
                    tentativa + 2,
                )
            except Exception as exc:
                erro = _normalizar_erro_gemini(exc)
                if not erro.retryable or tentativa:
                    raise erro from exc
                logger.warning(
                    "ai_provider_retry provider=gemini model=%s erro_codigo=%s tentativa=%s",
                    self.model,
                    erro.error_code,
                    tentativa + 2,
                )

        raise AIProviderError("falha ao consultar Gemini", error_code="provider_error")

    def _client_and_types(self) -> tuple[Any, Any]:
        if self._client is not None:
            from google.genai import types

            return self._client, types

        try:
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(
                    timeout=int(self.timeout_seconds * 1000),
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
            return self._client, types
        except Exception as exc:
            raise _normalizar_erro_gemini(exc) from exc

    def _result_from_response(self, resposta: Any) -> AIProviderResult:
        content = str(getattr(resposta, "text", "") or "").strip()
        if not content:
            raise AIProviderError("Gemini retornou resposta vazia", error_code="content_empty")

        usage_metadata = getattr(resposta, "usage_metadata", None)
        usage = _usage_dict(usage_metadata)
        candidates = getattr(resposta, "candidates", None) or []
        finish_reason = None
        if candidates:
            finish_reason = str(getattr(candidates[0], "finish_reason", "") or "") or None
        return AIProviderResult(
            content=content,
            parsed_json=_parse_json_object(content),
            provider=self.name,
            model=self.model,
            finish_reason=finish_reason,
            usage=usage,
        )


def _convert_messages(messages: Sequence[Mapping[str, Any]], types: Any) -> tuple[list[Any], str]:
    system_parts: list[str] = []
    contents: list[Any] = []
    for item in messages:
        role = str(item.get("role") or "").strip().lower()
        text = str(item.get("content") or "")
        if not text:
            continue
        if role in {"system", "developer"}:
            system_parts.append(text)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        contents.append(
            types.Content(
                role=gemini_role,
                parts=[types.Part.from_text(text=text)],
            )
        )
    return contents, "\n\n".join(system_parts)


def _parse_json_object(texto: str) -> dict[str, Any] | None:
    try:
        valor = json.loads(texto)
    except json.JSONDecodeError:
        return None
    return dict(valor) if isinstance(valor, Mapping) else None


def _usage_dict(metadata: Any) -> dict[str, Any]:
    if metadata is None:
        return {}
    resultado: dict[str, Any] = {}
    for campo in ("prompt_token_count", "candidates_token_count", "total_token_count"):
        valor = getattr(metadata, campo, None)
        if valor is not None:
            resultado[campo] = valor
    return resultado


def _normalizar_erro_gemini(exc: Exception) -> AIProviderError:
    status = _status_code(exc)
    texto = re.sub(r"\s+", " ", str(exc or "")).strip()
    minusculo = texto.lower()
    if status in {401, 403} or any(token in minusculo for token in ("api key", "authentication", "unauthorized", "permission")):
        codigo, retryable = "authentication", False
    elif status == 404 or "model" in minusculo and "not found" in minusculo:
        codigo, retryable = "model_not_found", False
    elif status == 429 or any(token in minusculo for token in ("quota", "rate limit", "resource exhausted")):
        codigo, retryable = "rate_limit", True
    elif status == 408:
        codigo, retryable = "timeout", True
    elif status is not None and 500 <= status <= 599:
        codigo, retryable = "server_error", True
    elif "timeout" in minusculo or isinstance(exc, TimeoutError):
        codigo, retryable = "timeout", True
    elif any(token in minusculo for token in ("safety", "blocked", "block reason")):
        codigo, retryable = "safety_block", False
    elif any(token in minusculo for token in ("schema", "invalid argument", "invalid request")):
        codigo, retryable = "invalid_schema", False
    elif any(token in minusculo for token in ("connection", "connect", "network")):
        codigo, retryable = "network", True
    else:
        codigo, retryable = "provider_error", False
    return AIProviderError(
        texto[:500] or "erro no provider Gemini",
        error_code=codigo,
        retryable=retryable,
        status_code=status,
    )


def _status_code(exc: Exception) -> int | None:
    for campo in ("code", "status_code", "status"):
        valor = getattr(exc, campo, None)
        if isinstance(valor, int):
            return valor
    response = getattr(exc, "response", None)
    valor = getattr(response, "status_code", None)
    return valor if isinstance(valor, int) else None
