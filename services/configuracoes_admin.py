from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services import config_restaurante, supabase
from services.repositorio_config_restaurante import (
    TABELA_CONFIGURACOES_RESERVA,
    TABELA_ESPACOS,
    TABELA_ESTABELECIMENTOS,
    TABELA_FAQ_CONTEUDOS,
    TABELA_HORARIOS_FUNCIONAMENTO,
)


logger = logging.getLogger(__name__)

HORARIO_RE: Final[re.Pattern[str]] = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PAGE_SIZE_PADRAO: Final[int] = 30
PAGE_SIZE_MAX: Final[int] = 100
ESTABELECIMENTO_EDITAVEIS: Final[set[str]] = {
    "nome",
    "telefone",
    "whatsapp",
    "endereco",
    "ponto_referencia",
    "timezone",
    "ativo",
}
ESTABELECIMENTO_IMUTAVEIS: Final[set[str]] = {"id", "slug", "created_at", "updated_at"}
RESERVA_EDITAVEIS: Final[set[str]] = {
    "quantidade_minima",
    "quantidade_maxima_automatica",
    "horarios_permitidos",
    "taxa_valor",
    "taxa_convertida_consumacao",
    "prazo_cancelamento_horas",
    "pix_chave",
    "pix_titular",
    "exige_comprovante",
    "tolerancia_atraso_minutos",
    "politica_cancelamento",
    "instrucoes_reserva",
    "ativo",
}
ESPACO_EDITAVEIS: Final[set[str]] = {
    "nome",
    "descricao",
    "capacidade_maxima",
    "permite_preferencia",
    "regras",
    "ativo",
}
FAQ_EDITAVEIS: Final[set[str]] = {"categoria", "titulo", "conteudo", "tags", "ativo"}


def obter_estabelecimento() -> dict[str, Any]:
    contexto = _contexto_estabelecimento()
    if not contexto["ok"]:
        return contexto
    return {"ok": True, "data": _serializar_estabelecimento(contexto["estabelecimento"])}


def atualizar_estabelecimento(payload: Mapping[str, Any]) -> dict[str, Any]:
    contexto = _contexto_estabelecimento()
    if not contexto["ok"]:
        return contexto

    normalizado = _normalizar_estabelecimento(payload)
    if not normalizado["ok"]:
        return normalizado
    dados = normalizado["data"]
    if not dados:
        return _erro("payload_vazio", "Informe ao menos um campo para atualizar.")

    resultado = supabase.atualizar(
        TABELA_ESTABELECIMENTOS,
        dados,
        filtros={"id": f"eq.{contexto['estabelecimento_id']}"},
    )
    if not resultado.get("ok"):
        return _erro_supabase(resultado)

    item = _primeiro_item(resultado.get("data"))
    if item is None:
        return _erro("estabelecimento_nao_encontrado", "Estabelecimento nao encontrado.", status=404)

    config_restaurante.limpar_cache_config()
    return {"ok": True, "data": _serializar_estabelecimento(item)}


def listar_horarios() -> dict[str, Any]:
    contexto = _contexto_estabelecimento()
    if not contexto["ok"]:
        return contexto
    resultado = supabase.selecionar(
        TABELA_HORARIOS_FUNCIONAMENTO,
        filtros={"estabelecimento_id": f"eq.{contexto['estabelecimento_id']}"},
        colunas="id,dia_semana,fechado,horario_abertura,horario_fechamento,observacao,ativo",
        limite=14,
        order="dia_semana.asc",
    )
    if not resultado.get("ok"):
        return _erro_supabase(resultado)

    por_dia = {
        int(item.get("dia_semana")): item
        for item in _lista_dicts(resultado.get("data"))
        if _inteiro_intervalo(item.get("dia_semana"), minimo=0, maximo=6) is not None
    }
    items = []
    for dia in range(7):
        item = por_dia.get(dia)
        if item is None:
            items.append(
                {
                    "id": None,
                    "dia_semana": dia,
                    "fechado": True,
                    "horario_abertura": None,
                    "horario_fechamento": None,
                    "observacao": None,
                    "ativo": False,
                }
            )
        else:
            items.append(_serializar_horario(item))
    return {"ok": True, "items": items}


def atualizar_horarios(payload: Mapping[str, Any]) -> dict[str, Any]:
    contexto = _contexto_estabelecimento()
    if not contexto["ok"]:
        return contexto
    items_recebidos = payload.get("items") if isinstance(payload, Mapping) else None
    normalizado = _normalizar_horarios(items_recebidos, contexto["estabelecimento_id"])
    if not normalizado["ok"]:
        return normalizado

    resultado = supabase.upsert(
        TABELA_HORARIOS_FUNCIONAMENTO,
        normalizado["items"],
        on_conflict="estabelecimento_id,dia_semana",
    )
    if not resultado.get("ok"):
        return _erro_supabase(resultado)

    config_restaurante.limpar_cache_config()
    return listar_horarios()


def obter_configuracao_reserva() -> dict[str, Any]:
    contexto = _contexto_estabelecimento()
    if not contexto["ok"]:
        return contexto
    item = _buscar_configuracao_reserva(contexto["estabelecimento_id"])
    if item is None:
        return _erro("configuracao_reserva_nao_encontrada", "Configuracao de reserva nao encontrada.", status=404)
    if not item.get("ok", True):
        return item
    return {"ok": True, "data": _serializar_configuracao_reserva(item)}


def atualizar_configuracao_reserva(payload: Mapping[str, Any]) -> dict[str, Any]:
    contexto = _contexto_estabelecimento()
    if not contexto["ok"]:
        return contexto
    atual = _buscar_configuracao_reserva(contexto["estabelecimento_id"])
    if atual is None:
        return _erro("configuracao_reserva_nao_encontrada", "Configuracao de reserva nao encontrada.", status=404)
    if not atual.get("ok", True):
        return atual

    normalizado = _normalizar_configuracao_reserva(payload, atual)
    if not normalizado["ok"]:
        return normalizado
    dados = normalizado["data"]
    if not dados:
        return _erro("payload_vazio", "Informe ao menos um campo para atualizar.")

    resultado = supabase.atualizar(
        TABELA_CONFIGURACOES_RESERVA,
        dados,
        filtros={"id": f"eq.{atual['id']}", "estabelecimento_id": f"eq.{contexto['estabelecimento_id']}"},
    )
    if not resultado.get("ok"):
        return _erro_supabase(resultado)
    item = _primeiro_item(resultado.get("data"))
    if item is None:
        return _erro("configuracao_reserva_nao_encontrada", "Configuracao de reserva nao encontrada.", status=404)

    config_restaurante.limpar_cache_config()
    return {"ok": True, "data": _serializar_configuracao_reserva(item)}


def listar_espacos() -> dict[str, Any]:
    contexto = _contexto_estabelecimento()
    if not contexto["ok"]:
        return contexto
    resultado = supabase.selecionar(
        TABELA_ESPACOS,
        filtros={"estabelecimento_id": f"eq.{contexto['estabelecimento_id']}"},
        colunas="id,nome,descricao,capacidade_maxima,permite_preferencia,regras,ativo",
        limite=200,
        order="nome.asc",
    )
    if not resultado.get("ok"):
        return _erro_supabase(resultado)
    return {"ok": True, "items": [_serializar_espaco(item) for item in _lista_dicts(resultado.get("data"))]}


def criar_espaco(payload: Mapping[str, Any]) -> dict[str, Any]:
    contexto = _contexto_estabelecimento()
    if not contexto["ok"]:
        return contexto
    normalizado = _normalizar_espaco(payload, parcial=False)
    if not normalizado["ok"]:
        return normalizado
    conflito = _espaco_nome_ativo_existe(
        contexto["estabelecimento_id"],
        str(normalizado["data"].get("nome") or ""),
    )
    if not conflito["ok"]:
        return conflito
    if conflito["existe"]:
        return _erro("espaco_duplicado", "Ja existe um espaco ativo com esse nome.", status=409)

    dados = {"estabelecimento_id": contexto["estabelecimento_id"], **normalizado["data"]}
    resultado = supabase.inserir(TABELA_ESPACOS, dados)
    if not resultado.get("ok"):
        return _erro_supabase(resultado)
    item = _primeiro_item(resultado.get("data"))
    if item is None:
        return _erro("erro_supabase", "Nao foi possivel criar o espaco.", status=500)

    config_restaurante.limpar_cache_config()
    return {"ok": True, "data": _serializar_espaco(item), "_status": 201}


def atualizar_espaco(espaco_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    contexto = _contexto_estabelecimento()
    if not contexto["ok"]:
        return contexto
    espaco = _buscar_item_estabelecimento(TABELA_ESPACOS, espaco_id, contexto["estabelecimento_id"], _colunas_espaco())
    if espaco is None:
        return _erro("espaco_nao_encontrado", "Espaco nao encontrado.", status=404)
    if not espaco.get("ok", True):
        return espaco

    normalizado = _normalizar_espaco(payload, parcial=True)
    if not normalizado["ok"]:
        return normalizado
    dados = normalizado["data"]
    if not dados:
        return _erro("payload_vazio", "Informe ao menos um campo para atualizar.")

    nome_final = str(dados.get("nome") or espaco.get("nome") or "")
    ativo_final = bool(dados.get("ativo", espaco.get("ativo", True)))
    if ativo_final:
        conflito = _espaco_nome_ativo_existe(contexto["estabelecimento_id"], nome_final, ignorar_id=espaco_id)
        if not conflito["ok"]:
            return conflito
        if conflito["existe"]:
            return _erro("espaco_duplicado", "Ja existe um espaco ativo com esse nome.", status=409)

    resultado = supabase.atualizar(
        TABELA_ESPACOS,
        dados,
        filtros={"id": f"eq.{espaco_id}", "estabelecimento_id": f"eq.{contexto['estabelecimento_id']}"},
    )
    if not resultado.get("ok"):
        return _erro_supabase(resultado)
    item = _primeiro_item(resultado.get("data"))
    if item is None:
        return _erro("espaco_nao_encontrado", "Espaco nao encontrado.", status=404)

    config_restaurante.limpar_cache_config()
    return {"ok": True, "data": _serializar_espaco(item)}


def excluir_espaco(espaco_id: str) -> dict[str, Any]:
    return _desativar_item(TABELA_ESPACOS, espaco_id, _colunas_espaco(), "espaco_nao_encontrado", "Espaco nao encontrado.")


def listar_faqs(filtros: Mapping[str, Any] | None = None) -> dict[str, Any]:
    contexto = _contexto_estabelecimento()
    if not contexto["ok"]:
        return contexto
    filtros = filtros or {}
    page = _inteiro_minimo(filtros.get("page"), 1, padrao=1)
    page_size = min(_inteiro_minimo(filtros.get("page_size"), 1, padrao=PAGE_SIZE_PADRAO), PAGE_SIZE_MAX)

    resultado = supabase.selecionar(
        TABELA_FAQ_CONTEUDOS,
        filtros={"estabelecimento_id": f"eq.{contexto['estabelecimento_id']}"},
        colunas="id,categoria,titulo,conteudo,tags,ativo",
        limite=1000,
        order="categoria.asc,titulo.asc",
    )
    if not resultado.get("ok"):
        return _erro_supabase(resultado)

    items = [_serializar_faq(item) for item in _lista_dicts(resultado.get("data"))]
    categoria = _texto(filtros.get("categoria"), max_len=120)
    if categoria:
        categoria_norm = _normalizar_busca(categoria)
        items = [item for item in items if _normalizar_busca(str(item.get("categoria") or "")) == categoria_norm]

    ativo = _booleano_filtro(filtros.get("ativo"))
    if ativo is not None:
        items = [item for item in items if bool(item.get("ativo")) is ativo]

    search = _normalizar_busca(_texto(filtros.get("search"), max_len=120))
    if search:
        items = [item for item in items if search in _normalizar_busca(" ".join(_textos_faq_busca(item)))]

    total = len(items)
    inicio = (page - 1) * page_size
    fim = inicio + page_size
    pagina = items[inicio:fim]
    total_pages = (total + page_size - 1) // page_size if total else 0
    return {
        "ok": True,
        "items": pagina,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
    }


def criar_faq(payload: Mapping[str, Any]) -> dict[str, Any]:
    contexto = _contexto_estabelecimento()
    if not contexto["ok"]:
        return contexto
    normalizado = _normalizar_faq(payload, parcial=False)
    if not normalizado["ok"]:
        return normalizado
    dados = {"estabelecimento_id": contexto["estabelecimento_id"], **normalizado["data"]}
    resultado = supabase.inserir(TABELA_FAQ_CONTEUDOS, dados)
    if not resultado.get("ok"):
        return _erro_supabase(resultado)
    item = _primeiro_item(resultado.get("data"))
    if item is None:
        return _erro("erro_supabase", "Nao foi possivel criar o conteudo.", status=500)

    config_restaurante.limpar_cache_config()
    return {"ok": True, "data": _serializar_faq(item), "_status": 201}


def atualizar_faq(faq_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    contexto = _contexto_estabelecimento()
    if not contexto["ok"]:
        return contexto
    faq = _buscar_item_estabelecimento(TABELA_FAQ_CONTEUDOS, faq_id, contexto["estabelecimento_id"], _colunas_faq())
    if faq is None:
        return _erro("faq_nao_encontrada", "FAQ nao encontrada.", status=404)
    if not faq.get("ok", True):
        return faq

    normalizado = _normalizar_faq(payload, parcial=True)
    if not normalizado["ok"]:
        return normalizado
    dados = normalizado["data"]
    if not dados:
        return _erro("payload_vazio", "Informe ao menos um campo para atualizar.")

    resultado = supabase.atualizar(
        TABELA_FAQ_CONTEUDOS,
        dados,
        filtros={"id": f"eq.{faq_id}", "estabelecimento_id": f"eq.{contexto['estabelecimento_id']}"},
    )
    if not resultado.get("ok"):
        return _erro_supabase(resultado)
    item = _primeiro_item(resultado.get("data"))
    if item is None:
        return _erro("faq_nao_encontrada", "FAQ nao encontrada.", status=404)

    config_restaurante.limpar_cache_config()
    return {"ok": True, "data": _serializar_faq(item)}


def excluir_faq(faq_id: str) -> dict[str, Any]:
    return _desativar_item(TABELA_FAQ_CONTEUDOS, faq_id, _colunas_faq(), "faq_nao_encontrada", "FAQ nao encontrada.")


def _contexto_estabelecimento() -> dict[str, Any]:
    resultado = supabase.selecionar(
        TABELA_ESTABELECIMENTOS,
        filtros={"ativo": "eq.true"},
        colunas="id,slug,nome,telefone,whatsapp,endereco,ponto_referencia,timezone,ativo,created_at,updated_at",
        limite=2,
        order="created_at.asc",
    )
    if not resultado.get("ok"):
        return _erro_supabase(resultado)
    items = _lista_dicts(resultado.get("data"))
    if not items:
        return _erro("estabelecimento_nao_encontrado", "Nenhum estabelecimento ativo encontrado.", status=404)
    if len(items) > 1:
        logger.warning(
            "Mais de um estabelecimento ativo para configuracao admin; usando o primeiro. ids=%s",
            [str(item.get("id") or "") for item in items],
        )
    estabelecimento = dict(items[0])
    estabelecimento_id = str(estabelecimento.get("id") or "").strip()
    if not estabelecimento_id:
        return _erro("estabelecimento_invalido", "Estabelecimento ativo sem id.", status=500)
    return {"ok": True, "estabelecimento": estabelecimento, "estabelecimento_id": estabelecimento_id}


def _buscar_configuracao_reserva(estabelecimento_id: str) -> dict[str, Any] | None:
    resultado = supabase.selecionar(
        TABELA_CONFIGURACOES_RESERVA,
        filtros={"estabelecimento_id": f"eq.{estabelecimento_id}"},
        colunas="id,quantidade_minima,quantidade_maxima_automatica,horarios_permitidos,taxa_valor,taxa_convertida_consumacao,prazo_cancelamento_horas,pix_chave,pix_titular,exige_comprovante,tolerancia_atraso_minutos,politica_cancelamento,instrucoes_reserva,ativo",
        limite=1,
    )
    if not resultado.get("ok"):
        return _erro_supabase(resultado)
    return _primeiro_item(resultado.get("data"))


def _buscar_item_estabelecimento(tabela: str, item_id: str, estabelecimento_id: str, colunas: str) -> dict[str, Any] | None:
    item_id = str(item_id or "").strip()
    if not item_id:
        return None
    resultado = supabase.selecionar(
        tabela,
        filtros={"id": f"eq.{item_id}", "estabelecimento_id": f"eq.{estabelecimento_id}"},
        colunas=colunas,
        limite=1,
    )
    if not resultado.get("ok"):
        return _erro_supabase(resultado)
    return _primeiro_item(resultado.get("data"))


def _desativar_item(tabela: str, item_id: str, colunas: str, erro: str, mensagem: str) -> dict[str, Any]:
    contexto = _contexto_estabelecimento()
    if not contexto["ok"]:
        return contexto
    item = _buscar_item_estabelecimento(tabela, item_id, contexto["estabelecimento_id"], colunas)
    if item is None:
        return _erro(erro, mensagem, status=404)
    if not item.get("ok", True):
        return item

    resultado = supabase.atualizar(
        tabela,
        {"ativo": False},
        filtros={"id": f"eq.{item_id}", "estabelecimento_id": f"eq.{contexto['estabelecimento_id']}"},
    )
    if not resultado.get("ok"):
        return _erro_supabase(resultado)
    atualizado = _primeiro_item(resultado.get("data"))
    if atualizado is None:
        return _erro(erro, mensagem, status=404)

    config_restaurante.limpar_cache_config()
    if tabela == TABELA_ESPACOS:
        return {"ok": True, "data": _serializar_espaco(atualizado)}
    return {"ok": True, "data": _serializar_faq(atualizado)}


def _normalizar_estabelecimento(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return _erro("payload_invalido", "JSON precisa ser um objeto.")
    bloqueados = ESTABELECIMENTO_IMUTAVEIS.intersection(payload)
    if bloqueados:
        return _erro("campo_nao_editavel", f"Campo nao editavel: {sorted(bloqueados)[0]}.")
    dados: dict[str, Any] = {}
    for campo in ESTABELECIMENTO_EDITAVEIS.intersection(payload):
        valor = payload.get(campo)
        if campo == "ativo":
            booleano = _booleano(valor)
            if booleano is None:
                return _erro("ativo_invalido", "O campo ativo deve ser verdadeiro ou falso.")
            dados[campo] = booleano
        elif campo == "timezone":
            texto = _texto_obrigatorio(valor, campo="timezone", max_len=80)
            if isinstance(texto, dict):
                return texto
            if not _timezone_valido(texto):
                return _erro("timezone_invalido", "Timezone invalido.")
            dados[campo] = texto
        elif campo == "nome":
            texto = _texto_obrigatorio(valor, campo="nome", max_len=120)
            if isinstance(texto, dict):
                return texto
            dados[campo] = texto
        else:
            texto = _texto_opcional(valor, max_len=500 if campo in {"endereco", "ponto_referencia"} else 80)
            if isinstance(texto, dict):
                return texto
            dados[campo] = texto
    return {"ok": True, "data": dados}


def _normalizar_horarios(items: Any, estabelecimento_id: str) -> dict[str, Any]:
    if not isinstance(items, list) or len(items) != 7:
        return _erro("horarios_invalidos", "Envie os sete dias da semana em items.")
    vistos: set[int] = set()
    normalizados: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            return _erro("horario_invalido", "Cada dia precisa ser um objeto.")
        dia = _inteiro_intervalo(item.get("dia_semana"), minimo=0, maximo=6)
        if dia is None:
            return _erro("dia_semana_invalido", "dia_semana deve estar entre 0 e 6.")
        if dia in vistos:
            return _erro("dia_semana_duplicado", "Nao envie dias duplicados.")
        vistos.add(dia)

        fechado = _booleano(item.get("fechado"))
        if fechado is None:
            return _erro("fechado_invalido", "O campo fechado deve ser verdadeiro ou falso.")
        ativo = _booleano(item.get("ativo", True))
        if ativo is None:
            return _erro("ativo_invalido", "O campo ativo deve ser verdadeiro ou falso.")
        abertura = _horario_opcional(item.get("horario_abertura"))
        fechamento = _horario_opcional(item.get("horario_fechamento"))
        if isinstance(abertura, dict):
            return abertura
        if isinstance(fechamento, dict):
            return fechamento
        if not fechado and (not abertura or not fechamento):
            return _erro("horario_obrigatorio", "Quando o dia esta aberto, abertura e fechamento sao obrigatorios.")
        observacao = _texto_opcional(item.get("observacao"), max_len=300)
        if isinstance(observacao, dict):
            return observacao
        normalizados.append(
            {
                "estabelecimento_id": estabelecimento_id,
                "dia_semana": dia,
                "fechado": fechado,
                "horario_abertura": None if fechado else abertura,
                "horario_fechamento": None if fechado else fechamento,
                "observacao": observacao,
                "ativo": ativo,
            }
        )
    return {"ok": True, "items": sorted(normalizados, key=lambda item: item["dia_semana"])}


def _normalizar_configuracao_reserva(payload: Mapping[str, Any], atual: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return _erro("payload_invalido", "JSON precisa ser um objeto.")
    dados: dict[str, Any] = {}
    for campo in RESERVA_EDITAVEIS.intersection(payload):
        valor = payload.get(campo)
        if campo in {"quantidade_minima", "quantidade_maxima_automatica"}:
            inteiro = _inteiro_positivo_ou_none(valor, campo=campo)
            if isinstance(inteiro, dict):
                return inteiro
            dados[campo] = inteiro
        elif campo == "horarios_permitidos":
            horarios = _normalizar_lista_horarios(valor)
            if isinstance(horarios, dict):
                return horarios
            dados[campo] = horarios
        elif campo == "taxa_valor":
            monetario = _monetario_nao_negativo(valor)
            if isinstance(monetario, dict):
                return monetario
            dados[campo] = monetario
        elif campo in {"prazo_cancelamento_horas", "tolerancia_atraso_minutos"}:
            inteiro = _inteiro_nao_negativo_ou_none(valor, campo=campo)
            if isinstance(inteiro, dict):
                return inteiro
            dados[campo] = inteiro
        elif campo in {"taxa_convertida_consumacao", "exige_comprovante", "ativo"}:
            booleano = _booleano(valor)
            if booleano is None:
                return _erro(f"{campo}_invalido", f"O campo {campo} deve ser verdadeiro ou falso.")
            dados[campo] = booleano
        elif campo in {"politica_cancelamento", "instrucoes_reserva"}:
            texto = _texto_opcional(valor, max_len=3000)
            if isinstance(texto, dict):
                return texto
            dados[campo] = texto
        elif campo in {"pix_chave", "pix_titular"}:
            texto = _texto_opcional(valor, max_len=200)
            if isinstance(texto, dict):
                return texto
            dados[campo] = texto

    minimo = dados.get("quantidade_minima", atual.get("quantidade_minima"))
    maximo = dados.get("quantidade_maxima_automatica", atual.get("quantidade_maxima_automatica"))
    if minimo is not None and maximo is not None and int(maximo) < int(minimo):
        return _erro("quantidade_maxima_invalida", "A quantidade maxima deve ser maior ou igual a minima.")
    return {"ok": True, "data": dados}


def _normalizar_espaco(payload: Mapping[str, Any], *, parcial: bool) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return _erro("payload_invalido", "JSON precisa ser um objeto.")
    dados: dict[str, Any] = {}
    for campo in ESPACO_EDITAVEIS.intersection(payload):
        valor = payload.get(campo)
        if campo == "nome":
            texto = _texto_obrigatorio(valor, campo="nome", max_len=120)
            if isinstance(texto, dict):
                return texto
            dados[campo] = texto
        elif campo == "capacidade_maxima":
            capacidade = _inteiro_positivo_ou_none(valor, campo="capacidade_maxima")
            if isinstance(capacidade, dict):
                return capacidade
            dados[campo] = capacidade
        elif campo in {"permite_preferencia", "ativo"}:
            booleano = _booleano(valor)
            if booleano is None:
                return _erro(f"{campo}_invalido", f"O campo {campo} deve ser verdadeiro ou falso.")
            dados[campo] = booleano
        else:
            texto = _texto_opcional(valor, max_len=2000 if campo == "regras" else 1000)
            if isinstance(texto, dict):
                return texto
            dados[campo] = texto
    if not parcial and "nome" not in dados:
        return _erro("nome_obrigatorio", "Nome e obrigatorio.")
    if not parcial:
        dados.setdefault("descricao", None)
        dados.setdefault("capacidade_maxima", None)
        dados.setdefault("permite_preferencia", True)
        dados.setdefault("regras", None)
        dados.setdefault("ativo", True)
    return {"ok": True, "data": dados}


def _normalizar_faq(payload: Mapping[str, Any], *, parcial: bool) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return _erro("payload_invalido", "JSON precisa ser um objeto.")
    dados: dict[str, Any] = {}
    for campo in FAQ_EDITAVEIS.intersection(payload):
        valor = payload.get(campo)
        if campo in {"categoria", "titulo", "conteudo"}:
            texto = _texto_obrigatorio(valor, campo=campo, max_len=4000 if campo == "conteudo" else 180)
            if isinstance(texto, dict):
                return texto
            dados[campo] = texto
        elif campo == "tags":
            tags = _normalizar_tags(valor)
            if isinstance(tags, dict):
                return tags
            dados[campo] = tags
        elif campo == "ativo":
            booleano = _booleano(valor)
            if booleano is None:
                return _erro("ativo_invalido", "O campo ativo deve ser verdadeiro ou falso.")
            dados[campo] = booleano
    if not parcial:
        for campo in ("categoria", "titulo", "conteudo"):
            if campo not in dados:
                return _erro(f"{campo}_obrigatorio", f"{campo} e obrigatorio.")
        dados.setdefault("tags", [])
        dados.setdefault("ativo", True)
    return {"ok": True, "data": dados}


def _espaco_nome_ativo_existe(estabelecimento_id: str, nome: str, *, ignorar_id: str = "") -> dict[str, Any]:
    resultado = supabase.selecionar(
        TABELA_ESPACOS,
        filtros={"estabelecimento_id": f"eq.{estabelecimento_id}", "ativo": "eq.true"},
        colunas="id,nome",
        limite=200,
    )
    if not resultado.get("ok"):
        return _erro_supabase(resultado)
    nome_normalizado = _normalizar_nome_unico(nome)
    for item in _lista_dicts(resultado.get("data")):
        if ignorar_id and str(item.get("id") or "") == ignorar_id:
            continue
        if _normalizar_nome_unico(str(item.get("nome") or "")) == nome_normalizado:
            return {"ok": True, "existe": True}
    return {"ok": True, "existe": False}


def _erro_supabase(resultado: Mapping[str, Any]) -> dict[str, Any]:
    status = int(resultado.get("status") or 500)
    if status == 404:
        return _erro("supabase_nao_encontrado", "Recurso nao encontrado no Supabase.", status=404)
    if status == 401 or status == 403:
        return _erro("supabase_nao_autorizado", "Backend sem autorizacao para acessar o Supabase.", status=500)
    if status == 409:
        return _erro("conflito", "Registro em conflito com dados existentes.", status=409)
    detalhe = str(resultado.get("detalhe") or resultado.get("erro") or "Erro ao acessar Supabase.")
    logger.warning("Falha Supabase em configuracoes admin: tabela=%s status=%s detalhe=%s", resultado.get("tabela", ""), status, detalhe[:300])
    return _erro("erro_supabase", "Nao foi possivel salvar ou consultar as configuracoes agora.", status=500)


def _erro(erro: str, mensagem: str, *, status: int = 400) -> dict[str, Any]:
    return {"ok": False, "erro": erro, "mensagem": mensagem, "_status": status}


def _serializar_estabelecimento(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _texto(item.get("id")),
        "slug": _texto(item.get("slug")),
        "nome": _texto(item.get("nome")),
        "telefone": _nulo_ou_texto(item.get("telefone")),
        "whatsapp": _nulo_ou_texto(item.get("whatsapp")),
        "endereco": _nulo_ou_texto(item.get("endereco")),
        "ponto_referencia": _nulo_ou_texto(item.get("ponto_referencia")),
        "timezone": _texto(item.get("timezone")) or "America/Sao_Paulo",
        "ativo": bool(item.get("ativo", True)),
    }


def _serializar_horario(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _nulo_ou_texto(item.get("id")),
        "dia_semana": int(item.get("dia_semana", 0)),
        "fechado": bool(item.get("fechado")),
        "horario_abertura": _normalizar_horario_saida(item.get("horario_abertura")),
        "horario_fechamento": _normalizar_horario_saida(item.get("horario_fechamento")),
        "observacao": _nulo_ou_texto(item.get("observacao")),
        "ativo": bool(item.get("ativo", True)),
    }


def _serializar_configuracao_reserva(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _texto(item.get("id")),
        "quantidade_minima": _inteiro_ou_none(item.get("quantidade_minima")),
        "quantidade_maxima_automatica": _inteiro_ou_none(item.get("quantidade_maxima_automatica")),
        "horarios_permitidos": _normalizar_lista_horarios(item.get("horarios_permitidos"))
        if isinstance(_normalizar_lista_horarios(item.get("horarios_permitidos")), list)
        else [],
        "taxa_valor": _float_ou_none(item.get("taxa_valor")),
        "taxa_convertida_consumacao": bool(item.get("taxa_convertida_consumacao")),
        "prazo_cancelamento_horas": _inteiro_ou_none(item.get("prazo_cancelamento_horas")),
        "pix_chave": _nulo_ou_texto(item.get("pix_chave")),
        "pix_titular": _nulo_ou_texto(item.get("pix_titular")),
        "exige_comprovante": bool(item.get("exige_comprovante")),
        "tolerancia_atraso_minutos": _inteiro_ou_none(item.get("tolerancia_atraso_minutos")),
        "politica_cancelamento": _nulo_ou_texto(item.get("politica_cancelamento")),
        "instrucoes_reserva": _nulo_ou_texto(item.get("instrucoes_reserva")),
        "ativo": bool(item.get("ativo", True)),
    }


def _serializar_espaco(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _texto(item.get("id")),
        "nome": _texto(item.get("nome")),
        "descricao": _nulo_ou_texto(item.get("descricao")),
        "capacidade_maxima": _inteiro_ou_none(item.get("capacidade_maxima")),
        "permite_preferencia": bool(item.get("permite_preferencia", True)),
        "regras": _nulo_ou_texto(item.get("regras")),
        "ativo": bool(item.get("ativo", True)),
    }


def _serializar_faq(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _texto(item.get("id")),
        "categoria": _texto(item.get("categoria")),
        "titulo": _texto(item.get("titulo")),
        "conteudo": _texto(item.get("conteudo")),
        "tags": _normalizar_tags(item.get("tags")) if isinstance(_normalizar_tags(item.get("tags")), list) else [],
        "ativo": bool(item.get("ativo", True)),
    }


def _colunas_espaco() -> str:
    return "id,nome,descricao,capacidade_maxima,permite_preferencia,regras,ativo"


def _colunas_faq() -> str:
    return "id,categoria,titulo,conteudo,tags,ativo"


def _lista_dicts(valor: Any) -> list[Mapping[str, Any]]:
    return [item for item in valor if isinstance(item, Mapping)] if isinstance(valor, list) else []


def _primeiro_item(valor: Any) -> dict[str, Any] | None:
    items = _lista_dicts(valor)
    return dict(items[0]) if items else None


def _texto(valor: Any, *, max_len: int | None = None) -> str:
    texto = str(valor or "").strip()
    if max_len is not None:
        texto = texto[:max_len]
    return texto


def _nulo_ou_texto(valor: Any) -> str | None:
    texto = str(valor or "").strip()
    return texto or None


def _texto_opcional(valor: Any, *, max_len: int) -> str | None | dict[str, Any]:
    if valor is None:
        return None
    texto = str(valor).replace("\r", " ").replace("\n", " ").strip()
    if len(texto) > max_len:
        return _erro("texto_muito_longo", f"Texto maior que o limite de {max_len} caracteres.")
    return texto or None


def _texto_obrigatorio(valor: Any, *, campo: str, max_len: int) -> str | dict[str, Any]:
    texto = str(valor or "").replace("\r", " ").replace("\n", " ").strip()
    if not texto:
        return _erro(f"{campo}_obrigatorio", f"{campo} e obrigatorio.")
    if len(texto) > max_len:
        return _erro("texto_muito_longo", f"Texto maior que o limite de {max_len} caracteres.")
    return texto


def _booleano(valor: Any) -> bool | None:
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str):
        texto = valor.strip().lower()
        if texto in {"true", "1", "sim", "yes"}:
            return True
        if texto in {"false", "0", "nao", "no"}:
            return False
    return None


def _booleano_filtro(valor: Any) -> bool | None:
    if valor in (None, ""):
        return None
    return _booleano(valor)


def _horario_opcional(valor: Any) -> str | None | dict[str, Any]:
    if valor in (None, ""):
        return None
    texto = _normalizar_horario_saida(valor)
    if not texto or not HORARIO_RE.fullmatch(texto):
        return _erro("horario_invalido", "O horario deve estar no formato HH:MM.")
    return texto


def _normalizar_horario_saida(valor: Any) -> str | None:
    texto = str(valor or "").strip()
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?", texto)
    if not match:
        return None
    return f"{match.group(1)}:{match.group(2)}"


def _normalizar_lista_horarios(valor: Any) -> list[str] | dict[str, Any]:
    if not isinstance(valor, list):
        return _erro("horarios_permitidos_invalidos", "horarios_permitidos deve ser uma lista.")
    vistos: set[str] = set()
    horarios: list[str] = []
    for item in valor:
        horario = _horario_opcional(item)
        if isinstance(horario, dict):
            return horario
        if horario and horario not in vistos:
            vistos.add(horario)
            horarios.append(horario)
    return sorted(horarios, key=lambda item: int(item[:2]) * 60 + int(item[3:5]))


def _normalizar_tags(valor: Any) -> list[str] | dict[str, Any]:
    if valor is None:
        return []
    if not isinstance(valor, list):
        return _erro("tags_invalidas", "tags deve ser uma lista de textos.")
    tags: list[str] = []
    vistos: set[str] = set()
    for item in valor:
        texto = str(item or "").replace("\r", " ").replace("\n", " ").strip()
        texto = re.sub(r"\s+", " ", texto)
        if not texto:
            continue
        if len(texto) > 60:
            return _erro("tag_muito_longa", "Cada tag deve ter no maximo 60 caracteres.")
        chave = _normalizar_busca(texto)
        if chave not in vistos:
            vistos.add(chave)
            tags.append(texto)
    return tags


def _inteiro_intervalo(valor: Any, *, minimo: int, maximo: int) -> int | None:
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return None
    return numero if minimo <= numero <= maximo else None


def _inteiro_minimo(valor: Any, minimo: int, *, padrao: int) -> int:
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return padrao
    return max(minimo, numero)


def _inteiro_positivo_ou_none(valor: Any, *, campo: str) -> int | None | dict[str, Any]:
    if valor in (None, ""):
        return None
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return _erro(f"{campo}_invalido", f"{campo} deve ser um numero inteiro positivo.")
    if numero <= 0:
        return _erro(f"{campo}_invalido", f"{campo} deve ser positivo.")
    return numero


def _inteiro_nao_negativo_ou_none(valor: Any, *, campo: str) -> int | None | dict[str, Any]:
    if valor in (None, ""):
        return None
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return _erro(f"{campo}_invalido", f"{campo} deve ser um numero inteiro nao negativo.")
    if numero < 0:
        return _erro(f"{campo}_invalido", f"{campo} deve ser nao negativo.")
    return numero


def _inteiro_ou_none(valor: Any) -> int | None:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _monetario_nao_negativo(valor: Any) -> float | None | dict[str, Any]:
    if valor in (None, ""):
        return None
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, ValueError):
        return _erro("taxa_valor_invalida", "Valor monetario invalido.")
    if numero < 0:
        return _erro("taxa_valor_invalida", "Valor monetario nao pode ser negativo.")
    return float(numero)


def _float_ou_none(valor: Any) -> float | None:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _timezone_valido(valor: str) -> bool:
    try:
        ZoneInfo(valor)
    except ZoneInfoNotFoundError:
        return False
    return True


def _normalizar_nome_unico(valor: str) -> str:
    return re.sub(r"\s+", " ", _normalizar_busca(valor)).strip()


def _normalizar_busca(valor: str) -> str:
    texto = unicodedata.normalize("NFD", str(valor or "").lower())
    return "".join(char for char in texto if unicodedata.category(char) != "Mn")


def _textos_faq_busca(item: Mapping[str, Any]) -> list[str]:
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    return [
        str(item.get("categoria") or ""),
        str(item.get("titulo") or ""),
        str(item.get("conteudo") or ""),
        " ".join(str(tag) for tag in tags),
    ]
