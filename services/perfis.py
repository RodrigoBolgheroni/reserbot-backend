from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, Final, TypedDict

from services import mensagens, supabase
from services.modelos import PerfilCliente


logger = logging.getLogger(__name__)

TABELA_PERFIS_PADRAO: Final[str] = "perfis_clientes"
PLACEHOLDERS_SUPORTADOS: Final[tuple[str, ...]] = ("nome", "primeiro_nome", "idade", "telefone", "perfil")


class ResultadoPerfil(TypedDict, total=False):
    ok: bool
    tabela: str
    perfil: PerfilCliente
    perfis: list[PerfilCliente]
    erro: str
    detalhe: str


def listar_perfis(*, ativos: bool | None = None, tabela: str | None = None) -> list[PerfilCliente]:
    tabela_final = tabela or _tabela_perfis()
    filtros = {"ativo": f"eq.{str(ativos).lower()}"} if ativos is not None else None
    resultado = supabase.selecionar(tabela_final, filtros=filtros)
    if not resultado.get("ok"):
        logger.warning("Nao foi possivel listar perfis de clientes: %s", resultado.get("erro"))
        return []

    dados = resultado.get("data")
    if not isinstance(dados, list):
        return []

    perfis = [_normalizar_perfil_bruto(perfil) for perfil in dados if isinstance(perfil, dict)]
    return sorted(perfis, key=lambda perfil: perfil.get("nome", "").casefold())


def buscar_perfil(perfil_id: str, *, tabela: str | None = None) -> PerfilCliente | None:
    perfil_id_limpo = perfil_id.strip()
    if not perfil_id_limpo:
        return None

    resultado = supabase.selecionar(
        tabela or _tabela_perfis(),
        filtros={"id": f"eq.{perfil_id_limpo}"},
        limite=1,
    )
    if not resultado.get("ok"):
        logger.warning("Nao foi possivel buscar perfil %s: %s", perfil_id_limpo, resultado.get("erro"))
        return None
    return _primeiro_perfil(resultado.get("data"))


def buscar_perfil_por_nome(nome: str, *, tabela: str | None = None) -> PerfilCliente | None:
    nome_limpo = _texto(nome)
    if not nome_limpo:
        return None

    resultado = supabase.selecionar(
        tabela or _tabela_perfis(),
        filtros={"nome": f"eq.{nome_limpo}"},
        limite=1,
    )
    if not resultado.get("ok"):
        logger.warning("Nao foi possivel buscar perfil %s: %s", nome_limpo, resultado.get("erro"))
        return None
    return _primeiro_perfil(resultado.get("data"))


def salvar_perfil(perfil: Mapping[str, Any], *, tabela: str | None = None) -> ResultadoPerfil:
    tabela_final = tabela or _tabela_perfis()
    try:
        payload = normalizar_perfil(perfil)
    except ValueError as erro:
        return {"ok": False, "tabela": tabela_final, "erro": str(erro)}

    perfil_id = _texto(perfil.get("id"))
    if perfil_id:
        resultado = supabase.atualizar(
            tabela_final,
            payload,
            filtros={"id": f"eq.{perfil_id}"},
        )
    else:
        resultado = supabase.inserir(tabela_final, payload)

    if not resultado.get("ok"):
        return {
            "ok": False,
            "tabela": tabela_final,
            "erro": resultado.get("erro", "Falha ao salvar perfil"),
            "detalhe": resultado.get("detalhe", ""),
        }

    perfil_salvo = _primeiro_perfil(resultado.get("data")) or payload
    logger.info("Perfil de cliente salvo: %s.", perfil_salvo.get("nome", ""))
    return {"ok": True, "tabela": tabela_final, "perfil": perfil_salvo}


def excluir_perfil(perfil_id: str, *, tabela: str | None = None) -> ResultadoPerfil:
    perfil_id_limpo = perfil_id.strip()
    tabela_final = tabela or _tabela_perfis()
    if not perfil_id_limpo:
        return {"ok": False, "tabela": tabela_final, "erro": "id do perfil obrigatorio"}

    resultado = supabase.deletar(tabela_final, filtros={"id": f"eq.{perfil_id_limpo}"})
    if not resultado.get("ok"):
        return {
            "ok": False,
            "tabela": tabela_final,
            "erro": resultado.get("erro", "Falha ao excluir perfil"),
            "detalhe": resultado.get("detalhe", ""),
        }
    logger.info("Perfil de cliente excluido: %s.", perfil_id_limpo)
    return {"ok": True, "tabela": tabela_final}


def definir_ativo(perfil_id: str, ativo: bool, *, tabela: str | None = None) -> ResultadoPerfil:
    perfil_id_limpo = perfil_id.strip()
    tabela_final = tabela or _tabela_perfis()
    if not perfil_id_limpo:
        return {"ok": False, "tabela": tabela_final, "erro": "id do perfil obrigatorio"}

    resultado = supabase.atualizar(
        tabela_final,
        {"ativo": bool(ativo)},
        filtros={"id": f"eq.{perfil_id_limpo}"},
    )
    if not resultado.get("ok"):
        return {
            "ok": False,
            "tabela": tabela_final,
            "erro": resultado.get("erro", "Falha ao atualizar perfil"),
            "detalhe": resultado.get("detalhe", ""),
        }
    perfil_salvo = _primeiro_perfil(resultado.get("data"))
    return {"ok": True, "tabela": tabela_final, "perfil": perfil_salvo or {"id": perfil_id_limpo, "ativo": ativo}}


def normalizar_perfil(perfil: Mapping[str, Any]) -> PerfilCliente:
    nome = _texto(perfil.get("nome"))
    if len(nome) < 3:
        raise ValueError("nome do perfil precisa ter pelo menos 3 caracteres")

    criterios = _normalizar_criterios(perfil.get("criterios"))
    return {
        "nome": nome,
        "descricao": _texto(perfil.get("descricao")),
        "ativo": bool(perfil.get("ativo", True)),
        "criterios": criterios,
        "mensagem_disparo": _texto(perfil.get("mensagem_disparo")),
        "prompt_ia": _texto(perfil.get("prompt_ia")),
    }


def classificar_cliente(
    cliente: Mapping[str, Any],
    *,
    perfis_disponiveis: Sequence[Mapping[str, Any]] | None = None,
) -> PerfilCliente | None:
    perfis_ativos = [
        _normalizar_perfil_bruto(perfil)
        for perfil in (perfis_disponiveis if perfis_disponiveis is not None else listar_perfis(ativos=True))
        if perfil.get("ativo", True)
    ]
    if not perfis_ativos:
        return None

    idade = mensagens.calcular_idade(cliente.get("data_nascimento"), cliente.get("idade"))
    texto_cliente = _normalizar_texto(" ".join(_valores_cliente(cliente)))
    frequencia = _frequencia_cliente(cliente)
    historico_consumo = _historico_consumo(cliente)

    melhor: tuple[int, PerfilCliente] | None = None
    for perfil in perfis_ativos:
        pontuacao = _pontuar_perfil(
            perfil=perfil,
            cliente=cliente,
            idade=idade,
            texto_cliente=texto_cliente,
            frequencia=frequencia,
            historico_consumo=historico_consumo,
        )
        if melhor is None or pontuacao > melhor[0]:
            melhor = (pontuacao, perfil)

    if melhor is None or melhor[0] <= 0:
        logger.info("Cliente %s sem perfil compativel.", cliente.get("telefone", ""))
        return None

    perfil = dict(melhor[1])
    perfil["metadata"] = {"score_classificacao": melhor[0]}
    logger.info(
        "Cliente %s classificado como %s com score %s.",
        cliente.get("telefone", ""),
        perfil.get("nome", ""),
        melhor[0],
    )
    return perfil


def resolver_perfil_cliente(cliente: Mapping[str, Any]) -> PerfilCliente | None:
    perfil_embutido = cliente.get("perfil")
    if isinstance(perfil_embutido, dict):
        return _normalizar_perfil_bruto(perfil_embutido)

    perfil_id = _texto(cliente.get("perfil_id"))
    if perfil_id:
        perfil = buscar_perfil(perfil_id)
        if perfil:
            return perfil

    perfil_nome = _texto(cliente.get("perfil_nome"))
    if perfil_nome:
        perfil = buscar_perfil_por_nome(perfil_nome)
        if perfil:
            return perfil

    return None


def aplicar_template(template: str, cliente: Mapping[str, Any], perfil: Mapping[str, Any] | None = None) -> str:
    nome = _texto(cliente.get("nome")) or "Cliente"
    idade = mensagens.calcular_idade(cliente.get("data_nascimento"), cliente.get("idade"))
    contexto = {
        "nome": nome,
        "primeiro_nome": nome.split()[0] if nome else "Cliente",
        "idade": "" if idade is None else str(idade),
        "telefone": _texto(cliente.get("telefone")),
        "perfil": _texto((perfil or {}).get("nome")) or _texto(cliente.get("perfil_nome")),
    }
    return template.format_map(_SafeDict(contexto)).strip()


def _pontuar_perfil(
    *,
    perfil: Mapping[str, Any],
    cliente: Mapping[str, Any],
    idade: int | None,
    texto_cliente: str,
    frequencia: int,
    historico_consumo: str,
) -> int:
    criterios = perfil.get("criterios") if isinstance(perfil.get("criterios"), dict) else {}
    texto_perfil = _normalizar_texto(
        " ".join(
            [
                _texto(perfil.get("nome")),
                _texto(perfil.get("descricao")),
                _texto(criterios.get("texto") if isinstance(criterios, dict) else ""),
            ]
        )
    )
    score = 0

    idade_min = _int_opcional(criterios.get("idade_min") if isinstance(criterios, dict) else None)
    idade_max = _int_opcional(criterios.get("idade_max") if isinstance(criterios, dict) else None)
    if idade_min is None and idade_max is None:
        idade_min, idade_max = _extrair_faixa_idade(texto_perfil)
    if idade is not None and (idade_min is not None or idade_max is not None):
        dentro_min = idade_min is None or idade >= idade_min
        dentro_max = idade_max is None or idade <= idade_max
        score += 8 if dentro_min and dentro_max else -4

    if idade is not None:
        if "jovem" in texto_perfil and idade <= 30:
            score += 8
        if ("senior" in texto_perfil or "sênior" in texto_perfil or "idoso" in texto_perfil) and idade >= 60:
            score += 8

    if "premium" in texto_perfil and _contem_termo(texto_cliente, historico_consumo, ("premium", "vip", "alto", "especial", "executivo")):
        score += 7
    if "frequente" in texto_perfil and frequencia >= 3:
        score += 7
    if "familia" in texto_perfil and _contem_termo(texto_cliente, historico_consumo, ("familia", "crianca", "filho", "kids")):
        score += 6
    if "casal" in texto_perfil and _contem_termo(texto_cliente, historico_consumo, ("casal", "dois", "namoro", "jantar a dois")):
        score += 6
    if "social" in texto_perfil and _contem_termo(texto_cliente, historico_consumo, ("grupo", "amigos", "social", "happy")):
        score += 5

    palavras_chave = _palavras_chave(criterios)
    for palavra in palavras_chave:
        if palavra and palavra in texto_cliente:
            score += 2

    if not palavras_chave and texto_perfil:
        for palavra in ("premium", "vip", "familia", "casal", "frequente", "jovem", "senior", "social"):
            if palavra in texto_perfil and palavra in texto_cliente:
                score += 2

    if not texto_cliente.strip() and idade is not None:
        score += 1

    return score


def _normalizar_perfil_bruto(perfil: Mapping[str, Any]) -> PerfilCliente:
    criterios = _normalizar_criterios(perfil.get("criterios"))
    normalizado: PerfilCliente = {
        "id": _texto(perfil.get("id")),
        "nome": _texto(perfil.get("nome")),
        "descricao": _texto(perfil.get("descricao")),
        "ativo": bool(perfil.get("ativo", True)),
        "criterios": criterios,
        "mensagem_disparo": _texto(perfil.get("mensagem_disparo")),
        "prompt_ia": _texto(perfil.get("prompt_ia")),
        "created_at": _texto(perfil.get("created_at")),
        "updated_at": _texto(perfil.get("updated_at")),
    }
    return {chave: valor for chave, valor in normalizado.items() if valor not in ("", None, {})}  # type: ignore[return-value]


def _normalizar_criterios(valor: Any) -> dict[str, Any]:
    if isinstance(valor, dict):
        criterios = dict(valor)
    else:
        criterios = {"texto": _texto(valor)}

    texto = _texto(criterios.get("texto"))
    if texto:
        criterios["texto"] = texto
    else:
        criterios.pop("texto", None)

    for campo in ("idade_min", "idade_max", "frequencia_min"):
        numero = _int_opcional(criterios.get(campo))
        if numero is None:
            criterios.pop(campo, None)
        else:
            criterios[campo] = numero

    palavras = criterios.get("palavras_chave")
    if isinstance(palavras, str):
        criterios["palavras_chave"] = [item.strip() for item in palavras.split(",") if item.strip()]
    elif isinstance(palavras, list):
        criterios["palavras_chave"] = [str(item).strip() for item in palavras if str(item).strip()]

    return criterios


def _primeiro_perfil(data: Any) -> PerfilCliente | None:
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return _normalizar_perfil_bruto(data[0])
    if isinstance(data, dict):
        return _normalizar_perfil_bruto(data)
    return None


def _tabela_perfis() -> str:
    return supabase.tabela_env("SUPABASE_PERFIS_TABLE", TABELA_PERFIS_PADRAO)


def _valores_cliente(cliente: Mapping[str, Any]) -> list[str]:
    valores = [
        cliente.get("nome"),
        cliente.get("tipo"),
        cliente.get("regiao"),
        cliente.get("info_topo_pdf"),
        cliente.get("periodo_aniversario"),
        cliente.get("origem"),
    ]
    metadata = cliente.get("metadata")
    if isinstance(metadata, dict):
        valores.extend(str(valor) for valor in metadata.values() if isinstance(valor, (str, int, float)))
        historico = metadata.get("historico_consumo")
        if isinstance(historico, list):
            valores.extend(str(item) for item in historico)
    return [_texto(valor) for valor in valores if _texto(valor)]


def _historico_consumo(cliente: Mapping[str, Any]) -> str:
    candidatos = [cliente.get("historico_consumo")]
    metadata = cliente.get("metadata")
    if isinstance(metadata, dict):
        candidatos.extend([metadata.get("historico_consumo"), metadata.get("consumo"), metadata.get("frequencia")])
    return _normalizar_texto(" ".join(_texto(item) for item in candidatos if item is not None))


def _frequencia_cliente(cliente: Mapping[str, Any]) -> int:
    for chave in ("frequencia", "frequencia_visitas", "visitas", "total_visitas"):
        numero = _int_opcional(cliente.get(chave))
        if numero is not None:
            return numero

    metadata = cliente.get("metadata")
    if isinstance(metadata, dict):
        for chave in ("frequencia", "frequencia_visitas", "visitas", "total_visitas"):
            numero = _int_opcional(metadata.get(chave))
            if numero is not None:
                return numero
        historico = metadata.get("historico_consumo")
        if isinstance(historico, list):
            return len(historico)
    return 0


def _palavras_chave(criterios: Any) -> list[str]:
    if not isinstance(criterios, dict):
        return []
    palavras = criterios.get("palavras_chave")
    if not isinstance(palavras, list):
        return []
    return [_normalizar_texto(str(palavra)) for palavra in palavras if str(palavra).strip()]


def _extrair_faixa_idade(texto: str) -> tuple[int | None, int | None]:
    match = re.search(r"idade\s*(?:entre|de)?\s*(\d{1,3})\s*(?:a|ate|-)\s*(\d{1,3})", texto)
    if match:
        return int(match.group(1)), int(match.group(2))

    min_match = re.search(r"idade\s*(?:>=|maior(?:\s+que)?|acima\s+de)\s*(\d{1,3})", texto)
    max_match = re.search(r"idade\s*(?:<=|menor(?:\s+que)?|ate)\s*(\d{1,3})", texto)
    return (
        int(min_match.group(1)) if min_match else None,
        int(max_match.group(1)) if max_match else None,
    )


def _contem_termo(texto_cliente: str, historico_consumo: str, termos: Sequence[str]) -> bool:
    base = f"{texto_cliente} {historico_consumo}"
    return any(_normalizar_texto(termo) in base for termo in termos)


def _normalizar_texto(valor: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", valor).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", sem_acento.lower()).strip()


def _texto(valor: Any) -> str:
    if valor is None:
        return ""
    return str(valor).strip()


def _int_opcional(valor: Any) -> int | None:
    if isinstance(valor, bool) or valor in (None, ""):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


class _SafeDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
