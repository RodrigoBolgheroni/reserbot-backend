from __future__ import annotations

import logging
import json
import os
import re
import threading
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final, Literal, TypedDict

from services import config_restaurante


logger = logging.getLogger(__name__)

FLAG_RESERVA_CONFIRMADA: Final[str] = "RESERVA_CONFIRMADA"
MENSAGEM_RECUSA_RESERVA: Final[str] = "Tudo bem! 😊 Se quiser fazer uma reserva depois, é só chamar por aqui."
MODELO_PADRAO: Final[str] = "llama-3.3-70b-versatile"
MAX_HISTORICO_MENSAGENS: Final[int] = 12

MESES: Final[dict[str, int]] = {
    "janeiro": 1,
    "jan": 1,
    "fevereiro": 2,
    "fev": 2,
    "marco": 3,
    "março": 3,
    "mar": 3,
    "abril": 4,
    "abr": 4,
    "maio": 5,
    "mai": 5,
    "junho": 6,
    "jun": 6,
    "julho": 7,
    "jul": 7,
    "agosto": 8,
    "ago": 8,
    "setembro": 9,
    "set": 9,
    "outubro": 10,
    "out": 10,
    "novembro": 11,
    "nov": 11,
    "dezembro": 12,
    "dez": 12,
}

NUMEROS_POR_EXTENSO: Final[dict[str, int]] = {
    "um": 1,
    "uma": 1,
    "dois": 2,
    "duas": 2,
    "tres": 3,
    "três": 3,
    "quatro": 4,
    "cinco": 5,
    "seis": 6,
    "sete": 7,
    "oito": 8,
    "nove": 9,
    "dez": 10,
}

Role = Literal["system", "user", "assistant"]


class Mensagem(TypedDict):
    role: Role
    content: str


class DadosReserva(TypedDict, total=False):
    data_reserva: str
    horario: str
    pessoas: int
    nome_cliente: str
    observacoes: str


class RespostaAgente(TypedDict):
    texto: str
    reserva_confirmada: bool
    dados_reserva: DadosReserva
    status_reserva: str
    confianca: float


class ResultadoReservaEstruturada(TypedDict):
    texto: str
    reserva_confirmada: bool
    dados_reserva: DadosReserva
    status_reserva: str
    confianca: float
    intencao: str
    acao: str
    proximo_campo: str
    correcoes: dict[str, Any]


class EstadoReserva(TypedDict, total=False):
    data_reserva: str
    data_reserva_original: str
    horario: str
    horario_invalido: str
    pessoas: int
    nome_cliente: str
    observacoes: str
    campo_pendente: str
    etapa: str
    pedido_imediato: bool
    tentativas_campos: dict[str, int]
    aguardando_confirmacao: bool
    conversa_id: str
    ultimo_texto_bot: str
    ultimo_campo_perguntado: str
    ultima_intencao: str
    validacoes: list[dict[str, Any]]
    updated_at: str
    versao: int


_historicos: dict[str, list[Mensagem]] = {}
_estados_reserva: dict[str, EstadoReserva] = {}
_locks_conversa: dict[str, threading.RLock] = {}
_locks_conversa_guard = threading.Lock()

ETAPA_POR_CAMPO: Final[dict[str, str]] = {
    "data_reserva": "aguardando_data",
    "horario": "aguardando_horario",
    "pessoas": "aguardando_quantidade",
    "nome_cliente": "aguardando_nome",
    "telefone": "aguardando_telefone",
    "confirmacao": "aguardando_confirmacao",
}

CAMPO_POR_ETAPA: Final[dict[str, str]] = {etapa: campo for campo, etapa in ETAPA_POR_CAMPO.items()}
INTENCOES_CONVERSACIONAIS_IA: Final[set[str]] = {
    "comentario",
    "brincadeira",
    "indecisao",
    "saudacao",
    "pergunta_bot",
    "pergunta_fonte",
    "pergunta_restaurante",
    "pergunta_disponibilidade",
    "duvida",
}
MAPA_CAMPO_IA: Final[dict[str, str]] = {
    "data": "data_reserva",
    "dia": "data_reserva",
    "data_reserva": "data_reserva",
    "horario": "horario",
    "hora": "horario",
    "quantidade": "pessoas",
    "quantidade_pessoas": "pessoas",
    "pessoas": "pessoas",
    "nome": "nome_cliente",
    "nome_cliente": "nome_cliente",
    "confirmacao": "confirmacao",
}
CHAVES_ESTADO_PERSISTIDO: Final[set[str]] = {
    "data_reserva",
    "data_reserva_original",
    "horario",
    "horario_invalido",
    "pessoas",
    "nome_cliente",
    "observacoes",
    "campo_pendente",
    "etapa",
    "pedido_imediato",
    "tentativas_campos",
    "aguardando_confirmacao",
    "conversa_id",
    "ultimo_texto_bot",
    "ultimo_campo_perguntado",
    "ultima_intencao",
    "validacoes",
    "updated_at",
    "versao",
}


def processar_mensagem(
    telefone: str,
    mensagem_cliente: str,
    nome_cliente: str = "",
    perfil_cliente: Mapping[str, Any] | None = None,
) -> RespostaAgente:
    telefone_limpo = telefone.strip()
    mensagem_limpa = mensagem_cliente.strip()
    if not telefone_limpo or not mensagem_limpa:
        return {
            "texto": "",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "sem_mensagem",
            "confianca": 0.0,
        }

    with _lock_conversa(telefone_limpo):
        return _processar_mensagem_sem_lock(
            telefone=telefone_limpo,
            mensagem_cliente=mensagem_limpa,
            nome_cliente=nome_cliente,
            perfil_cliente=perfil_cliente,
        )


def _processar_mensagem_sem_lock(
    telefone: str,
    mensagem_cliente: str,
    nome_cliente: str = "",
    perfil_cliente: Mapping[str, Any] | None = None,
) -> RespostaAgente:
    telefone_limpo = telefone.strip()
    mensagem_limpa = mensagem_cliente.strip()

    if not telefone_limpo or not mensagem_limpa:
        return {
            "texto": "",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "sem_mensagem",
            "confianca": 0.0,
        }

    if _eh_recusa_reserva(mensagem_limpa):
        logger.info("Cliente recusou convite de reserva. telefone=%s", telefone_limpo)
        limpar_historico(telefone_limpo)
        return {
            "texto": MENSAGEM_RECUSA_RESERVA,
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "sem_interesse",
            "confianca": 1.0,
        }

    historico = _historicos.setdefault(telefone_limpo, [])
    historico.append({"role": "user", "content": mensagem_limpa})
    _limitar_historico(historico)

    if not os.getenv("GROQ_API_KEY", "").strip():
        logger.warning("GROQ_API_KEY nao configurada. Usando resposta de contingencia.")
        texto_modelo = _resposta_contingencia()
    else:
        try:
            texto_modelo = _chamar_groq(
                mensagens=[
                    _mensagem_sistema(nome_cliente, perfil_cliente=perfil_cliente),
                    _mensagem_contexto_reserva(telefone_limpo),
                    *historico,
                ],
                modelo=os.getenv("GROQ_MODEL", MODELO_PADRAO),
            )
        except Exception:
            logger.exception("Falha ao processar mensagem com Groq.")
            texto_modelo = _resposta_contingencia()

    interpretacao = interpretar_resposta_modelo(
        texto_modelo=texto_modelo,
        mensagem_cliente=mensagem_limpa,
    )
    resposta_segura = aplicar_guardrails_reserva(
        telefone=telefone_limpo,
        mensagem_cliente=mensagem_limpa,
        interpretacao=interpretacao,
        nome_cliente=nome_cliente,
    )
    reserva_confirmada = resposta_segura["reserva_confirmada"]
    texto_cliente = resposta_segura["texto"]

    if texto_cliente:
        historico.append({"role": "assistant", "content": texto_cliente})
        _limitar_historico(historico)
        _registrar_ultima_resposta(telefone_limpo, texto_cliente)

    if reserva_confirmada:
        limpar_historico(telefone_limpo)

    return {
        "texto": texto_cliente,
        "reserva_confirmada": reserva_confirmada,
        "dados_reserva": resposta_segura["dados_reserva"],
        "status_reserva": resposta_segura["status_reserva"],
        "confianca": resposta_segura["confianca"],
    }


def _lock_conversa(telefone: str) -> threading.RLock:
    with _locks_conversa_guard:
        lock = _locks_conversa.get(telefone)
        if lock is None:
            lock = threading.RLock()
            _locks_conversa[telefone] = lock
        return lock


def limpar_historico(telefone: str) -> None:
    telefone_limpo = telefone.strip()
    _historicos.pop(telefone_limpo, None)
    _estados_reserva.pop(telefone_limpo, None)


def definir_estado_reserva(telefone: str, estado: Mapping[str, Any] | None) -> None:
    telefone_limpo = telefone.strip()
    if not telefone_limpo:
        return

    with _lock_conversa(telefone_limpo):
        estado_normalizado = _normalizar_estado_reserva_persistido(estado)
        if estado_normalizado:
            _estados_reserva[telefone_limpo] = estado_normalizado
        else:
            _estados_reserva.pop(telefone_limpo, None)


def obter_estado_reserva(telefone: str) -> dict[str, Any]:
    telefone_limpo = telefone.strip()
    if not telefone_limpo:
        return {}

    with _lock_conversa(telefone_limpo):
        estado = _estados_reserva.get(telefone_limpo)
        if not estado:
            return {}
        estado["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        estado["versao"] = 2
        return _serializar_estado_reserva(estado)


def _normalizar_estado_reserva_persistido(estado: Mapping[str, Any] | None) -> EstadoReserva:
    if not isinstance(estado, Mapping):
        return {}

    normalizado: EstadoReserva = {}
    for chave in CHAVES_ESTADO_PERSISTIDO:
        valor = estado.get(chave)
        if valor in (None, "", [], {}):
            continue
        if chave == "pessoas":
            pessoas = _pessoas_json(valor)
            if pessoas is not None:
                normalizado["pessoas"] = pessoas
            continue
        if chave == "tentativas_campos" and isinstance(valor, Mapping):
            normalizado["tentativas_campos"] = {
                str(campo): int(total)
                for campo, total in valor.items()
                if str(campo) in {"data_reserva", "horario", "pessoas", "nome_cliente", "telefone"}
                and str(total).isdigit()
            }
            continue
        if chave == "validacoes" and isinstance(valor, list):
            normalizado["validacoes"] = [dict(item) for item in valor if isinstance(item, Mapping)][-10:]
            continue
        if chave in {"pedido_imediato", "aguardando_confirmacao"}:
            normalizado[chave] = bool(valor)  # type: ignore[literal-required]
            continue
        if chave == "versao":
            try:
                normalizado["versao"] = int(valor)
            except (TypeError, ValueError):
                normalizado["versao"] = 2
            continue
        normalizado[chave] = str(valor).strip()  # type: ignore[literal-required]

    campo = _campo_aguardado(normalizado)
    if campo:
        _definir_campo_pendente(normalizado, campo)
    return normalizado


def _serializar_estado_reserva(estado: Mapping[str, Any]) -> dict[str, Any]:
    serializado: dict[str, Any] = {}
    for chave in CHAVES_ESTADO_PERSISTIDO:
        valor = estado.get(chave)
        if valor in (None, "", [], {}):
            continue
        if isinstance(valor, Mapping):
            serializado[chave] = dict(valor)
        elif isinstance(valor, list):
            serializado[chave] = [dict(item) if isinstance(item, Mapping) else item for item in valor][-10:]
        else:
            serializado[chave] = valor
    return serializado


def _mensagem_contexto_reserva(telefone: str) -> Mensagem:
    estado = _estados_reserva.get(telefone, {})
    dados = _dados_reserva_do_estado(estado)
    campo = _campo_retomada_apos_informacao(estado, telefone) if estado else "data_reserva"
    payload = {
        "dados_reserva_ja_coletados": dados,
        "campo_que_ainda_falta": campo,
        "etapa_interna": estado.get("etapa", ""),
    }
    return {
        "role": "system",
        "content": (
            "Contexto interno da conversa atual. Use isto para responder de forma natural, "
            "sem repetir perguntas ja feitas e sem perder dados coletados: "
            f"{json.dumps(payload, ensure_ascii=False)}. "
            "Antes de pedir o campo faltante, responda ao significado da mensagem do cliente. "
            "Se o cliente fizer uma pergunta ou comentario, isso nao e erro."
        ),
    }


def _registrar_ultima_resposta(telefone: str, texto: str) -> None:
    estado = _estados_reserva.get(telefone)
    if estado is None:
        return
    estado["ultimo_texto_bot"] = texto.strip()
    campo = _campo_aguardado(estado)
    if campo:
        estado["ultimo_campo_perguntado"] = campo


def aplicar_guardrails_reserva(
    *,
    telefone: str,
    mensagem_cliente: str,
    interpretacao: ResultadoReservaEstruturada,
    nome_cliente: str = "",
) -> RespostaAgente:
    telefone_limpo = telefone.strip()
    estado = _estados_reserva.setdefault(telefone_limpo, {})
    aguardava_confirmacao = bool(estado.get("aguardando_confirmacao"))
    confirmacao_cliente = _eh_confirmacao_cliente(mensagem_cliente)
    status_modelo = str(interpretacao.get("status_reserva") or "em_coleta").strip().lower()
    intencao_modelo = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
    if intencao_modelo:
        estado["ultima_intencao"] = intencao_modelo

    if _nome_parece_valido(nome_cliente):
        estado["nome_cliente"] = nome_cliente.strip()

    intencao_conversacional = _intencao_conversacional(mensagem_cliente)
    categoria_pergunta_restaurante = _categoria_pergunta_restaurante(mensagem_cliente)
    if not categoria_pergunta_restaurante and intencao_modelo == "pergunta_restaurante":
        categoria_pergunta_restaurante = "geral"
    if (
        not intencao_conversacional
        and intencao_modelo in INTENCOES_CONVERSACIONAIS_IA
        and not _interpretacao_tem_dados_reserva(interpretacao)
    ):
        intencao_conversacional = intencao_modelo

    if _eh_recusa_reserva(mensagem_cliente):
        _estados_reserva.pop(telefone_limpo, None)
        return {
            "texto": MENSAGEM_RECUSA_RESERVA,
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "sem_interesse",
            "confianca": 1.0,
        }

    if (status_modelo == "cancelada" or _eh_cancelamento_cliente(mensagem_cliente)) and not categoria_pergunta_restaurante:
        _estados_reserva.pop(telefone_limpo, None)
        return {
            "texto": interpretacao["texto"] or "Tudo bem, nao vou seguir com essa reserva.",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "cancelada",
            "confianca": interpretacao["confianca"],
        }

    if intencao_conversacional:
        return _responder_intencao_conversacional(
            intencao=intencao_conversacional,
            telefone=telefone_limpo,
            estado=estado,
            interpretacao=interpretacao,
        )

    if categoria_pergunta_restaurante:
        return _responder_pergunta_restaurante(
            categoria=categoria_pergunta_restaurante,
            telefone=telefone_limpo,
            estado=estado,
            confianca=max(interpretacao["confianca"], 0.8),
        )

    if _eh_pedido_imediato(mensagem_cliente):
        return _resposta_pedido_imediato(
            estado,
            mensagem_cliente=mensagem_cliente,
            confianca=max(interpretacao["confianca"], 0.9),
        )

    if _pergunta_sobre_horario_indisponivel(mensagem_cliente, estado) and _mensagem_pede_explicacao_horario(mensagem_cliente):
        estado["aguardando_confirmacao"] = False
        _definir_campo_pendente(estado, "horario")
        return {
            "texto": _mensagem_horario_fora_funcionamento(),
            "reserva_confirmada": False,
            "dados_reserva": _dados_reserva_do_estado(estado),
            "status_reserva": "em_coleta",
            "confianca": max(interpretacao["confianca"], 0.8),
        }

    if _pergunta_data_disponivel(mensagem_cliente):
        estado["aguardando_confirmacao"] = False
        _definir_campo_pendente(estado, "data_reserva")
        return {
            "texto": "Você tem alguma data em mente? Me fala o dia e eu verifico para você.",
            "reserva_confirmada": False,
            "dados_reserva": _dados_reserva_do_estado(estado),
            "status_reserva": "em_coleta",
            "confianca": interpretacao["confianca"],
        }

    if _deve_respeitar_nao_aplicavel(estado, mensagem_cliente, status_modelo):
        return {
            "texto": interpretacao["texto"],
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": status_modelo,
            "confianca": interpretacao["confianca"],
        }

    invalidos: set[str] = set()
    if not (confirmacao_cliente and aguardava_confirmacao):
        estado["aguardando_confirmacao"] = False
        _atualizar_estado_reserva(
            estado,
            mensagem_cliente=mensagem_cliente,
            dados_modelo=interpretacao["dados_reserva"],
            invalidos=invalidos,
            interpretacao=interpretacao,
        )

    _remover_campos_invalidos_estado(estado, invalidos)
    if _campo_aguardado(estado) == "nome_cliente" and not confirmacao_cliente:
        nome_informado = _extrair_nome_cliente(mensagem_cliente)
        if nome_informado:
            estado["nome_cliente"] = nome_informado

    faltantes = _campos_obrigatorios_faltantes(estado, telefone_limpo)
    dados_estado = _dados_reserva_do_estado(estado)

    if confirmacao_cliente and aguardava_confirmacao and not faltantes:
        if not dados_reserva_obrigatorios_ok(dados_estado, nome_cliente=nome_cliente, telefone=telefone_limpo):
            logger.warning(
                "Confirmacao final bloqueada por validacao obrigatoria: data=%s horario=%s pessoas=%s nome=%s telefone=%s.",
                dados_estado.get("data_reserva", ""),
                dados_estado.get("horario", ""),
                dados_estado.get("pessoas", ""),
                bool(nome_cliente or dados_estado.get("nome_cliente")),
                bool(telefone_limpo),
            )
            campo = _primeiro_campo_pendente([], estado, telefone_limpo)
            _definir_campo_pendente(estado, campo)
            return {
                "texto": _mensagem_pedir_campo(campo, estado),
                "reserva_confirmada": False,
                "dados_reserva": dados_estado,
                "status_reserva": "em_coleta",
                "confianca": interpretacao["confianca"],
            }
        logger.info(
            "Confirmacao final validada: data=%s horario=%s pessoas=%s telefone=%s.",
            dados_estado.get("data_reserva", ""),
            dados_estado.get("horario", ""),
            dados_estado.get("pessoas", ""),
            telefone_limpo,
        )
        return {
            "texto": _mensagem_reserva_confirmada(dados_estado),
            "reserva_confirmada": True,
            "dados_reserva": dados_estado,
            "status_reserva": "confirmada",
            "confianca": max(interpretacao["confianca"], 0.9),
        }

    if invalidos:
        campo = _primeiro_campo_pendente(list(invalidos), estado, telefone_limpo)
        _definir_campo_pendente(estado, campo)
        texto = _mensagem_validacao_falhou(campo, estado, telefone_limpo, interpretacao)
        status_reserva = "aguardando_humano" if _deve_encaminhar_humano_por_tentativas(estado, campo) else "em_coleta"
        return {
            "texto": texto,
            "reserva_confirmada": False,
            "dados_reserva": dados_estado,
            "status_reserva": status_reserva,
            "confianca": interpretacao["confianca"],
        }

    if faltantes:
        campo = faltantes[0]
        _definir_campo_pendente(estado, campo)
        texto = _texto_modelo_para_pedir_campo(interpretacao["texto"], campo, estado, mensagem_cliente)
        if not texto:
            texto = _mensagem_pedir_campo(campo, estado)
        status_reserva = "aguardando_humano" if _deve_encaminhar_humano_por_tentativas(estado, campo) else "em_coleta"
        return {
            "texto": texto,
            "reserva_confirmada": False,
            "dados_reserva": dados_estado,
            "status_reserva": status_reserva,
            "confianca": interpretacao["confianca"],
        }

    estado["aguardando_confirmacao"] = True
    _definir_campo_pendente(estado, "confirmacao")
    dados_estado = _dados_reserva_do_estado(estado)
    return {
        "texto": _mensagem_confirmacao_previa_segura(dados_estado),
        "reserva_confirmada": False,
        "dados_reserva": dados_estado,
        "status_reserva": "aguardando_confirmacao",
        "confianca": max(interpretacao["confianca"], 0.8),
    }


def dados_reserva_obrigatorios_ok(
    dados: Mapping[str, Any],
    *,
    nome_cliente: str = "",
    telefone: str = "",
) -> bool:
    nome = str(dados.get("nome_cliente") or nome_cliente or "").strip()
    telefone_limpo = str(telefone or "").strip()
    pessoas = _pessoas_json(dados.get("pessoas"))
    return bool(
        _data_reserva_valida(str(dados.get("data_reserva") or ""))
        and _horario_reserva_valido(str(dados.get("horario") or ""))
        and pessoas is not None
        and nome
        and telefone_limpo
    )


def contem_flag_reserva(texto: str) -> bool:
    return FLAG_RESERVA_CONFIRMADA in texto


def remover_flag_reserva(texto: str) -> str:
    texto_sem_flag = texto.replace(FLAG_RESERVA_CONFIRMADA, "")
    return re.sub(r"\s+", " ", texto_sem_flag).strip()


def extrair_dados_reserva(
    mensagem_cliente: str,
    resposta_agente: str,
) -> DadosReserva:
    return _extrair_dados_reserva_contextual(
        mensagem_cliente,
        resposta_agente,
        campo_aguardado="",
    )


def _extrair_dados_reserva_contextual(
    mensagem_cliente: str,
    resposta_agente: str,
    *,
    campo_aguardado: str,
) -> DadosReserva:
    texto = f"{mensagem_cliente}\n{resposta_agente}"
    dados: DadosReserva = {}

    if campo_aguardado == "data_reserva":
        data_reserva = _extrair_data(texto, permitir_dia_isolado=True)
        if data_reserva:
            dados["data_reserva"] = data_reserva
    elif campo_aguardado == "horario":
        horario = _extrair_horario(texto, permitir_numero_isolado=True)
        if horario:
            dados["horario"] = horario
    elif campo_aguardado == "pessoas":
        pessoas = _extrair_pessoas_solicitadas(texto, permitir_numero_isolado=True)
        if pessoas is not None and 1 <= pessoas <= _limite_pessoas_reserva():
            dados["pessoas"] = pessoas
    else:
        data_reserva = _extrair_data(texto)
        if data_reserva:
            dados["data_reserva"] = data_reserva

        horario = _extrair_horario(texto)
        if horario:
            dados["horario"] = horario

        pessoas = _extrair_pessoas(texto)
        if pessoas:
            dados["pessoas"] = pessoas

    observacoes = remover_flag_reserva(resposta_agente)
    if observacoes:
        dados["observacoes"] = observacoes

    return dados


def interpretar_resposta_modelo(
    *,
    texto_modelo: str,
    mensagem_cliente: str = "",
) -> ResultadoReservaEstruturada:
    payload = _extrair_json_resposta(texto_modelo)
    if payload is None:
        texto_cliente = remover_flag_reserva(texto_modelo)
        return {
            "texto": texto_cliente,
            "reserva_confirmada": contem_flag_reserva(texto_modelo),
            "dados_reserva": extrair_dados_reserva(mensagem_cliente, texto_cliente),
            "status_reserva": "confirmada" if contem_flag_reserva(texto_modelo) else "em_coleta",
            "confianca": 0.5 if contem_flag_reserva(texto_modelo) else 0.0,
            "intencao": "",
            "acao": "",
            "proximo_campo": "",
            "correcoes": {},
        }

    reserva = payload.get("reserva")
    reserva_dict = reserva if isinstance(reserva, dict) else {}
    acao = str(payload.get("acao") or "").strip().lower()
    status = str(reserva_dict.get("status") or payload.get("status_reserva") or "em_coleta").strip().lower()
    if acao == "confirmar_reserva":
        status = "confirmada"
    elif acao == "cancelar":
        status = "cancelada"
    elif acao == "encaminhar_humano":
        status = "aguardando_humano"
    if acao in {"continuar_conversa", "continuar_reserva"} and status == "nao_aplicavel":
        status = "em_coleta"
    dados_reserva = _dados_reserva_de_json(reserva_dict)
    dados_extraidos = payload.get("dados_extraidos")
    if isinstance(dados_extraidos, dict):
        dados_reserva.update(_dados_extraidos_de_json(dados_extraidos))
    texto_cliente = str(
        payload.get("resposta_natural")
        or payload.get("resposta_cliente")
        or payload.get("resposta")
        or payload.get("texto")
        or ""
    ).strip()
    if not texto_cliente:
        texto_cliente = _resposta_contingencia()

    reserva_confirmada = status == "confirmada" and _dados_reserva_minimos(dados_reserva)
    if not reserva_confirmada and bool(reserva_dict.get("confirmada")):
        reserva_confirmada = _dados_reserva_minimos(dados_reserva)
    correcoes_payload = payload.get("correcoes")
    correcoes = dict(correcoes_payload) if isinstance(correcoes_payload, Mapping) else {}

    return {
        "texto": texto_cliente,
        "reserva_confirmada": reserva_confirmada,
        "dados_reserva": dados_reserva,
        "status_reserva": status,
        "confianca": _confianca(payload.get("confianca") or reserva_dict.get("confianca")),
        "intencao": _normalizar_intencao_ia(str(payload.get("intencao") or "")),
        "acao": acao,
        "proximo_campo": _normalizar_campo_ia(str(payload.get("proximo_campo") or "")),
        "correcoes": correcoes,
    }


def _dados_extraidos_de_json(dados: Mapping[str, Any]) -> DadosReserva:
    reserva: DadosReserva = {}
    data_reserva = dados.get("data_reserva") or dados.get("data")
    if isinstance(data_reserva, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_reserva.strip()):
        reserva["data_reserva"] = data_reserva.strip()
    elif isinstance(data_reserva, str):
        data_interpretada = _extrair_data(data_reserva, permitir_dia_isolado=True)
        if data_interpretada:
            reserva["data_reserva"] = data_interpretada

    horario = dados.get("horario") or dados.get("hora")
    if isinstance(horario, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", horario.strip()):
        reserva["horario"] = horario.strip()
    elif isinstance(horario, str):
        horario_interpretado = _extrair_horario(horario, permitir_numero_isolado=True)
        if horario_interpretado:
            reserva["horario"] = horario_interpretado

    pessoas = _pessoas_json(dados.get("pessoas") or dados.get("quantidade") or dados.get("quantidade_pessoas"))
    if pessoas is None:
        quantidade_texto = dados.get("quantidade") or dados.get("quantidade_pessoas") or dados.get("pessoas")
        if isinstance(quantidade_texto, str):
            pessoas = _extrair_pessoas_solicitadas(quantidade_texto, permitir_numero_isolado=True)
    if pessoas is not None:
        reserva["pessoas"] = pessoas

    nome = dados.get("nome_cliente") or dados.get("nome")
    if isinstance(nome, str) and _nome_parece_valido(nome):
        reserva["nome_cliente"] = nome.strip()

    return reserva


def _normalizar_intencao_ia(valor: str) -> str:
    normalizado = _normalizar_busca(valor).replace(" ", "_")
    mapa = {
        "correcao": "correcao",
        "corrigir_dado": "correcao",
        "fornecimento_de_dados": "fornecimento_dados",
        "informar_dados": "fornecimento_dados",
        "informar_data": "fornecimento_dados",
        "informar_horario": "fornecimento_dados",
        "informar_quantidade": "fornecimento_dados",
        "pergunta_sobre_restaurante": "pergunta_restaurante",
        "pergunta_sobre_disponibilidade": "pergunta_disponibilidade",
        "sem_interesse": "recusa",
    }
    return mapa.get(normalizado, normalizado)


def _normalizar_campo_ia(valor: str) -> str:
    normalizado = _normalizar_busca(valor).replace(" ", "_")
    if normalizado in {"", "null", "none", "nenhum"}:
        return ""
    return MAPA_CAMPO_IA.get(normalizado, normalizado)


def _atualizar_estado_reserva(
    estado: EstadoReserva,
    *,
    mensagem_cliente: str,
    dados_modelo: Mapping[str, Any],
    invalidos: set[str],
    interpretacao: Mapping[str, Any],
) -> None:
    campo_pendente = _campo_aguardado(estado)
    intencao_modelo = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
    mensagem_eh_conversa = intencao_modelo in INTENCOES_CONVERSACIONAIS_IA or bool(_intencao_conversacional(mensagem_cliente))
    dados_usuario = _extrair_dados_reserva_contextual(
        mensagem_cliente,
        "",
        campo_aguardado=campo_pendente,
    )

    pessoas_solicitadas = _extrair_pessoas_solicitadas(
        mensagem_cliente,
        permitir_numero_isolado=campo_pendente == "pessoas",
    )
    if pessoas_solicitadas is not None:
        if 1 <= pessoas_solicitadas <= _limite_pessoas_reserva():
            dados_usuario["pessoas"] = pessoas_solicitadas
        else:
            invalidos.add("pessoas")

    horario_usuario = _extrair_horario(
        mensagem_cliente,
        permitir_numero_isolado=campo_pendente == "horario",
    )
    if horario_usuario and not _horario_reserva_valido(horario_usuario):
        invalidos.add("horario")

    tem_sinal_data = _mensagem_tem_sinal_data(mensagem_cliente) or (
        campo_pendente == "data_reserva" and _mensagem_parece_numero_isolado(mensagem_cliente)
    )
    if (
        campo_pendente in {"", "data_reserva"}
        and tem_sinal_data
        and not mensagem_eh_conversa
        and not dados_usuario.get("data_reserva")
    ):
        invalidos.add("data_reserva")

    if _pode_aceitar_dado_modelo("data_reserva", mensagem_cliente, estado, interpretacao):
        _definir_data_estado(
            estado,
            dados_modelo.get("data_reserva"),
            invalidos=invalidos,
            valor_original=mensagem_cliente,
            fonte="modelo",
            permitir_sobrescrever=_mensagem_indica_correcao(mensagem_cliente, interpretacao),
        )
    if _pode_aceitar_dado_modelo("horario", mensagem_cliente, estado, interpretacao):
        _definir_horario_estado(estado, dados_modelo.get("horario"), invalidos=invalidos)
    if _pode_aceitar_dado_modelo("pessoas", mensagem_cliente, estado, interpretacao):
        _definir_pessoas_estado(estado, dados_modelo.get("pessoas"), invalidos=invalidos)

    _definir_data_estado(
        estado,
        dados_usuario.get("data_reserva"),
        invalidos=invalidos,
        valor_original=mensagem_cliente,
        fonte="cliente",
        permitir_sobrescrever=campo_pendente not in {"horario", "pessoas"} and tem_sinal_data,
    )
    _definir_horario_estado(estado, dados_usuario.get("horario"), invalidos=invalidos)
    _definir_pessoas_estado(estado, dados_usuario.get("pessoas"), invalidos=invalidos)

    observacoes = str(dados_modelo.get("observacoes") or dados_usuario.get("observacoes") or "").strip()
    if observacoes:
        estado["observacoes"] = observacoes


def _definir_data_estado(
    estado: EstadoReserva,
    valor: Any,
    *,
    invalidos: set[str],
    valor_original: str = "",
    fonte: str = "",
    permitir_sobrescrever: bool = False,
) -> None:
    if not isinstance(valor, str) or not valor.strip():
        return
    data_texto = valor.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_texto):
        return
    if not _data_reserva_valida(data_texto):
        invalidos.add("data_reserva")
        logger.info(
            "Data de reserva rejeitada: original=%r interpretada=%s salva=%s fonte=%s.",
            valor_original,
            data_texto,
            estado.get("data_reserva", ""),
            fonte,
        )
        return
    data_anterior = str(estado.get("data_reserva") or "")
    if data_anterior and data_anterior != data_texto and not permitir_sobrescrever:
        logger.info(
            "Data de reserva mantida sem alteracao: original=%r interpretada=%s salva=%s fonte=%s.",
            valor_original,
            data_texto,
            data_anterior,
            fonte,
        )
        return
    estado["data_reserva"] = data_texto
    if valor_original:
        estado["data_reserva_original"] = valor_original.strip()
    _limpar_tentativas_campo(estado, "data_reserva")
    logger.info(
        "Data de reserva registrada: original=%r interpretada=%s salva=%s fonte=%s.",
        valor_original,
        data_texto,
        estado.get("data_reserva", ""),
        fonte,
    )


def _remover_campos_invalidos_estado(estado: EstadoReserva, invalidos: set[str]) -> None:
    data_estado = str(estado.get("data_reserva") or "")
    if data_estado and not _data_reserva_valida(data_estado):
        estado.pop("data_reserva", None)
        invalidos.add("data_reserva")
    elif data_estado and not _data_estado_confere_origem(estado):
        logger.warning(
            "Data de reserva divergente da mensagem original: original=%r salva=%s.",
            estado.get("data_reserva_original", ""),
            data_estado,
        )
        estado.pop("data_reserva", None)
        invalidos.add("data_reserva")
    horario_estado = str(estado.get("horario") or "")
    if horario_estado and not _horario_reserva_valido(horario_estado):
        estado.pop("horario", None)
        invalidos.add("horario")
    if estado.get("pessoas") and _pessoas_json(estado.get("pessoas")) is None:
        estado.pop("pessoas", None)
        invalidos.add("pessoas")


def _definir_horario_estado(estado: EstadoReserva, valor: Any, *, invalidos: set[str]) -> None:
    if not isinstance(valor, str) or not valor.strip():
        return
    horario = valor.strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", horario):
        return
    if not _horario_reserva_valido(horario):
        estado["horario_invalido"] = horario
        invalidos.add("horario")
        return
    estado["horario"] = horario
    estado.pop("horario_invalido", None)
    _limpar_tentativas_campo(estado, "horario")


def _definir_pessoas_estado(estado: EstadoReserva, valor: Any, *, invalidos: set[str]) -> None:
    try:
        pessoas = int(valor)
    except (TypeError, ValueError):
        return
    if not 1 <= pessoas <= _limite_pessoas_reserva():
        invalidos.add("pessoas")
        return
    estado["pessoas"] = pessoas
    _limpar_tentativas_campo(estado, "pessoas")


def _definir_campo_pendente(estado: EstadoReserva, campo: str) -> None:
    estado["campo_pendente"] = campo
    etapa = ETAPA_POR_CAMPO.get(campo)
    if etapa:
        estado["etapa"] = etapa


def _campo_aguardado(estado: Mapping[str, Any]) -> str:
    etapa = str(estado.get("etapa") or "").strip()
    if etapa in CAMPO_POR_ETAPA:
        return CAMPO_POR_ETAPA[etapa]
    return str(estado.get("campo_pendente") or "").strip()


def _registrar_tentativa_campo(estado: EstadoReserva, campo: str) -> int:
    tentativas = estado.setdefault("tentativas_campos", {})
    tentativas[campo] = int(tentativas.get(campo, 0)) + 1
    return tentativas[campo]


def _tentativas_campo(estado: Mapping[str, Any], campo: str) -> int:
    tentativas = estado.get("tentativas_campos")
    if not isinstance(tentativas, Mapping):
        return 0
    try:
        return int(tentativas.get(campo, 0))
    except (TypeError, ValueError):
        return 0


def _limpar_tentativas_campo(estado: EstadoReserva, campo: str) -> None:
    tentativas = estado.get("tentativas_campos")
    if isinstance(tentativas, dict):
        tentativas.pop(campo, None)


def _deve_encaminhar_humano_por_tentativas(estado: Mapping[str, Any], campo: str) -> bool:
    return _tentativas_campo(estado, campo) >= 3


def _pode_aceitar_dado_modelo(
    campo: str,
    mensagem_cliente: str,
    estado: EstadoReserva,
    interpretacao: Mapping[str, Any],
) -> bool:
    campo_aguardado = _campo_aguardado(estado)
    intencao = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
    proximo_campo = _normalizar_campo_ia(str(interpretacao.get("proximo_campo") or ""))
    correcao = _mensagem_indica_correcao(mensagem_cliente, interpretacao)
    if intencao in INTENCOES_CONVERSACIONAIS_IA:
        return False
    if correcao:
        if campo == "data_reserva":
            return _mensagem_tem_sinal_data(mensagem_cliente) or bool(interpretacao.get("correcoes"))
        if campo == "horario":
            return _mensagem_tem_sinal_horario(mensagem_cliente) or bool(interpretacao.get("correcoes"))
        if campo == "pessoas":
            return _mensagem_tem_sinal_pessoas(mensagem_cliente) or bool(interpretacao.get("correcoes"))
        return True
    if campo_aguardado in {"data_reserva", "horario", "pessoas"}:
        return campo == campo_aguardado
    if campo_aguardado == "confirmacao":
        return False
    if proximo_campo and proximo_campo in {"data_reserva", "horario", "pessoas", "nome_cliente"}:
        return campo == proximo_campo and not estado.get(campo)
    if campo == "data_reserva":
        return not estado.get("data_reserva") and _mensagem_tem_sinal_data(mensagem_cliente)
    if campo == "horario":
        return not estado.get("horario") and _mensagem_tem_sinal_horario(mensagem_cliente)
    if campo == "pessoas":
        return not estado.get("pessoas") and _mensagem_tem_sinal_pessoas(mensagem_cliente)
    return False


def _deve_respeitar_nao_aplicavel(estado: EstadoReserva, mensagem_cliente: str, status_modelo: str) -> bool:
    if status_modelo != "nao_aplicavel":
        return False
    if any(estado.get(campo) for campo in ("data_reserva", "horario", "pessoas")):
        return False
    return not _mensagem_indica_reserva(mensagem_cliente)


def _interpretacao_tem_dados_reserva(interpretacao: Mapping[str, Any]) -> bool:
    dados = interpretacao.get("dados_reserva")
    return isinstance(dados, Mapping) and any(dados.get(campo) not in (None, "", []) for campo in ("data_reserva", "horario", "pessoas", "nome_cliente"))


def _mensagem_indica_correcao(mensagem_cliente: str, interpretacao: Mapping[str, Any] | None = None) -> bool:
    normalizado = _normalizar_busca(mensagem_cliente)
    if re.search(r"\b(na verdade|corrige|corrigir|correcao|correção|troca|muda|mudar|melhor|queria dizer|quis dizer|ao inves|em vez)\b", normalizado):
        return True
    if isinstance(interpretacao, Mapping):
        intencao = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
        if intencao == "correcao":
            return True
        correcoes = interpretacao.get("correcoes")
        return isinstance(correcoes, Mapping) and bool(correcoes)
    return False


def _campos_obrigatorios_faltantes(estado: EstadoReserva, telefone: str) -> list[str]:
    faltantes: list[str] = []
    if not _data_reserva_valida(str(estado.get("data_reserva") or "")):
        faltantes.append("data_reserva")
    if not _horario_reserva_valido(str(estado.get("horario") or "")):
        faltantes.append("horario")
    if _pessoas_json(estado.get("pessoas")) is None:
        faltantes.append("pessoas")
    if not estado.get("nome_cliente"):
        faltantes.append("nome_cliente")
    if not telefone.strip():
        faltantes.append("telefone")
    return faltantes


def _dados_reserva_do_estado(estado: EstadoReserva) -> DadosReserva:
    dados: DadosReserva = {}
    for campo in ("data_reserva", "horario", "pessoas", "nome_cliente", "observacoes"):
        valor = estado.get(campo)
        if valor not in (None, "", []):
            dados[campo] = valor  # type: ignore[literal-required]
    return dados


def _primeiro_campo_pendente(invalidos: list[str], estado: EstadoReserva, telefone: str) -> str:
    faltantes = _campos_obrigatorios_faltantes(estado, telefone)
    for campo in ("data_reserva", "horario", "pessoas", "nome_cliente", "telefone"):
        if campo in invalidos:
            return campo
    for campo in ("data_reserva", "horario", "pessoas", "nome_cliente", "telefone"):
        if campo in faltantes:
            return campo
    return "data_reserva"


def _mensagem_campo_invalido(campo: str, estado: EstadoReserva, telefone: str) -> str:
    tentativa = _registrar_tentativa_campo(estado, campo)
    if campo == "data_reserva":
        if tentativa >= 3:
            return "Não consegui entender uma data válida. Me passa no formato dia/mês, como 26/07? Se preferir, posso chamar alguém da equipe."
        return "Essa data já passou. Me fala uma data a partir de hoje para eu verificar a reserva."
    if campo == "horario":
        if tentativa >= 3:
            return "Não consegui entender o horário. Você pode me passar no formato 19h ou 19:30? Se preferir, posso chamar alguém da equipe."
        return _mensagem_horario_fora_funcionamento()
    if campo == "pessoas":
        if tentativa >= 3:
            return "Não consegui entender a quantidade de pessoas. Pode me mandar só o número, como 4 ou 6? Se preferir, posso chamar alguém da equipe."
        return "Esse numero de pessoas e maior do que consigo confirmar por aqui. Para quantas pessoas sera a reserva?"
    return _mensagem_pedir_campo(campo, estado)


def _mensagem_validacao_falhou(
    campo: str,
    estado: EstadoReserva,
    telefone: str,
    interpretacao: Mapping[str, Any],
) -> str:
    texto_modelo = _texto_modelo_para_validacao(str(interpretacao.get("texto") or ""), campo)
    if texto_modelo:
        _registrar_tentativa_campo(estado, campo)
        _registrar_validacao_estado(estado, campo=campo, resposta="modelo")
        return texto_modelo

    texto = _mensagem_campo_invalido(campo, estado, telefone)
    _registrar_validacao_estado(estado, campo=campo, resposta="fallback")
    return texto


def _texto_modelo_para_validacao(texto: str, campo: str) -> str:
    texto_limpo = remover_flag_reserva(str(texto or ""))
    if not texto_limpo or _texto_modelo_parece_rigido(texto_limpo):
        return ""
    normalizado = _normalizar_busca(texto_limpo)
    if re.search(r"\b(confirmad|posso confirmar|reserva para)\b", normalizado):
        return ""
    termos_por_campo = {
        "data_reserva": r"\b(passou|a partir de hoje|data valida|data válida|nova data)\b",
        "horario": r"\b(horario|hora|funcionamento|abre|fecha|periodo)\b",
        "pessoas": r"\b(pessoas|quantidade|grupo|limite|total)\b",
    }
    padrao = termos_por_campo.get(campo)
    if padrao and re.search(padrao, normalizado):
        return texto_limpo
    return ""


def _registrar_validacao_estado(estado: EstadoReserva, *, campo: str, resposta: str) -> None:
    validacoes = estado.setdefault("validacoes", [])
    validacoes.append(
        {
            "campo": campo,
            "resposta": resposta,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    del validacoes[:-10]


def _mensagem_pedir_campo(campo: str, estado: EstadoReserva) -> str:
    tentativa = _registrar_tentativa_campo(estado, campo)
    if campo == "data_reserva":
        if tentativa == 1:
            return "Certo, para qual data você quer fazer a reserva?"
        if tentativa == 2:
            return "Você tem algum dia em mente? Pode me mandar como 26/07."
        return "Não consegui entender a data. Me passa no formato dia/mês, como 26/07? Se preferir, posso chamar alguém da equipe."
    if campo == "horario":
        if tentativa >= 3:
            return "Não consegui entender o horário. Você pode me passar no formato 19h ou 19:30? Se preferir, posso chamar alguém da equipe."
        if tentativa == 2:
            return "Você pode me passar um horário aproximado, como 19h ou 20h?"
        if estado.get("horario_invalido"):
            return _mensagem_horario_fora_funcionamento()
        if estado.get("data_reserva") and estado.get("pessoas"):
            return "Certo, tenho a data e a quantidade de pessoas. Só falta o horário. Qual horário fica melhor para você?"
        if estado.get("data_reserva"):
            return "Certo, tenho a data. Qual horário fica melhor para você?"
        return "Certo, qual horário fica melhor para você?"
    if campo == "pessoas":
        if tentativa == 1:
            return "Beleza, para quantas pessoas será a reserva?"
        if tentativa == 2:
            return "Me manda só a quantidade de pessoas, pode ser um número como 4 ou 6."
        return "Não consegui entender a quantidade de pessoas. Pode me mandar só o número, como 4 ou 6? Se preferir, posso chamar alguém da equipe."
    if campo == "nome_cliente":
        if tentativa >= 3:
            return "Não consegui identificar o nome para a reserva. Se preferir, posso chamar alguém da equipe para ajudar."
        return "Só falta seu nome para deixar a reserva certinha. Qual nome devo colocar?"
    return "Antes de confirmar, preciso completar os dados da reserva."


def _mensagem_horario_fora_funcionamento() -> str:
    abertura, fechamento = _horario_funcionamento_texto()
    return (
        "Esse horário fica fora do nosso funcionamento, que é das "
        f"{_horario_para_cliente(abertura)} às {_horario_para_cliente(fechamento)}. "
        "Qual horário dentro desse período você prefere?"
    )


def _mensagem_confirmacao_previa(dados: Mapping[str, Any]) -> str:
    nome = str(dados.get("nome_cliente") or "").strip()
    nome_trecho = f", no nome de {nome}" if nome else ""
    return (
        "Deixa eu confirmar: ficou para "
        f"{_formatar_data_cliente(str(dados.get('data_reserva') or ''))}, "
        f"às {dados.get('horario')}, para {dados.get('pessoas')} pessoas"
        f"{nome_trecho}. Posso confirmar?"
    )


def _mensagem_confirmacao_previa_segura(dados: Mapping[str, Any]) -> str:
    texto = _mensagem_confirmacao_previa(dados)
    if not _resumo_confere_estado(texto, dados):
        logger.warning(
            "Resumo de reserva divergente do estado: data=%s horario=%s pessoas=%s texto=%r.",
            dados.get("data_reserva", ""),
            dados.get("horario", ""),
            dados.get("pessoas", ""),
            texto,
        )
    else:
        logger.info(
            "Resumo de reserva validado contra estado: data=%s horario=%s pessoas=%s.",
            dados.get("data_reserva", ""),
            dados.get("horario", ""),
            dados.get("pessoas", ""),
        )
    return texto


def _resumo_confere_estado(texto: str, dados: Mapping[str, Any]) -> bool:
    data_formatada = _formatar_data_cliente(str(dados.get("data_reserva") or ""))
    horario = str(dados.get("horario") or "")
    pessoas = str(dados.get("pessoas") or "")
    return bool(data_formatada and horario and pessoas and data_formatada in texto and horario in texto and pessoas in texto)


def _mensagem_reserva_confirmada(dados: Mapping[str, Any]) -> str:
    return (
        "Reserva confirmada para "
        f"{_formatar_data_cliente(str(dados.get('data_reserva') or ''))}, "
        f"às {dados.get('horario')}, para {dados.get('pessoas')} pessoas."
    )


def _formatar_data_cliente(valor: str) -> str:
    try:
        data_reserva = date.fromisoformat(valor[:10])
    except ValueError:
        return valor
    return data_reserva.strftime("%d/%m/%Y")


def _eh_confirmacao_cliente(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.fullmatch(r"(sim|s|confirmo|confirmado|pode confirmar|isso|isso mesmo|ok|fechado|beleza|perfeito)", normalizado)
        or re.search(r"\b(pode confirmar|confirmo|esta certo|ta certo|fechado)\b", normalizado)
    )


def _eh_cancelamento_cliente(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(re.search(r"\b(cancelar|cancela|desistir|nao quero mais)\b", normalizado))


def _eh_recusa_reserva(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    normalizado = re.sub(r"[^\w\s]", " ", normalizado)
    normalizado = re.sub(r"\s+", " ", normalizado).strip()
    if not normalizado:
        return False

    respostas_exatas = {
        "n",
        "nao",
        "nao quero",
        "agora nao",
        "depois",
        "mais tarde",
        "obrigado nao",
        "obrigada nao",
        "nao obrigado",
        "nao obrigada",
        "nao tenho interesse",
        "sem interesse",
    }
    if normalizado in respostas_exatas:
        return True

    return bool(
        re.search(r"\b(?:agora|hoje|por enquanto)\s+nao\b", normalizado)
        or re.search(r"\bnao\s+(?:quero|tenho interesse|vou querer|preciso)\b", normalizado)
        or re.search(r"\bobrigad[ao]\s+nao\b", normalizado)
    )


def _eh_pedido_imediato(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\b(agora|agr|hj agora|hoje agora)\b", normalizado)
        or re.search(r"\b(to|tou|estou)\s+(indo|chegando)\b", normalizado)
        or re.search(r"\b(chegando|indo ai|indo agora|mais rapido possivel|o quanto antes)\b", normalizado)
        or re.search(r"\bquero\s+(?:pra\s+)?agora\b", normalizado)
    )


def _resposta_pedido_imediato(
    estado: EstadoReserva,
    *,
    mensagem_cliente: str,
    confianca: float,
) -> RespostaAgente:
    estado["pedido_imediato"] = True
    estado["aguardando_confirmacao"] = False
    estado["data_reserva"] = _hoje().isoformat()
    estado["data_reserva_original"] = mensagem_cliente.strip()
    _definir_campo_pendente(estado, "horario")
    logger.info(
        "Pedido imediato identificado. original=%r data_salva=%s.",
        mensagem_cliente,
        estado.get("data_reserva", ""),
    )
    return {
        "texto": (
            "Entendi, você quer para agora. Como é uma reserva em cima da hora, "
            "preciso que a equipe confirme a disponibilidade."
        ),
        "reserva_confirmada": False,
        "dados_reserva": _dados_reserva_do_estado(estado),
        "status_reserva": "aguardando_humano",
        "confianca": confianca,
    }


def _pergunta_sobre_horario_indisponivel(texto: str, estado: Mapping[str, Any]) -> bool:
    normalizado = _normalizar_busca(texto)
    horario = _extrair_horario(texto, permitir_numero_isolado=False) or str(estado.get("horario_invalido") or "")
    if horario and not _horario_reserva_valido(horario):
        return True
    return bool(
        re.search(r"\b(por que|pq|porque|motivo|nao pode|nao disponivel|indisponivel)\b", normalizado)
        and re.search(r"\b(horario|hora|manha|madrugada)\b", normalizado)
    )


def _mensagem_pede_explicacao_horario(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        "?" in texto
        or re.search(r"\b(por que|pq|porque|motivo|nao pode|nao disponivel|indisponivel)\b", normalizado)
    )


def _intencao_conversacional(texto: str) -> str | None:
    normalizado = _normalizar_busca(texto)
    if not normalizado:
        return None
    if re.search(r"\b(kkkk|kkk|rsrs|haha|se nao estiver|se nao tiver|talvez|depende)\b", normalizado):
        return "comentario"
    if _mensagem_tem_sinal_data(texto) or _mensagem_tem_sinal_horario(texto) or _mensagem_tem_sinal_pessoas(texto):
        return None
    if re.search(r"\b(como|de onde|onde)\b", normalizado) and re.search(
        r"\b(sabe|sabem|saber|informacoes|dados|horarios|funcionamento|endereco)\b",
        normalizado,
    ):
        return "pergunta_fonte"
    if re.search(r"\b(voce|vc|vcs|atendente|assistente)\b", normalizado) and re.search(
        r"\b(robo|bot|virtual|automatico|ia|inteligencia artificial|humano)\b",
        normalizado,
    ):
        return "pergunta_bot"
    if re.search(r"\b(quem e voce|quem esta falando|estou falando com quem|isso e automatico)\b", normalizado):
        return "pergunta_bot"
    if re.search(
        r"\b(nao sei ainda|nao sei|estou vendo|to vendo|vou ver|preciso ver|preciso confirmar|vendo com|falar com meus amigos|falar com a galera|depois te falo|te aviso|ainda nao decidi)\b",
        normalizado,
    ):
        return "comentario_indecisao"
    if _eh_saudacao_simples(normalizado):
        return "saudacao"
    return None


def _responder_intencao_conversacional(
    *,
    intencao: str,
    telefone: str,
    estado: EstadoReserva,
    interpretacao: ResultadoReservaEstruturada,
) -> RespostaAgente:
    campo = _campo_retomada_apos_informacao(estado, telefone)
    if campo:
        _definir_campo_pendente(estado, campo)

    texto = _texto_modelo_para_intencao(interpretacao.get("texto", ""), intencao)
    if not texto:
        texto = _texto_fallback_intencao(intencao, estado)

    logger.info(
        "Intencao conversacional respondida sem contar erro: intencao=%s campo_retomada=%s.",
        intencao,
        campo,
    )
    return {
        "texto": texto,
        "reserva_confirmada": False,
        "dados_reserva": _dados_reserva_do_estado(estado),
        "status_reserva": "aguardando_confirmacao" if campo == "confirmacao" else "em_coleta",
        "confianca": max(interpretacao["confianca"], 0.8),
    }


def _texto_modelo_para_intencao(texto: str, intencao: str) -> str:
    texto_limpo = remover_flag_reserva(str(texto or ""))
    if not texto_limpo or _texto_modelo_parece_rigido(texto_limpo):
        return ""
    normalizado = _normalizar_busca(texto_limpo)
    if intencao == "pergunta_fonte":
        return texto_limpo if re.search(r"\b(cadastrad|equipe|sistema|informacoes|dados)\b", normalizado) else ""
    if intencao == "pergunta_bot":
        return texto_limpo if re.search(r"\b(assistente|virtual|bot|robo|atendente|equipe)\b", normalizado) else ""
    if intencao == "comentario_indecisao":
        return texto_limpo if re.search(r"\b(sem problema|tranquilo|quando|decidir|continuo|por aqui)\b", normalizado) else ""
    if intencao in {"comentario", "brincadeira", "indecisao", "duvida"}:
        return texto_limpo if not re.search(r"\b(nao consegui entender|invalido|invalida)\b", normalizado) else ""
    if intencao in {"pergunta_restaurante", "pergunta_disponibilidade"}:
        return texto_limpo if "?" not in texto_limpo or not _texto_so_pede_campo(texto_limpo) else ""
    if intencao == "saudacao":
        return texto_limpo if not _texto_so_pede_campo(texto_limpo) else ""
    return texto_limpo


def _texto_fallback_intencao(intencao: str, estado: EstadoReserva) -> str:
    if intencao == "pergunta_fonte":
        return _com_continuacao_fluxo(
            "Essas informacoes foram cadastradas pela propria equipe do restaurante no nosso sistema.",
            estado,
        )
    if intencao == "pergunta_bot":
        return (
            "Sou o assistente virtual do restaurante e estou aqui para ajudar com sua reserva. "
            "Se preferir falar com alguem da equipe, tambem posso encaminhar."
        )
    if intencao == "comentario_indecisao":
        return "Sem problema. Quando decidir, me chama por aqui que eu continuo a reserva com voce."
    if intencao in {"comentario", "brincadeira", "indecisao", "duvida"}:
        return _com_continuacao_fluxo("Sem problema, sigo com voce.", estado)
    if intencao == "pergunta_disponibilidade":
        return _com_continuacao_fluxo(
            "Consigo verificar a data ou o horario que voce preferir, mas nao tenho uma agenda de disponibilidade aberta aqui.",
            estado,
        )
    if intencao == "pergunta_restaurante":
        return _com_continuacao_fluxo("Posso te ajudar com as informacoes do restaurante e com a reserva.", estado)
    if intencao == "saudacao":
        return "Oi! Estou por aqui para ajudar com sua reserva."
    return _com_continuacao_fluxo("Certo, sigo com voce.", estado)


def _texto_modelo_parece_rigido(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\bnao consegui entender\b", normalizado)
        or re.search(r"\bme passa no formato\b", normalizado)
        or re.search(r"\breserva confirmada\b", normalizado)
        or re.search(r"\bantes de confirmar preciso completar\b", normalizado)
        or re.search(r"\btive uma instabilidade\b", normalizado)
    )


def _texto_modelo_para_pedir_campo(texto: str, campo: str, estado: EstadoReserva, mensagem_cliente: str) -> str:
    if not _mensagem_pode_usar_coleta_natural(mensagem_cliente):
        return ""
    texto_limpo = remover_flag_reserva(str(texto or ""))
    if not texto_limpo or _texto_modelo_parece_rigido(texto_limpo):
        return ""
    normalizado = _normalizar_busca(texto_limpo)
    if re.search(r"\b(confirmad|posso confirmar|confirma\??|reserva para)\b", normalizado):
        return ""
    termos_por_campo = {
        "data_reserva": r"\b(data|dia|quando)\b",
        "horario": r"\b(horario|hora|periodo)\b",
        "pessoas": r"\b(pessoas|quantas|total|convidados)\b",
        "nome_cliente": r"\b(nome)\b",
    }
    padrao = termos_por_campo.get(campo)
    if padrao and not re.search(padrao, normalizado):
        return ""
    ultimo_texto = str(estado.get("ultimo_texto_bot") or "")
    if ultimo_texto and _textos_muito_parecidos(ultimo_texto, texto_limpo):
        return ""
    return texto_limpo


def _mensagem_pode_usar_coleta_natural(mensagem_cliente: str) -> bool:
    normalizado = _normalizar_busca(mensagem_cliente)
    return bool(
        _eh_confirmacao_cliente(mensagem_cliente)
        or mensagem_indica_interesse_reserva(mensagem_cliente)
        or _eh_saudacao_simples(normalizado)
        or re.search(r"\b(claro|pode ser|vamos|bora|beleza|perfeito|quero sim|gostaria sim)\b", normalizado)
    )


def _textos_muito_parecidos(anterior: str, atual: str) -> bool:
    anterior_norm = _normalizar_busca(anterior)
    atual_norm = _normalizar_busca(atual)
    if not anterior_norm or not atual_norm:
        return False
    if anterior_norm == atual_norm:
        return True
    palavras_anterior = {p for p in re.findall(r"\w+", anterior_norm) if len(p) > 3}
    palavras_atual = {p for p in re.findall(r"\w+", atual_norm) if len(p) > 3}
    if not palavras_anterior or not palavras_atual:
        return False
    intersecao = len(palavras_anterior & palavras_atual)
    menor = min(len(palavras_anterior), len(palavras_atual))
    return menor > 0 and intersecao / menor >= 0.8


def _texto_so_pede_campo(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.fullmatch(r".*\b(qual|me fala|me diga|pode me passar)\b.*\b(data|dia|horario|pessoas|nome)\b.*", normalizado)
        and not re.search(r"\b(oi|ola|claro|perfeito|certo|sem problema)\b", normalizado)
    )


def _eh_saudacao_simples(normalizado: str) -> bool:
    return bool(re.fullmatch(r"\s*(oi|ola|bom dia|boa tarde|boa noite|opa|e ai|eae)\s*[!.]*\s*", normalizado))


def _categoria_pergunta_restaurante(texto: str) -> str | None:
    normalizado = _normalizar_busca(texto)
    if not normalizado:
        return None

    marcador_pergunta = bool(
        "?" in texto
        or re.search(r"\b(quando|qual|quais|onde|como|quanto|quantas|tem|aceita|aceitam|funciona|abre|fecha|fica)\b", normalizado)
    )

    if re.search(r"\b(horarios?|hora)\b", normalizado) and re.search(
        r"\b(disponivel|disponiveis|disponibilidade|livres|vagos)\b",
        normalizado,
    ):
        return "horarios_disponiveis"
    if re.search(r"\b(abre|abrir|fecha|fechar|funciona|funcionamento|que horas)\b", normalizado):
        return "funcionamento"
    if re.search(r"\b(endereco|onde fica|localizacao|localizado|rua|avenida|bairro)\b", normalizado):
        return "endereco"
    if re.search(r"\b(estacionamento|estacionar|manobrista|valet)\b", normalizado):
        return "estacionamento"
    if re.search(r"\b(pagamento|pagar|pix|cartao|debito|credito|dinheiro|voucher|vale refeicao)\b", normalizado):
        return "pagamento"
    if re.search(r"\b(aniversario|bolo|decoracao|decorar|parabens|comemorar)\b", normalizado):
        return "aniversario"
    if re.search(r"\b(quantas pessoas|maximo|maxima|limite|grupo grande|muita gente)\b", normalizado):
        return "limite_pessoas"
    if marcador_pergunta and re.search(r"\b(cancelamento|cancelar|desmarcar|remarcar)\b", normalizado):
        return "cancelamento"
    if marcador_pergunta and re.search(
        r"\b(antecedencia|minima|politica|regra|regras|como funciona a reserva|precisa reservar|em cima da hora)\b",
        normalizado,
    ):
        return "politica_reserva"
    if marcador_pergunta and re.search(r"\b(restaurante|casa|reserva|reservas)\b", normalizado):
        return "geral"
    return None


def _responder_pergunta_restaurante(
    *,
    categoria: str,
    telefone: str,
    estado: EstadoReserva,
    confianca: float,
) -> RespostaAgente:
    campo = _campo_retomada_apos_informacao(estado, telefone)
    if campo:
        _definir_campo_pendente(estado, campo)

    texto = _texto_pergunta_restaurante(categoria, estado)
    logger.info(
        "Pergunta sobre restaurante respondida durante fluxo de reserva: categoria=%s campo_retomada=%s.",
        categoria,
        campo,
    )
    return {
        "texto": texto,
        "reserva_confirmada": False,
        "dados_reserva": _dados_reserva_do_estado(estado),
        "status_reserva": "aguardando_confirmacao" if campo == "confirmacao" else "em_coleta",
        "confianca": confianca,
    }


def _campo_retomada_apos_informacao(estado: EstadoReserva, telefone: str) -> str:
    campo_atual = _campo_aguardado(estado)
    if campo_atual in set(ETAPA_POR_CAMPO):
        return campo_atual
    if estado.get("aguardando_confirmacao"):
        return "confirmacao"
    faltantes = _campos_obrigatorios_faltantes(estado, telefone)
    if not faltantes:
        return "confirmacao"
    return faltantes[0]


def _texto_pergunta_restaurante(categoria: str, estado: EstadoReserva) -> str:
    config = config_restaurante.obter_config()
    abertura, fechamento = _horario_funcionamento_texto()
    abertura_cliente = _horario_para_cliente(abertura)
    fechamento_cliente = _horario_para_cliente(fechamento)

    if categoria == "horarios_disponiveis":
        return (
            f"Atendemos entre {abertura_cliente} e {fechamento_cliente}. "
            "Me fala o horario que voce prefere e eu verifico a solicitacao."
        )

    if categoria == "funcionamento":
        if estado.get("data_reserva"):
            base = f"Nesse dia abrimos as {abertura_cliente} e fechamos as {fechamento_cliente}."
        else:
            base = f"Funcionamos {config.dias_funcionamento}, das {abertura_cliente} as {fechamento_cliente}."
    elif categoria == "endereco":
        base = f"O endereco e {config.endereco}"
    elif categoria == "estacionamento":
        base = config.estacionamento
    elif categoria == "pagamento":
        base = f"Aceitamos {config.formas_pagamento}."
    elif categoria == "aniversario":
        base = config.aniversario
    elif categoria == "limite_pessoas":
        base = f"Consigo confirmar automaticamente reservas de ate {config.limite_pessoas_reserva} pessoas por aqui."
    elif categoria == "cancelamento":
        base = config.politica_cancelamento
        if config.telefone_atendimento:
            base = f"{base} O telefone do atendimento e {config.telefone_atendimento}."
    elif categoria == "politica_reserva":
        base = (
            f"Para reservas, trabalhamos {config.horarios_aceitos_reserva}. "
            f"Antecedencia minima: {config.antecedencia_minima}. "
            f"{config.regras_reservas_imediatas}"
        )
    else:
        base = config.informacoes_gerais or (
            f"Posso te ajudar com funcionamento, endereco, pagamentos e reservas do {config.nome}."
        )

    return _com_continuacao_fluxo(base, estado)


def _com_continuacao_fluxo(base: str, estado: EstadoReserva) -> str:
    texto = base.strip()
    if texto and texto[-1] not in ".!?":
        texto = f"{texto}."

    continuacao = _continuacao_fluxo_apos_informacao(estado)
    if not continuacao:
        return texto
    return f"{texto} {continuacao}"


def _continuacao_fluxo_apos_informacao(estado: EstadoReserva) -> str:
    campo = _campo_aguardado(estado)
    if campo == "data_reserva":
        return "Me fala o dia que voce quer reservar."
    if campo == "horario":
        return "Qual horario dentro desse periodo fica melhor para voce?"
    if campo == "pessoas":
        return "Para quantas pessoas sera a reserva?"
    if campo == "nome_cliente":
        return "Qual nome devo colocar na reserva?"
    if campo == "confirmacao":
        return "Quando estiver tudo certo, posso confirmar a reserva?"
    return ""


def mensagem_indica_interesse_reserva(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\b(quero|gostaria|queria|preciso|pode|vamos|bora)\s+(?:fazer\s+)?(?:uma\s+)?reserva(?:r)?\b", normalizado)
        or re.search(r"\b(?:reservar|fazer reserva|mesa para|quero uma mesa)\b", normalizado)
        or re.search(r"\b(mudei de ideia|pensando melhor|agora quero|reserva pra mim|reserva para mim)\b", normalizado)
    )


def _mensagem_indica_reserva(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\b(reserva|reservar|mesa|pessoas|pessoa|convidados|lugares|horario|dia|data|disponivel|disponibilidade|confirmar)\b", normalizado)
        or _eh_confirmacao_cliente(texto)
        or _mensagem_tem_sinal_data(texto)
        or _mensagem_tem_sinal_horario(texto)
        or _mensagem_tem_sinal_pessoas(texto)
    )


def _pergunta_data_disponivel(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\bqual(?:\s+\w+){0,4}\s+(?:data|dia)(?:\s+\w+){0,4}\s+disponivel\b", normalizado)
        or re.search(r"\b(?:data|dia)(?:\s+\w+){0,4}\s+disponivel\b", normalizado)
        or re.search(r"\btem(?:\s+\w+){0,4}\s+(?:data|dia)(?:\s+\w+){0,4}\s+disponivel\b", normalizado)
    )


def _mensagem_tem_sinal_data(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\b(hoje|amanha|dia\s+\d{1,2}|\d{1,2}[/-]\d{1,2})\b", normalizado)
        or re.search(r"\b\d{1,2}\s+de\s+[a-z]{3,}\b", normalizado)
    )


def _mensagem_tem_sinal_horario(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\b([01]?\d|2[0-3])[:h]([0-5]\d)?\b", normalizado)
        or re.search(r"\b(?:as|às)\s+(\d{1,2})\b", texto, flags=re.IGNORECASE)
        or re.search(r"\b\d{1,2}\s+horas?\b", normalizado)
        or re.search(r"\b\d{1,2}\s*(?:da|de)\s*(manha|tarde|noite)\b", normalizado)
    )


def _mensagem_tem_sinal_pessoas(texto: str) -> bool:
    return _extrair_pessoas_solicitadas(texto, permitir_numero_isolado=False) is not None


def _mensagem_parece_numero_isolado(texto: str) -> bool:
    return bool(re.fullmatch(r"\D*\d{1,4}\D*", _normalizar_busca(texto)))


def _nome_parece_valido(nome: str) -> bool:
    nome_limpo = str(nome or "").strip()
    if not nome_limpo or nome_limpo.lower() in {"cliente", "contato"}:
        return False
    if re.fullmatch(r"\+?\d[\d\s().-]{6,}", nome_limpo):
        return False
    return bool(re.search(r"[A-Za-zÀ-ÿ]{2,}", nome_limpo))


def _extrair_nome_cliente(texto: str) -> str | None:
    texto_limpo = re.sub(r"\s+", " ", texto).strip()
    if not 2 <= len(texto_limpo) <= 80:
        return None
    normalizado = _normalizar_busca(texto_limpo)
    if _eh_confirmacao_cliente(texto_limpo) or _mensagem_indica_reserva(texto_limpo):
        return None
    if re.search(r"\b(cancelar|obrigado|valeu|horario|pessoas|mesa|reserva)\b", normalizado):
        return None
    if not re.fullmatch(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' .-]{1,79}", texto_limpo):
        return None
    return texto_limpo


def _normalizar_busca(texto: str) -> str:
    texto_normalizado = unicodedata.normalize("NFD", str(texto or "").lower())
    return "".join(char for char in texto_normalizado if unicodedata.category(char) != "Mn").strip()


def _data_reserva_valida(valor: str) -> bool:
    try:
        data_reserva = date.fromisoformat(str(valor or "")[:10])
    except ValueError:
        return False
    return data_reserva >= _hoje()


def _data_estado_confere_origem(estado: Mapping[str, Any]) -> bool:
    data_estado = str(estado.get("data_reserva") or "")
    original = str(estado.get("data_reserva_original") or "")
    if not data_estado or not original:
        return True
    data_original = _extrair_data(original)
    if data_original is None:
        return True
    return data_original == data_estado


def _horario_reserva_valido(horario: str) -> bool:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", horario):
        return False
    minuto = _horario_para_minutos(horario)
    abertura, fechamento = _horario_funcionamento_texto()
    inicio = _horario_para_minutos(abertura)
    fim = _horario_para_minutos(fechamento)
    if inicio is None or fim is None or minuto is None:
        return True
    if inicio <= fim:
        return inicio <= minuto <= fim
    return minuto >= inicio or minuto <= fim


def _horario_funcionamento_texto() -> tuple[str, str]:
    abertura, fechamento = config_restaurante.horario_funcionamento()
    if _horario_para_minutos(abertura) is None:
        abertura = "10:00"
    if _horario_para_minutos(fechamento) is None:
        fechamento = "23:59"
    return abertura, fechamento


def _horario_para_cliente(horario: str) -> str:
    if horario == "23:59":
        return "meia-noite"
    return horario


def _limite_pessoas_reserva() -> int:
    return config_restaurante.limite_pessoas_reserva()


def _horario_para_minutos(horario: str) -> int | None:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(horario or "").strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _hoje() -> date:
    return date.today()


def _resposta_contingencia() -> str:
    return (
        "Tive uma instabilidade aqui, mas ja estou retomando. "
        "Pode me confirmar o dia, horario e numero de pessoas para a reserva?"
    )


def _chamar_groq(mensagens: Sequence[Mensagem], modelo: str) -> str:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY nao configurada.")

    from groq import Groq

    client = Groq(api_key=api_key)
    resposta = client.chat.completions.create(
        model=modelo.strip() or MODELO_PADRAO,
        messages=list(mensagens),
        temperature=0.4,
        max_tokens=500,
    )

    conteudo = resposta.choices[0].message.content
    if not conteudo:
        raise RuntimeError("Groq retornou resposta vazia.")

    return conteudo.strip()


def _mensagem_sistema(nome_cliente: str, *, perfil_cliente: Mapping[str, Any] | None = None) -> Mensagem:
    config = config_restaurante.obter_config()
    restaurante = config.nome
    hoje = _hoje().strftime("%d/%m/%Y")
    nome = nome_cliente.strip() or "cliente"
    perfil_nome = str((perfil_cliente or {}).get("nome") or "").strip()
    perfil_prompt = str((perfil_cliente or {}).get("prompt_ia") or "").strip()
    instrucao_perfil = ""
    if perfil_nome or perfil_prompt:
        instrucao_perfil = (
            "\n\n"
            f"Perfil do cliente: {perfil_nome or 'perfil sem nome'}.\n"
            "Adapte o atendimento a esse perfil sem alongar as respostas.\n"
        )
        if perfil_prompt:
            instrucao_perfil += f"Orientacao especifica do perfil: {perfil_prompt}\n"

    return {
        "role": "system",
        "content": (
            f"Você é a atendente virtual do {restaurante}. "
            f"Está conversando com {nome}. "
            "Responda sempre em português do Brasil. "
            f"{instrucao_perfil}"
            "\n\n"
            "Seu jeito de falar:\n"
            "- Direta e simpática, como uma atendente real no WhatsApp\n"
            "- Sem 'Olá', 'Oi' ou saudações — a abertura já foi feita\n"
            "- Respostas curtas, no máximo 1-2 linhas\n"
            "- Sem formalidade excessiva, sem 'prezado', sem 'atenciosamente'\n"
            "- Use linguagem natural\n"
            "- Não use emojis\n"
            "- Tente ao maximo parecer um humano fazendo o atendimento\n"
            "\n"
            "Exemplos de tom:\n"
            "Cliente: 'Quero reservar'\n"
            "Você: 'Ótimo! Qual data e horário você prefere?'\n"
            "\n"
            "Cliente: 'Sábado às 20h'\n"
            "Você: 'Perfeito! Quantas pessoas no total?'\n"
            "\n"
            "Cliente: '8 pessoas'\n"
            "Você: 'Anotado! Sábado, 20h, 8 pessoas — confirma?'\n"
            "\n"
            "Seu objetivo:\n"
            "Coletar dia, horário e número de pessoas. "
            "Converse primeiro como uma atendente humana inteligente. "
            "Antes de tratar a mensagem como preenchimento de campo, classifique a intencao: "
            "pedido de humano, pergunta sobre restaurante, pergunta sobre o bot/conversa, correcao, recusa/cancelamento, "
            "fornecimento de data/horario/quantidade ou mensagem incompreensivel. "
            "Perguntas, comentarios e duvidas nao sao erro e nao devem soar como formulario. "
            "Quando fizer sentido, responda ao que o cliente disse e retome naturalmente o proximo dado da reserva. "
            "Interprete dados como uma pessoa interpretaria: '11 horas' e '11 da manha' significam horario 11:00; "
            "'20 sem contar comigo' significa 21 pessoas; 'eu e mais 3' significa 4 pessoas; "
            "comentarios como 'se nao estiver chovendo eu vou no dia 28 kkk' sao comentarios/condicionais, nao erro de horario. "
            "Use a etapa atual para interpretar numeros: se o sistema aguarda quantidade, um numero solto e quantidade; "
            "se aguarda horario, um numero solto pode ser horario; se aguarda data, pode ser dia. "
            "Pergunte data e horário juntos na primeira pergunta quando a conversa ainda estiver iniciando. "
            "Depois pergunte o total de pessoas. "
            "Nunca confirme uma reserva sem data, horario, quantidade de pessoas, nome e telefone. "
            "Se o cliente disser sim mas faltar algum campo, pergunte o campo faltante. "
            "Se o horario ou a quantidade forem invalidos, nao use esse valor. "
            "Antes da confirmacao final, envie um resumo com data, horario e pessoas e pergunte se pode confirmar. "
            "So use status confirmada depois que o cliente confirmar esse resumo. "
            "\n\n"
            "Informacoes reais do restaurante para responder perguntas durante a reserva:\n"
            f"- Endereco: {config.endereco}\n"
            f"- Funcionamento: {config.dias_funcionamento}, das {config.horario_abertura} as {config.horario_fechamento}\n"
            f"- Horarios aceitos para reserva: {config.horarios_aceitos_reserva}\n"
            f"- Limite de pessoas por reserva automatica: {config.limite_pessoas_reserva}\n"
            f"- Formas de pagamento: {config.formas_pagamento}\n"
            f"- Cancelamento: {config.politica_cancelamento}\n"
            f"- Estacionamento: {config.estacionamento}\n"
            f"- Aniversarios: {config.aniversario}\n"
            "Se o cliente fizer uma pergunta normal sobre o restaurante, responda e retome o campo da reserva que faltava. "
            "Se o cliente perguntar como voce sabe uma informacao, explique que os dados foram cadastrados pela equipe no sistema. "
            "Nunca diga que um horario esta disponivel sem uma agenda real integrada; diga que atende dentro do funcionamento e vai verificar a solicitacao. "
            "Responda sempre e somente em JSON válido, sem markdown. Use este formato principal: "
            '{"resposta_natural":"texto natural para enviar ao cliente",'
            '"intencao":"pedido_humano|pergunta_restaurante|pergunta_bot|pergunta_fonte|correcao|recusa|fornecimento_dados|comentario|incompreensivel",'
            '"dados_extraidos":{"data":"YYYY-MM-DD ou null","horario":"HH:MM ou null","quantidade":numero ou null},'
            '"correcoes":{"campo":"valor corrigido"} ou {},'
            '"acao":"continuar_conversa|confirmar_reserva|cancelar|encaminhar_humano",'
            '"proximo_campo":"data|horario|quantidade|nome|confirmacao|null",'
            '"confianca":0.0}. '
            "O formato antigo tambem e aceito internamente: "
            '{"resposta_cliente":"texto para enviar ao cliente",'
            '"reserva":{"status":"em_coleta|confirmada|cancelada|nao_aplicavel",'
            '"data_reserva":"YYYY-MM-DD ou null","horario":"HH:MM ou null",'
            '"pessoas":numero ou null,"observacoes":"texto curto ou null"},'
            '"confianca":0.0}. '
            "Use status confirmada apenas quando data, horário, pessoas e confirmação do cliente estiverem claros. "
            f"Data de hoje: {hoje}."
        ),
    }


def _extrair_json_resposta(texto: str) -> dict[str, Any] | None:
    texto_limpo = texto.strip()
    if not texto_limpo:
        return None

    candidatos = [texto_limpo]
    match = re.search(r"\{.*\}", texto_limpo, flags=re.DOTALL)
    if match:
        candidatos.append(match.group(0))

    for candidato in candidatos:
        try:
            dados = json.loads(candidato)
        except json.JSONDecodeError:
            continue
        if isinstance(dados, dict):
            return dados
    return None


def _dados_reserva_de_json(reserva: dict[str, Any]) -> DadosReserva:
    dados: DadosReserva = {}
    data_reserva = reserva.get("data_reserva")
    if isinstance(data_reserva, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_reserva):
        dados["data_reserva"] = data_reserva
    elif isinstance(data_reserva, str):
        data_interpretada = _extrair_data(data_reserva, permitir_dia_isolado=True)
        if data_interpretada:
            dados["data_reserva"] = data_interpretada

    horario = reserva.get("horario")
    if isinstance(horario, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", horario):
        dados["horario"] = horario
    elif isinstance(horario, str):
        horario_interpretado = _extrair_horario(horario, permitir_numero_isolado=True)
        if horario_interpretado:
            dados["horario"] = horario_interpretado

    pessoas = _pessoas_json(reserva.get("pessoas"))
    if pessoas is None and isinstance(reserva.get("pessoas"), str):
        pessoas = _extrair_pessoas_solicitadas(str(reserva.get("pessoas")), permitir_numero_isolado=True)
    if pessoas is not None:
        dados["pessoas"] = pessoas

    observacoes = reserva.get("observacoes")
    if isinstance(observacoes, str) and observacoes.strip():
        dados["observacoes"] = observacoes.strip()

    return dados


def _pessoas_json(valor: Any) -> int | None:
    try:
        pessoas = int(valor)
    except (TypeError, ValueError):
        return None
    return pessoas if 1 <= pessoas <= _limite_pessoas_reserva() else None


def _dados_reserva_minimos(dados: DadosReserva) -> bool:
    return dados_reserva_obrigatorios_ok(
        dados,
        nome_cliente=str(dados.get("nome_cliente") or "cliente"),
        telefone="telefone",
    )


def _confianca(valor: Any) -> float:
    try:
        confianca = float(valor)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(confianca, 1.0))

def _limitar_historico(historico: list[Mensagem]) -> None:
    excesso = len(historico) - MAX_HISTORICO_MENSAGENS
    if excesso > 0:
        del historico[:excesso]


def _extrair_data(texto: str, *, permitir_dia_isolado: bool = False) -> str | None:
    data_relativa = _extrair_data_relativa(texto)
    if data_relativa:
        return data_relativa

    data_mes_extenso = _extrair_data_mes_extenso(texto)
    if data_mes_extenso:
        return data_mes_extenso

    match = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", texto)
    if match:
        dia = int(match.group(1))
        mes = int(match.group(2))
        ano_raw = match.group(3)
        ano = _hoje().year
        if ano_raw:
            ano = int(ano_raw)
            if ano < 100:
                ano += 2000

        return _formatar_data_futura(dia, mes, ano)

    match_dia = re.search(r"\bdia\s+(\d{1,2})\b", texto, flags=re.IGNORECASE)
    if match_dia:
        hoje = _hoje()
        dia = int(match_dia.group(1))
        return _formatar_data_futura(dia, hoje.month, hoje.year)

    if permitir_dia_isolado:
        match_isolado = re.fullmatch(r"\D*(\d{1,2})\D*", _normalizar_busca(texto))
        if match_isolado:
            hoje = _hoje()
            dia = int(match_isolado.group(1))
            return _formatar_data_futura(dia, hoje.month, hoje.year)

    return None


def _extrair_data_relativa(texto: str) -> str | None:
    texto_normalizado = _normalizar_busca(texto)
    hoje = _hoje()

    if re.search(r"\bhoje\b", texto_normalizado):
        return hoje.isoformat()

    if re.search(r"\bamanha\b", texto_normalizado):
        return (hoje + timedelta(days=1)).isoformat()

    return None


def _extrair_data_mes_extenso(texto: str) -> str | None:
    match = re.search(
        r"\b(\d{1,2})\s+de\s+([A-Za-zÀ-ÿ]{3,})\b",
        texto,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    dia = int(match.group(1))
    mes_texto = _normalizar_busca(match.group(2))
    mes = MESES.get(mes_texto)
    if mes is None:
        return None

    return _formatar_data_futura(dia, mes, _hoje().year)


def _formatar_data_futura(dia: int, mes: int, ano: int) -> str | None:
    try:
        data_reserva = date(ano, mes, dia)
    except ValueError:
        return None

    if data_reserva < _hoje():
        return None

    return data_reserva.isoformat()


def _extrair_horario(texto: str, *, permitir_numero_isolado: bool = False) -> str | None:
    texto_normalizado = _normalizar_busca(texto)
    match = re.search(r"\b([01]?\d|2[0-3])[:h]([0-5]\d)?\b", texto_normalizado)
    if match:
        hora = int(match.group(1))
        minuto = int(match.group(2) or 0)
        return f"{hora:02d}:{minuto:02d}"

    match_periodo_normalizado = re.search(
        r"\b(\d{1,2})\s*(?:horas?\s*)?(?:da|de)\s*(manha|tarde|noite)\b",
        texto_normalizado,
    )
    if match_periodo_normalizado:
        hora = int(match_periodo_normalizado.group(1))
        periodo = match_periodo_normalizado.group(2)
        if periodo in {"tarde", "noite"} and 1 <= hora <= 11:
            hora += 12
        if 0 <= hora <= 23:
            return f"{hora:02d}:00"

    match_as = re.search(r"\bas\s+(\d{1,2})(?:\s*horas?)?\b", texto_normalizado)
    if match_as:
        hora = int(match_as.group(1))
        if 0 <= hora <= 23:
            return f"{hora:02d}:00"

    match_horas = re.search(r"\b(\d{1,2})\s+horas?\b", texto_normalizado)
    if match_horas:
        hora = int(match_horas.group(1))
        if 0 <= hora <= 23:
            return f"{hora:02d}:00"

    if permitir_numero_isolado:
        match_isolado = re.fullmatch(r"\s*(\d{1,2})\s*[.!?]?\s*", texto_normalizado)
        if match_isolado:
            hora = int(match_isolado.group(1))
            if 0 <= hora <= 23:
                return f"{hora:02d}:00"

    match_periodo = re.search(
        r"\b(\d{1,2})\s*(?:horas?\s*)?(?:da|de)\s*(manha|manhã|tarde|noite)\b",
        texto,
        flags=re.IGNORECASE,
    )
    if not match_periodo:
        return None

    hora = int(match_periodo.group(1))
    periodo = match_periodo.group(2).lower()

    if periodo in {"tarde", "noite"} and 1 <= hora <= 11:
        hora += 12

    if 0 <= hora <= 23:
        return f"{hora:02d}:00"

    return None


def _extrair_pessoas(texto: str) -> int | None:
    pessoas_solicitadas = _extrair_pessoas_solicitadas(texto, permitir_numero_isolado=False)
    if pessoas_solicitadas is not None and 1 <= pessoas_solicitadas <= _limite_pessoas_reserva():
        return pessoas_solicitadas
    return None


def _extrair_pessoas_solicitadas(texto: str, *, permitir_numero_isolado: bool) -> int | None:
    texto_normalizado = _normalizar_busca(texto)

    match_sem_contar = re.search(
        r"\b(\d{1,4}|um|uma|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez)\s+sem\s+contar\s+(?:comigo|eu)\b",
        texto_normalizado,
    )
    if match_sem_contar:
        numero = _numero_texto_para_int(match_sem_contar.group(1))
        if numero is not None:
            return numero + 1

    match_e_mais = re.search(
        r"\b(?:eu|vou|vai\s+eu|vamos\s+eu)\s+e\s+mais\s+(\d{1,4}|um|uma|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez)\b",
        texto_normalizado,
    )
    if match_e_mais:
        numero = _numero_texto_para_int(match_e_mais.group(1))
        if numero is not None:
            return numero + 1

    pessoas_descritas = _contar_pessoas_descricao_simples(texto_normalizado)
    if pessoas_descritas is not None:
        return pessoas_descritas

    padroes = (
        r"\b(\d{1,4})\s*(?:pessoas|pessoa|convidados|lugares)\b",
        r"\bmesa\s+para\s+(\d{1,4})\b",
        r"\bpara\s+(\d{1,4})\b",
        r"\b(?:vao|iremos|somos)\s+(\d{1,4})\b",
    )

    for padrao in padroes:
        match = re.search(padrao, texto_normalizado)
        if match:
            return int(match.group(1))

    if permitir_numero_isolado:
        match_isolado = re.fullmatch(r"\s*(\d{1,4})\s*[.!?]?\s*", texto_normalizado)
        if match_isolado:
            return int(match_isolado.group(1))

    for palavra, numero in NUMEROS_POR_EXTENSO.items():
        padrao = rf"\b{re.escape(palavra)}\s*(?:pessoas|pessoa|convidados|lugares)\b"
        if re.search(padrao, texto_normalizado):
            return numero

    return None


def _numero_texto_para_int(valor: str) -> int | None:
    valor_limpo = _normalizar_busca(valor)
    if valor_limpo.isdigit():
        return int(valor_limpo)
    return NUMEROS_POR_EXTENSO.get(valor_limpo)


def _contar_pessoas_descricao_simples(texto_normalizado: str) -> int | None:
    if not re.search(r"\b(vai|vou|vamos|iremos|somos)\s+eu\b", texto_normalizado):
        return None

    total = 1
    if re.search(r"\b(minha\s+esposa|meu\s+esposo|minha\s+mulher|meu\s+marido|namorada|namorado)\b", texto_normalizado):
        total += 1

    match_criancas = re.search(
        r"\b(\d{1,2}|um|uma|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez)\s+(?:criancas|crianca|filhos|filhas)\b",
        texto_normalizado,
    )
    if match_criancas:
        numero = _numero_texto_para_int(match_criancas.group(1))
        if numero is not None:
            total += numero

    return total if total > 1 else None
