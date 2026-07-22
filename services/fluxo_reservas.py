from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, TypedDict

from services import agente, clientes_supabase, dados, perfis, supabase, whatsapp
from services.comunicacao import MensagemRecebida
from services.modelos import Conversa, OrigemConversa, RemetenteMensagem


logger = logging.getLogger(__name__)

TABELA_CONVERSAS_PADRAO = "conversas"
TABELA_MENSAGENS_PADRAO = "mensagens"
TABELA_RESERVAS_PADRAO = "reservas"
TABELA_DISPAROS_PADRAO = "disparos_mensagens"
STATUS_BOT_ATIVO = {"bot_ativo", "aberta", "aguardando_cliente", "em_atendimento"}
STATUS_HUMANO = {"humano", "aguardando_humano", "finalizada"}
STATUS_CONVERSA_PERMITIDOS = STATUS_BOT_ATIVO | STATUS_HUMANO | {"erro"}
PADROES_PEDIDO_HUMANO = (
    r"\batendente\b",
    r"\bhumano\b",
    r"\bpessoa\b",
    r"falar\s+com\s+algu[eé]m",
    r"falar\s+com\s+(?:um\s+)?funcion[aá]rio",
    r"quero\s+falar\s+com\s+(?:um\s+)?funcion[aá]rio",
    r"cancelar\s+bot",
    r"n[aã]o\s+quero\s+bot",
)


class ResultadoWebhook(TypedDict, total=False):
    ok: bool
    telefone: str
    status: str
    conversa_id: str
    reserva_confirmada: bool
    resposta_enviada: bool
    erro: str


def iniciar_conversa(
    cliente: Mapping[str, Any],
    *,
    origem: OrigemConversa = "aniversario",
    mensagem_inicial: str = "",
    status: str = "bot_ativo",
) -> Conversa:
    agora = _agora()
    telefone = str(cliente.get("telefone") or "").strip()
    perfil_cliente = _resolver_perfil_seguro(cliente)
    conversa: Conversa = {
        "cliente_id": str(cliente.get("id") or ""),
        "cliente_telefone": telefone,
        "status": status if status in STATUS_CONVERSA_PERMITIDOS else "bot_ativo",
        "data_inicio": agora,
        "origem": origem,
        "metadata": {
            "cliente_nome": cliente.get("nome", ""),
            "perfil_mensagem": cliente.get("perfil_mensagem") or (perfil_cliente or {}).get("nome"),
            "perfil_id": (perfil_cliente or {}).get("id"),
            "perfil_nome": (perfil_cliente or {}).get("nome"),
        },
    }

    payload = _sem_vazios(conversa)
    resultado = supabase.inserir(_tabela_conversas(), payload)
    if resultado.get("ok"):
        conversa_salva = _primeiro(resultado.get("data"))
        if conversa_salva:
            conversa.update(conversa_salva)
        if conversa.get("status") == "bot_ativo":
            logger.info("Conversa criada pelo disparo/fluxo do ReservaBot: telefone=%s origem=%s.", telefone, origem)
        else:
            logger.info("Conversa registrada no Supabase para %s com status=%s.", telefone, conversa.get("status"))
    else:
        conversa["id"] = f"local:{telefone}:{agora}"
        logger.warning("Conversa mantida localmente para %s: %s", telefone, resultado.get("erro"))

    if mensagem_inicial:
        registrar_mensagem(
            conversa,
            remetente="bot",
            conteudo=mensagem_inicial,
        )

    return conversa


def registrar_mensagem(
    conversa: Mapping[str, Any],
    *,
    remetente: RemetenteMensagem,
    conteudo: str,
    provider_message_id: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> None:
    texto = conteudo.strip()
    if not texto:
        return

    payload = {
        "conversa_id": conversa.get("id"),
        "remetente": remetente,
        "conteudo": texto,
        "timestamp": _agora(),
        "provider_message_id": provider_message_id,
        "metadata": dict(metadata or {}),
    }
    resultado = supabase.inserir(_tabela_mensagens(), _sem_vazios(payload), retornar=False)
    if resultado.get("ok"):
        logger.info("Mensagem %s registrada para conversa %s.", remetente, conversa.get("id"))
    else:
        logger.warning("Mensagem nao registrada no Supabase: %s", resultado.get("erro"))


def processar_resposta_cliente(
    *,
    telefone: str,
    mensagem_cliente: str,
    conversa: Mapping[str, Any] | None = None,
    nome_cliente: str = "",
    provider_message_id: str = "",
    metadata_mensagem: Mapping[str, Any] | None = None,
) -> agente.RespostaAgente:
    telefone_limpo = str(telefone or "").strip()
    mensagem_limpa = mensagem_cliente.strip()
    if not telefone_limpo or not mensagem_limpa:
        return {
            "texto": "",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "sem_mensagem",
            "confianca": 0.0,
        }

    if provider_message_id and _mensagem_ja_processada(provider_message_id):
        logger.info("Mensagem de webhook ja processada: %s", provider_message_id)
        return {
            "texto": "",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "duplicada",
            "confianca": 1.0,
        }

    cliente = clientes_supabase.buscar_cliente_por_telefone(telefone_limpo) or {
        "telefone": telefone_limpo,
        "nome": nome_cliente,
    }
    perfil_cliente = _resolver_perfil_seguro(cliente)
    conversa_atual = conversa or buscar_conversa_ativa_por_telefone(telefone_limpo)
    if conversa_atual is None:
        conversa_anterior = buscar_conversa_por_telefone(telefone_limpo)
        if (
            conversa_anterior is not None
            and str(conversa_anterior.get("status") or "") == "finalizada"
            and agente.mensagem_indica_interesse_reserva(mensagem_limpa)
        ):
            conversa_atual = iniciar_conversa(cliente, origem="webhook", status="bot_ativo")
            logger.info("Cliente retomou interesse em reserva. Novo fluxo iniciado. telefone=%s", telefone_limpo)
        else:
            conversa_atual = conversa_anterior
            if conversa_atual is None:
                conversa_atual = iniciar_conversa(cliente, origem="webhook", status="aguardando_humano")
            logger.info("Mensagem recebida fora de fluxo ativo. Bot nao respondeu. telefone=%s", telefone_limpo)
            registrar_mensagem(
                conversa_atual,
                remetente="cliente",
                conteudo=mensagem_limpa,
                provider_message_id=provider_message_id,
                metadata=metadata_mensagem,
            )
            if str(conversa_atual.get("status") or "") not in STATUS_HUMANO:
                atualizar_status_conversa(conversa_atual, status="aguardando_humano")
            return {
                "texto": "",
                "reserva_confirmada": False,
                "dados_reserva": {},
                "status_reserva": "aguardando_humano",
                "confianca": 1.0,
            }

    status_conversa = str(conversa_atual.get("status") or "")
    if status_conversa == "finalizada" and agente.mensagem_indica_interesse_reserva(mensagem_limpa):
        conversa_atual = iniciar_conversa(cliente, origem="webhook", status="bot_ativo")
        status_conversa = "bot_ativo"
        logger.info("Cliente retomou interesse em reserva. Novo fluxo iniciado. telefone=%s", telefone_limpo)

    if status_conversa in STATUS_HUMANO:
        logger.info(
            "Bot ignorou mensagem porque conversa esta em atendimento humano. telefone=%s status=%s",
            telefone_limpo,
            status_conversa,
        )
        registrar_mensagem(
            conversa_atual,
            remetente="cliente",
            conteudo=mensagem_limpa,
            provider_message_id=provider_message_id,
            metadata=metadata_mensagem,
        )
        return {
            "texto": "",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": status_conversa,
            "confianca": 1.0,
        }

    registrar_mensagem(
        conversa_atual,
        remetente="cliente",
        conteudo=mensagem_limpa,
        provider_message_id=provider_message_id,
        metadata=metadata_mensagem,
    )
    if _pediu_atendimento_humano(mensagem_limpa):
        atualizar_status_conversa(conversa_atual, status="humano")
        logger.info("Bot pausado por pedido de humano. telefone=%s", telefone_limpo)
        return {
            "texto": "",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "humano",
            "confianca": 1.0,
        }

    atualizar_status_conversa(conversa_atual, status="bot_ativo")
    logger.info("Bot respondeu porque conversa esta ativa. telefone=%s", telefone_limpo)
    resposta = agente.processar_mensagem(
        telefone=telefone_limpo,
        mensagem_cliente=mensagem_limpa,
        nome_cliente=str(cliente.get("nome") or nome_cliente or ""),
        perfil_cliente=perfil_cliente,
    )
    nome_confirmacao = str(resposta["dados_reserva"].get("nome_cliente") or cliente.get("nome") or nome_cliente or "")
    if resposta["reserva_confirmada"] and not agente.dados_reserva_obrigatorios_ok(
        resposta["dados_reserva"],
        nome_cliente=nome_confirmacao,
        telefone=telefone_limpo,
    ):
        logger.warning("Confirmacao de reserva bloqueada por campos obrigatorios ausentes. telefone=%s", telefone_limpo)
        resposta = {
            **resposta,
            "texto": "Perfeito, antes de confirmar preciso completar data, horario, quantidade de pessoas e nome.",
            "reserva_confirmada": False,
            "status_reserva": "em_coleta",
        }

    if resposta["texto"]:
        envio = whatsapp.enviar_com_resultado(telefone_limpo, resposta["texto"])
        registrar_mensagem(
            conversa_atual,
            remetente="bot",
            conteudo=resposta["texto"],
            provider_message_id=str(envio.get("provider_message_id") or ""),
            metadata={
                "envio": envio,
                "envio_ok": bool(envio.get("ok")),
                "status_reserva": resposta.get("status_reserva", ""),
                "confianca": resposta.get("confianca", 0),
            },
        )

    if resposta.get("status_reserva") == "sem_interesse":
        finalizar_conversa(conversa_atual, status="finalizada")
        agente.limpar_historico(telefone_limpo)
        logger.info("Conversa finalizada por recusa ao convite de reserva. telefone=%s", telefone_limpo)
        return resposta

    if resposta["reserva_confirmada"]:
        registrar_reserva_confirmada(
            cliente=cliente,
            conversa=conversa_atual,
            dados_reserva=resposta["dados_reserva"],
        )
        finalizar_conversa(conversa_atual, status="finalizada")

    return resposta


def processar_mensagem_webhook(mensagem: MensagemRecebida) -> ResultadoWebhook:
    telefone = str(mensagem.get("telefone") or "").strip()
    texto = str(mensagem.get("texto") or "").strip()
    if not telefone or not texto:
        return {
            "ok": False,
            "telefone": telefone,
            "status": "ignorada",
            "erro": "mensagem sem telefone ou texto",
        }

    conversa = buscar_conversa_ativa_por_telefone(telefone)
    metadata = {
        "provider": "cloud",
        "timestamp_provider": mensagem.get("timestamp", ""),
        "remetente_whatsapp": mensagem.get("remetente", ""),
        "raw": mensagem.get("raw", {}),
    }
    resposta = processar_resposta_cliente(
        telefone=telefone,
        mensagem_cliente=texto,
        conversa=conversa,
        nome_cliente=str(mensagem.get("remetente") or ""),
        provider_message_id=str(mensagem.get("provider_message_id") or ""),
        metadata_mensagem=metadata,
    )
    return {
        "ok": True,
        "telefone": telefone,
        "status": resposta.get("status_reserva", "processada"),
        "conversa_id": str((conversa or {}).get("id") or ""),
        "reserva_confirmada": bool(resposta.get("reserva_confirmada")),
        "resposta_enviada": bool(resposta.get("texto")),
    }


def processar_status_whatsapp(status: Mapping[str, Any]) -> dict[str, Any]:
    message_id = str(status.get("message_id") or "").strip()
    status_meta = str(status.get("status") or "").strip().lower()
    timestamp = str(status.get("timestamp") or "")
    recipient_id = str(status.get("recipient_id") or "")
    erros = status.get("errors") if isinstance(status.get("errors"), list) else []
    erro_texto = _erro_status_texto(erros)
    logger.info(
        "Status WhatsApp recebido: wamid=%s status=%s timestamp=%s recipient_id=%s erro=%s",
        message_id,
        status_meta,
        timestamp,
        recipient_id,
        erro_texto,
    )
    if not message_id:
        return {"ok": False, "status": status_meta, "erro": "message_id ausente"}

    status_interno = _status_whatsapp_interno(status_meta)
    atualizacoes = 0
    metadata_status = {
        "whatsapp_status": status_meta,
        "whatsapp_status_timestamp": timestamp,
        "whatsapp_recipient_id": recipient_id,
        "whatsapp_errors": erros,
    }

    if _atualizar_disparo_status(message_id, status_interno, erro_texto, metadata_status):
        atualizacoes += 1
    if _atualizar_mensagem_status(message_id, status_interno, erro_texto, metadata_status):
        atualizacoes += 1

    return {
        "ok": True,
        "message_id": message_id,
        "status": status_meta,
        "status_interno": status_interno,
        "atualizacoes": atualizacoes,
    }


def registrar_reserva_confirmada(
    *,
    cliente: Mapping[str, Any],
    conversa: Mapping[str, Any] | None,
    dados_reserva: agente.DadosReserva,
) -> bool:
    telefone = str(cliente.get("telefone") or "").strip()
    nome = str(dados_reserva.get("nome_cliente") or cliente.get("nome") or "").strip()
    conversa_id = str((conversa or {}).get("id") or "")
    if not agente.dados_reserva_obrigatorios_ok(dados_reserva, nome_cliente=nome, telefone=telefone):
        logger.warning(
            "Reserva nao registrada por campos obrigatorios ausentes. telefone=%s conversa=%s dados=%s",
            telefone,
            conversa_id,
            {
                "data_reserva": bool(dados_reserva.get("data_reserva")),
                "horario": bool(dados_reserva.get("horario")),
                "pessoas": bool(dados_reserva.get("pessoas")),
                "nome": bool(nome),
                "telefone": bool(telefone),
            },
        )
        return False
    if _reserva_confirmada_existente(conversa_id):
        logger.info("Reserva ja registrada para conversa %s; ignorando duplicidade.", conversa_id)
        return True

    perfil_cliente = _resolver_perfil_seguro(cliente)
    payload = {
        "cliente_id": cliente.get("id"),
        "cliente_telefone": telefone,
        "conversa_id": conversa_id,
        "data_reserva": dados_reserva.get("data_reserva"),
        "horario": dados_reserva.get("horario"),
        "pessoas": dados_reserva.get("pessoas"),
        "observacoes": dados_reserva.get("observacoes"),
        "status": "confirmada",
        "metadata": {
            "nome": nome,
            "perfil_id": (perfil_cliente or {}).get("id"),
            "perfil_nome": (perfil_cliente or {}).get("nome"),
        },
    }
    resultado = supabase.inserir(_tabela_reservas(), _sem_vazios(payload))
    if resultado.get("ok"):
        logger.info("Reserva registrada no Supabase para %s.", telefone)
        return True

    logger.warning("Reserva nao registrada no Supabase para %s: %s", telefone, resultado.get("erro"))
    return dados.adicionar_reserva(
        {
            "telefone": telefone,
            "nome": nome,
            "confirmado_em": _agora(),
            **dados_reserva,
        }
    )


def listar_reservas(*, limite: int = 500) -> list[dict[str, Any]]:
    resultado = supabase.selecionar(
        _tabela_reservas(),
        colunas="*",
        limite=max(1, min(limite, 2000)),
    )
    if resultado.get("ok"):
        registros = resultado.get("data")
        if isinstance(registros, list):
            reservas = [item for item in registros if isinstance(item, dict)]
            return sorted(
                reservas,
                key=lambda item: (
                    str(item.get("data_reserva") or ""),
                    str(item.get("horario") or ""),
                ),
                reverse=True,
            )

    logger.warning("Reservas nao listadas no Supabase: %s", resultado.get("erro"))
    reservas_locais = dados.ler_reservas()
    return [dict(item) for item in reservas_locais if isinstance(item, dict)]


def finalizar_conversa(conversa: Mapping[str, Any], *, status: str = "finalizada") -> None:
    conversa_id = str(conversa.get("id") or "")
    if not conversa_id or conversa_id.startswith("local:"):
        return

    resultado = supabase.atualizar(
        _tabela_conversas(),
        {"status": status, "data_fim": _agora()},
        filtros={"id": f"eq.{conversa_id}"},
        retornar=False,
    )
    if resultado.get("ok"):
        logger.info("Conversa %s finalizada.", conversa_id)
    else:
        logger.warning("Nao foi possivel finalizar conversa %s: %s", conversa_id, resultado.get("erro"))


def buscar_conversa_ativa_por_telefone(telefone: str) -> Conversa | None:
    return buscar_conversa_por_telefone(telefone, statuses=STATUS_BOT_ATIVO)


def buscar_conversa_por_telefone(telefone: str, *, statuses: set[str] | None = None) -> Conversa | None:
    telefone_limpo = telefone.strip()
    if not telefone_limpo:
        return None

    filtros = {"cliente_telefone": f"eq.{telefone_limpo}"}
    if statuses:
        filtros["status"] = f"in.({','.join(sorted(statuses))})"

    resultado = supabase.selecionar(
        _tabela_conversas(),
        filtros=filtros,
        limite=25,
    )
    if not resultado.get("ok"):
        logger.debug("Sem conversa recuperada para %s: %s", telefone_limpo, resultado.get("erro"))
        return None

    dados = resultado.get("data")
    if not isinstance(dados, list):
        return None

    conversas = [item for item in dados if isinstance(item, dict)]
    if not conversas:
        return None

    return dict(max(conversas, key=lambda item: str(item.get("data_inicio") or "")))


def atualizar_status_conversa(conversa: Mapping[str, Any], *, status: str) -> None:
    conversa_id = str(conversa.get("id") or "")
    if not conversa_id or conversa_id.startswith("local:"):
        return

    resultado = supabase.atualizar(
        _tabela_conversas(),
        {"status": status},
        filtros={"id": f"eq.{conversa_id}"},
        retornar=False,
    )
    if resultado.get("ok"):
        if status in {"humano", "aguardando_humano"}:
            logger.info("Bot pausado manualmente/por regra para conversa %s status=%s.", conversa_id, status)
        elif status == "bot_ativo":
            logger.info("Bot retomado para conversa %s.", conversa_id)
    else:
        logger.warning("Nao foi possivel atualizar conversa %s: %s", conversa_id, resultado.get("erro"))


def definir_status_conversa_por_telefone(*, telefone: str, status: str) -> dict[str, Any]:
    telefone_limpo = str(telefone or "").strip()
    status_limpo = str(status or "").strip()
    if not telefone_limpo:
        return {"ok": False, "erro": "Telefone obrigatorio."}
    if status_limpo not in STATUS_CONVERSA_PERMITIDOS:
        return {"ok": False, "erro": "Status de atendimento invalido."}

    cliente = clientes_supabase.buscar_cliente_por_telefone(telefone_limpo) or {"telefone": telefone_limpo}
    conversa = buscar_conversa_por_telefone(telefone_limpo)
    if conversa is None:
        conversa = iniciar_conversa(cliente, origem="manual", status=status_limpo)
    else:
        atualizar_status_conversa(conversa, status=status_limpo)
        conversa = {**dict(conversa), "status": status_limpo}

    if status_limpo in {"humano", "aguardando_humano"}:
        logger.info("Bot pausado manualmente. telefone=%s status=%s", telefone_limpo, status_limpo)
    elif status_limpo == "bot_ativo":
        logger.info("Bot retomado manualmente. telefone=%s", telefone_limpo)

    return {
        "ok": True,
        "telefone": telefone_limpo,
        "status": status_limpo,
        "conversa_id": str(conversa.get("id") or ""),
    }


def _tabela_conversas() -> str:
    return supabase.tabela_env("SUPABASE_CONVERSAS_TABLE", TABELA_CONVERSAS_PADRAO)


def _tabela_mensagens() -> str:
    return supabase.tabela_env("SUPABASE_MENSAGENS_TABLE", TABELA_MENSAGENS_PADRAO)


def _tabela_reservas() -> str:
    return supabase.tabela_env("SUPABASE_RESERVAS_TABLE", TABELA_RESERVAS_PADRAO)


def _tabela_disparos() -> str:
    return supabase.tabela_env("SUPABASE_DISPAROS_TABLE", TABELA_DISPAROS_PADRAO)


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _primeiro(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    return None


def _sem_vazios(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        chave: valor
        for chave, valor in payload.items()
        if valor not in ("", [], None) and valor != {}
    }


def _resolver_perfil_seguro(cliente: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        perfil = perfis.resolver_perfil_cliente(cliente)
    except Exception:
        logger.exception("Falha ao resolver perfil do cliente %s.", cliente.get("telefone", ""))
        return None
    return dict(perfil) if perfil else None


def _pediu_atendimento_humano(texto: str) -> bool:
    normalizado = _normalizar_texto(texto)
    return any(re.search(padrao, normalizado) for padrao in PADROES_PEDIDO_HUMANO)


def _normalizar_texto(texto: str) -> str:
    substituicoes = str.maketrans(
        "áàãâäéèêëíìîïóòõôöúùûüç",
        "aaaaaeeeeiiiiooooouuuuc",
    )
    return str(texto or "").lower().translate(substituicoes)


def _mensagem_ja_processada(provider_message_id: str) -> bool:
    resultado = supabase.selecionar(
        _tabela_mensagens(),
        filtros={"provider_message_id": f"eq.{provider_message_id}"},
        limite=1,
    )
    if not resultado.get("ok"):
        return False

    dados = resultado.get("data")
    return isinstance(dados, list) and bool(dados)


def _reserva_confirmada_existente(conversa_id: str) -> bool:
    if not conversa_id or conversa_id.startswith("local:"):
        return False

    resultado = supabase.selecionar(
        _tabela_reservas(),
        filtros={"conversa_id": f"eq.{conversa_id}", "status": "eq.confirmada"},
        limite=1,
    )
    if not resultado.get("ok"):
        return False

    dados = resultado.get("data")
    return isinstance(dados, list) and bool(dados)


def _status_whatsapp_interno(status: str) -> str:
    mapa = {
        "sent": "enviado",
        "delivered": "entregue",
        "read": "lido",
        "failed": "falha",
    }
    return mapa.get(status, "enviado")


def _erro_status_texto(erros: Any) -> str:
    if not isinstance(erros, list) or not erros:
        return ""
    partes: list[str] = []
    for erro in erros:
        if not isinstance(erro, Mapping):
            continue
        code = erro.get("code")
        title = erro.get("title")
        message = erro.get("message")
        details = erro.get("details")
        partes.append(" | ".join(str(item) for item in (code, title, message, details) if item))
    return "; ".join(partes)


def _atualizar_disparo_status(
    message_id: str,
    status_interno: str,
    erro: str,
    metadata_status: Mapping[str, Any],
) -> bool:
    payload = {
        "status": status_interno,
        "erro": erro or None,
        "metadata": metadata_status,
    }
    resultado = supabase.atualizar(
        _tabela_disparos(),
        _sem_vazios(payload),
        filtros={"provider_message_id": f"eq.{message_id}"},
        retornar=False,
    )
    if resultado.get("ok"):
        logger.info("Status de disparo atualizado: wamid=%s status=%s.", message_id, status_interno)
        return True
    logger.warning("Nao foi possivel atualizar disparo %s: %s", message_id, resultado.get("erro"))
    return False


def _atualizar_mensagem_status(
    message_id: str,
    status_interno: str,
    erro: str,
    metadata_status: Mapping[str, Any],
) -> bool:
    payload = {
        "metadata": {
            **dict(metadata_status),
            "status_entrega": status_interno,
            "erro_entrega": erro,
        },
    }
    resultado = supabase.atualizar(
        _tabela_mensagens(),
        payload,
        filtros={"provider_message_id": f"eq.{message_id}"},
        retornar=False,
    )
    if resultado.get("ok"):
        logger.info("Status de mensagem atualizado: wamid=%s status=%s.", message_id, status_interno)
        return True
    logger.warning("Nao foi possivel atualizar mensagem %s: %s", message_id, resultado.get("erro"))
    return False
