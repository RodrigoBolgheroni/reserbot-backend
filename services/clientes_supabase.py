from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
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
    duplicados: int
    erro: str
    detalhe: str


class ResultadoListaClientes(TypedDict):
    clientes: list[dict[str, Any]]
    total: int | None


class ResultadoAniversariosProximos(TypedDict):
    dias: int
    total: int
    clientes: list[dict[str, Any]]
    analisados: int


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
    total_normalizados = len(payload)
    payload, duplicados = _deduplicar_por_telefone(payload)
    logger.info(
        "Confirmacao de importacao: clientes_extraidos=%s clientes_apos_normalizacao=%s duplicados_mesclados=%s clientes_finais=%s.",
        len(registros),
        total_normalizados,
        duplicados,
        len(payload),
    )
    if not payload:
        return {
            "ok": True,
            "tabela": tabela_final,
            "enviados": 0,
            "salvos": 0,
            "duplicados": duplicados,
        }

    logger.info("Campos enviados para Supabase.%s: %s", tabela_final, ", ".join(CAMPOS_CLIENTES))
    salvos = 0
    batch_size = _batch_size()
    for indice, lote in enumerate(_chunks(payload, batch_size), start=1):
        lote, duplicados_lote = _deduplicar_por_telefone(lote)
        if duplicados_lote:
            duplicados += duplicados_lote
            logger.warning(
                "Lote %s ainda continha %s telefone(s) duplicado(s); registros foram mesclados antes do upsert.",
                indice,
                duplicados_lote,
            )
        _validar_lote_sem_duplicados(lote, indice)
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
                "duplicados": duplicados,
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
        "duplicados": duplicados,
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
    offset: int = 0,
    contar: bool = False,
) -> ResultadoListaClientes:
    tabela_final = tabela or supabase.tabela_env("SUPABASE_CLIENTES_TABLE", DEFAULT_TABLE)
    resultado = supabase.selecionar(
        tabela_final,
        colunas="*",
        limite=max(1, limite),
        offset=max(0, offset),
        contar=contar,
    )
    if not resultado.get("ok"):
        logger.warning("Nao foi possivel listar clientes: %s", resultado.get("erro"))
        return {"clientes": [], "total": None}

    dados = resultado.get("data")
    if not isinstance(dados, list):
        return {"clientes": [], "total": _total_resultado(resultado)}
    clientes = [item for item in dados if isinstance(item, dict)]
    return {
        "clientes": sorted(clientes, key=lambda item: str(item.get("nome") or "").lower()),
        "total": _total_resultado(resultado),
    }


def listar_aniversarios_proximos(
    *,
    dias: int = 15,
    limite_clientes: int = 50,
    hoje: date | None = None,
    tabela: str | None = None,
) -> ResultadoAniversariosProximos:
    dias_final = max(1, min(int(dias or 15), 60))
    limite_final = max(1, min(int(limite_clientes or 50), 2000))
    hoje_final = hoje or date.today()
    tabela_final = tabela or supabase.tabela_env("SUPABASE_CLIENTES_TABLE", DEFAULT_TABLE)
    page_size = 500
    offset = 0
    total_base: int | None = None
    analisados = 0
    encontrados: list[dict[str, Any]] = []

    while True:
        resultado = supabase.selecionar(
            tabela_final,
            colunas="id,nome,telefone,data_nascimento,aniversario_ddmm,perfil_id,perfil_nome",
            limite=page_size,
            offset=offset,
            contar=offset == 0,
        )
        if not resultado.get("ok"):
            logger.warning("Nao foi possivel listar aniversarios proximos: %s", resultado.get("erro"))
            break

        if offset == 0:
            total_base = _total_resultado(resultado)

        dados = resultado.get("data")
        if not isinstance(dados, list) or not dados:
            break

        for cliente in dados:
            if not isinstance(cliente, dict):
                continue
            analisados += 1
            dias_ate = _dias_ate_aniversario(cliente, hoje_final)
            if dias_ate is None or dias_ate > dias_final:
                continue
            encontrados.append(_resumir_aniversariante(cliente, dias_ate, hoje_final))

        offset += len(dados)
        if total_base is not None and offset >= total_base:
            break
        if len(dados) < page_size:
            break

    encontrados.sort(key=lambda item: (int(item.get("dias_ate_aniversario") or 0), str(item.get("nome") or "").lower()))
    logger.info(
        "Aniversarios proximos calculados: dias=%s clientes_analisados=%s aniversariantes=%s.",
        dias_final,
        analisados,
        len(encontrados),
    )
    return {
        "dias": dias_final,
        "total": len(encontrados),
        "clientes": encontrados[:limite_final],
        "analisados": analisados,
    }


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


def _dias_ate_aniversario(cliente: Mapping[str, Any], hoje: date) -> int | None:
    dia_mes = _dia_mes_aniversario(cliente)
    if dia_mes is None:
        return None

    dia, mes = dia_mes
    aniversario = _data_aniversario_no_ano(dia, mes, hoje.year)
    if aniversario is None:
        return None
    if aniversario < hoje:
        aniversario = _data_aniversario_no_ano(dia, mes, hoje.year + 1)
    if aniversario is None:
        return None
    return (aniversario - hoje).days


def _dia_mes_aniversario(cliente: Mapping[str, Any]) -> tuple[int, int] | None:
    data_nascimento = _texto(cliente.get("data_nascimento"))
    if data_nascimento:
        try:
            data = datetime.strptime(data_nascimento[:10], "%Y-%m-%d").date()
            return data.day, data.month
        except ValueError:
            pass

    ddmm = _texto(cliente.get("aniversario_ddmm"))
    match = re.match(r"^\s*(\d{1,2})[/-](\d{1,2})\s*$", ddmm)
    if not match:
        return None
    dia = int(match.group(1))
    mes = int(match.group(2))
    if not (1 <= dia <= 31 and 1 <= mes <= 12):
        return None
    return dia, mes


def _data_aniversario_no_ano(dia: int, mes: int, ano: int) -> date | None:
    try:
        return date(ano, mes, dia)
    except ValueError:
        return None


def _resumir_aniversariante(cliente: Mapping[str, Any], dias_ate: int, hoje: date) -> dict[str, Any]:
    dia_mes = _dia_mes_aniversario(cliente)
    aniversario = ""
    if dia_mes is not None:
        dia, mes = dia_mes
        data = _data_aniversario_no_ano(dia, mes, hoje.year)
        if data is not None and data < hoje:
            data = _data_aniversario_no_ano(dia, mes, hoje.year + 1)
        aniversario = f"{mes:02d}-{dia:02d}"

    return {
        "id": cliente.get("id"),
        "nome": _texto(cliente.get("nome")),
        "telefone": _texto(cliente.get("telefone")),
        "aniversario": aniversario,
        "aniversario_ddmm": f"{dia_mes[0]:02d}/{dia_mes[1]:02d}" if dia_mes is not None else None,
        "data_nascimento": cliente.get("data_nascimento"),
        "dias_ate_aniversario": dias_ate,
        "perfil_id": cliente.get("perfil_id"),
        "perfil_nome": _texto(cliente.get("perfil_nome")) or None,
        "perfil": _texto(cliente.get("perfil_nome")) or None,
    }


def _lista_json(valor: Any) -> list[Any]:
    if isinstance(valor, list):
        return valor
    if isinstance(valor, tuple):
        return list(valor)
    return []


def _dict_json(valor: Any) -> dict[str, Any]:
    return dict(valor) if isinstance(valor, Mapping) else {}


def _deduplicar_por_telefone(registros: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    deduplicados: dict[str, dict[str, Any]] = {}
    duplicados_por_telefone: dict[str, int] = {}

    for registro in registros:
        telefone = _texto(registro.get("telefone"))
        if not telefone:
            continue
        registro_normalizado = _normalizar_campos_cliente(registro)
        if telefone in deduplicados:
            deduplicados[telefone] = _mesclar_cliente(deduplicados[telefone], registro_normalizado)
            duplicados_por_telefone[telefone] = duplicados_por_telefone.get(telefone, 1) + 1
        else:
            deduplicados[telefone] = registro_normalizado

    if duplicados_por_telefone:
        exemplos = ", ".join(list(duplicados_por_telefone)[:10])
        logger.warning(
            "Clientes duplicados por telefone mesclados: total_duplicado=%s telefones_exemplo=%s",
            sum(quantidade - 1 for quantidade in duplicados_por_telefone.values()),
            exemplos,
        )

    return list(deduplicados.values()), sum(quantidade - 1 for quantidade in duplicados_por_telefone.values())


def _mesclar_cliente(atual: dict[str, Any], novo: dict[str, Any]) -> dict[str, Any]:
    mesclado = dict(atual)
    score_atual = _perfil_score(mesclado)
    score_novo = _perfil_score(novo)

    for campo in CAMPOS_CLIENTES:
        valor_atual = mesclado.get(campo)
        valor_novo = novo.get(campo)
        if campo == "telefones":
            mesclado[campo] = _mesclar_listas(valor_atual, valor_novo)
        elif campo == "metadata":
            mesclado[campo] = _mesclar_metadata(valor_atual, valor_novo)
        elif campo in ("perfil_id", "perfil_nome"):
            if score_novo > score_atual or _vazio(valor_atual):
                mesclado[campo] = valor_novo
        elif _vazio(valor_atual) and not _vazio(valor_novo):
            mesclado[campo] = valor_novo
        elif campo in ("nome", "info_topo_pdf", "periodo_aniversario", "tipo", "regiao") and _mais_completo(valor_novo, valor_atual):
            mesclado[campo] = valor_novo

    if score_novo > score_atual:
        metadata = _dict_json(mesclado.get("metadata"))
        metadata["perfil_classificacao"] = _dict_json(_dict_json(novo.get("metadata")).get("perfil_classificacao"))
        mesclado["metadata"] = metadata

    return _normalizar_campos_cliente(mesclado)


def _validar_lote_sem_duplicados(lote: Sequence[Mapping[str, Any]], indice: int) -> None:
    vistos: set[str] = set()
    duplicados: set[str] = set()
    for registro in lote:
        telefone = _texto(registro.get("telefone"))
        if telefone in vistos:
            duplicados.add(telefone)
        vistos.add(telefone)
    if duplicados:
        exemplos = ", ".join(list(duplicados)[:10])
        raise ValueError(f"Lote {indice} contem telefone(s) duplicado(s) antes do upsert: {exemplos}")


def _mesclar_listas(valor_atual: Any, valor_novo: Any) -> list[Any]:
    itens: list[Any] = []
    for valor in [*_lista_json(valor_atual), *_lista_json(valor_novo)]:
        if valor not in itens:
            itens.append(valor)
    return itens


def _mesclar_metadata(valor_atual: Any, valor_novo: Any) -> dict[str, Any]:
    atual = _dict_json(valor_atual)
    novo = _dict_json(valor_novo)
    mesclado = dict(atual)
    for chave, valor in novo.items():
        if chave not in mesclado or _vazio(mesclado.get(chave)):
            mesclado[chave] = valor
        elif isinstance(mesclado.get(chave), dict) and isinstance(valor, Mapping):
            mesclado[chave] = _mesclar_metadata(mesclado[chave], valor)
        elif isinstance(mesclado.get(chave), list) and isinstance(valor, list):
            mesclado[chave] = _mesclar_listas(mesclado[chave], valor)
    return mesclado


def _perfil_score(cliente: Mapping[str, Any]) -> float:
    metadata = cliente.get("metadata")
    if not isinstance(metadata, Mapping):
        return 0.0
    perfil = metadata.get("perfil_classificacao")
    if not isinstance(perfil, Mapping):
        return 0.0
    try:
        return float(perfil.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _vazio(valor: Any) -> bool:
    return valor in ("", None, [], {})


def _mais_completo(novo: Any, atual: Any) -> bool:
    return isinstance(novo, str) and len(novo.strip()) > len(_texto(atual))


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


def _total_resultado(resultado: Mapping[str, Any]) -> int | None:
    total = resultado.get("total")
    if isinstance(total, int):
        return total
    return None
