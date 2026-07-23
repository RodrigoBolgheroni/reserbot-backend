from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final, TypedDict

from services import supabase


logger = logging.getLogger(__name__)

TABELA_ESTABELECIMENTOS: Final[str] = "estabelecimentos"
TABELA_HORARIOS_FUNCIONAMENTO: Final[str] = "horarios_funcionamento"
TABELA_CONFIGURACOES_RESERVA: Final[str] = "configuracoes_reserva"
TABELA_ESPACOS: Final[str] = "espacos"
TABELA_FAQ_CONTEUDOS: Final[str] = "faq_conteudos"


class ResultadoConfigBruta(TypedDict, total=False):
    ok: bool
    erro: str
    erro_tipo: str
    erros_parciais: list[dict[str, str]]
    estabelecimento: dict[str, Any]
    horarios: list[dict[str, Any]]
    configuracao_reserva: dict[str, Any]
    espacos: list[dict[str, Any]]
    faq_conteudos: list[dict[str, Any]]


def carregar_configuracao_bruta() -> ResultadoConfigBruta:
    if not supabase.configurado():
        return {
            "ok": False,
            "erro_tipo": "nao_configurado",
            "erro": "Supabase nao configurado para configuracao estruturada.",
        }

    resultado_estabelecimentos = supabase.selecionar(
        TABELA_ESTABELECIMENTOS,
        filtros={"ativo": "eq.true"},
        colunas="id,slug,nome,telefone,whatsapp,endereco,ponto_referencia,timezone,ativo",
        limite=2,
        order="created_at.asc",
    )
    if not resultado_estabelecimentos.get("ok"):
        return _erro_fatal(TABELA_ESTABELECIMENTOS, resultado_estabelecimentos)

    estabelecimentos = _lista_dicts(resultado_estabelecimentos.get("data"))
    if not estabelecimentos:
        logger.warning("Nenhum estabelecimento ativo encontrado no Supabase.")
        return {
            "ok": False,
            "erro_tipo": "sem_estabelecimento_ativo",
            "erro": "Nenhum estabelecimento ativo encontrado.",
        }
    if len(estabelecimentos) > 1:
        logger.warning(
            "Mais de um estabelecimento ativo encontrado; usando o primeiro por created_at. ids=%s",
            [str(item.get("id") or "") for item in estabelecimentos],
        )

    estabelecimento = dict(estabelecimentos[0])
    estabelecimento_id = str(estabelecimento.get("id") or "").strip()
    if not estabelecimento_id:
        return {
            "ok": False,
            "erro_tipo": "dados_invalidos",
            "erro": "Estabelecimento ativo sem id.",
        }

    erros_parciais: list[dict[str, str]] = []
    horarios = _consultar_lista(
        TABELA_HORARIOS_FUNCIONAMENTO,
        filtros={"estabelecimento_id": f"eq.{estabelecimento_id}", "ativo": "eq.true"},
        colunas="dia_semana,fechado,horario_abertura,horario_fechamento,observacao,ativo",
        limite=14,
        order="dia_semana.asc",
        erros_parciais=erros_parciais,
    )
    configuracoes = _consultar_lista(
        TABELA_CONFIGURACOES_RESERVA,
        filtros={"estabelecimento_id": f"eq.{estabelecimento_id}", "ativo": "eq.true"},
        colunas="quantidade_minima,quantidade_maxima_automatica,horarios_permitidos,taxa_valor,taxa_convertida_consumacao,prazo_cancelamento_horas,pix_chave,pix_titular,exige_comprovante,tolerancia_atraso_minutos,politica_cancelamento,instrucoes_reserva,ativo",
        limite=1,
        erros_parciais=erros_parciais,
    )
    espacos = _consultar_lista(
        TABELA_ESPACOS,
        filtros={"estabelecimento_id": f"eq.{estabelecimento_id}", "ativo": "eq.true"},
        colunas="id,nome,descricao,capacidade_maxima,permite_preferencia,regras,ativo",
        limite=50,
        order="nome.asc",
        erros_parciais=erros_parciais,
    )
    faq_conteudos = _consultar_lista(
        TABELA_FAQ_CONTEUDOS,
        filtros={"estabelecimento_id": f"eq.{estabelecimento_id}", "ativo": "eq.true"},
        colunas="categoria,titulo,conteudo,tags,ativo",
        limite=200,
        order="categoria.asc,titulo.asc",
        erros_parciais=erros_parciais,
    )

    logger.info(
        "Configuracao estruturada consultada no Supabase: tabelas=%s estabelecimento_id=%s "
        "horarios=%s configuracoes_reserva=%s espacos=%s faq_conteudos=%s erros_parciais=%s",
        [
            TABELA_ESTABELECIMENTOS,
            TABELA_HORARIOS_FUNCIONAMENTO,
            TABELA_CONFIGURACOES_RESERVA,
            TABELA_ESPACOS,
            TABELA_FAQ_CONTEUDOS,
        ],
        estabelecimento_id,
        len(horarios),
        len(configuracoes),
        len(espacos),
        len(faq_conteudos),
        len(erros_parciais),
    )

    return {
        "ok": True,
        "estabelecimento": estabelecimento,
        "horarios": horarios,
        "configuracao_reserva": dict(configuracoes[0]) if configuracoes else {},
        "espacos": espacos,
        "faq_conteudos": faq_conteudos,
        "erros_parciais": erros_parciais,
    }


def _consultar_lista(
    tabela: str,
    *,
    filtros: Mapping[str, str],
    colunas: str,
    limite: int,
    erros_parciais: list[dict[str, str]],
    order: str | None = None,
) -> list[dict[str, Any]]:
    resultado = supabase.selecionar(
        tabela,
        filtros=filtros,
        colunas=colunas,
        limite=limite,
        order=order,
    )
    if resultado.get("ok"):
        return [dict(item) for item in _lista_dicts(resultado.get("data"))]

    erro = _classificar_erro(resultado)
    erros_parciais.append({"tabela": tabela, **erro})
    logger.warning(
        "Configuracao estruturada parcial indisponivel: tabela=%s tipo=%s detalhe=%s",
        tabela,
        erro["erro_tipo"],
        erro["erro"],
    )
    return []


def _erro_fatal(tabela: str, resultado: Mapping[str, Any]) -> ResultadoConfigBruta:
    erro = _classificar_erro(resultado)
    logger.warning(
        "Configuracao estruturada indisponivel: tabela=%s tipo=%s detalhe=%s",
        tabela,
        erro["erro_tipo"],
        erro["erro"],
    )
    return {
        "ok": False,
        "erro_tipo": erro["erro_tipo"],
        "erro": erro["erro"],
    }


def _classificar_erro(resultado: Mapping[str, Any]) -> dict[str, str]:
    status = resultado.get("status")
    detalhe = str(resultado.get("detalhe") or resultado.get("erro") or "").strip()
    detalhe_lower = detalhe.lower()
    try:
        status_int = int(status)
    except (TypeError, ValueError):
        status_int = 0

    if status_int in {401, 403}:
        erro_tipo = "autenticacao"
    elif status_int == 404 or "does not exist" in detalhe_lower or "not found" in detalhe_lower:
        erro_tipo = "tabela_inexistente"
    elif "timed out" in detalhe_lower or "timeout" in detalhe_lower or "tempo" in detalhe_lower:
        erro_tipo = "timeout"
    else:
        erro_tipo = "erro_generico"

    return {
        "erro_tipo": erro_tipo,
        "erro": detalhe[:300] if detalhe else "Erro ao consultar configuracao estruturada.",
    }


def _lista_dicts(valor: Any) -> list[Mapping[str, Any]]:
    return [item for item in valor if isinstance(item, Mapping)] if isinstance(valor, list) else []
