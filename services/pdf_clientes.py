from __future__ import annotations

import logging
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Final, TypedDict

from services.planilha import normalizar_telefone

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - validado pelo preflight
    PdfReader = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

DATA_RE: Final[re.Pattern[str]] = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
ESPACO_COLUNA_RE: Final[re.Pattern[str]] = re.compile(r"\s{2,}")
DIGITOS_RE: Final[re.Pattern[str]] = re.compile(r"\d+")
NUMERO_TIPO_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<numero>\d+)\s+(?P<tipo>Tipo\b.*)$", re.IGNORECASE)
HEADER_MARKERS: Final[tuple[str, ...]] = (
    "Listagem de clientes",
    "Cliente telefone",
    "NCR Colibri",
    "Software Express",
)


class InconsistenciaPDF(TypedDict):
    pagina: int
    linha: int
    campo: str
    mensagem: str
    valor: str


class ClientePDF(TypedDict, total=False):
    nome: str
    telefone: str
    telefone_raw: str
    telefones: list[str]
    telefones_raw: list[str]
    data_nascimento: str
    data_nascimento_raw: str
    aniversario_ddmm: str
    info_topo_pdf: str
    periodo_aniversario: str
    tipo: str
    regiao: str
    numero: int
    origem: str
    pagina: int
    linha: int
    importavel: bool
    inconsistencias: list[str]
    metadata: dict[str, Any]


class ResultadoExtracaoPDF(TypedDict):
    arquivo: str
    loja: str
    periodo_aniversario: str
    total_paginas: int
    total_linhas: int
    registros: list[ClientePDF]
    inconsistencias: list[InconsistenciaPDF]


def extrair_clientes_pdf(
    origem: str | Path | bytes | BinaryIO,
    nome_arquivo: str = "clientes.pdf",
) -> ResultadoExtracaoPDF:
    if PdfReader is None:
        raise RuntimeError("Dependencia pypdf ausente. Instale com: pip install pypdf")

    leitor = PdfReader(_abrir_origem(origem))
    registros: list[ClientePDF] = []
    inconsistencias: list[InconsistenciaPDF] = []
    loja = ""
    periodo = ""

    for indice_pagina, pagina in enumerate(leitor.pages, start=1):
        try:
            texto = pagina.extract_text(extraction_mode="layout") or ""
        except Exception:
            logger.exception("Falha ao extrair texto da pagina %s de %s.", indice_pagina, nome_arquivo)
            continue

        loja = loja or _extrair_loja(texto)
        periodo = periodo or _extrair_periodo(texto)

        for indice_linha, linha in enumerate(texto.splitlines(), start=1):
            registro = parse_linha_cliente_pdf(
                linha,
                pagina=indice_pagina,
                linha_numero=indice_linha,
                origem=nome_arquivo,
                loja=loja,
                periodo_aniversario=periodo,
            )
            if registro is None:
                continue

            registros.append(registro)
            for mensagem in registro.get("inconsistencias", []):
                inconsistencias.append(
                    {
                        "pagina": indice_pagina,
                        "linha": indice_linha,
                        "campo": _campo_inconsistencia(mensagem),
                        "mensagem": mensagem,
                        "valor": linha.strip(),
                    }
                )

    return {
        "arquivo": nome_arquivo,
        "loja": loja,
        "periodo_aniversario": periodo,
        "total_paginas": len(leitor.pages),
        "total_linhas": len(registros),
        "registros": registros,
        "inconsistencias": inconsistencias,
    }


def parse_linha_cliente_pdf(
    linha: str,
    *,
    pagina: int,
    linha_numero: int,
    origem: str,
    loja: str = "",
    periodo_aniversario: str = "",
) -> ClientePDF | None:
    texto = linha.strip()
    if not _linha_de_cliente(texto):
        return None

    grupos = _separar_grupos(texto)
    if len(grupos) < 4:
        return _registro_incompleto(
            texto,
            pagina=pagina,
            linha_numero=linha_numero,
            origem=origem,
            loja=loja,
            periodo_aniversario=periodo_aniversario,
            mensagem="linha com colunas insuficientes",
        )

    info_topo = _normalizar_nome(periodo_aniversario)
    data_raw = grupos[-1]
    regiao = grupos[-2]
    numero, tipo = _separar_numero_tipo(grupos[-3])
    campos_esquerda = grupos[:-3]
    if numero is None and campos_esquerda and _parece_numero_coluna(campos_esquerda[-1]):
        numero = int(campos_esquerda[-1])
        campos_esquerda = campos_esquerda[:-1]

    telefones_raw = [grupo for grupo in campos_esquerda if _parece_telefone(grupo)]
    nome_partes = [grupo for grupo in campos_esquerda if grupo not in telefones_raw]
    nome = _normalizar_nome(" ".join(nome_partes))
    telefones = _normalizar_telefones(telefones_raw)
    data_iso, aniversario_ddmm = _normalizar_data(data_raw)

    problemas: list[str] = []
    if not _nome_valido(nome):
        problemas.append("nome ausente ou com menos de 3 letras")
    if not telefones:
        problemas.append("telefone ausente ou invalido")
    if data_iso is None:
        problemas.append("data de nascimento invalida")

    metadata: dict[str, Any] = {
        "arquivo": origem,
        "loja": loja,
        "periodo_aniversario": info_topo,
        "info_topo_pdf": info_topo,
        "telefones_raw": telefones_raw,
        "linha_original": texto,
    }
    if problemas:
        metadata["inconsistencias"] = problemas

    registro: ClientePDF = {
        "nome": nome,
        "telefone_raw": telefones_raw[0] if telefones_raw else "",
        "telefones_raw": telefones_raw,
        "telefones": telefones,
        "data_nascimento_raw": data_raw,
        "aniversario_ddmm": aniversario_ddmm,
        "info_topo_pdf": info_topo,
        "periodo_aniversario": info_topo,
        "tipo": tipo,
        "regiao": regiao,
        "origem": origem,
        "pagina": pagina,
        "linha": linha_numero,
        "importavel": bool(nome and telefones),
        "inconsistencias": problemas,
        "metadata": metadata,
    }

    if telefones:
        registro["telefone"] = telefones[0]
    if data_iso:
        registro["data_nascimento"] = data_iso
    if numero is not None:
        registro["numero"] = numero

    return registro


def resumir_extracao(resultado: ResultadoExtracaoPDF, limite_preview: int = 100) -> dict[str, Any]:
    registros = resultado["registros"]
    importaveis = [registro for registro in registros if registro.get("importavel")]
    nao_importaveis = [registro for registro in registros if not registro.get("importavel")]

    return {
        "arquivo": resultado["arquivo"],
        "loja": resultado["loja"],
        "periodo_aniversario": resultado["periodo_aniversario"],
        "total_paginas": resultado["total_paginas"],
        "total_linhas": resultado["total_linhas"],
        "total_importaveis": len(importaveis),
        "total_nao_importaveis": len(nao_importaveis),
        "total_inconsistencias": len(resultado["inconsistencias"]),
        "registros_preview": registros[:limite_preview],
        "inconsistencias_preview": resultado["inconsistencias"][:limite_preview],
    }


def registros_importaveis(resultado: ResultadoExtracaoPDF) -> list[ClientePDF]:
    return [registro for registro in resultado["registros"] if registro.get("importavel")]


def _abrir_origem(origem: str | Path | bytes | BinaryIO) -> str | Path | BinaryIO:
    if isinstance(origem, bytes):
        return BytesIO(origem)
    return origem


def _linha_de_cliente(texto: str) -> bool:
    if not texto or any(marker in texto for marker in HEADER_MARKERS):
        return False
    if not DATA_RE.search(texto):
        return False
    grupos = _separar_grupos(texto)
    return bool(grupos and DATA_RE.fullmatch(grupos[-1]))


def _separar_grupos(texto: str) -> list[str]:
    return [grupo.strip() for grupo in ESPACO_COLUNA_RE.split(texto.strip()) if grupo.strip()]


def _parece_telefone(valor: str) -> bool:
    if any(caractere.isalpha() for caractere in valor):
        return False
    digitos = "".join(DIGITOS_RE.findall(valor))
    return len(digitos) >= 5


def _parece_numero_coluna(valor: str) -> bool:
    return valor.isdigit() and len(valor) <= 4


def _normalizar_telefones(valores: list[str]) -> list[str]:
    telefones: list[str] = []
    for valor in valores:
        telefone = normalizar_telefone(valor)
        if telefone and telefone not in telefones:
            telefones.append(telefone)
    return telefones


def _normalizar_nome(valor: str) -> str:
    return re.sub(r"\s+", " ", valor).strip()


def _nome_valido(nome: str) -> bool:
    return sum(1 for caractere in nome if caractere.isalpha()) >= 3


def _normalizar_data(valor: str) -> tuple[str | None, str]:
    match = DATA_RE.search(valor)
    if not match:
        return None, ""

    data_raw = match.group(0)
    aniversario_ddmm = data_raw[:5]
    try:
        return datetime.strptime(data_raw, "%d/%m/%Y").date().isoformat(), aniversario_ddmm
    except ValueError:
        return None, aniversario_ddmm


def _separar_numero_tipo(valor: str) -> tuple[int | None, str]:
    texto = _normalizar_nome(valor)
    match = NUMERO_TIPO_RE.match(texto)
    if match:
        return int(match.group("numero")), match.group("tipo")
    if texto.isdigit():
        return int(texto), ""
    return None, texto


def _registro_incompleto(
    texto: str,
    *,
    pagina: int,
    linha_numero: int,
    origem: str,
    loja: str,
    periodo_aniversario: str,
    mensagem: str,
) -> ClientePDF:
    info_topo = _normalizar_nome(periodo_aniversario)
    return {
        "nome": "",
        "telefone_raw": "",
        "telefones_raw": [],
        "telefones": [],
        "data_nascimento_raw": "",
        "aniversario_ddmm": "",
        "info_topo_pdf": info_topo,
        "periodo_aniversario": info_topo,
        "tipo": "",
        "regiao": "",
        "origem": origem,
        "pagina": pagina,
        "linha": linha_numero,
        "importavel": False,
        "inconsistencias": [mensagem],
        "metadata": {
            "arquivo": origem,
            "loja": loja,
            "periodo_aniversario": info_topo,
            "info_topo_pdf": info_topo,
            "linha_original": texto,
            "inconsistencias": [mensagem],
        },
    }


def _extrair_loja(texto: str) -> str:
    for linha in texto.splitlines():
        conteudo = linha.strip()
        if conteudo.startswith("Loja "):
            return _normalizar_nome(conteudo.removeprefix("Loja "))
    return ""


def _extrair_periodo(texto: str) -> str:
    for linha in texto.splitlines():
        conteudo = linha.strip()
        if conteudo.startswith("Aniversário:"):
            return _normalizar_nome(conteudo)
    return ""


def _campo_inconsistencia(mensagem: str) -> str:
    if "nome" in mensagem:
        return "nome"
    if "telefone" in mensagem:
        return "telefone"
    if "data" in mensagem:
        return "data_nascimento"
    return "linha"
