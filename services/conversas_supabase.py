from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final, TypedDict
from urllib.parse import quote

from services import comprovantes_reserva, supabase


logger = logging.getLogger(__name__)

DEFAULT_CONVERSAS_TABLE: Final[str] = "conversas"
DEFAULT_MENSAGENS_TABLE: Final[str] = "mensagens"
DEFAULT_CLIENTES_TABLE: Final[str] = "clientes"
DEFAULT_RESERVAS_TABLE: Final[str] = "reservas"
PAGE_SIZE_PADRAO: Final[int] = 30
PAGE_SIZE_MAX: Final[int] = 100
BUSCA_LIMITE: Final[int] = 500
RECENTES_LIMITE: Final[int] = 1000
CAMPOS_METADATA_PRIVADOS: Final[set[str]] = {
    "bucket",
    "storage_path",
    "signed_url",
    "url_assinada",
    "url_publica",
    "service_role_key",
}
STATUS_FILTROS: Final[dict[str, tuple[str, ...]]] = {
    "bot_ativo": ("bot_ativo", "aberta", "aguardando_cliente"),
    "aguardando_humano": ("aguardando_humano",),
    "humano": ("humano", "em_atendimento"),
    "atendimento_humano": ("humano", "em_atendimento"),
    "finalizada": ("finalizada",),
}


class ResultadoListaConversas(TypedDict):
    items: list[dict[str, Any]]
    page: int
    page_size: int
    total: int
    has_next: bool
    has_prev: bool


def listar_conversas(
    *,
    page: int = 1,
    page_size: int = PAGE_SIZE_PADRAO,
    search: str = "",
    status: str = "",
) -> ResultadoListaConversas:
    page_final = max(1, int(page or 1))
    page_size_final = max(1, min(int(page_size or PAGE_SIZE_PADRAO), PAGE_SIZE_MAX))
    offset = (page_final - 1) * page_size_final
    termo = _limpar_busca(search)
    status_valores = _status_valores(status)

    conversas = _buscar_conversas_base(status_valores=status_valores, limite=RECENTES_LIMITE, contar=not termo)
    total_base = conversas.get("total") if isinstance(conversas.get("total"), int) else None
    candidatos = _mapear_por_id(conversas.get("data"))

    mensagens_recentes = _buscar_mensagens_recentes(termo=termo)
    for conversa_id in _ids_conversas(mensagens_recentes):
        if conversa_id not in candidatos:
            candidatos.update(_mapear_por_id(_buscar_conversas_por_ids([conversa_id], status_valores=status_valores)))

    clientes_encontrados = _buscar_clientes_por_termo(termo) if termo else {}
    if clientes_encontrados:
        candidatos.update(
            _mapear_por_id(
                _buscar_conversas_por_clientes(
                    cliente_ids=[str(item.get("id") or "") for item in clientes_encontrados.values()],
                    telefones=[str(item.get("telefone") or "") for item in clientes_encontrados.values()],
                    status_valores=status_valores,
                )
            )
        )

    if termo:
        candidatos.update(
            _mapear_por_id(
                _buscar_conversas_por_telefone(
                    telefone=termo,
                    status_valores=status_valores,
                )
            )
        )

    conversa_ids = list(candidatos)
    ultimas = _ultimas_mensagens_por_conversa(conversa_ids)
    clientes = _clientes_por_conversas(candidatos.values())
    itens = [
        _resumir_conversa(conversa, cliente=_cliente_da_conversa(conversa, clientes), ultima=ultimas.get(str(conversa.get("id") or "")))
        for conversa in candidatos.values()
    ]
    if termo:
        itens = [item for item in itens if _item_conversa_bate_busca(item, termo)]

    itens.sort(key=_ordenacao_conversa, reverse=True)
    total = len(itens) if termo or total_base is None else total_base
    pagina = itens[offset : offset + page_size_final]
    return {
        "items": pagina,
        "page": page_final,
        "page_size": page_size_final,
        "total": total,
        "has_next": offset + page_size_final < total,
        "has_prev": page_final > 1,
    }


def obter_conversa(conversa_id: str) -> dict[str, Any] | None:
    conversa_id = str(conversa_id or "").strip()
    if not conversa_id:
        return None
    conversas = _buscar_conversas_por_ids([conversa_id], status_valores=())
    if not conversas:
        return None
    conversa = conversas[0]
    clientes = _clientes_por_conversas([conversa])
    return _resumir_conversa(conversa, cliente=_cliente_da_conversa(conversa, clientes), ultima=None)


def listar_mensagens_conversa(conversa_id: str) -> dict[str, Any] | None:
    conversa_id = str(conversa_id or "").strip()
    conversa = obter_conversa(conversa_id)
    if conversa is None:
        return None

    resultado = supabase.selecionar(
        _tabela_mensagens(),
        filtros={"conversa_id": f"eq.{conversa_id}"},
        colunas="id,conversa_id,remetente,conteudo,timestamp,provider_message_id,metadata,created_at",
        limite=1000,
        order="timestamp.asc",
    )
    if not resultado.get("ok"):
        logger.warning("Nao foi possivel listar mensagens da conversa %s: %s", conversa_id, resultado.get("erro"))
        return {
            "conversa": conversa["conversa"],
            "cliente": conversa["cliente"],
            "mensagens": [],
        }

    dados = resultado.get("data")
    itens_mensagem = [item for item in dados if isinstance(item, dict)] if isinstance(dados, list) else []
    comprovantes = comprovantes_reserva.listar_por_conversa(conversa_id) if any(
        _mensagem_tem_indicio_midia(item) for item in itens_mensagem
    ) else []
    comprovantes_por_id = {str(item.get("id") or ""): item for item in comprovantes if item.get("id")}
    comprovantes_por_provider = {
        str(item.get("provider_message_id") or ""): item
        for item in comprovantes
        if item.get("provider_message_id")
    }
    reservas_por_id = _reservas_por_ids(
        str(item.get("reserva_id") or "")
        for item in comprovantes
        if item.get("reserva_id")
    )
    mensagens = [
        _resumir_mensagem(
            item,
            comprovantes_por_id=comprovantes_por_id,
            comprovantes_por_provider=comprovantes_por_provider,
            reservas_por_id=reservas_por_id,
        )
        for item in itens_mensagem
    ]
    return {
        "conversa": conversa["conversa"],
        "cliente": conversa["cliente"],
        "mensagens": mensagens,
    }


def _buscar_conversas_base(*, status_valores: tuple[str, ...], limite: int, contar: bool = False) -> dict[str, Any]:
    filtros: dict[str, str] = {}
    if status_valores:
        filtros["status"] = _filtro_in(status_valores)
    resultado = supabase.selecionar(
        _tabela_conversas(),
        filtros=filtros,
        colunas="id,cliente_id,cliente_telefone,status,data_inicio,origem,metadata,created_at,updated_at",
        limite=max(1, min(limite, RECENTES_LIMITE)),
        contar=contar,
        order="updated_at.desc",
    )
    if not resultado.get("ok"):
        logger.warning("Nao foi possivel listar conversas: %s", resultado.get("erro"))
        return {"data": [], "total": 0}
    return {"data": resultado.get("data"), "total": _total_resultado(resultado)}


def _buscar_conversas_por_ids(ids: Iterable[str], *, status_valores: tuple[str, ...]) -> list[dict[str, Any]]:
    ids_limpos = [item for item in _unicos(ids) if item]
    if not ids_limpos:
        return []
    filtros = {"id": _filtro_in(ids_limpos)}
    if status_valores:
        filtros["status"] = _filtro_in(status_valores)
    return _lista_resultado(
        supabase.selecionar(
            _tabela_conversas(),
            filtros=filtros,
            colunas="id,cliente_id,cliente_telefone,status,data_inicio,origem,metadata,created_at,updated_at",
            limite=min(len(ids_limpos), BUSCA_LIMITE),
        )
    )


def _buscar_conversas_por_clientes(
    *,
    cliente_ids: Iterable[str],
    telefones: Iterable[str],
    status_valores: tuple[str, ...],
) -> list[dict[str, Any]]:
    conversas: dict[str, dict[str, Any]] = {}
    ids_limpos = [item for item in _unicos(cliente_ids) if item]
    if ids_limpos:
        filtros = {"cliente_id": _filtro_in(ids_limpos)}
        if status_valores:
            filtros["status"] = _filtro_in(status_valores)
        conversas.update(_mapear_por_id(_lista_resultado(supabase.selecionar(_tabela_conversas(), filtros=filtros, limite=BUSCA_LIMITE))))

    telefones_limpos = [item for item in _unicos(telefones) if item]
    if telefones_limpos:
        filtros = {"cliente_telefone": _filtro_in(telefones_limpos)}
        if status_valores:
            filtros["status"] = _filtro_in(status_valores)
        conversas.update(_mapear_por_id(_lista_resultado(supabase.selecionar(_tabela_conversas(), filtros=filtros, limite=BUSCA_LIMITE))))
    return list(conversas.values())


def _buscar_conversas_por_telefone(*, telefone: str, status_valores: tuple[str, ...]) -> list[dict[str, Any]]:
    filtros = {"cliente_telefone": f"ilike.*{_escape_ilike(telefone)}*"}
    if status_valores:
        filtros["status"] = _filtro_in(status_valores)
    return _lista_resultado(supabase.selecionar(_tabela_conversas(), filtros=filtros, limite=BUSCA_LIMITE))


def _buscar_mensagens_recentes(*, termo: str = "") -> list[dict[str, Any]]:
    filtros = {"conteudo": f"ilike.*{_escape_ilike(termo)}*"} if termo else None
    resultado = supabase.selecionar(
        _tabela_mensagens(),
        filtros=filtros,
        colunas="id,conversa_id,remetente,conteudo,timestamp,provider_message_id,metadata,created_at",
        limite=RECENTES_LIMITE,
        order="timestamp.desc",
    )
    if not resultado.get("ok"):
        logger.warning("Nao foi possivel listar mensagens recentes: %s", resultado.get("erro"))
        return []
    return _lista_resultado(resultado)


def _buscar_clientes_por_termo(termo: str) -> dict[str, dict[str, Any]]:
    if not termo:
        return {}
    filtro = _escape_ilike(termo)
    resultado = supabase.selecionar(
        _tabela_clientes(),
        filtros={"or": f"(nome.ilike.*{filtro}*,telefone.ilike.*{filtro}*)"},
        colunas="id,nome,telefone",
        limite=BUSCA_LIMITE,
    )
    return _mapear_clientes(_lista_resultado(resultado))


def _clientes_por_conversas(conversas: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    ids = [str(item.get("cliente_id") or "") for item in conversas if item.get("cliente_id")]
    telefones = [str(item.get("cliente_telefone") or "") for item in conversas if item.get("cliente_telefone")]
    clientes: dict[str, dict[str, Any]] = {}
    if ids:
        clientes.update(_mapear_clientes(_lista_resultado(supabase.selecionar(_tabela_clientes(), filtros={"id": _filtro_in(_unicos(ids))}, colunas="id,nome,telefone", limite=BUSCA_LIMITE))))
    if telefones:
        clientes.update(_mapear_clientes(_lista_resultado(supabase.selecionar(_tabela_clientes(), filtros={"telefone": _filtro_in(_unicos(telefones))}, colunas="id,nome,telefone", limite=BUSCA_LIMITE))))
    return clientes


def _ultimas_mensagens_por_conversa(conversa_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = [item for item in _unicos(conversa_ids) if item]
    if not ids:
        return {}
    resultado = supabase.selecionar(
        _tabela_mensagens(),
        filtros={"conversa_id": _filtro_in(ids)},
        colunas="id,conversa_id,remetente,conteudo,timestamp,provider_message_id,metadata,created_at",
        limite=min(RECENTES_LIMITE, max(len(ids) * 10, len(ids))),
        order="timestamp.desc",
    )
    ultimas: dict[str, dict[str, Any]] = {}
    for mensagem in _lista_resultado(resultado):
        conversa_id = str(mensagem.get("conversa_id") or "")
        if conversa_id and conversa_id not in ultimas:
            ultimas[conversa_id] = _resumir_mensagem(mensagem)
    return ultimas


def _resumir_conversa(
    conversa: Mapping[str, Any],
    *,
    cliente: Mapping[str, Any] | None,
    ultima: Mapping[str, Any] | None,
) -> dict[str, Any]:
    cliente_final = {
        "nome": str((cliente or {}).get("nome") or _metadata(conversa).get("cliente_nome") or "Cliente").strip(),
        "telefone": str((cliente or {}).get("telefone") or conversa.get("cliente_telefone") or "").strip(),
    }
    conversa_final = {
        "id": str(conversa.get("id") or ""),
        "status": str(conversa.get("status") or ""),
        "origem": str(conversa.get("origem") or ""),
        "created_at": str(conversa.get("created_at") or conversa.get("data_inicio") or ""),
        "updated_at": str(conversa.get("updated_at") or ""),
        "data_inicio": str(conversa.get("data_inicio") or ""),
    }
    return {
        "id": conversa_final["id"],
        "cliente": cliente_final,
        "status": conversa_final["status"],
        "origem": conversa_final["origem"],
        "conversa": conversa_final,
        "ultima_mensagem": dict(ultima or {}),
        "nao_lidas": _nao_lidas(conversa),
    }


def _resumir_mensagem(
    mensagem: Mapping[str, Any],
    *,
    comprovantes_por_id: Mapping[str, Mapping[str, Any]] | None = None,
    comprovantes_por_provider: Mapping[str, Mapping[str, Any]] | None = None,
    reservas_por_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    metadata = _metadata(mensagem)
    resumo = {
        "id": str(mensagem.get("id") or ""),
        "conversa_id": str(mensagem.get("conversa_id") or ""),
        "texto": str(mensagem.get("conteudo") or ""),
        "remetente": str(mensagem.get("remetente") or ""),
        "status": str(metadata.get("status_entrega") or metadata.get("status") or "") or None,
        "erro": str(metadata.get("erro_entrega") or metadata.get("erro") or "") or None,
        "provider_message_id": str(mensagem.get("provider_message_id") or "") or None,
        "created_at": str(mensagem.get("timestamp") or mensagem.get("created_at") or ""),
        "metadata": _sanitizar_metadata(metadata),
    }
    tipo = _tipo_midia_mensagem(mensagem)
    if not tipo:
        return resumo

    comprovante_id = str(metadata.get("comprovante_id") or "")
    provider_message_id = str(mensagem.get("provider_message_id") or "")
    comprovante = (comprovantes_por_id or {}).get(comprovante_id) or (comprovantes_por_provider or {}).get(provider_message_id)
    comprovante = comprovante or {}
    mime_type = str(comprovante.get("mime_type") or metadata.get("mime_type") or "")
    if mime_type == "application/pdf":
        tipo = "document"
    elif mime_type.startswith("image/"):
        tipo = "image"
    filename = str(comprovante.get("nome_original") or "").strip()
    if not filename:
        filename = "comprovante.pdf" if tipo == "document" else "comprovante.jpg"
    mensagem_id = str(mensagem.get("id") or "")
    conversa_id = str(mensagem.get("conversa_id") or "")
    reserva_id = str(comprovante.get("reserva_id") or "")
    reserva = (reservas_por_id or {}).get(reserva_id) or {}
    comprovante_metadata = comprovante.get("metadata") if isinstance(comprovante.get("metadata"), Mapping) else {}
    auditoria = (
        comprovante_metadata.get("analise_humana")
        if isinstance(comprovante_metadata.get("analise_humana"), Mapping)
        else {}
    )
    resumo["tipo"] = tipo
    resumo["media"] = {
        "comprovante_id": str(comprovante.get("id") or ""),
        "reserva_id": reserva_id,
        "disponivel": bool(comprovante.get("disponivel")),
        "mime_type": mime_type or ("application/pdf" if tipo == "document" else "image/jpeg"),
        "filename": filename,
        "tamanho": _inteiro_seguro(comprovante.get("tamanho_bytes")),
        "comprovante_status": str(comprovante.get("status_analise") or metadata.get("comprovante_status") or ""),
        "analisado_em": str(comprovante.get("analisado_em") or ""),
        "analisado_por": str(comprovante.get("analisado_por") or ""),
        "motivo_rejeicao": str(auditoria.get("motivo") or ""),
        "notificacao_status": str(
            (auditoria.get("notificacao") or {}).get("status")
            if isinstance(auditoria.get("notificacao"), Mapping)
            else ""
        ),
        "reserva": _resumir_reserva_comprovante(reserva),
        "url_endpoint": (
            f"/api/mensagens/{quote(mensagem_id, safe='')}/midia?conversa_id={quote(conversa_id, safe='')}"
            if mensagem_id and conversa_id and comprovante
            else ""
        ),
    }
    return resumo


def _reservas_por_ids(reserva_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = list(dict.fromkeys(str(item or "").strip() for item in reserva_ids if str(item or "").strip()))
    if not ids:
        return {}
    resultado = supabase.selecionar(
        _tabela_reservas(),
        filtros={"id": _filtro_in(ids)},
        colunas=(
            "id,conversa_id,cliente_telefone,data_reserva,horario,pessoas,observacoes,"
            "status,status_pagamento,espaco_id,metadata"
        ),
        limite=len(ids),
    )
    if not resultado.get("ok"):
        logger.warning("Reservas de comprovantes nao carregadas: %s", resultado.get("erro"))
        return {}
    return {
        str(item.get("id") or ""): dict(item)
        for item in (resultado.get("data") or [])
        if isinstance(item, Mapping) and item.get("id")
    }


def _resumir_reserva_comprovante(reserva: Mapping[str, Any]) -> dict[str, Any]:
    if not reserva:
        return {}
    metadata = reserva.get("metadata") if isinstance(reserva.get("metadata"), Mapping) else {}
    horario = str(reserva.get("horario") or "")[:5]
    flexivel = horario in {"18:00", "19:00"} or not reserva.get("espaco_id")
    espaco = ""
    if not flexivel:
        espaco = str(
            metadata.get("espaco_sugerido_nome")
            or metadata.get("preferencia_espaco_nome")
            or ""
        )
    return {
        "id": str(reserva.get("id") or ""),
        "data": str(reserva.get("data_reserva") or ""),
        "horario": horario,
        "pessoas": _inteiro_seguro(reserva.get("pessoas")),
        "observacoes": str(reserva.get("observacoes") or ""),
        "status": str(reserva.get("status") or ""),
        "status_pagamento": str(reserva.get("status_pagamento") or ""),
        "espaco": espaco,
        "local_flexivel": flexivel,
    }


def _mensagem_tem_indicio_midia(mensagem: Mapping[str, Any]) -> bool:
    return bool(_tipo_midia_mensagem(mensagem))


def _inteiro_seguro(valor: Any) -> int:
    try:
        return int(valor or 0)
    except (TypeError, ValueError):
        return 0


def _tipo_midia_mensagem(mensagem: Mapping[str, Any]) -> str:
    metadata = _metadata(mensagem)
    tipo = str(metadata.get("tipo") or "").strip().lower()
    if not tipo:
        tipos = metadata.get("tipos_midia")
        if isinstance(tipos, list) and tipos:
            tipo = str(tipos[0] or "").strip().lower()
    mime_type = str(metadata.get("mime_type") or "").strip().lower()
    if mime_type == "application/pdf" or tipo in {"document", "pdf"}:
        return "document"
    if mime_type.startswith("image/") or tipo in {"image", "imagem"}:
        return "image"
    conteudo = str(mensagem.get("conteudo") or "").strip().lower()
    if conteudo == "imagem recebida":
        return "image"
    if conteudo == "documento recebido" or conteudo.endswith(".pdf"):
        return "document"
    return ""


def _cliente_da_conversa(conversa: Mapping[str, Any], clientes: Mapping[str, dict[str, Any]]) -> dict[str, Any] | None:
    cliente_id = str(conversa.get("cliente_id") or "")
    telefone = str(conversa.get("cliente_telefone") or "")
    return clientes.get(cliente_id) or clientes.get(telefone)


def _mapear_clientes(clientes: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    mapa: dict[str, dict[str, Any]] = {}
    for cliente in clientes:
        registro = dict(cliente)
        cliente_id = str(registro.get("id") or "")
        telefone = str(registro.get("telefone") or "")
        if cliente_id:
            mapa[cliente_id] = registro
        if telefone:
            mapa[telefone] = registro
    return mapa


def _mapear_por_id(registros: Any) -> dict[str, dict[str, Any]]:
    mapa: dict[str, dict[str, Any]] = {}
    if not isinstance(registros, list):
        return mapa
    for item in registros:
        if isinstance(item, dict):
            item_id = str(item.get("id") or "")
            if item_id:
                mapa[item_id] = dict(item)
    return mapa


def _ids_conversas(mensagens: Iterable[Mapping[str, Any]]) -> list[str]:
    return _unicos(str(item.get("conversa_id") or "") for item in mensagens)


def _lista_resultado(resultado: Mapping[str, Any]) -> list[dict[str, Any]]:
    dados = resultado.get("data")
    return [item for item in dados if isinstance(item, dict)] if isinstance(dados, list) else []


def _item_conversa_bate_busca(item: Mapping[str, Any], termo: str) -> bool:
    termo_norm = _normalizar(termo)
    partes = [
        item.get("status", ""),
        item.get("origem", ""),
        (item.get("cliente") or {}).get("nome", "") if isinstance(item.get("cliente"), Mapping) else "",
        (item.get("cliente") or {}).get("telefone", "") if isinstance(item.get("cliente"), Mapping) else "",
        (item.get("ultima_mensagem") or {}).get("texto", "") if isinstance(item.get("ultima_mensagem"), Mapping) else "",
    ]
    return termo_norm in _normalizar(" ".join(str(parte or "") for parte in partes))


def _ordenacao_conversa(item: Mapping[str, Any]) -> str:
    ultima = item.get("ultima_mensagem") if isinstance(item.get("ultima_mensagem"), Mapping) else {}
    conversa = item.get("conversa") if isinstance(item.get("conversa"), Mapping) else {}
    return str(
        (ultima or {}).get("created_at")
        or (conversa or {}).get("updated_at")
        or (conversa or {}).get("data_inicio")
        or (conversa or {}).get("created_at")
        or ""
    )


def _status_valores(status: str) -> tuple[str, ...]:
    status_limpo = _limpar_busca(status).replace("-", "_")
    if status_limpo in {"", "todas", "todos", "all"}:
        return ()
    return STATUS_FILTROS.get(status_limpo, (status_limpo,))


def _nao_lidas(conversa: Mapping[str, Any]) -> int:
    metadata = _metadata(conversa)
    for chave in ("nao_lidas", "nao_lidos", "unread_count"):
        try:
            return max(0, int(metadata.get(chave) or 0))
        except (TypeError, ValueError):
            continue
    return 0


def _metadata(registro: Mapping[str, Any]) -> dict[str, Any]:
    valor = registro.get("metadata")
    return dict(valor) if isinstance(valor, Mapping) else {}


def _sanitizar_metadata(valor: Any) -> Any:
    if isinstance(valor, Mapping):
        return {
            str(chave): _sanitizar_metadata(item)
            for chave, item in valor.items()
            if str(chave).lower() not in CAMPOS_METADATA_PRIVADOS
        }
    if isinstance(valor, list):
        return [_sanitizar_metadata(item) for item in valor]
    return valor


def _filtro_in(valores: Iterable[str]) -> str:
    return f"in.({','.join(_escape_in(valor) for valor in valores if valor)})"


def _escape_in(valor: str) -> str:
    return str(valor).replace('"', "").replace(",", "").strip()


def _escape_ilike(valor: str) -> str:
    return re.sub(r"[%*,()]", " ", str(valor or "")).strip()


def _limpar_busca(valor: str) -> str:
    return re.sub(r"\s+", " ", str(valor or "").strip())[:120]


def _normalizar(valor: str) -> str:
    return str(valor or "").lower()


def _unicos(valores: Iterable[str]) -> list[str]:
    vistos: set[str] = set()
    unicos: list[str] = []
    for valor in valores:
        texto = str(valor or "").strip()
        if texto and texto not in vistos:
            vistos.add(texto)
            unicos.append(texto)
    return unicos


def _total_resultado(resultado: Mapping[str, Any]) -> int:
    try:
        return int(resultado.get("total") or 0)
    except (TypeError, ValueError):
        dados = resultado.get("data")
        return len(dados) if isinstance(dados, list) else 0


def _tabela_conversas() -> str:
    return supabase.tabela_env("SUPABASE_CONVERSAS_TABLE", DEFAULT_CONVERSAS_TABLE)


def _tabela_mensagens() -> str:
    return supabase.tabela_env("SUPABASE_MENSAGENS_TABLE", DEFAULT_MENSAGENS_TABLE)


def _tabela_clientes() -> str:
    return supabase.tabela_env("SUPABASE_CLIENTES_TABLE", DEFAULT_CLIENTES_TABLE)


def _tabela_reservas() -> str:
    return supabase.tabela_env("SUPABASE_RESERVAS_TABLE", DEFAULT_RESERVAS_TABLE)
