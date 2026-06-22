from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Literal, TypedDict


logger = logging.getLogger(__name__)

PerfilMensagem = Literal["jovem", "adulto", "senior", "padrao"]


class MensagemPersonalizada(TypedDict, total=False):
    texto: str
    perfil: str
    idade: int | None
    perfil_id: str
    perfil_nome: str


def gerar_mensagem_aniversario(cliente: Mapping[str, Any]) -> MensagemPersonalizada:
    nome = _nome_cliente(cliente)
    idade = calcular_idade(cliente.get("data_nascimento"), cliente.get("idade"))
    perfil_cliente = _resolver_perfil_cliente(cliente)
    if perfil_cliente and str(perfil_cliente.get("mensagem_disparo") or "").strip():
        try:
            from services import perfis

            texto_perfil = perfis.aplicar_template(str(perfil_cliente["mensagem_disparo"]), cliente, perfil_cliente)
        except (KeyError, ValueError):
            logger.warning("Mensagem de disparo do perfil %s invalida; usando fallback.", perfil_cliente.get("nome", ""))
        else:
            perfil_nome = str(perfil_cliente.get("nome") or "").strip()
            logger.info("Mensagem de aniversario gerada para %s com perfil %s.", cliente.get("telefone", ""), perfil_nome)
            return {
                "texto": texto_perfil,
                "perfil": perfil_nome,
                "perfil_id": str(perfil_cliente.get("id") or ""),
                "perfil_nome": perfil_nome,
                "idade": idade,
            }

    perfil = _perfil_por_idade(idade)
    template_env = os.getenv("MENSAGEM_ANIVERSARIO", "").strip()

    if template_env and perfil == "padrao":
        texto = _formatar_template(template_env, nome=nome, idade=idade, perfil=perfil)
    else:
        texto = _mensagem_por_perfil(nome, idade, perfil)

    logger.info("Mensagem de aniversario gerada para %s com perfil %s.", cliente.get("telefone", ""), perfil)
    return {"texto": texto, "perfil": perfil, "idade": idade}


def calcular_idade(data_nascimento: Any, idade_informada: Any = None, data_base: date | None = None) -> int | None:
    idade = _int_opcional(idade_informada)
    if idade is not None and 0 <= idade <= 130:
        return idade

    data = _parse_data(data_nascimento)
    if data is None:
        return None

    hoje = data_base or date.today()
    idade_calculada = hoje.year - data.year - ((hoje.month, hoje.day) < (data.month, data.day))
    if 0 <= idade_calculada <= 130:
        return idade_calculada
    return None


def _perfil_por_idade(idade: int | None) -> PerfilMensagem:
    if idade is None:
        return "padrao"
    if idade <= 25:
        return "jovem"
    if idade >= 70:
        return "senior"
    return "adulto"


def _mensagem_por_perfil(nome: str, idade: int | None, perfil: PerfilMensagem) -> str:
    primeiro_nome = nome.split()[0] if nome else "Cliente"
    if perfil == "jovem":
        return (
            f"{primeiro_nome}, feliz aniversario! Que tal comemorar com uma mesa especial por aqui? "
            "Posso reservar para voce?"
        )
    if perfil == "senior":
        return (
            f"{primeiro_nome}, feliz aniversario. Desejamos um dia muito especial para voce. "
            "Sera um prazer receber voce para comemorar; gostaria que eu reservasse uma mesa?"
        )
    if perfil == "adulto":
        detalhe_idade = f" seus {idade} anos" if idade is not None else ""
        return (
            f"{primeiro_nome}, feliz aniversario{detalhe_idade}! "
            "Temos uma condicao especial para voce comemorar aqui. Quer reservar uma mesa?"
        )
    return (
        f"{primeiro_nome}, feliz aniversario! Temos uma condicao especial para voce comemorar aqui. "
        "Quer reservar uma mesa?"
    )


def _formatar_template(template: str, *, nome: str, idade: int | None, perfil: PerfilMensagem) -> str:
    try:
        return template.format(
            nome=nome,
            primeiro_nome=nome.split()[0] if nome else "Cliente",
            idade=idade or "",
            perfil=perfil,
        )
    except (IndexError, KeyError, ValueError):
        logger.warning("MENSAGEM_ANIVERSARIO invalida; usando mensagem padrao personalizada.")
        return _mensagem_por_perfil(nome, idade, perfil)


def _nome_cliente(cliente: Mapping[str, Any]) -> str:
    nome = str(cliente.get("nome") or "").strip()
    return " ".join(nome.split()) or "Cliente"


def _resolver_perfil_cliente(cliente: Mapping[str, Any]) -> Mapping[str, Any] | None:
    perfil = cliente.get("perfil")
    if isinstance(perfil, Mapping):
        return perfil
    if cliente.get("perfil_id") or cliente.get("perfil_nome"):
        try:
            from services import perfis

            return perfis.resolver_perfil_cliente(cliente)
        except Exception:
            logger.exception("Falha ao resolver perfil do cliente %s.", cliente.get("telefone", ""))
            return None
    return None


def _parse_data(valor: Any) -> date | None:
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = str(valor or "").strip()
    if not texto:
        return None

    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    return None


def _int_opcional(valor: Any) -> int | None:
    if isinstance(valor, bool) or valor in (None, ""):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None
