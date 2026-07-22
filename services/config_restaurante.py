from __future__ import annotations

import os
import re
from dataclasses import dataclass


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


def obter_config() -> ConfigRestaurante:
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
    )


def horario_funcionamento() -> tuple[str, str]:
    config = obter_config()
    return config.horario_abertura, config.horario_fechamento


def limite_pessoas_reserva() -> int:
    return obter_config().limite_pessoas_reserva


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
