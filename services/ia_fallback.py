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
TABELA_STATUS_IA: Final[str] = "ai_provider_status"
MENSAGEM_HANDOFF_IA: Final[str] = (
    "Estou com uma instabilidade no atendimento automatico. "
    "Vou encaminhar sua conversa para a equipe continuar por aqui."
)

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
            if codigo in {"connection", "timeout", "server_error", "response_format"}:
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

    from groq import Groq

    client = Groq(api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": modelo.strip() or MODELO_GROQ_PADRAO,
        "messages": [dict(mensagem) for mensagem in mensagens],
        "temperature": 0.4,
        "max_tokens": 500,
    }
    if response_format_json:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        resposta = client.chat.completions.create(**kwargs)
    except TypeError:
        if not response_format_json:
            raise
        logger.warning("Groq SDK nao aceitou response_format JSON. Repetindo sem response_format.")
        kwargs.pop("response_format", None)
        resposta = client.chat.completions.create(**kwargs)
    except Exception as exc:
        if not response_format_json or "response_format" not in str(exc).lower():
            raise
        logger.warning("Groq rejeitou response_format JSON. Repetindo sem response_format: %s", _sanitizar_erro(exc))
        kwargs.pop("response_format", None)
        resposta = client.chat.completions.create(**kwargs)

    conteudo = resposta.choices[0].message.content
    if not conteudo:
        raise RuntimeError("Groq retornou resposta vazia.")
    return str(conteudo).strip()


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
    if nome == "RateLimitError" or status == 429 or "rate_limit_exceeded" in texto:
        return "rate_limit"
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
