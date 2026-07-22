from __future__ import annotations

import logging
import json
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Final, Literal, TypedDict


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
    aguardando_confirmacao: bool


_historicos: dict[str, list[Mensagem]] = {}
_estados_reserva: dict[str, EstadoReserva] = {}

ETAPA_POR_CAMPO: Final[dict[str, str]] = {
    "data_reserva": "aguardando_data",
    "horario": "aguardando_horario",
    "pessoas": "aguardando_quantidade",
    "nome_cliente": "aguardando_nome",
    "telefone": "aguardando_telefone",
    "confirmacao": "aguardando_confirmacao",
}

CAMPO_POR_ETAPA: Final[dict[str, str]] = {etapa: campo for campo, etapa in ETAPA_POR_CAMPO.items()}


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

    if reserva_confirmada:
        limpar_historico(telefone_limpo)

    return {
        "texto": texto_cliente,
        "reserva_confirmada": reserva_confirmada,
        "dados_reserva": resposta_segura["dados_reserva"],
        "status_reserva": resposta_segura["status_reserva"],
        "confianca": resposta_segura["confianca"],
    }


def limpar_historico(telefone: str) -> None:
    telefone_limpo = telefone.strip()
    _historicos.pop(telefone_limpo, None)
    _estados_reserva.pop(telefone_limpo, None)


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

    if _nome_parece_valido(nome_cliente):
        estado["nome_cliente"] = nome_cliente.strip()

    if _eh_recusa_reserva(mensagem_cliente):
        _estados_reserva.pop(telefone_limpo, None)
        return {
            "texto": MENSAGEM_RECUSA_RESERVA,
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "sem_interesse",
            "confianca": 1.0,
        }

    if status_modelo == "cancelada" or _eh_cancelamento_cliente(mensagem_cliente):
        _estados_reserva.pop(telefone_limpo, None)
        return {
            "texto": interpretacao["texto"] or "Tudo bem, nao vou seguir com essa reserva.",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "cancelada",
            "confianca": interpretacao["confianca"],
        }

    if _pergunta_data_disponivel(mensagem_cliente):
        estado["aguardando_confirmacao"] = False
        _definir_campo_pendente(estado, "data_reserva")
        return {
            "texto": "Consigo verificar a data que você preferir. Me fala o dia que você quer reservar.",
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
        return {
            "texto": _mensagem_campo_invalido(campo, estado, telefone_limpo),
            "reserva_confirmada": False,
            "dados_reserva": dados_estado,
            "status_reserva": "em_coleta",
            "confianca": interpretacao["confianca"],
        }

    if faltantes:
        campo = faltantes[0]
        _definir_campo_pendente(estado, campo)
        return {
            "texto": _mensagem_pedir_campo(campo, estado),
            "reserva_confirmada": False,
            "dados_reserva": dados_estado,
            "status_reserva": "em_coleta",
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
        if pessoas is not None and 1 <= pessoas <= 30:
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
        }

    reserva = payload.get("reserva")
    reserva_dict = reserva if isinstance(reserva, dict) else {}
    status = str(reserva_dict.get("status") or payload.get("status_reserva") or "em_coleta").strip().lower()
    dados_reserva = _dados_reserva_de_json(reserva_dict)
    texto_cliente = str(payload.get("resposta_cliente") or payload.get("texto") or "").strip()
    if not texto_cliente:
        texto_cliente = _resposta_contingencia()

    reserva_confirmada = status == "confirmada" and _dados_reserva_minimos(dados_reserva)
    if not reserva_confirmada and bool(reserva_dict.get("confirmada")):
        reserva_confirmada = _dados_reserva_minimos(dados_reserva)

    return {
        "texto": texto_cliente,
        "reserva_confirmada": reserva_confirmada,
        "dados_reserva": dados_reserva,
        "status_reserva": status,
        "confianca": _confianca(payload.get("confianca") or reserva_dict.get("confianca")),
    }


def _atualizar_estado_reserva(
    estado: EstadoReserva,
    *,
    mensagem_cliente: str,
    dados_modelo: Mapping[str, Any],
    invalidos: set[str],
) -> None:
    campo_pendente = _campo_aguardado(estado)
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
        if 1 <= pessoas_solicitadas <= 30:
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
    if campo_pendente != "pessoas" and tem_sinal_data and not dados_usuario.get("data_reserva"):
        invalidos.add("data_reserva")

    if _pode_aceitar_dado_modelo("data_reserva", mensagem_cliente, estado):
        _definir_data_estado(
            estado,
            dados_modelo.get("data_reserva"),
            invalidos=invalidos,
            valor_original=mensagem_cliente,
            fonte="modelo",
            permitir_sobrescrever=False,
        )
    if _pode_aceitar_dado_modelo("horario", mensagem_cliente, estado):
        _definir_horario_estado(estado, dados_modelo.get("horario"), invalidos=invalidos)
    if _pode_aceitar_dado_modelo("pessoas", mensagem_cliente, estado):
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


def _definir_pessoas_estado(estado: EstadoReserva, valor: Any, *, invalidos: set[str]) -> None:
    try:
        pessoas = int(valor)
    except (TypeError, ValueError):
        return
    if not 1 <= pessoas <= 30:
        invalidos.add("pessoas")
        return
    estado["pessoas"] = pessoas


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


def _pode_aceitar_dado_modelo(campo: str, mensagem_cliente: str, estado: EstadoReserva) -> bool:
    campo_aguardado = _campo_aguardado(estado)
    if campo_aguardado in {"data_reserva", "horario", "pessoas"}:
        return campo == campo_aguardado
    if campo_aguardado == "confirmacao":
        return False
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
    if campo == "data_reserva":
        return "Essa data já passou. Me fala uma data a partir de hoje para eu verificar a reserva."
    if campo == "horario":
        return _mensagem_horario_fora_funcionamento()
    if campo == "pessoas":
        return "Esse numero de pessoas e maior do que consigo confirmar por aqui. Para quantas pessoas sera a reserva?"
    return _mensagem_pedir_campo(campo, estado)


def _mensagem_pedir_campo(campo: str, estado: EstadoReserva) -> str:
    if campo == "data_reserva":
        return "Perfeito, para qual data voce quer fazer a reserva?"
    if campo == "horario":
        if estado.get("horario_invalido"):
            return _mensagem_horario_fora_funcionamento()
        if estado.get("data_reserva") and estado.get("pessoas"):
            return "Perfeito, tenho a data e a quantidade de pessoas. Só falta o horário. Qual horário você prefere?"
        if estado.get("data_reserva"):
            return "Perfeito, tenho a data. Qual horário você prefere?"
        return "Perfeito, qual horário você prefere?"
    if campo == "pessoas":
        return "Perfeito, para quantas pessoas será a reserva?"
    if campo == "nome_cliente":
        return "Perfeito, só falta seu nome para deixar a reserva certinha. Qual nome devo colocar?"
    return "Perfeito, antes de confirmar preciso completar os dados da reserva."


def _mensagem_horario_fora_funcionamento() -> str:
    abertura, fechamento = _horario_funcionamento_texto()
    return (
        "Esse horário fica fora do nosso funcionamento, que é das "
        f"{abertura} às {fechamento}. Qual horário dentro desse período você prefere?"
    )


def _mensagem_confirmacao_previa(dados: Mapping[str, Any]) -> str:
    return (
        "Perfeito, só confirmando: reserva para "
        f"{_formatar_data_cliente(str(dados.get('data_reserva') or ''))}, "
        f"às {dados.get('horario')}, para {dados.get('pessoas')} pessoas. Posso confirmar?"
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
    abertura = os.getenv("RESERVA_HORARIO_INICIO", "10:00").strip() or "10:00"
    fechamento = os.getenv("RESERVA_HORARIO_FIM", "23:59").strip() or "23:59"
    if _horario_para_minutos(abertura) is None:
        abertura = "10:00"
    if _horario_para_minutos(fechamento) is None:
        fechamento = "23:59"
    return abertura, fechamento


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
    restaurante = os.getenv("NOME_RESTAURANTE", "o restaurante").strip()
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
            "Pergunte data e horário juntos na primeira pergunta. "
            "Depois pergunte o total de pessoas. "
            "Nunca confirme uma reserva sem data, horario, quantidade de pessoas, nome e telefone. "
            "Se o cliente disser sim mas faltar algum campo, pergunte o campo faltante. "
            "Se o horario ou a quantidade forem invalidos, nao use esse valor. "
            "Antes da confirmacao final, envie um resumo com data, horario e pessoas e pergunte se pode confirmar. "
            "So use status confirmada depois que o cliente confirmar esse resumo. "
            "Responda sempre e somente em JSON válido, sem markdown, neste formato: "
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

    horario = reserva.get("horario")
    if isinstance(horario, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", horario):
        dados["horario"] = horario

    pessoas = _pessoas_json(reserva.get("pessoas"))
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
    return pessoas if 1 <= pessoas <= 30 else None


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

    match_as = re.search(r"\bas\s+(\d{1,2})(?:\s*horas?)?\b", texto_normalizado)
    if match_as:
        hora = int(match_as.group(1))
        if 0 <= hora <= 23:
            return f"{hora:02d}:00"

    if permitir_numero_isolado:
        match_isolado = re.fullmatch(r"\D*(\d{1,2})\D*", texto_normalizado)
        if match_isolado:
            hora = int(match_isolado.group(1))
            if 0 <= hora <= 23:
                return f"{hora:02d}:00"

    match_periodo_normalizado = re.search(
        r"\b(\d{1,2})\s*(?:da|de)\s*(manha|tarde|noite)\b",
        texto_normalizado,
    )
    if match_periodo_normalizado:
        hora = int(match_periodo_normalizado.group(1))
        periodo = match_periodo_normalizado.group(2)
        if periodo in {"tarde", "noite"} and 1 <= hora <= 11:
            hora += 12
        if 0 <= hora <= 23:
            return f"{hora:02d}:00"

    match_periodo = re.search(
        r"\b(\d{1,2})\s*(?:da|de)\s*(manha|manhã|tarde|noite)\b",
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
    if pessoas_solicitadas is not None and 1 <= pessoas_solicitadas <= 30:
        return pessoas_solicitadas
    return None


def _extrair_pessoas_solicitadas(texto: str, *, permitir_numero_isolado: bool) -> int | None:
    texto_normalizado = _normalizar_busca(texto)
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
        match_isolado = re.fullmatch(r"\D*(\d{1,4})\D*", texto_normalizado)
        if match_isolado:
            return int(match_isolado.group(1))

    for palavra, numero in NUMEROS_POR_EXTENSO.items():
        padrao = rf"\b{re.escape(palavra)}\s*(?:pessoas|pessoa|convidados|lugares)\b"
        if re.search(padrao, texto_normalizado):
            return numero

    return None
