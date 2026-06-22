from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, TypedDict

import pandas as pd


logger = logging.getLogger(__name__)

ROOT_DIR: Final[Path] = Path(__file__).resolve().parents[1]
PLANILHA_PADRAO: Final[Path] = ROOT_DIR / "data" / "aniversariantes.xlsx"
COLUNAS_INTERNAS: Final[tuple[str, str, str]] = ("nome", "telefone", "aniversario")


class Aniversariante(TypedDict):
    nome: str
    telefone: str
    aniversario: str


def obter_aniversariantes_do_dia(
    caminho_planilha: str | Path = PLANILHA_PADRAO,
    data_referencia: date | None = None,
) -> list[Aniversariante]:
    data_base = data_referencia or date.today()
    planilha = _ler_planilha(Path(caminho_planilha))
    if planilha is None:
        return []

    aniversariantes: list[Aniversariante] = []

    for _, linha in planilha.iterrows():
        nome = _normalizar_nome(linha["nome"])
        if not _nome_valido(nome):
            continue

        telefone = normalizar_telefone(linha["telefone"])
        if telefone is None:
            continue

        dia_mes = _extrair_dia_mes(linha["aniversario"])
        if dia_mes != (data_base.day, data_base.month):
            continue

        aniversariantes.append(
            {
                "nome": nome,
                "telefone": telefone,
                "aniversario": f"{dia_mes[0]:02d}/{dia_mes[1]:02d}",
            }
        )

    return aniversariantes


def ler_aniversariantes_do_dia(
    caminho_planilha: str | Path = PLANILHA_PADRAO,
    data_referencia: date | None = None,
) -> list[Aniversariante]:
    return obter_aniversariantes_do_dia(caminho_planilha, data_referencia)


def normalizar_telefone(valor: Any) -> str | None:
    if _valor_vazio(valor):
        return None

    digitos = _extrair_digitos_telefone(valor)
    if digitos is None:
        return None

    if len(digitos) in (10, 11):
        digitos = f"55{digitos}"

    if len(digitos) in (12, 13) and digitos.startswith("55"):
        return digitos

    return None


def _ler_planilha(caminho: Path) -> pd.DataFrame | None:
    try:
        if not caminho.exists() or caminho.stat().st_size == 0:
            logger.warning("Planilha nao encontrada ou vazia: %s.", caminho)
            return None

        dados = pd.read_excel(caminho, engine="openpyxl")
    except Exception:
        logger.exception("Falha ao ler planilha %s.", caminho)
        return None

    if dados.empty:
        return pd.DataFrame(columns=COLUNAS_INTERNAS)

    if len(dados.columns) < 3:
        logger.warning("Planilha %s precisa ter ao menos 3 colunas.", caminho)
        return None

    dados = dados.iloc[:, :3].copy()
    dados.columns = COLUNAS_INTERNAS
    return dados


def _normalizar_nome(valor: Any) -> str:
    if _valor_vazio(valor):
        return ""

    return re.sub(r"\s+", " ", str(valor).strip())


def _nome_valido(nome: str) -> bool:
    letras = sum(1 for caractere in nome if caractere.isalpha())
    return letras >= 3


def _extrair_digitos_telefone(valor: Any) -> str | None:
    if isinstance(valor, bool):
        return None

    if isinstance(valor, int):
        return str(valor)

    if isinstance(valor, float):
        if not valor.is_integer():
            return None
        return str(int(valor))

    texto = str(valor).strip()
    if not texto or "," in texto or any(caractere.isalpha() for caractere in texto):
        return None

    if texto.endswith(".0") and texto[:-2].isdigit():
        return texto[:-2]

    return re.sub(r"\D", "", texto)


def _extrair_dia_mes(valor: Any) -> tuple[int, int] | None:
    if _valor_vazio(valor):
        return None

    if isinstance(valor, datetime):
        return valor.day, valor.month

    if isinstance(valor, date):
        return valor.day, valor.month

    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        return _dia_mes_de_numero_excel(valor)

    texto = str(valor).strip()
    match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})(?:[/-]\d{2,4})?", texto)
    if match:
        dia = int(match.group(1))
        mes = int(match.group(2))
        if _dia_mes_valido(dia, mes):
            return dia, mes
        return None

    return _dia_mes_de_texto(texto)


def _dia_mes_de_numero_excel(valor: int | float) -> tuple[int, int] | None:
    try:
        data_convertida = pd.to_datetime(valor, unit="D", origin="1899-12-30")
    except (OverflowError, TypeError, ValueError):
        return None

    if pd.isna(data_convertida):
        return None

    return int(data_convertida.day), int(data_convertida.month)


def _dia_mes_de_texto(texto: str) -> tuple[int, int] | None:
    try:
        data_convertida = pd.to_datetime(texto, dayfirst=True, errors="coerce")
    except (OverflowError, TypeError, ValueError):
        return None

    if pd.isna(data_convertida):
        return None

    return int(data_convertida.day), int(data_convertida.month)


def _dia_mes_valido(dia: int, mes: int) -> bool:
    try:
        date(2000, mes, dia)
    except ValueError:
        return False
    return True


def _valor_vazio(valor: Any) -> bool:
    if valor is None:
        return True

    try:
        resultado = pd.isna(valor)
    except (TypeError, ValueError):
        return False

    if resultado is pd.NA:
        return True

    try:
        return bool(resultado)
    except (TypeError, ValueError):
        return False
