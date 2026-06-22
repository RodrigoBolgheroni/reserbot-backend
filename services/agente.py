from __future__ import annotations

import logging
import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Final, Literal, TypedDict


logger = logging.getLogger(__name__)

FLAG_RESERVA_CONFIRMADA: Final[str] = "RESERVA_CONFIRMADA"
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


_historicos: dict[str, list[Mensagem]] = {}


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
    reserva_confirmada = interpretacao["reserva_confirmada"]
    texto_cliente = interpretacao["texto"]
    dados_reserva = interpretacao["dados_reserva"]

    if not dados_reserva:
        dados_reserva = extrair_dados_reserva(
            mensagem_cliente=mensagem_limpa,
            resposta_agente=texto_cliente,
        )

    if texto_cliente:
        historico.append({"role": "assistant", "content": texto_cliente})
        _limitar_historico(historico)

    if reserva_confirmada:
        limpar_historico(telefone_limpo)

    return {
        "texto": texto_cliente,
        "reserva_confirmada": reserva_confirmada,
        "dados_reserva": dados_reserva,
        "status_reserva": interpretacao["status_reserva"],
        "confianca": interpretacao["confianca"],
    }


def limpar_historico(telefone: str) -> None:
    _historicos.pop(telefone.strip(), None)


def contem_flag_reserva(texto: str) -> bool:
    return FLAG_RESERVA_CONFIRMADA in texto


def remover_flag_reserva(texto: str) -> str:
    texto_sem_flag = texto.replace(FLAG_RESERVA_CONFIRMADA, "")
    return re.sub(r"\s+", " ", texto_sem_flag).strip()


def extrair_dados_reserva(
    mensagem_cliente: str,
    resposta_agente: str,
) -> DadosReserva:
    texto = f"{mensagem_cliente}\n{resposta_agente}"
    dados: DadosReserva = {}

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
    hoje = date.today().strftime("%d/%m/%Y")
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
            "Quando tiver tudo confirmado pelo cliente, finalize naturalmente. "
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
    return bool(dados.get("data_reserva") and dados.get("horario") and dados.get("pessoas"))


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


def _extrair_data(texto: str) -> str | None:
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
        ano = date.today().year
        if ano_raw:
            ano = int(ano_raw)
            if ano < 100:
                ano += 2000

        return _formatar_data_futura(dia, mes, ano)

    match_dia = re.search(r"\bdia\s+(\d{1,2})\b", texto, flags=re.IGNORECASE)
    if match_dia:
        hoje = date.today()
        dia = int(match_dia.group(1))
        return _formatar_data_futura(dia, hoje.month, hoje.year)

    return None


def _extrair_data_relativa(texto: str) -> str | None:
    texto_normalizado = texto.lower()
    hoje = date.today()

    if re.search(r"\bhoje\b", texto_normalizado):
        return hoje.isoformat()

    if re.search(r"\bamanha\b|\bamanhã\b", texto_normalizado):
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
    mes_texto = match.group(2).lower()
    mes = MESES.get(mes_texto)
    if mes is None:
        return None

    return _formatar_data_futura(dia, mes, date.today().year)


def _formatar_data_futura(dia: int, mes: int, ano: int) -> str | None:
    try:
        data_reserva = date(ano, mes, dia)
    except ValueError:
        return None

    hoje = date.today()
    if data_reserva < hoje and ano == hoje.year:
        try:
            data_reserva = date(ano + 1, mes, dia)
        except ValueError:
            return None

    return data_reserva.isoformat()


def _extrair_horario(texto: str) -> str | None:
    match = re.search(r"\b([01]?\d|2[0-3])[:hH]([0-5]\d)?\b", texto)
    if match:
        hora = int(match.group(1))
        minuto = int(match.group(2) or 0)
        return f"{hora:02d}:{minuto:02d}"

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
    padroes = (
        r"\b(\d{1,2})\s*(?:pessoas|pessoa|convidados|lugares)\b",
        r"\bmesa\s+para\s+(\d{1,2})\b",
        r"\bpara\s+(\d{1,2})\b",
    )

    for padrao in padroes:
        match = re.search(padrao, texto, flags=re.IGNORECASE)
        if match:
            pessoas = int(match.group(1))
            if 1 <= pessoas <= 30:
                return pessoas

    for palavra, numero in NUMEROS_POR_EXTENSO.items():
        padrao = rf"\b{re.escape(palavra)}\s*(?:pessoas|pessoa|convidados|lugares)\b"
        if re.search(padrao, texto, flags=re.IGNORECASE):
            return numero

    return None
