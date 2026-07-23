from __future__ import annotations

import os
import re
import hashlib
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from services import repositorio_config_restaurante as repositorio


logger = logging.getLogger(__name__)

TABELA_ESTABELECIMENTOS = repositorio.TABELA_ESTABELECIMENTOS
TABELA_HORARIOS_FUNCIONAMENTO = repositorio.TABELA_HORARIOS_FUNCIONAMENTO
TABELA_CONFIGURACOES_RESERVA = repositorio.TABELA_CONFIGURACOES_RESERVA
TABELA_ESPACOS = repositorio.TABELA_ESPACOS
TABELA_FAQ_CONTEUDOS = repositorio.TABELA_FAQ_CONTEUDOS
CACHE_TTL_SUCESSO_SEGUNDOS = 60.0
CACHE_TTL_FALLBACK_SEGUNDOS = 5.0

DIAS_SEMANA: tuple[tuple[int, str], ...] = (
    (1, "segunda"),
    (2, "terca"),
    (3, "quarta"),
    (4, "quinta"),
    (5, "sexta"),
    (6, "sabado"),
    (0, "domingo"),
)

ENV_KEYS_CONFIG: tuple[str, ...] = (
    "NOME_RESTAURANTE",
    "RESTAURANTE_ENDERECO",
    "RESTAURANTE_DIAS_FUNCIONAMENTO",
    "RESERVA_HORARIO_INICIO",
    "RESERVA_HORARIO_FIM",
    "RESERVA_HORARIOS_ACEITOS",
    "RESERVA_LIMITE_PESSOAS",
    "RESERVA_ANTECEDENCIA_MINIMA",
    "RESTAURANTE_FORMAS_PAGAMENTO",
    "RESTAURANTE_POLITICA_CANCELAMENTO",
    "RESTAURANTE_TELEFONE_ATENDIMENTO",
    "RESTAURANTE_ESTACIONAMENTO",
    "RESTAURANTE_INFO_ANIVERSARIO",
    "RESTAURANTE_REGRAS_RESERVAS_IMEDIATAS",
    "RESTAURANTE_INFORMACOES_GERAIS",
    "RESTAURANTE_WHATSAPP",
    "RESTAURANTE_PONTO_REFERENCIA",
    "TIMEZONE",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_ANON_KEY",
)

_cache_lock = threading.RLock()
_cache_config: ConfigRestaurante | None = None
_cache_expira_em = 0.0
_cache_assinatura = ""


@dataclass(frozen=True)
class HorarioFuncionamento:
    dia_semana: int
    fechado: bool
    horario_abertura: str
    horario_fechamento: str
    observacao: str = ""
    ativo: bool = True


@dataclass(frozen=True)
class EspacoRestaurante:
    id: str
    nome: str
    descricao: str
    capacidade_maxima: int | None
    permite_preferencia: bool
    regras: str
    ativo: bool = True


@dataclass(frozen=True)
class FaqConteudo:
    categoria: str
    titulo: str
    conteudo: str
    tags: tuple[str, ...]
    ativo: bool = True


@dataclass(frozen=True)
class ConfigRestaurante:
    nome: str
    endereco: str
    dias_funcionamento: str
    horario_abertura: str
    horario_fechamento: str
    horarios_aceitos_reserva: str
    limite_pessoas_reserva: int
    antecedencia_minima: str
    formas_pagamento: str
    politica_cancelamento: str
    telefone_atendimento: str
    estacionamento: str
    aniversario: str
    regras_reservas_imediatas: str
    informacoes_gerais: str
    whatsapp: str = ""
    ponto_referencia: str = ""
    timezone: str = "America/Sao_Paulo"
    estabelecimento_id: str = ""
    fonte: str = "env"
    quantidade_minima_reserva: int | None = None
    horarios_permitidos_reserva: tuple[str, ...] = ()
    taxa_valor: float | None = None
    taxa_convertida_consumacao: bool = False
    prazo_cancelamento_horas: int | None = None
    pix_chave: str = ""
    pix_titular: str = ""
    exige_comprovante: bool = False
    tolerancia_atraso_minutos: int | None = None
    horarios: tuple[HorarioFuncionamento, ...] = ()
    espacos: tuple[EspacoRestaurante, ...] = ()
    faq_conteudos: tuple[FaqConteudo, ...] = ()


def obter_config() -> ConfigRestaurante:
    global _cache_config, _cache_expira_em, _cache_assinatura
    assinatura = _assinatura_cache()
    agora = time.monotonic()
    with _cache_lock:
        if _cache_config is not None and _cache_assinatura == assinatura and agora < _cache_expira_em:
            return _cache_config

        config = _carregar_config_sem_cache()
        ttl = CACHE_TTL_SUCESSO_SEGUNDOS if config.fonte == "supabase" else CACHE_TTL_FALLBACK_SEGUNDOS
        _cache_config = config
        _cache_assinatura = assinatura
        _cache_expira_em = time.monotonic() + ttl
        return config


def limpar_cache_config() -> None:
    with _cache_lock:
        global _cache_config, _cache_expira_em, _cache_assinatura
        _cache_config = None
        _cache_expira_em = 0.0
        _cache_assinatura = ""


def _carregar_config_sem_cache() -> ConfigRestaurante:
    config_supabase = _obter_config_supabase()
    if config_supabase is not None:
        return config_supabase
    return _obter_config_env()


def _obter_config_env() -> ConfigRestaurante:
    abertura = _horario_env("RESERVA_HORARIO_INICIO", "10:00")
    fechamento = _horario_env("RESERVA_HORARIO_FIM", "23:59")
    return ConfigRestaurante(
        nome=_texto_env("NOME_RESTAURANTE", "o restaurante"),
        endereco=_texto_env("RESTAURANTE_ENDERECO", "Endereco ainda nao configurado."),
        dias_funcionamento=_texto_env("RESTAURANTE_DIAS_FUNCIONAMENTO", "todos os dias"),
        horario_abertura=abertura,
        horario_fechamento=fechamento,
        horarios_aceitos_reserva=_texto_env(
            "RESERVA_HORARIOS_ACEITOS",
            f"entre {abertura} e {fechamento}",
        ),
        limite_pessoas_reserva=_inteiro_env("RESERVA_LIMITE_PESSOAS", 30, minimo=1, maximo=500),
        antecedencia_minima=_texto_env("RESERVA_ANTECEDENCIA_MINIMA", "sem antecedencia minima configurada"),
        formas_pagamento=_texto_env(
            "RESTAURANTE_FORMAS_PAGAMENTO",
            "dinheiro, Pix, cartao de debito e cartao de credito",
        ),
        politica_cancelamento=_texto_env(
            "RESTAURANTE_POLITICA_CANCELAMENTO",
            "Para cancelar, avise a equipe pelo WhatsApp do restaurante.",
        ),
        telefone_atendimento=_texto_env("RESTAURANTE_TELEFONE_ATENDIMENTO", ""),
        estacionamento=_texto_env(
            "RESTAURANTE_ESTACIONAMENTO",
            "Ainda nao tenho informacao de estacionamento configurada.",
        ),
        aniversario=_texto_env(
            "RESTAURANTE_INFO_ANIVERSARIO",
            "Para aniversarios, posso registrar a reserva e a equipe confirma os detalhes se precisar.",
        ),
        regras_reservas_imediatas=_texto_env(
            "RESTAURANTE_REGRAS_RESERVAS_IMEDIATAS",
            "Reservas em cima da hora precisam de confirmacao da equipe.",
        ),
        informacoes_gerais=_texto_env("RESTAURANTE_INFORMACOES_GERAIS", ""),
        whatsapp=_texto_env("RESTAURANTE_WHATSAPP", ""),
        ponto_referencia=_texto_env("RESTAURANTE_PONTO_REFERENCIA", ""),
        timezone=_texto_env("TIMEZONE", "America/Sao_Paulo"),
    )


def horario_funcionamento(
    data_reserva: str | date | None = None,
    *,
    config: ConfigRestaurante | None = None,
) -> tuple[str, str]:
    config = config or obter_config()
    horario = horario_para_data(config, data_reserva)
    if horario is not None and not horario.fechado and horario.horario_abertura and horario.horario_fechamento:
        return horario.horario_abertura, horario.horario_fechamento
    return config.horario_abertura, config.horario_fechamento


def horario_para_data(config: ConfigRestaurante, data_reserva: str | date | None) -> HorarioFuncionamento | None:
    data = _parse_data(data_reserva)
    if data is None:
        return None
    return horario_para_dia_semana(config, _dia_semana_supabase(data))


def horario_para_dia_semana(config: ConfigRestaurante, dia_semana: int) -> HorarioFuncionamento | None:
    for horario in config.horarios:
        if horario.ativo and horario.dia_semana == dia_semana:
            return horario
    return None


def horario_valido_para_chegada(
    horario: str,
    data_reserva: str | date | None,
    *,
    config: ConfigRestaurante | None = None,
) -> bool:
    minuto = _horario_para_minutos(horario)
    if minuto is None:
        return False
    config = config or obter_config()
    data = _parse_data(data_reserva)
    if data is None:
        return _horario_dentro_funcionamento_sem_data(minuto, config.horario_abertura, config.horario_fechamento)

    horario_dia = horario_para_data(config, data)
    if horario_dia is None:
        return _horario_dentro_funcionamento_sem_data(minuto, config.horario_abertura, config.horario_fechamento)
    if horario_dia and not horario_dia.fechado and _horario_no_dia_de_abertura(minuto, horario_dia):
        return True

    horario_anterior = horario_para_data(config, data - timedelta(days=1))
    return bool(horario_anterior and not horario_anterior.fechado and _horario_na_extensao_madrugada(minuto, horario_anterior))


def fechado_na_data(
    data_reserva: str | date | None,
    *,
    config: ConfigRestaurante | None = None,
) -> bool:
    config = config or obter_config()
    horario = horario_para_data(config, data_reserva)
    return bool(horario and horario.fechado)


def limite_pessoas_reserva(*, config: ConfigRestaurante | None = None) -> int:
    return (config or obter_config()).limite_pessoas_reserva


def _obter_config_supabase() -> ConfigRestaurante | None:
    resultado = repositorio.carregar_configuracao_bruta()
    if not resultado.get("ok"):
        logger.warning(
            "Configuracao estruturada nao carregada; usando env vars. tipo=%s erro=%s",
            resultado.get("erro_tipo", ""),
            resultado.get("erro", ""),
        )
        return None

    estabelecimento = resultado.get("estabelecimento") if isinstance(resultado.get("estabelecimento"), Mapping) else {}
    estabelecimento_id = str(estabelecimento.get("id") or "").strip()
    if not estabelecimento_id:
        logger.warning("Configuracao estruturada sem estabelecimento_id; usando env vars.")
        return None

    horarios = _mapear_horarios(resultado.get("horarios"))
    configuracao = dict(resultado.get("configuracao_reserva") or {})
    espacos = _mapear_espacos(resultado.get("espacos"))
    faq_conteudos = _mapear_faq_conteudos(resultado.get("faq_conteudos"))

    fallback = _obter_config_env()
    abertura, fechamento = _horario_global(horarios) or (fallback.horario_abertura, fallback.horario_fechamento)
    horarios_permitidos = _lista_horarios_json(configuracao.get("horarios_permitidos"))
    politica_cancelamento = _texto(configuracao.get("politica_cancelamento")) or fallback.politica_cancelamento
    instrucoes_reserva = _texto(configuracao.get("instrucoes_reserva"))

    return ConfigRestaurante(
        nome=_texto(estabelecimento.get("nome")) or fallback.nome,
        endereco=_texto(estabelecimento.get("endereco")) or fallback.endereco,
        dias_funcionamento=_formatar_horarios_funcionamento(horarios) or fallback.dias_funcionamento,
        horario_abertura=abertura,
        horario_fechamento=fechamento,
        horarios_aceitos_reserva=_formatar_lista_horarios(horarios_permitidos) or fallback.horarios_aceitos_reserva,
        limite_pessoas_reserva=_inteiro(configuracao.get("quantidade_maxima_automatica"), fallback.limite_pessoas_reserva, minimo=1, maximo=500),
        antecedencia_minima=fallback.antecedencia_minima,
        formas_pagamento=fallback.formas_pagamento,
        politica_cancelamento=politica_cancelamento,
        telefone_atendimento=_texto(estabelecimento.get("telefone")) or fallback.telefone_atendimento,
        estacionamento=_conteudo_faq(faq_conteudos, "estacionamento") or fallback.estacionamento,
        aniversario=_conteudo_faq(faq_conteudos, "aniversario") or fallback.aniversario,
        regras_reservas_imediatas=instrucoes_reserva or fallback.regras_reservas_imediatas,
        informacoes_gerais=fallback.informacoes_gerais,
        whatsapp=_texto(estabelecimento.get("whatsapp")) or fallback.whatsapp,
        ponto_referencia=_texto(estabelecimento.get("ponto_referencia")) or fallback.ponto_referencia,
        timezone=_texto(estabelecimento.get("timezone")) or fallback.timezone,
        estabelecimento_id=estabelecimento_id,
        fonte="supabase",
        quantidade_minima_reserva=_inteiro_opcional(configuracao.get("quantidade_minima")),
        horarios_permitidos_reserva=tuple(horarios_permitidos),
        taxa_valor=_monetario(configuracao.get("taxa_valor")),
        taxa_convertida_consumacao=_booleano(configuracao.get("taxa_convertida_consumacao")),
        prazo_cancelamento_horas=_inteiro_opcional(configuracao.get("prazo_cancelamento_horas")),
        pix_chave=_texto(configuracao.get("pix_chave")),
        pix_titular=_texto(configuracao.get("pix_titular")),
        exige_comprovante=_booleano(configuracao.get("exige_comprovante")),
        tolerancia_atraso_minutos=_inteiro_opcional(configuracao.get("tolerancia_atraso_minutos")),
        horarios=tuple(horarios),
        espacos=tuple(espacos),
        faq_conteudos=tuple(faq_conteudos),
    )


def _assinatura_cache() -> str:
    valores = "\n".join(f"{chave}={os.getenv(chave, '')}" for chave in ENV_KEYS_CONFIG)
    return hashlib.sha256(valores.encode("utf-8")).hexdigest()


def _horario_dentro_funcionamento_sem_data(minuto: int, abertura: str, fechamento: str) -> bool:
    inicio = _horario_para_minutos(abertura)
    fim = _horario_para_minutos(fechamento)
    if inicio is None or fim is None:
        return True
    if inicio < fim:
        return inicio <= minuto <= fim
    return minuto >= inicio or minuto <= fim


def _horario_no_dia_de_abertura(minuto: int, horario: HorarioFuncionamento) -> bool:
    inicio = _horario_para_minutos(horario.horario_abertura)
    fim = _horario_para_minutos(horario.horario_fechamento)
    if inicio is None or fim is None:
        return False
    if inicio < fim:
        return inicio <= minuto <= fim
    return minuto >= inicio


def _horario_na_extensao_madrugada(minuto: int, horario: HorarioFuncionamento) -> bool:
    inicio = _horario_para_minutos(horario.horario_abertura)
    fim = _horario_para_minutos(horario.horario_fechamento)
    if inicio is None or fim is None or fim > inicio:
        return False
    return 0 <= minuto <= fim


def _mapear_horarios(valor: Any) -> list[HorarioFuncionamento]:
    horarios: list[HorarioFuncionamento] = []
    for item in _lista_dicts(valor):
        dia = _inteiro(item.get("dia_semana"), -1, minimo=-1, maximo=6)
        if dia < 0:
            continue
        horarios.append(
            HorarioFuncionamento(
                dia_semana=dia,
                fechado=_booleano(item.get("fechado")),
                horario_abertura=_normalizar_horario(item.get("horario_abertura")),
                horario_fechamento=_normalizar_horario(item.get("horario_fechamento")),
                observacao=_texto(item.get("observacao")),
                ativo=_booleano(item.get("ativo"), padrao=True),
            )
        )
    return horarios


def _mapear_espacos(valor: Any) -> list[EspacoRestaurante]:
    espacos: list[EspacoRestaurante] = []
    for item in _lista_dicts(valor):
        espacos.append(
            EspacoRestaurante(
                id=_texto(item.get("id")),
                nome=_texto(item.get("nome")),
                descricao=_texto(item.get("descricao")),
                capacidade_maxima=_inteiro_opcional(item.get("capacidade_maxima")),
                permite_preferencia=_booleano(item.get("permite_preferencia"), padrao=True),
                regras=_texto(item.get("regras")),
                ativo=_booleano(item.get("ativo"), padrao=True),
            )
        )
    return espacos


def _mapear_faq_conteudos(valor: Any) -> list[FaqConteudo]:
    faqs: list[FaqConteudo] = []
    for item in _lista_dicts(valor):
        faqs.append(
            FaqConteudo(
                categoria=_texto(item.get("categoria")),
                titulo=_texto(item.get("titulo")),
                conteudo=_texto(item.get("conteudo")),
                tags=tuple(_lista_textos(item.get("tags"))),
                ativo=_booleano(item.get("ativo"), padrao=True),
            )
        )
    return faqs


def _formatar_horarios_funcionamento(horarios: list[HorarioFuncionamento]) -> str:
    por_dia = {horario.dia_semana: horario for horario in horarios if horario.ativo}
    partes: list[str] = []
    for dia, nome in DIAS_SEMANA:
        horario = por_dia.get(dia)
        if horario is None:
            continue
        if horario.fechado:
            partes.append(f"{nome}: fechado")
            continue
        if horario.horario_abertura and horario.horario_fechamento:
            partes.append(f"{nome}: {horario.horario_abertura}-{horario.horario_fechamento}")
    return "; ".join(partes)


def _horario_global(horarios: list[HorarioFuncionamento]) -> tuple[str, str] | None:
    aberturas: list[int] = []
    fechamentos: list[tuple[int, str]] = []
    for horario in horarios:
        if not horario.ativo or horario.fechado:
            continue
        abertura = _horario_para_minutos(horario.horario_abertura)
        fechamento = _horario_para_minutos(horario.horario_fechamento)
        if abertura is None or fechamento is None:
            continue
        aberturas.append(abertura)
        fechamento_ordenado = fechamento + (24 * 60 if fechamento <= abertura else 0)
        fechamentos.append((fechamento_ordenado, horario.horario_fechamento))
    if not aberturas or not fechamentos:
        return None
    abertura_min = min(aberturas)
    fechamento_texto = max(fechamentos, key=lambda item: item[0])[1]
    return _minutos_para_horario(abertura_min), fechamento_texto


def _conteudo_faq(faqs: list[FaqConteudo], categoria: str) -> str:
    for faq in faqs:
        if faq.ativo and faq.categoria == categoria and faq.conteudo:
            return faq.conteudo
    return ""


def _lista_dicts(valor: Any) -> list[Mapping[str, Any]]:
    return [item for item in valor if isinstance(item, Mapping)] if isinstance(valor, list) else []


def _lista_textos(valor: Any) -> list[str]:
    if not isinstance(valor, list):
        return []
    return [_texto(item) for item in valor if _texto(item)]


def _lista_horarios_json(valor: Any) -> list[str]:
    if not isinstance(valor, list):
        return []
    horarios: list[str] = []
    for item in valor:
        horario = _normalizar_horario(item)
        if horario:
            horarios.append(horario)
    return horarios


def _formatar_lista_horarios(horarios: list[str]) -> str:
    if not horarios:
        return ""
    if len(horarios) == 1:
        return horarios[0]
    return ", ".join(horarios[:-1]) + f" e {horarios[-1]}"


def _parse_data(valor: str | date | None) -> date | None:
    if isinstance(valor, date):
        return valor
    texto = str(valor or "").strip()
    if not texto:
        return None
    try:
        return date.fromisoformat(texto[:10])
    except ValueError:
        return None


def _dia_semana_supabase(data_reserva: date) -> int:
    return (data_reserva.weekday() + 1) % 7


def _texto_env(nome: str, padrao: str) -> str:
    return os.getenv(nome, padrao).strip() or padrao


def _horario_env(nome: str, padrao: str) -> str:
    valor = _texto_env(nome, padrao)
    return valor if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", valor) else padrao


def _inteiro_env(nome: str, padrao: int, *, minimo: int, maximo: int) -> int:
    try:
        valor = int(os.getenv(nome, str(padrao)).strip())
    except (AttributeError, TypeError, ValueError):
        return padrao
    return max(minimo, min(valor, maximo))


def _texto(valor: Any) -> str:
    return str(valor or "").strip()


def _normalizar_horario(valor: Any) -> str:
    texto = _texto(valor)
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?", texto)
    if not match:
        return ""
    return f"{match.group(1)}:{match.group(2)}"


def _inteiro(valor: Any, padrao: int, *, minimo: int, maximo: int) -> int:
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return padrao
    return max(minimo, min(numero, maximo))


def _inteiro_opcional(valor: Any) -> int | None:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _monetario(valor: Any) -> float | None:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _booleano(valor: Any, *, padrao: bool = False) -> bool:
    if isinstance(valor, bool):
        return valor
    if valor in (None, ""):
        return padrao
    texto = str(valor).strip().lower()
    if texto in {"true", "t", "1", "sim", "yes"}:
        return True
    if texto in {"false", "f", "0", "nao", "no"}:
        return False
    return padrao


def _horario_para_minutos(horario: str) -> int | None:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(horario or "").strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _minutos_para_horario(minutos: int) -> str:
    minutos = minutos % (24 * 60)
    return f"{minutos // 60:02d}:{minutos % 60:02d}"
