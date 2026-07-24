from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Final, TypedDict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from services import supabase


logger = logging.getLogger(__name__)

MODELO_GROQ_PADRAO: Final[str] = "llama-3.3-70b-versatile"
MODELOS_GPT_OSS: Final[set[str]] = {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}
TABELA_STATUS_IA: Final[str] = "ai_provider_status"
MENSAGEM_HANDOFF_IA: Final[str] = (
    "Estou com uma instabilidade no atendimento automatico. "
    "Vou encaminhar sua conversa para a equipe continuar por aqui."
)
INSTRUCAO_JSON_TOLERANTE: Final[str] = (
    "Retorne somente um objeto JSON valido. Nao use markdown, blocos de codigo, comentarios "
    "ou texto antes/depois do JSON.\n"
    "Use este contrato reduzido quando nao houver outros dados necessarios: "
    '{"resposta":"texto para o cliente","intencao":"tipo da intencao",'
    '"dados_confirmados":{},"dados_mencionados":{},"acao":"responder",'
    '"deve_avancar_estado":false}.'
)
SCHEMA_RESERVA_BOT_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "resposta",
        "intencao",
        "dados_confirmados",
        "dados_mencionados",
        "acao",
        "deve_avancar_estado",
    ],
    "properties": {
        "resposta": {"type": "string"},
        "intencao": {"type": "string"},
        "dados_confirmados": {
            "type": "object",
            "additionalProperties": False,
            "required": ["data", "horario", "quantidade", "nome"],
            "properties": {
                "data": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "horario": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "quantidade": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "nome": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
        },
        "dados_mencionados": {
            "type": "object",
            "additionalProperties": False,
            "required": ["data", "horario", "quantidade"],
            "properties": {
                "data": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "horario": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "quantidade": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            },
        },
        "acao": {"type": "string"},
        "deve_avancar_estado": {"type": "boolean"},
    },
}

_cooldown_lock = threading.RLock()
_cooldowns_memoria: dict[tuple[str, str], dict[str, Any]] = {}


class MensagemIA(TypedDict):
    role: str
    content: str


class ResultadoIA(TypedDict, total=False):
    ok: bool
    provider: str | None
    model: str | None
    conteudo: str | None
    usou_fallback: bool
    encaminhar_humano: bool
    erro_codigo: str
    erro: str


class CompletionExhaustedDuringReasoning(RuntimeError):
    pass


class ContentEmpty(RuntimeError):
    pass


class JsonRepairFailed(RuntimeError):
    pass


def tem_provedor_configurado() -> bool:
    return bool(
        os.getenv("GROQ_API_KEY", "").strip()
        or (
            os.getenv("AI_FALLBACK_PROVIDER", "").strip()
            and os.getenv("AI_FALLBACK_API_KEY", "").strip()
            and os.getenv("AI_FALLBACK_MODEL", "").strip()
        )
    )


def executar_ia_com_fallback(
    mensagens: Sequence[Mapping[str, str]],
    *,
    telefone: str | None = None,
    conversa_id: str | None = None,
    modelo_preferido: str | None = None,
    response_format_json: bool = False,
) -> ResultadoIA:
    candidatos = _candidatos_groq(modelo_preferido)
    erros: list[str] = []

    for indice, modelo in enumerate(candidatos):
        papel = "primary" if indice == 0 else "fallback"
        if _cooldown_ativo("groq", modelo):
            logger.warning(
                "ai_model_cooldown_active provider=groq model=%s papel=%s telefone=%s conversa_id=%s",
                modelo,
                papel,
                _mascarar_telefone(telefone),
                conversa_id or "",
            )
            erros.append(f"groq:{modelo}:cooldown")
            continue

        logger.info(
            "ai_%s_started provider=groq model=%s telefone=%s conversa_id=%s",
            papel,
            modelo,
            _mascarar_telefone(telefone),
            conversa_id or "",
        )
        try:
            conteudo = _executar_groq_modelo(
                mensagens,
                modelo=modelo,
                response_format_json=response_format_json,
            )
        except Exception as exc:
            codigo = _classificar_erro_ia(exc)
            erros.append(f"groq:{modelo}:{codigo}")
            if codigo == "rate_limit":
                segundos = _retry_after_segundos(exc)
                _registrar_cooldown(
                    "groq",
                    modelo,
                    segundos=segundos,
                    motivo="rate_limit",
                    mensagem=_sanitizar_erro(exc),
                )
                logger.warning(
                    "ai_%s_rate_limited provider=groq model=%s retry_after_segundos=%s telefone=%s conversa_id=%s",
                    papel,
                    modelo,
                    segundos,
                    _mascarar_telefone(telefone),
                    conversa_id or "",
                )
                continue
            if codigo in {
                "connection",
                "timeout",
                "server_error",
                "response_format",
                "bad_request",
                "json_validate_failed",
                "completion_exhausted_during_reasoning",
                "content_empty",
                "json_repair_failed",
            }:
                logger.warning(
                    "ai_%s_failed provider=groq model=%s erro_codigo=%s telefone=%s conversa_id=%s",
                    papel,
                    modelo,
                    codigo,
                    _mascarar_telefone(telefone),
                    conversa_id or "",
                )
                continue
            logger.exception(
                "ai_%s_failed_unexpected provider=groq model=%s telefone=%s conversa_id=%s",
                papel,
                modelo,
                _mascarar_telefone(telefone),
                conversa_id or "",
            )
            continue

        logger.info(
            "ai_%s_success provider=groq model=%s telefone=%s conversa_id=%s",
            papel,
            modelo,
            _mascarar_telefone(telefone),
            conversa_id or "",
        )
        return {
            "ok": True,
            "provider": "groq",
            "model": modelo,
            "conteudo": conteudo,
            "usou_fallback": indice > 0,
        }

    resultado_provider = _executar_provider_alternativo(
        mensagens,
        telefone=telefone,
        conversa_id=conversa_id,
        response_format_json=response_format_json,
    )
    if resultado_provider.get("ok"):
        return resultado_provider
    if resultado_provider.get("erro_codigo"):
        erros.append(str(resultado_provider["erro_codigo"]))

    logger.error(
        "ai_all_providers_failed telefone=%s conversa_id=%s erros=%s",
        _mascarar_telefone(telefone),
        conversa_id or "",
        ",".join(erros[-5:]),
    )
    logger.warning(
        "ai_human_handoff telefone=%s conversa_id=%s motivo=todos_provedores_indisponiveis",
        _mascarar_telefone(telefone),
        conversa_id or "",
    )
    return {
        "ok": False,
        "provider": None,
        "model": None,
        "conteudo": None,
        "usou_fallback": bool(candidatos) or bool(_provider_fallback_config()),
        "encaminhar_humano": True,
        "erro_codigo": "todos_provedores_indisponiveis",
    }


def limpar_cooldowns_memoria() -> None:
    with _cooldown_lock:
        _cooldowns_memoria.clear()


def cooldown_memoria(provider: str, model: str) -> dict[str, Any] | None:
    with _cooldown_lock:
        valor = _cooldowns_memoria.get((provider, model))
        return dict(valor) if valor else None


def _candidatos_groq(modelo_preferido: str | None) -> list[str]:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return []
    primary = (
        os.getenv("GROQ_PRIMARY_MODEL", "").strip()
        or (modelo_preferido or "").strip()
        or os.getenv("GROQ_MODEL", "").strip()
        or MODELO_GROQ_PADRAO
    )
    fallback = os.getenv("GROQ_FALLBACK_MODEL", "").strip()
    candidatos = [primary]
    if fallback and fallback not in candidatos:
        candidatos.append(fallback)
    return candidatos


def _executar_groq_modelo(
    mensagens: Sequence[Mapping[str, str]],
    *,
    modelo: str,
    response_format_json: bool,
) -> str:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY nao configurada.")

    client = _criar_cliente_groq(api_key)
    kwargs = _kwargs_chat_groq(
        mensagens,
        modelo=modelo,
        response_format_json=response_format_json,
        temperature=0.4,
    )

    try:
        return _executar_completion_groq_final(
            client,
            kwargs,
            modelo=modelo,
            retry_completion_exhausted=_modelo_gpt_oss(modelo),
        )
    except TypeError:
        if not response_format_json:
            raise
        logger.warning("Groq SDK nao aceitou response_format JSON. Repetindo sem response_format.")
        kwargs.pop("response_format", None)
        return _executar_completion_groq_final(
            client,
            kwargs,
            modelo=modelo,
            retry_completion_exhausted=_modelo_gpt_oss(modelo),
        )
    except Exception as exc:
        if response_format_json and _erro_json_validate_failed(exc):
            logger.warning(
                "ai_fallback_json_mode_failed provider=groq model=%s codigo=json_validate_failed erro=%s",
                modelo,
                _sanitizar_erro(exc),
            )
            return _executar_groq_sem_json_mode_tolerante(
                client,
                mensagens,
                modelo=modelo,
            )
        codigo = _classificar_erro_ia(exc)
        if codigo in {"connection", "timeout", "server_error"}:
            logger.warning(
                "ai_groq_retry_started provider=groq model=%s erro_codigo=%s",
                modelo,
                codigo,
            )
            return _executar_completion_groq_final(
                client,
                kwargs,
                modelo=modelo,
                retry_completion_exhausted=_modelo_gpt_oss(modelo),
            )
        else:
            raise


def _kwargs_chat_groq(
    mensagens: Sequence[Mapping[str, str]],
    *,
    modelo: str,
    response_format_json: bool,
    temperature: float,
    max_completion_tokens: int | None = None,
) -> dict[str, Any]:
    modelo_nome = modelo.strip() or MODELO_GROQ_PADRAO
    modelo_gpt_oss = _modelo_gpt_oss(modelo_nome)
    kwargs: dict[str, Any] = {
        "model": modelo_nome,
        "messages": [dict(mensagem) for mensagem in mensagens],
        "temperature": temperature,
    }
    if modelo_gpt_oss:
        kwargs["reasoning_effort"] = "low"
        kwargs["reasoning_format"] = "hidden"
        kwargs["max_completion_tokens"] = max(max_completion_tokens or 2048, 2048)
    else:
        kwargs["max_tokens"] = 500
    if response_format_json:
        kwargs["response_format"] = _response_format_groq(modelo_nome)
    return kwargs


def _modelo_gpt_oss(modelo: str) -> bool:
    return str(modelo or "").strip().lower() in MODELOS_GPT_OSS


def _response_format_groq(modelo: str) -> dict[str, Any]:
    if _modelo_gpt_oss(modelo):
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "reserva_bot_response",
                "strict": True,
                "schema": SCHEMA_RESERVA_BOT_RESPONSE,
            },
        }
    return {"type": "json_object"}


def _executar_completion_groq_final(
    client: Any,
    kwargs: Mapping[str, Any],
    *,
    modelo: str,
    retry_completion_exhausted: bool,
) -> str:
    kwargs_execucao = dict(kwargs)
    ja_repetiu_length = False
    while True:
        resposta = _criar_completion_groq(client, kwargs_execucao)
        conteudo, diagnostico = _extrair_conteudo_completion(resposta)
        _log_diagnostico_completion(modelo, kwargs_execucao, conteudo, diagnostico)
        if conteudo:
            return conteudo

        if _completion_exhausted_during_reasoning(modelo, diagnostico):
            if retry_completion_exhausted and not ja_repetiu_length and _max_completion_tokens_kwargs(kwargs_execucao) < 4096:
                ja_repetiu_length = True
                kwargs_execucao = {**kwargs_execucao, "max_completion_tokens": 4096}
                logger.warning(
                    "ai_groq_completion_exhausted_retry_started provider=groq model=%s codigo=completion_exhausted_during_reasoning max_completion_tokens=4096",
                    modelo,
                )
                continue
            logger.warning(
                "ai_groq_completion_exhausted_during_reasoning provider=groq model=%s finish_reason=%s reasoning_tokens=%s",
                modelo,
                diagnostico.get("finish_reason", ""),
                diagnostico.get("reasoning_tokens", ""),
            )
            raise CompletionExhaustedDuringReasoning("completion_exhausted_during_reasoning")

        logger.warning(
            "ai_groq_content_empty provider=groq model=%s finish_reason=%s has_reasoning=%s",
            modelo,
            diagnostico.get("finish_reason", ""),
            bool(diagnostico.get("has_reasoning")),
        )
        raise ContentEmpty("content_empty")


def _criar_completion_groq(client: Any, kwargs: Mapping[str, Any]) -> Any:
    return client.chat.completions.create(**dict(kwargs))


def _criar_cliente_groq(api_key: str) -> Any:
    from groq import Groq

    return Groq(api_key=api_key)


def _executar_groq_sem_json_mode_tolerante(
    client: Any,
    mensagens: Sequence[Mapping[str, str]],
    *,
    modelo: str,
) -> str:
    logger.info(
        "ai_fallback_plain_text_retry_started provider=groq model=%s",
        modelo,
    )
    kwargs = _kwargs_chat_groq(
        _mensagens_json_tolerante(mensagens),
        modelo=modelo,
        response_format_json=False,
        temperature=0.0,
    )
    conteudo = _executar_completion_groq_final(
        client,
        kwargs,
        modelo=modelo,
        retry_completion_exhausted=_modelo_gpt_oss(modelo),
    )
    payload = extrair_json_resposta(conteudo)
    if payload is None:
        logger.warning(
            "ai_fallback_json_repair_failed provider=groq model=%s",
            modelo,
        )
        raise JsonRepairFailed("json_repair_failed")
    if not _json_texto_completo_valido(conteudo):
        logger.info(
            "ai_fallback_json_repair_success provider=groq model=%s",
            modelo,
        )
    logger.info(
        "ai_fallback_plain_text_retry_success provider=groq model=%s",
        modelo,
    )
    return json.dumps(payload, ensure_ascii=False)


def _mensagens_json_tolerante(mensagens: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    mensagens_tolerantes = [dict(mensagem) for mensagem in mensagens]
    for indice in range(len(mensagens_tolerantes) - 1, -1, -1):
        if mensagens_tolerantes[indice].get("role") == "system":
            conteudo = str(mensagens_tolerantes[indice].get("content") or "").rstrip()
            mensagens_tolerantes[indice]["content"] = f"{conteudo}\n\n{INSTRUCAO_JSON_TOLERANTE}"
            return mensagens_tolerantes
    return [{"role": "system", "content": INSTRUCAO_JSON_TOLERANTE}, *mensagens_tolerantes]


def _extrair_conteudo_completion(resposta: Any) -> tuple[str, dict[str, Any]]:
    choice = _primeira_choice(resposta)
    message = _campo_objeto(choice, "message") if choice is not None else None
    usage = _campo_objeto(resposta, "usage")
    completion_tokens_details = _campo_objeto(usage, "completion_tokens_details")
    candidatos = [
        _campo_objeto(message, "content"),
        _campo_objeto(resposta, "output_text"),
        _campo_objeto(message, "output_text"),
        _campo_objeto(resposta, "text"),
        _campo_objeto(choice, "text") if choice is not None else None,
    ]
    conteudo = ""
    for candidato in candidatos:
        conteudo = _texto_candidato_completion(candidato)
        if conteudo:
            break
    diagnostico = {
        "finish_reason": _campo_objeto(choice, "finish_reason") if choice is not None else "",
        "completion_tokens": _campo_objeto(usage, "completion_tokens"),
        "reasoning_tokens": _campo_objeto(completion_tokens_details, "reasoning_tokens")
        or _campo_objeto(usage, "reasoning_tokens"),
        "has_message_content": bool(_texto_candidato_completion(_campo_objeto(message, "content"))),
        "has_output_text": bool(_texto_candidato_completion(_campo_objeto(resposta, "output_text")))
        or bool(_texto_candidato_completion(_campo_objeto(message, "output_text"))),
        "has_reasoning": bool(_texto_candidato_completion(_campo_objeto(message, "reasoning")))
        or bool(_texto_candidato_completion(_campo_objeto(resposta, "reasoning"))),
    }
    return conteudo, diagnostico


def _primeira_choice(resposta: Any) -> Any | None:
    choices = getattr(resposta, "choices", None)
    if isinstance(choices, Sequence) and choices:
        return choices[0]
    if isinstance(resposta, Mapping):
        choices_map = resposta.get("choices")
        if isinstance(choices_map, Sequence) and choices_map:
            return choices_map[0]
    return None


def _campo_objeto(objeto: Any, campo: str) -> Any:
    if isinstance(objeto, Mapping):
        return objeto.get(campo)
    return getattr(objeto, campo, None)


def _texto_candidato_completion(valor: Any) -> str:
    if isinstance(valor, str):
        return valor.strip()
    if isinstance(valor, Sequence) and not isinstance(valor, (str, bytes, bytearray)):
        partes: list[str] = []
        for item in valor:
            if isinstance(item, Mapping):
                texto = item.get("text") or item.get("content") or item.get("output_text")
                if isinstance(texto, str):
                    partes.append(texto)
            elif isinstance(item, str):
                partes.append(item)
        return "\n".join(parte.strip() for parte in partes if parte and parte.strip()).strip()
    if isinstance(valor, Mapping):
        texto = valor.get("text") or valor.get("content") or valor.get("output_text")
        return str(texto or "").strip()
    return ""


def _log_diagnostico_conteudo_retry(modelo: str, conteudo: str, diagnostico: Mapping[str, Any]) -> None:
    logger.info(
        "ai_fallback_plain_text_retry_diagnostic provider=groq model=%s tamanho=%s prefixo=%r finish_reason=%s "
        "tem_conteudo=%s primeiro_json=%s ultimo_json=%s has_message_content=%s has_output_text=%s has_reasoning=%s",
        modelo,
        len(conteudo),
        _sanitizar_texto_log(conteudo[:500]),
        diagnostico.get("finish_reason", ""),
        bool(conteudo),
        conteudo.find("{") if conteudo else -1,
        conteudo.rfind("}") if conteudo else -1,
        bool(diagnostico.get("has_message_content")),
        bool(diagnostico.get("has_output_text")),
        bool(diagnostico.get("has_reasoning")),
    )


def _log_diagnostico_completion(
    modelo: str,
    kwargs: Mapping[str, Any],
    conteudo: str,
    diagnostico: Mapping[str, Any],
) -> None:
    logger.info(
        "ai_groq_completion_diagnostic provider=groq model=%s reasoning_effort=%s reasoning_format=%s "
        "max_completion_tokens=%s response_format=%s finish_reason=%s completion_tokens=%s reasoning_tokens=%s content_length=%s",
        modelo,
        kwargs.get("reasoning_effort", ""),
        kwargs.get("reasoning_format", ""),
        kwargs.get("max_completion_tokens", kwargs.get("max_tokens", "")),
        _response_format_log(kwargs.get("response_format")),
        diagnostico.get("finish_reason", ""),
        diagnostico.get("completion_tokens", ""),
        diagnostico.get("reasoning_tokens", ""),
        len(conteudo),
    )


def _response_format_log(response_format: Any) -> str:
    if not response_format:
        return "none"
    if isinstance(response_format, Mapping):
        tipo = str(response_format.get("type") or "")
        if tipo == "json_schema":
            schema = response_format.get("json_schema")
            nome = schema.get("name") if isinstance(schema, Mapping) else ""
            return f"json_schema:{nome or 'unnamed'}"
        return tipo or "mapping"
    return str(type(response_format).__name__)


def _completion_exhausted_during_reasoning(modelo: str, diagnostico: Mapping[str, Any]) -> bool:
    finish_reason = str(diagnostico.get("finish_reason") or "").strip().lower()
    return finish_reason == "length" and (_modelo_gpt_oss(modelo) or bool(diagnostico.get("has_reasoning")))


def _max_completion_tokens_kwargs(kwargs: Mapping[str, Any]) -> int:
    valor = kwargs.get("max_completion_tokens") or kwargs.get("max_tokens") or 0
    try:
        return int(valor)
    except (TypeError, ValueError):
        return 0


def _executar_provider_alternativo(
    mensagens: Sequence[Mapping[str, str]],
    *,
    telefone: str | None,
    conversa_id: str | None,
    response_format_json: bool,
) -> ResultadoIA:
    config = _provider_fallback_config()
    if not config:
        return {"ok": False, "erro_codigo": "fallback_provider_nao_configurado"}

    provider = config["provider"]
    model = config["model"]
    if _cooldown_ativo(provider, model):
        logger.warning(
            "ai_model_cooldown_active provider=%s model=%s papel=provider_fallback telefone=%s conversa_id=%s",
            provider,
            model,
            _mascarar_telefone(telefone),
            conversa_id or "",
        )
        return {"ok": False, "erro_codigo": "fallback_provider_em_cooldown"}

    logger.info(
        "ai_fallback_started provider=%s model=%s telefone=%s conversa_id=%s",
        provider,
        model,
        _mascarar_telefone(telefone),
        conversa_id or "",
    )
    try:
        conteudo = _executar_fallback_provider(
            provider=provider,
            api_key=config["api_key"],
            model=model,
            mensagens=mensagens,
            response_format_json=response_format_json,
        )
    except Exception as exc:
        codigo = _classificar_erro_ia(exc)
        if codigo == "rate_limit":
            _registrar_cooldown(
                provider,
                model,
                segundos=_retry_after_segundos(exc),
                motivo="rate_limit",
                mensagem=_sanitizar_erro(exc),
            )
        logger.warning(
            "ai_fallback_failed provider=%s model=%s erro_codigo=%s telefone=%s conversa_id=%s",
            provider,
            model,
            codigo,
            _mascarar_telefone(telefone),
            conversa_id or "",
        )
        return {"ok": False, "erro_codigo": f"fallback_provider_{codigo}"}

    logger.info(
        "ai_fallback_success provider=%s model=%s telefone=%s conversa_id=%s",
        provider,
        model,
        _mascarar_telefone(telefone),
        conversa_id or "",
    )
    return {
        "ok": True,
        "provider": provider,
        "model": model,
        "conteudo": conteudo,
        "usou_fallback": True,
    }


def _provider_fallback_config() -> dict[str, str] | None:
    provider = os.getenv("AI_FALLBACK_PROVIDER", "").strip().lower()
    api_key = os.getenv("AI_FALLBACK_API_KEY", "").strip()
    model = os.getenv("AI_FALLBACK_MODEL", "").strip()
    if not provider or not api_key or not model:
        return None
    return {"provider": provider, "api_key": api_key, "model": model}


def _executar_fallback_provider(
    *,
    provider: str,
    api_key: str,
    model: str,
    mensagens: Sequence[Mapping[str, str]],
    response_format_json: bool,
) -> str:
    if provider == "openai":
        base_url = os.getenv("AI_FALLBACK_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    elif provider in {"openai_compatible", "compatible"}:
        base_url = os.getenv("AI_FALLBACK_BASE_URL", "").rstrip("/")
        if not base_url:
            raise RuntimeError("AI_FALLBACK_BASE_URL nao configurado para provedor compativel.")
    else:
        raise RuntimeError(f"AI_FALLBACK_PROVIDER nao suportado: {provider}")

    payload: dict[str, Any] = {
        "model": model,
        "messages": [dict(mensagem) for mensagem in mensagens],
        "temperature": 0.4,
        "max_tokens": 500,
    }
    if response_format_json:
        payload["response_format"] = {"type": "json_object"}

    request = Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            corpo = response.read().decode("utf-8")
    except HTTPError as exc:
        raise _ErroHTTPProvider(exc.code, _ler_erro_http(exc), dict(exc.headers)) from exc
    except URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc

    data = json.loads(corpo)
    conteudo = data["choices"][0]["message"]["content"]
    if not conteudo:
        raise RuntimeError("Provider fallback retornou resposta vazia.")
    return str(conteudo).strip()


class _ErroHTTPProvider(RuntimeError):
    def __init__(self, status_code: int, message: str, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.headers = dict(headers or {})


def _classificar_erro_ia(exc: Exception) -> str:
    nome = exc.__class__.__name__
    status = _status_code(exc)
    texto = str(exc).lower()
    if isinstance(exc, CompletionExhaustedDuringReasoning) or "completion_exhausted_during_reasoning" in texto:
        return "completion_exhausted_during_reasoning"
    if isinstance(exc, ContentEmpty) or "content_empty" in texto:
        return "content_empty"
    if isinstance(exc, JsonRepairFailed) or "json_repair_failed" in texto:
        return "json_repair_failed"
    if nome == "RateLimitError" or status == 429 or "rate_limit_exceeded" in texto:
        return "rate_limit"
    if _erro_json_validate_failed(exc):
        return "json_validate_failed"
    if nome == "BadRequestError" or status == 400:
        return "bad_request"
    if nome in {"APIConnectionError", "APIStatusError"} and status is None:
        return "connection"
    if nome == "APITimeoutError" or isinstance(exc, TimeoutError) or "timeout" in texto:
        return "timeout"
    if nome == "InternalServerError" or (status is not None and 500 <= status <= 599):
        return "server_error"
    if "response_format" in texto:
        return "response_format"
    if isinstance(exc, (ConnectionError, OSError)):
        return "connection"
    return "unknown"


def _erro_json_validate_failed(exc: Exception) -> bool:
    status = _status_code(exc)
    texto = str(exc).lower()
    return bool(
        status == 400
        and (
            "json_validate_failed" in texto
            or "failed to validate json" in texto
            or "validate json" in texto
        )
    )


def extrair_json_resposta(texto: str) -> dict[str, Any] | None:
    texto_limpo = str(texto or "").strip()
    if not texto_limpo:
        return None

    for candidato in _candidatos_json_texto(texto_limpo):
        payload = _loads_json_objeto(_normalizar_aspas_json(candidato))
        if payload is not None:
            normalizado = _normalizar_contrato_fallback(payload)
            if normalizado is not None:
                return normalizado

    reparado = _reparar_json_simples(texto_limpo)
    if reparado:
        payload = _loads_json_objeto(_normalizar_aspas_json(reparado))
        if payload is not None:
            return _normalizar_contrato_fallback(payload)
    if "{" not in texto_limpo and "}" not in texto_limpo:
        return _normalizar_contrato_fallback({"resposta": texto_limpo})
    return None


def _candidatos_json_texto(texto: str) -> list[str]:
    candidatos = [texto]
    sem_cerca = _remover_cerca_json(texto)
    if sem_cerca != texto:
        candidatos.append(sem_cerca)
    primeiro = texto.find("{")
    ultimo = texto.rfind("}")
    if primeiro != -1 and ultimo != -1 and ultimo > primeiro:
        candidatos.append(texto[primeiro : ultimo + 1])
    return candidatos


def _remover_cerca_json(texto: str) -> str:
    texto_limpo = texto.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", texto_limpo, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    texto_limpo = re.sub(r"^\s*```(?:json)?\s*", "", texto_limpo, flags=re.IGNORECASE)
    texto_limpo = re.sub(r"\s*```\s*$", "", texto_limpo)
    return texto_limpo.strip()


def _loads_json_objeto(texto: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(texto)
    except json.JSONDecodeError:
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _reparar_json_simples(texto: str) -> str:
    primeiro = texto.find("{")
    ultimo = texto.rfind("}")
    candidato = texto[primeiro : ultimo + 1] if primeiro != -1 and ultimo != -1 and ultimo > primeiro else texto
    candidato = _remover_cerca_json(candidato)
    candidato = _normalizar_aspas_json(candidato)
    candidato = re.sub(r",\s*([}\]])", r"\1", candidato)
    return candidato.strip()


def _normalizar_aspas_json(texto: str) -> str:
    return (
        str(texto or "")
        .replace("“", '"')
        .replace("”", '"')
        .replace("„", '"')
        .replace("‟", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )


def _normalizar_contrato_fallback(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    resposta = str(
        payload.get("resposta")
        or payload.get("resposta_natural")
        or payload.get("resposta_cliente")
        or payload.get("texto")
        or ""
    ).strip()
    if not resposta:
        return None
    normalizado = dict(payload)
    normalizado["resposta"] = resposta
    normalizado.setdefault("intencao", "continuar_conversa")
    normalizado.setdefault("acao", "responder")
    normalizado.setdefault("dados_confirmados", {})
    normalizado.setdefault("dados_mencionados", {})
    normalizado.setdefault("dados_incertos", {})
    normalizado.setdefault("correcoes", {})
    normalizado.setdefault("deve_avancar_estado", False)
    normalizado.setdefault("campo_sugerido", None)
    normalizado.setdefault("confianca", 0.7)
    return normalizado


def _json_texto_completo_valido(texto: str) -> bool:
    payload = _loads_json_objeto(str(texto or "").strip())
    return payload is not None and _normalizar_contrato_fallback(payload) is not None


def _status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status"):
        valor = getattr(exc, attr, None)
        if isinstance(valor, int):
            return valor
    response = getattr(exc, "response", None)
    valor = getattr(response, "status_code", None)
    return valor if isinstance(valor, int) else None


def _retry_after_segundos(exc: Exception) -> int:
    headers = _headers_erro(exc)
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after:
        segundos = _parse_retry_after_header(str(retry_after))
        if segundos is not None:
            return max(segundos, 1)
    segundos_mensagem = parse_tempo_retry_after(str(exc))
    if segundos_mensagem is not None:
        return max(segundos_mensagem, 1)
    return 300


def parse_tempo_retry_after(texto: str) -> int | None:
    match = re.search(r"try again in\s+((?:(?:\d+(?:\.\d+)?)\s*[hms]\s*)+)", texto, flags=re.IGNORECASE)
    if not match:
        return None
    total = 0.0
    for valor, unidade in re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", match.group(1), flags=re.IGNORECASE):
        numero = float(valor)
        unidade = unidade.lower()
        if unidade == "h":
            total += numero * 3600
        elif unidade == "m":
            total += numero * 60
        else:
            total += numero
    return int(total + 0.999) if total > 0 else None


def _parse_retry_after_header(valor: str) -> int | None:
    valor = valor.strip()
    if not valor:
        return None
    if valor.isdigit():
        return int(valor)
    try:
        data = parsedate_to_datetime(valor)
    except (TypeError, ValueError):
        return None
    if data.tzinfo is None:
        data = data.replace(tzinfo=timezone.utc)
    return max(int((data - _agora()).total_seconds() + 0.999), 0)


def _headers_erro(exc: Exception) -> dict[str, str]:
    candidatos = [
        getattr(exc, "headers", None),
        getattr(getattr(exc, "response", None), "headers", None),
    ]
    for headers in candidatos:
        if isinstance(headers, Mapping):
            return {str(k): str(v) for k, v in headers.items()}
    return {}


def _registrar_cooldown(
    provider: str,
    model: str,
    *,
    segundos: int,
    motivo: str,
    mensagem: str,
) -> None:
    ate = _agora() + timedelta(seconds=max(segundos, 1))
    payload = {
        "provider": provider,
        "model": model,
        "indisponivel_ate": ate.isoformat(),
        "motivo": motivo,
        "metadata": {"ultima_mensagem": mensagem[:500]},
        "updated_at": _agora().isoformat(),
    }
    resultado = supabase.upsert(TABELA_STATUS_IA, payload, on_conflict="provider,model", retornar=False)
    if not resultado.get("ok"):
        logger.warning(
            "Falha ao salvar cooldown de IA no Supabase; usando memoria. provider=%s model=%s status=%s erro=%s",
            provider,
            model,
            resultado.get("status", ""),
            resultado.get("erro", ""),
        )
        _registrar_cooldown_memoria(provider, model, payload)
        return
    _registrar_cooldown_memoria(provider, model, payload)


def _cooldown_ativo(provider: str, model: str) -> bool:
    registro = _buscar_cooldown_supabase(provider, model) or _buscar_cooldown_memoria(provider, model)
    if not registro:
        return False
    ate = _parse_iso(str(registro.get("indisponivel_ate") or ""))
    if ate is None or ate <= _agora():
        return False
    return True


def _buscar_cooldown_supabase(provider: str, model: str) -> Mapping[str, Any] | None:
    resultado = supabase.selecionar(
        TABELA_STATUS_IA,
        filtros={"provider": f"eq.{provider}", "model": f"eq.{model}"},
        limite=1,
    )
    if not resultado.get("ok"):
        return None
    data = resultado.get("data")
    if isinstance(data, list) and data and isinstance(data[0], Mapping):
        return data[0]
    return None


def _registrar_cooldown_memoria(provider: str, model: str, payload: Mapping[str, Any]) -> None:
    with _cooldown_lock:
        _cooldowns_memoria[(provider, model)] = dict(payload)


def _buscar_cooldown_memoria(provider: str, model: str) -> Mapping[str, Any] | None:
    with _cooldown_lock:
        registro = _cooldowns_memoria.get((provider, model))
        return dict(registro) if registro else None


def _parse_iso(valor: str) -> datetime | None:
    if not valor:
        return None
    try:
        data = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None
    if data.tzinfo is None:
        data = data.replace(tzinfo=timezone.utc)
    return data


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _sanitizar_erro(exc: Exception) -> str:
    texto = re.sub(r"\s+", " ", str(exc or "")).strip()
    texto = re.sub(r"(api[-_ ]?key|token|bearer)\s*[:=]\s*\S+", r"\1=<redacted>", texto, flags=re.IGNORECASE)
    return texto[:500]


def _sanitizar_texto_log(texto: str) -> str:
    texto_limpo = re.sub(r"\s+", " ", str(texto or "")).strip()
    texto_limpo = re.sub(
        r"(api[-_ ]?key|token|bearer)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        texto_limpo,
        flags=re.IGNORECASE,
    )
    return texto_limpo[:500]


def _mascarar_telefone(telefone: str | None) -> str:
    valor = re.sub(r"\D", "", telefone or "")
    if len(valor) <= 4:
        return valor
    return f"{valor[:4]}***{valor[-4:]}"


def _ler_erro_http(exc: HTTPError) -> str:
    try:
        corpo = exc.read().decode("utf-8")
    except Exception:
        corpo = str(exc)
    return _sanitizar_erro(RuntimeError(corpo))
