from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, TypedDict, cast


logger = logging.getLogger(__name__)

ROOT_DIR: Final[Path] = Path(__file__).resolve().parents[1]
DATA_DIR: Final[Path] = ROOT_DIR / "data"
ENVIADOS_PATH: Final[Path] = DATA_DIR / "enviados.json"
RESERVAS_PATH: Final[Path] = DATA_DIR / "reservas.json"


class Enviado(TypedDict, total=False):
    telefone: str
    data_envio: str
    enviado_em: str
    nome: str


class Reserva(TypedDict, total=False):
    telefone: str
    nome: str
    confirmado_em: str
    data_reserva: str
    horario: str
    pessoas: int
    observacoes: str


def ler_enviados(caminho: str | Path = ENVIADOS_PATH) -> list[Enviado]:
    registros = _ler_lista_json(Path(caminho))
    return [
        cast(Enviado, registro)
        for registro in registros
        if _enviado_valido(registro)
    ]


def salvar_enviados(
    enviados: Iterable[Enviado],
    caminho: str | Path = ENVIADOS_PATH,
) -> bool:
    return _salvar_lista_json(Path(caminho), enviados)


def ler_reservas(caminho: str | Path = RESERVAS_PATH) -> list[Reserva]:
    registros = _ler_lista_json(Path(caminho))
    return [
        cast(Reserva, registro)
        for registro in registros
        if _reserva_valida(registro)
    ]


def salvar_reservas(
    reservas: Iterable[Reserva],
    caminho: str | Path = RESERVAS_PATH,
) -> bool:
    return _salvar_lista_json(Path(caminho), reservas)


def ja_enviado(
    telefone: str,
    data_referencia: date | None = None,
    caminho: str | Path = ENVIADOS_PATH,
) -> bool:
    data_envio = (data_referencia or date.today()).isoformat()
    telefone_limpo = telefone.strip()
    if not telefone_limpo:
        return False

    return any(
        enviado["telefone"] == telefone_limpo
        and enviado["data_envio"] == data_envio
        for enviado in ler_enviados(caminho)
    )


def marcar_enviado(
    telefone: str,
    nome: str = "",
    data_referencia: date | None = None,
    caminho: str | Path = ENVIADOS_PATH,
) -> bool:
    telefone_limpo = telefone.strip()
    if not telefone_limpo:
        logger.warning("Tentativa de marcar envio sem telefone.")
        return False

    data_envio = (data_referencia or date.today()).isoformat()
    enviados = [
        enviado
        for enviado in ler_enviados(caminho)
        if not (
            enviado["telefone"] == telefone_limpo
            and enviado["data_envio"] == data_envio
        )
    ]

    registro: Enviado = {
        "telefone": telefone_limpo,
        "data_envio": data_envio,
        "enviado_em": datetime.now().isoformat(timespec="seconds"),
    }

    nome_limpo = nome.strip()
    if nome_limpo:
        registro["nome"] = nome_limpo

    enviados.append(registro)
    return salvar_enviados(enviados, caminho)


def limpar_enviados(caminho: str | Path = ENVIADOS_PATH) -> bool:
    return salvar_enviados([], caminho)


def adicionar_reserva(
    reserva: Reserva,
    caminho: str | Path = RESERVAS_PATH,
) -> bool:
    telefone = reserva.get("telefone", "").strip()
    nome = reserva.get("nome", "").strip()

    if not telefone or not nome:
        logger.warning("Reserva ignorada por falta de telefone ou nome.")
        return False

    registro: Reserva = {
        **reserva,
        "telefone": telefone,
        "nome": nome,
        "confirmado_em": reserva.get(
            "confirmado_em",
            datetime.now().isoformat(timespec="seconds"),
        ),
    }

    reservas = ler_reservas(caminho)
    reservas.append(registro)
    return salvar_reservas(reservas, caminho)


def _ler_lista_json(caminho: Path) -> list[dict[str, Any]]:
    try:
        if not caminho.exists() or caminho.stat().st_size == 0:
            return []

        with caminho.open("r", encoding="utf-8") as arquivo:
            conteudo = json.load(arquivo)

        if not isinstance(conteudo, list):
            logger.warning("JSON %s deve conter uma lista.", caminho)
            return []

        return [item for item in conteudo if isinstance(item, dict)]
    except json.JSONDecodeError as erro:
        logger.warning("JSON invalido em %s: %s.", caminho, erro)
    except OSError:
        logger.exception("Falha ao ler %s.", caminho)

    return []


def _salvar_lista_json(caminho: Path, dados: Iterable[Mapping[str, Any]]) -> bool:
    temporario = caminho.with_suffix(f"{caminho.suffix}.tmp")

    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        registros = [dict(item) for item in dados]

        with temporario.open("w", encoding="utf-8") as arquivo:
            json.dump(registros, arquivo, ensure_ascii=False, indent=2)
            arquivo.write("\n")

        os.replace(temporario, caminho)
        return True
    except (OSError, TypeError):
        logger.exception("Falha ao salvar %s.", caminho)
        _remover_temporario(temporario)
        return False


def _remover_temporario(caminho: Path) -> None:
    try:
        if caminho.exists():
            caminho.unlink()
    except OSError:
        logger.debug("Nao foi possivel remover arquivo temporario %s.", caminho)


def _enviado_valido(registro: dict[str, Any]) -> bool:
    return (
        isinstance(registro.get("telefone"), str)
        and bool(registro["telefone"].strip())
        and isinstance(registro.get("data_envio"), str)
        and bool(registro["data_envio"].strip())
        and isinstance(registro.get("enviado_em"), str)
        and bool(registro["enviado_em"].strip())
    )


def _reserva_valida(registro: dict[str, Any]) -> bool:
    return (
        isinstance(registro.get("telefone"), str)
        and bool(registro["telefone"].strip())
        and isinstance(registro.get("nome"), str)
        and bool(registro["nome"].strip())
        and isinstance(registro.get("confirmado_em"), str)
        and bool(registro["confirmado_em"].strip())
    )
