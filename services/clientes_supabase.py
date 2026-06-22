from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any, Final, TypedDict

from services import mensagens, perfis, supabase

logger = logging.getLogger(__name__)

DEFAULT_TABLE: Final[str] = "clientes"
DEFAULT_BATCH_SIZE: Final[int] = 100
CAMPOS_CLIENTES: Final[tuple[str, ...]] = (
    "nome",
    "telefone",
    "telefone_raw",
    "telefones",
    "data_nascimento",
    "data_nascimento_raw",
    "aniversario_ddmm",
    "idade",
    "info_topo_pdf",
    "periodo_aniversario",
    "tipo",
    "regiao",
    "numero",
    "origem",
    "pagina",
    "linha",
    "perfil_id",
    "perfil_nome",
    "metadata",
)


class ResultadoSupabase(TypedDict, total=False):
    ok: bool
    tabela: str
    enviados: int
    salvos: int
    erro: str
    detalhe: str


def salvar_clientes(
    registros: Sequence[Mapping[str, Any]],
    *,
    tabela: str | None = None,
) -> ResultadoSupabase:
    tabela_final = tabela or supabase.tabela_env("SUPABASE_CLIENTES_TABLE", DEFAULT_TABLE)
    perfis_ativos = perfis.listar_perfis(ativos=True)
    payload = [
        _preparar_registro(registro, perfis_ativos=perfis_ativos)
        for registro in registros
        if registro.get("importavel", True)
    ]
    payload = [registro for registro in payload if registro.get("nome") and registro.get("telefone")]
    logger.info(
        "Confirmacao de importacao: %s cliente(s) recebidos, %s importavel(is) para Supabase.",
        len(registros),
        len(payload),
    )
    if not payload:
        return {
            "ok": True,
            "tabela": tabela_final,
            "enviados": 0,
            "salvos": 0,
        }

    logger.info("Campos enviados para Supabase.%s: %s", tabela_final, ", ".join(CAMPOS_CLIENTES))
    salvos = 0
    batch_size = _batch_size()
    for indice, lote in enumerate(_chunks(payload, batch_size), start=1):
        resultado = supabase.upsert(tabela_final, lote, on_conflict="telefone")
        if not resultado.get("ok"):
            detalhe = resultado.get("detalhe", "")
            logger.warning(
                "Falha ao salvar lote %s de clientes no Supabase. enviados=%s salvos_antes=%s erro=%s detalhe=%s",
                indice,
                len(lote),
                salvos,
                resultado.get("erro", ""),
                detalhe,
            )
            return {
                "ok": False,
                "tabela": tabela_final,
                "enviados": len(payload),
                "salvos": salvos,
                "erro": "Nao foi possivel salvar os clientes importados.",
                "detalhe": detalhe or resultado.get("erro", "Falha ao salvar clientes no Supabase"),
            }

        salvos += _contar_registros_resposta(resultado.get("data"), len(lote))
        logger.info(
            "Lote %s salvo no Supabase.%s: %s cliente(s). Total salvo ate agora: %s.",
            indice,
            tabela_final,
            len(lote),
            salvos,
        )

    logger.info("%s cliente(s) enviados ao Supabase; %s salvo(s).", len(payload), salvos)
    return {
        "ok": True,
        "tabela": tabela_final,
        "enviados": len(payload),
        "salvos": salvos,
    }


def buscar_cliente_por_telefone(telefone: str, *, tabela: str | None = None) -> dict[str, Any] | None:
    telefone_limpo = _texto(telefone)
    if not telefone_limpo:
        return None

    tabela_final = tabela or supabase.tabela_env("SUPABASE_CLIENTES_TABLE", DEFAULT_TABLE)
    resultado = supabase.selecionar(
        tabela_final,
        filtros={"telefone": f"eq.{telefone_limpo}"},
        limite=1,
    )
    if not resultado.get("ok"):
        logger.warning("Nao foi possivel buscar cliente %s: %s", telefone_limpo, resultado.get("erro"))
        return None
    dados = resultado.get("data")
    if isinstance(dados, list) and dados and isinstance(dados[0], dict):
        return dados[0]
    return None


def listar_clientes(
    *,
    tabela: str | None = None,
    limite: int = 500,
) -> list[dict[str, Any]]:
    tabela_final = tabela or supabase.tabela_env("SUPABASE_CLIENTES_TABLE", DEFAULT_TABLE)
    resultado = supabase.selecionar(
        tabela_final,
        colunas="*",
        limite=max(1, min(limite, 2000)),
    )
    if not resultado.get("ok"):
        logger.warning("Nao foi possivel listar clientes: %s", resultado.get("erro"))
        return []

    dados = resultado.get("data")
    if not isinstance(dados, list):
        return []
    clientes = [item for item in dados if isinstance(item, dict)]
    return sorted(clientes, key=lambda item: str(item.get("nome") or "").lower())


def _preparar_registro(
    registro: Mapping[str, Any],
    *,
    perfis_ativos: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    metadata = dict(registro.get("metadata") or {})
    metadata.setdefault("origem", registro.get("origem", "pdf"))
    metadata.setdefault("pagina", registro.get("pagina"))
    metadata.setdefault("linha", registro.get("linha"))
    metadata.setdefault("telefones", registro.get("telefones", []))
    metadata.setdefault("info_topo_pdf", registro.get("info_topo_pdf") or registro.get("periodo_aniversario"))
    idade = mensagens.calcular_idade(registro.get("data_nascimento"), registro.get("idade"))
    perfil = perfis.classificar_cliente(registro, perfis_disponiveis=perfis_ativos or [])
    perfil_score = None
    if perfil:
        perfil_metadata = perfil.get("metadata")
        if isinstance(perfil_metadata, dict):
            perfil_score = perfil_metadata.get("score_classificacao")
        metadata.setdefault(
            "perfil_classificacao",
            {
                "perfil_id": perfil.get("id", ""),
                "perfil_nome": perfil.get("nome", ""),
                "score": perfil_score,
            },
        )

    payload: dict[str, Any] = {
        "nome": _texto(registro.get("nome")),
        "telefone": _texto(registro.get("telefone")),
        "telefone_raw": _texto(registro.get("telefone_raw")),
        "telefones": list(registro.get("telefones") or []),
        "data_nascimento": registro.get("data_nascimento") or None,
        "data_nascimento_raw": _texto(registro.get("data_nascimento_raw")),
        "aniversario_ddmm": _texto(registro.get("aniversario_ddmm")),
        "idade": idade,
        "info_topo_pdf": _texto(registro.get("info_topo_pdf") or registro.get("periodo_aniversario")),
        "periodo_aniversario": _texto(registro.get("periodo_aniversario") or registro.get("info_topo_pdf")),
        "tipo": _texto(registro.get("tipo")),
        "regiao": _texto(registro.get("regiao")),
        "numero": registro.get("numero"),
        "origem": _texto(registro.get("origem")) or "pdf",
        "pagina": registro.get("pagina"),
        "linha": registro.get("linha"),
        "perfil_id": perfil.get("id") if perfil else None,
        "perfil_nome": perfil.get("nome") if perfil else None,
        "metadata": metadata,
    }

    return _normalizar_campos_cliente(payload)


def _normalizar_campos_cliente(cliente: Mapping[str, Any]) -> dict[str, Any]:
    normalizado = {campo: cliente.get(campo) for campo in CAMPOS_CLIENTES}
    normalizado["nome"] = _texto(normalizado.get("nome"))
    normalizado["telefone"] = _texto(normalizado.get("telefone"))
    normalizado["telefone_raw"] = _texto(normalizado.get("telefone_raw")) or None
    normalizado["telefones"] = _lista_json(normalizado.get("telefones"))
    normalizado["data_nascimento"] = normalizado.get("data_nascimento") or None
    normalizado["data_nascimento_raw"] = _texto(normalizado.get("data_nascimento_raw")) or None
    normalizado["aniversario_ddmm"] = _texto(normalizado.get("aniversario_ddmm")) or None
    normalizado["idade"] = normalizado.get("idade")
    normalizado["info_topo_pdf"] = _texto(normalizado.get("info_topo_pdf")) or None
    normalizado["periodo_aniversario"] = _texto(normalizado.get("periodo_aniversario")) or None
    normalizado["tipo"] = _texto(normalizado.get("tipo")) or None
    normalizado["regiao"] = _texto(normalizado.get("regiao")) or None
    normalizado["numero"] = normalizado.get("numero")
    normalizado["origem"] = _texto(normalizado.get("origem")) or "pdf"
    normalizado["pagina"] = normalizado.get("pagina")
    normalizado["linha"] = normalizado.get("linha")
    normalizado["perfil_id"] = normalizado.get("perfil_id") or None
    normalizado["perfil_nome"] = _texto(normalizado.get("perfil_nome")) or None
    normalizado["metadata"] = _dict_json(normalizado.get("metadata"))
    return normalizado


def _texto(valor: Any) -> str:
    if valor is None:
        return ""
    return str(valor).strip()


def _lista_json(valor: Any) -> list[Any]:
    if isinstance(valor, list):
        return valor
    if isinstance(valor, tuple):
        return list(valor)
    return []


def _dict_json(valor: Any) -> dict[str, Any]:
    return dict(valor) if isinstance(valor, Mapping) else {}


def _chunks(registros: Sequence[dict[str, Any]], tamanho: int) -> list[list[dict[str, Any]]]:
    return [list(registros[indice : indice + tamanho]) for indice in range(0, len(registros), tamanho)]


def _batch_size() -> int:
    try:
        valor = int(os.getenv("SUPABASE_CLIENTES_BATCH_SIZE", str(DEFAULT_BATCH_SIZE)))
    except ValueError:
        return DEFAULT_BATCH_SIZE
    return max(1, min(valor, 500))


def _contar_registros_resposta(dados: Any, padrao: int) -> int:
    if not dados:
        return padrao

    if isinstance(dados, list):
        return len(dados)
    return padrao
