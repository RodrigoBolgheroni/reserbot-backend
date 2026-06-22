from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final, TypedDict

from services import mensagens, perfis, supabase

logger = logging.getLogger(__name__)

DEFAULT_TABLE: Final[str] = "clientes"


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
    if not payload:
        return {
            "ok": True,
            "tabela": tabela_final,
            "enviados": 0,
            "salvos": 0,
        }

    resultado = supabase.upsert(tabela_final, payload, on_conflict="telefone")
    if not resultado.get("ok"):
        return {
            "ok": False,
            "tabela": tabela_final,
            "enviados": len(payload),
            "salvos": 0,
            "erro": resultado.get("erro", "Falha ao salvar clientes no Supabase"),
            "detalhe": resultado.get("detalhe", ""),
        }

    logger.info("%s cliente(s) enviados ao Supabase.", len(payload))
    return {
        "ok": True,
        "tabela": tabela_final,
        "enviados": len(payload),
        "salvos": _contar_registros_resposta(resultado.get("data"), len(payload)),
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
    if perfil:
        metadata.setdefault(
            "perfil_classificacao",
            {
                "perfil_id": perfil.get("id", ""),
                "perfil_nome": perfil.get("nome", ""),
                "score": (perfil.get("metadata") or {}).get("score_classificacao")
                if isinstance(perfil.get("metadata"), dict)
                else None,
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

    return {chave: valor for chave, valor in payload.items() if valor not in ("", [], None)}


def _texto(valor: Any) -> str:
    if valor is None:
        return ""
    return str(valor).strip()


def _contar_registros_resposta(dados: Any, padrao: int) -> int:
    if not dados:
        return padrao

    if isinstance(dados, list):
        return len(dados)
    return padrao
