from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, TypedDict
from uuid import uuid4

from services import agente, clientes_supabase, comprovantes_reserva, config_restaurante, dados, perfis, supabase, whatsapp
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
DEBOUNCE_SECONDS_ENV = "WHATSAPP_DEBOUNCE_SECONDS"
DEBOUNCE_SECONDS_DEFAULT = 2.5
DEBOUNCE_RETRY_SECONDS = 1.0
DEBOUNCE_LEASE_SECONDS = 30.0
PADROES_PEDIDO_HUMANO = (
    r"\batendente\b",
    r"\bhumano\b",
    r"\bpessoa\b",
    r"falar\s+com\s+algu[eé]m",
    r"falar\s+com\s+(?:um\s+)?funcion[aá]rio",
    r"quero\s+falar\s+com\s+(?:um\s+)?funcion[aá]rio",
    r"cancelar\s+bot",
    r"n[aã]o\s+quero\s+bot",
    r"^\s*pare\s*$",
    r"\bpare\s+agora\b",
    r"^\s*para\s*$",
    r"\bpara\s+agora\b",
    r"n[aã]o\s+responda",
    r"\bdenunciar\b",
    r"vou\s+denunciar",
)
_debounce_lock = threading.RLock()
_debounce_lotes: dict[str, dict[str, Any]] = {}
_provider_ids_pendentes: set[str] = set()


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
    status_conversa = status if status in STATUS_CONVERSA_PERMITIDOS else "bot_ativo"
    if telefone and status_conversa in STATUS_BOT_ATIVO:
        _finalizar_conversas_ativas_por_telefone(telefone, motivo="nova_conversa_bot")
        agente.limpar_historico(telefone)
    perfil_cliente = _resolver_perfil_seguro(cliente)
    conversa: Conversa = {
        "cliente_id": str(cliente.get("id") or ""),
        "cliente_telefone": telefone,
        "status": status_conversa,
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
) -> bool:
    texto = conteudo.strip()
    if not texto:
        return False

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
        return True
    else:
        logger.warning("Mensagem nao registrada no Supabase: %s", resultado.get("erro"))
        return False


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

    provider_message_ids = _provider_message_ids(provider_message_id, metadata_mensagem)
    if any(message_id and _mensagem_ja_processada(message_id) for message_id in provider_message_ids):
        logger.info("Mensagem de webhook ja processada: %s", ",".join(provider_message_ids))
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
            _registrar_mensagens_cliente(
                conversa_atual,
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
        _registrar_mensagens_cliente(
            conversa_atual,
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

    _registrar_mensagens_cliente(
        conversa_atual,
        conteudo=mensagem_limpa,
        provider_message_id=provider_message_id,
        metadata=metadata_mensagem,
    )
    if _pediu_atendimento_humano(mensagem_limpa):
        atualizar_status_conversa(conversa_atual, status="humano")
        _limpar_estado_reserva_conversa(conversa_atual, telefone_limpo)
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
    _carregar_estado_reserva_conversa(conversa_atual, telefone_limpo)
    contexto_mensagem = _contexto_midia_metadata(metadata_mensagem)
    resposta = agente.processar_mensagem(
        telefone=telefone_limpo,
        mensagem_cliente=mensagem_limpa,
        nome_cliente=str(cliente.get("nome") or nome_cliente or ""),
        perfil_cliente=perfil_cliente,
        contexto_mensagem=contexto_mensagem,
    )
    resposta = _aplicar_fluxo_comprovante(
        telefone=telefone_limpo,
        mensagem_cliente=mensagem_limpa,
        cliente=cliente,
        conversa=conversa_atual,
        resposta=resposta,
    )
    resposta = _bloquear_afirmacao_comprovante_backend(
        telefone=telefone_limpo,
        resposta=resposta,
        comprovante_persistido=bool(contexto_mensagem.get("comprovante_persistido")),
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
    resposta = _bloquear_confirmacao_automatica_backend(
        telefone=telefone_limpo,
        resposta=resposta,
    )
    _salvar_estado_reserva_conversa(conversa_atual, telefone_limpo, resposta=resposta)

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

    if resposta.get("status_reserva") == "aguardando_humano":
        atualizar_status_conversa(conversa_atual, status="aguardando_humano")
        agente.limpar_historico(telefone_limpo)
        _limpar_estado_reserva_conversa(conversa_atual, telefone_limpo)
        logger.info("Bot pausado para atendimento humano. telefone=%s motivo=%s", telefone_limpo, resposta.get("status_reserva"))
        return resposta

    if resposta.get("status_reserva") == "sem_interesse":
        finalizar_conversa(conversa_atual, status="finalizada")
        agente.limpar_historico(telefone_limpo)
        _limpar_estado_reserva_conversa(conversa_atual, telefone_limpo)
        logger.info("Conversa finalizada por recusa ao convite de reserva. telefone=%s", telefone_limpo)
        return resposta

    return resposta


def _bloquear_confirmacao_automatica_backend(
    *,
    telefone: str,
    resposta: agente.RespostaAgente,
) -> agente.RespostaAgente:
    texto = str(resposta.get("texto") or "")
    status = str(resposta.get("status_reserva") or "")
    if not resposta.get("reserva_confirmada") and status != "confirmada" and not _texto_afirma_reserva_confirmada(texto):
        return resposta

    estado = agente.obter_estado_reserva(telefone) or {}
    etapa = str(estado.get("etapa") or "")
    if etapa == "aguardando_analise" or str(estado.get("comprovante_status") or "") == "aguardando_analise":
        texto_seguro = "A solicitacao esta aguardando a analise da equipe. Assim que for verificada, voce recebera a confirmacao."
        status_seguro = "aguardando_analise"
    elif estado:
        estado["aguardando_confirmacao"] = False
        estado["cliente_autorizou_confirmacao"] = False
        estado["etapa"] = "aguardando_comprovante"
        estado["campo_pendente"] = "comprovante"
        estado["comprovante_status"] = "aguardando_comprovante"
        agente.definir_estado_reserva(telefone, estado)
        texto_seguro = "A solicitacao ainda aguarda o comprovante. Envie a imagem ou o PDF por aqui para a equipe conferir."
        status_seguro = "aguardando_comprovante"
    else:
        texto_seguro = "Perfeito, sigo com os dados anotados por aqui."
        status_seguro = "em_coleta"

    logger.error(
        "Confirmacao automatica bloqueada antes de salvar ou enviar resposta. telefone=%s status_ia=%s texto_ia=%r status_final=%s.",
        telefone,
        status,
        texto,
        status_seguro,
    )
    return {
        **resposta,
        "texto": texto_seguro,
        "reserva_confirmada": False,
        "status_reserva": status_seguro,
    }


def _bloquear_afirmacao_comprovante_backend(
    *,
    telefone: str,
    resposta: agente.RespostaAgente,
    comprovante_persistido: bool,
) -> agente.RespostaAgente:
    texto = str(resposta.get("texto") or "")
    if comprovante_persistido or not _texto_afirma_comprovante_recebido(texto):
        return resposta

    estado = agente.obter_estado_reserva(telefone) or {}
    if (
        str(estado.get("etapa") or "") == "aguardando_analise"
        or str(estado.get("comprovante_status") or "") == "aguardando_analise"
    ):
        logger.warning(
            "Afirmacao de novo recebimento substituida pelo estado persistido de analise. telefone=%s texto_original=%r.",
            telefone,
            texto,
        )
        return {
            **resposta,
            "texto": "A solicitacao esta aguardando a analise da equipe. Assim que for verificada, voce recebera a confirmacao.",
            "reserva_confirmada": False,
            "status_reserva": "aguardando_analise",
        }

    if str(estado.get("etapa") or "") != "aguardando_comprovante":
        logger.error(
            "Afirmacao falsa de recebimento bloqueada fora da etapa de comprovante. telefone=%s texto_original=%r.",
            telefone,
            texto,
        )
        return {
            **resposta,
            "texto": "Nao apareceu nenhum arquivo por aqui. Primeiro precisamos concluir os dados da solicitacao.",
            "reserva_confirmada": False,
            "status_reserva": str(resposta.get("status_reserva") or "em_coleta"),
        }

    estado["etapa"] = "aguardando_comprovante"
    estado["campo_pendente"] = "comprovante"
    estado["comprovante_status"] = "aguardando_comprovante"
    estado["aguardando_confirmacao"] = False
    estado["cliente_autorizou_confirmacao"] = False
    agente.definir_estado_reserva(telefone, estado)
    texto_seguro = "Nao apareceu nenhum arquivo por aqui. Envie a imagem ou o PDF do comprovante para a equipe conferir."
    logger.error(
        "Afirmacao falsa de recebimento de comprovante bloqueada. telefone=%s texto_original=%r.",
        telefone,
        texto,
    )
    return {
        **resposta,
        "texto": texto_seguro,
        "reserva_confirmada": False,
        "status_reserva": "aguardando_comprovante",
    }


def _aplicar_fluxo_comprovante(
    *,
    telefone: str,
    mensagem_cliente: str,
    cliente: Mapping[str, Any],
    conversa: Mapping[str, Any],
    resposta: agente.RespostaAgente,
) -> agente.RespostaAgente:
    if str(resposta.get("status_reserva") or "") in {
        "aguardando_humano",
        "humano",
        "sem_interesse",
        "cancelada",
        "erro",
    }:
        return {**resposta, "reserva_confirmada": False}
    estado = agente.obter_estado_reserva(telefone)
    if not estado:
        return resposta

    if estado.get("regra_espaco_obrigatoria") and not estado.get("cliente_autorizou_espaco_direcionado"):
        estado["aguardando_confirmacao_espaco"] = True
        estado["aguardando_confirmacao"] = False
        estado["cliente_autorizou_confirmacao"] = False
        estado["etapa"] = "aguardando_confirmacao_espaco"
        estado["campo_pendente"] = "espaco"
        agente.definir_estado_reserva(telefone, estado)
        texto = agente._mensagem_direcionamento_espaco(estado)
        logger.warning(
            "Entrada em aguardando_comprovante bloqueada por aceite de espaco pendente. telefone=%s espaco=%s.",
            telefone,
            estado.get("espaco_direcionado_nome", ""),
        )
        return {
            **resposta,
            "texto": texto,
            "reserva_confirmada": False,
            "status_reserva": "em_coleta",
        }

    etapa = str(estado.get("etapa") or "")
    if etapa == "aguardando_analise":
        return {
            **resposta,
            "reserva_confirmada": False,
            "status_reserva": "aguardando_analise",
        }

    dados = dict(resposta.get("dados_reserva") or {})
    nome = str(dados.get("nome_cliente") or cliente.get("nome") or "").strip()
    if not agente.dados_reserva_obrigatorios_ok(dados, nome_cliente=nome, telefone=telefone):
        return {**resposta, "reserva_confirmada": False}
    if estado.get("quantidade_abaixo_minima"):
        return {**resposta, "reserva_confirmada": False}

    config = config_restaurante.obter_config()
    horario = str(estado.get("horario") or dados.get("horario") or "")[:5]
    horario_sem_preferencia = horario in {"18:00", "19:00"}
    regra_obrigatoria = bool(estado.get("regra_espaco_obrigatoria"))

    if etapa == "aguardando_comprovante" and estado.get("informacoes_pagamento_apresentadas"):
        if not _espaco_valido_para_comprovante(
            estado,
            config=config,
            horario_sem_preferencia=horario_sem_preferencia,
        ):
            logger.error(
                "Estado aguardando_comprovante bloqueado por espaco invalido. telefone=%s espaco_id=%s.",
                telefone,
                estado.get("espaco_direcionado_id") or estado.get("preferencia_espaco_id") or "",
            )
            return {
                **resposta,
                "texto": "A equipe precisa revisar o espaco da solicitacao antes de continuar o pagamento.",
                "reserva_confirmada": False,
                "status_reserva": "aguardando_humano",
            }
        texto = str(resposta.get("texto") or "").strip()
        resposta_comprovante = _resposta_texto_aguardando_comprovante(mensagem_cliente)
        if resposta_comprovante:
            texto = resposta_comprovante
        elif _mensagem_informa_pagamento_sem_midia(mensagem_cliente):
            texto = "Perfeito. Agora envie a imagem ou o PDF do comprovante por aqui para a equipe conferir."
        elif _texto_afirma_reserva_confirmada(texto):
            texto = "A solicitacao ainda aguarda o comprovante. Envie a imagem ou o PDF por aqui para a equipe conferir."
        return {
            **resposta,
            "texto": texto,
            "reserva_confirmada": False,
            "status_reserva": "aguardando_comprovante",
        }

    if regra_obrigatoria and not estado.get("cliente_autorizou_espaco_direcionado"):
        return {**resposta, "reserva_confirmada": False, "status_reserva": "em_coleta"}

    if horario_sem_preferencia and not regra_obrigatoria:
        for campo in ("preferencia_espaco_id", "preferencia_espaco_nome"):
            estado.pop(campo, None)
            dados.pop(campo, None)
        estado["espaco_confirmado"] = False
        estado["local_garantido"] = False
        estado["motivo_local_nao_garantido"] = "Distribuicao entre espacos conforme disponibilidade no horario."
    elif not regra_obrigatoria and not str(estado.get("preferencia_espaco_id") or "").strip():
        espacos_preferencia = [espaco for espaco in config.espacos if espaco.ativo and espaco.permite_preferencia]
        if not espacos_preferencia:
            return {
                **resposta,
                "texto": "Os dados estao completos, mas a equipe precisa orientar o local da reserva por aqui.",
                "reserva_confirmada": False,
                "status_reserva": "aguardando_humano",
            }
        if len(espacos_preferencia) == 1:
            estado["preferencia_espaco_id"] = espacos_preferencia[0].id
            estado["preferencia_espaco_nome"] = espacos_preferencia[0].nome
        else:
            estado["campo_pendente"] = "espaco"
            estado["etapa"] = "aguardando_espaco"
            agente.definir_estado_reserva(telefone, estado)
            texto_ia = str(resposta.get("texto") or "").strip()
            if not _texto_pede_preferencia_espaco(texto_ia):
                nomes = [espaco.nome for espaco in espacos_preferencia]
                opcoes = ", ".join(nomes[:-1]) + f" ou {nomes[-1]}"
                texto_ia = f"Perfeito. Voces preferem {opcoes}? A escolha fica como preferencia e depende da disponibilidade."
            return {
                **resposta,
                "texto": texto_ia,
                "reserva_confirmada": False,
                "status_reserva": "em_coleta",
            }

    if not _espaco_valido_para_comprovante(
        estado,
        config=config,
        horario_sem_preferencia=horario_sem_preferencia,
    ):
        logger.error(
            "Entrada em aguardando_comprovante bloqueada por espaco invalido. telefone=%s regra_obrigatoria=%s espaco_id=%s.",
            telefone,
            regra_obrigatoria,
            estado.get("espaco_direcionado_id") or estado.get("preferencia_espaco_id") or "",
        )
        return {
            **resposta,
            "texto": "Os dados estao completos, mas a equipe precisa orientar o espaco antes do pagamento.",
            "reserva_confirmada": False,
            "status_reserva": "aguardando_humano",
        }

    solicitacao = registrar_solicitacao_reserva(
        cliente=cliente,
        conversa=conversa,
        dados_reserva={**dados, "nome_cliente": nome},
        estado=estado,
        config=config,
    )
    if not solicitacao.get("ok"):
        logger.error(
            "Solicitacao de reserva nao persistida antes do pagamento. telefone=%s conversa_id=%s erro=%s",
            telefone,
            conversa.get("id", ""),
            solicitacao.get("erro", ""),
        )
        return {
            **resposta,
            "texto": "Nao consegui preparar o envio do comprovante agora. Vou deixar a equipe continuar seu atendimento por aqui.",
            "reserva_confirmada": False,
            "status_reserva": "aguardando_humano",
        }

    reserva = solicitacao.get("reserva") if isinstance(solicitacao.get("reserva"), Mapping) else {}
    estado["reserva_id"] = str(reserva.get("id") or estado.get("reserva_id") or "")
    estado["aguardando_confirmacao"] = False
    estado["cliente_autorizou_confirmacao"] = False
    estado["etapa"] = "aguardando_comprovante"
    estado["campo_pendente"] = "comprovante"
    estado["comprovante_status"] = "aguardando_comprovante"

    partes: list[str] = []
    if (
        horario_sem_preferencia
        and not regra_obrigatoria
        and not estado.get("regra_horario_sem_preferencia_apresentada")
    ):
        partes.append(
            "Nesse horario nao conseguimos garantir Salao ou Areia. As mesas sao distribuidas conforme a disponibilidade no momento."
        )
        estado["regra_horario_sem_preferencia_apresentada"] = True

    if _conversa_de_aniversario(conversa, estado) and not estado.get("informacoes_aniversario_apresentadas"):
        partes.append(_texto_aniversario_config(config))
        estado["informacoes_aniversario_apresentadas"] = True

    if not estado.get("informacoes_pagamento_apresentadas"):
        texto_pagamento = _texto_pagamento_config(config)
        if not texto_pagamento:
            logger.error(
                "Configuracao de pagamento incompleta. estabelecimento_id=%s taxa=%s pix=%s titular=%s",
                config.estabelecimento_id,
                config.taxa_valor,
                bool(config.pix_chave),
                bool(config.pix_titular),
            )
            estado["reserva_id"] = str(reserva.get("id") or estado.get("reserva_id") or "")
            agente.definir_estado_reserva(telefone, estado)
            return {
                **resposta,
                "texto": "Os dados da solicitacao estao completos, mas a equipe precisa continuar o pagamento por aqui.",
                "reserva_confirmada": False,
                "status_reserva": "aguardando_humano",
            }
        partes.append(texto_pagamento)
        estado["informacoes_pagamento_apresentadas"] = True

    if not estado.get("informacoes_cancelamento_apresentadas"):
        partes.append(_texto_cancelamento_config(config))
        estado["informacoes_cancelamento_apresentadas"] = True

    agente.definir_estado_reserva(telefone, estado)
    texto_final = "\n\n".join(parte.strip() for parte in partes if parte.strip())
    logger.info(
        "Solicitacao entrou em aguardando_comprovante: telefone=%s conversa_id=%s reserva_id=%s aniversario_info=%s pagamento_info=%s cancelamento_info=%s.",
        telefone,
        conversa.get("id", ""),
        estado.get("reserva_id", ""),
        bool(estado.get("informacoes_aniversario_apresentadas")),
        bool(estado.get("informacoes_pagamento_apresentadas")),
        bool(estado.get("informacoes_cancelamento_apresentadas")),
    )
    return {
        **resposta,
        "texto": texto_final or str(resposta.get("texto") or ""),
        "reserva_confirmada": False,
        "dados_reserva": {**dados, "nome_cliente": nome},
        "status_reserva": "aguardando_comprovante",
    }


def _espaco_valido_para_comprovante(
    estado: Mapping[str, Any],
    *,
    config: config_restaurante.ConfigRestaurante,
    horario_sem_preferencia: bool,
) -> bool:
    regra_obrigatoria = bool(estado.get("regra_espaco_obrigatoria"))
    if horario_sem_preferencia and not regra_obrigatoria:
        return True
    if regra_obrigatoria:
        espaco_id = str(estado.get("espaco_direcionado_id") or "").strip()
        espaco_nome = str(estado.get("espaco_direcionado_nome") or "").strip()
        if not estado.get("cliente_autorizou_espaco_direcionado"):
            return False
    else:
        espaco_id = str(estado.get("preferencia_espaco_id") or "").strip()
        espaco_nome = str(estado.get("preferencia_espaco_nome") or "").strip()
    return any(
        espaco.ativo and espaco.id == espaco_id and espaco.nome == espaco_nome
        for espaco in config.espacos
    )


def _texto_pagamento_config(config: config_restaurante.ConfigRestaurante) -> str:
    if (
        config.taxa_valor is None
        or config.taxa_valor <= 0
        or not config.pix_chave
        or not config.pix_titular
        or not config.exige_comprovante
    ):
        return ""
    taxa = f"R$ {config.taxa_valor:.2f}".replace(".", ",")
    conversao = ", convertida em consumacao no dia" if config.taxa_convertida_consumacao else ""
    return (
        f"Para prosseguir, a reserva tem uma taxa de {taxa}{conversao}. "
        f"O pagamento e via Pix para {config.pix_chave}, em nome de {config.pix_titular}. "
        "Depois do pagamento, envie a imagem ou o PDF do comprovante por aqui."
    )


def _texto_cancelamento_config(config: config_restaurante.ConfigRestaurante) -> str:
    horas = config.prazo_cancelamento_horas
    if horas:
        return (
            f"Em caso de cancelamento, o estorno e feito com aviso de pelo menos {horas} horas. "
            "Depois desse prazo, o valor nao e devolvido."
        )
    politica = str(config.politica_cancelamento or "").strip()
    return politica or "A equipe pode orientar sobre a politica de cancelamento antes do pagamento."


def _texto_aniversario_config(config: config_restaurante.ConfigRestaurante) -> str:
    return (
        "Como é aniversário, não trabalhamos com lista. Pode trazer bolo, e conseguimos guardá-lo "
        "na geladeira até a hora do parabéns. Recomendamos trazer pratos e garfos para servir."
    )


def _conversa_de_aniversario(conversa: Mapping[str, Any], estado: Mapping[str, Any]) -> bool:
    return str(conversa.get("origem") or estado.get("origem_conversa") or "").strip().lower() == "aniversario"


def _mensagem_informa_pagamento_sem_midia(texto: str) -> bool:
    normalizado = _normalizar_texto(texto)
    return bool(re.search(r"\b(ja paguei|paguei|pagamento feito|pix feito|fiz o pix|transferi)\b", normalizado))


def _resposta_texto_aguardando_comprovante(texto: str) -> str:
    normalizado = re.sub(r"[^\w\s]", " ", _normalizar_texto(texto))
    normalizado = re.sub(r"\s+", " ", normalizado).strip()
    if _mensagem_afirma_envio_comprovante_sem_midia(normalizado):
        return "Nao apareceu nenhum arquivo por aqui. Envie a imagem ou o PDF do comprovante para a equipe conferir."
    if re.search(r"\b(nao preciso|preciso enviar|precisa enviar|enviar nada)\b", normalizado):
        return (
            "Precisa sim. Envie a imagem ou o PDF do comprovante do Pix por aqui. "
            "A equipe so conclui a solicitacao depois da conferencia."
        )
    if re.fullmatch(r"(estou ciente|ciente|sim|ok|pode seguir|pode continuar|confirmo os dados)", normalizado):
        return "Perfeito. Agora e so enviar a imagem ou o PDF do comprovante por aqui para a equipe conferir."
    if re.fullmatch(r"(certo|obrigad[ao]|certo obrigad[ao]|valeu|beleza)", normalizado):
        return "Por nada! Fico aguardando o comprovante para a equipe analisar a solicitacao."
    return ""


def _mensagem_afirma_envio_comprovante_sem_midia(texto: str) -> bool:
    normalizado = re.sub(r"[^\w\s]", " ", _normalizar_texto(texto))
    normalizado = re.sub(r"\s+", " ", normalizado).strip()
    return bool(
        re.fullmatch(
            r"(enviei|ja enviei|mandei|ja mandei|ja paguei|paguei|foi|pronto|segue|segue o comprovante)",
            normalizado,
        )
        or re.search(r"\b(enviei|ja enviei|mandei|ja mandei|segue o comprovante)\b", normalizado)
    )


def _texto_afirma_comprovante_recebido(texto: str) -> bool:
    normalizado = _normalizar_texto(texto)
    return bool(
        re.search(r"\bcomprovante\s+(?:foi\s+)?recebid", normalizado)
        or re.search(r"\ba\s+equipe\s+(?:ja\s+)?recebeu\b", normalizado)
        or re.search(r"\bo\s+arquivo\s+foi\s+enviado\b", normalizado)
        or re.search(r"\b(?:recebi|recebemos|equipe\s+(?:ja\s+)?recebeu)\b.{0,40}\b(comprovante|arquivo)\b", normalizado)
        or re.search(r"\b(?:comprovante|arquivo)\b.{0,40}\b(?:recebid|registrad|enviado\s+com\s+sucesso)\b", normalizado)
        or re.search(r"\bequipe\s+vai\s+analisar\s+(?:o\s+)?comprovante\b", normalizado)
    )


def _texto_afirma_reserva_confirmada(texto: str) -> bool:
    normalizado = _normalizar_texto(texto)
    return bool(re.search(r"\b(reserva|mesa|solicitacao)\s+(?:esta\s+)?confirmad", normalizado))


def _texto_pede_preferencia_espaco(texto: str) -> bool:
    normalizado = _normalizar_texto(texto)
    return bool(
        re.search(r"\b(salao|areia)\b", normalizado)
        and re.search(r"\b(prefere|preferem|preferencia|qual|onde|ficar)\b", normalizado)
    )


def _provider_message_ids(provider_message_id: str, metadata_mensagem: Mapping[str, Any] | None) -> list[str]:
    ids: list[str] = []
    if provider_message_id:
        ids.append(str(provider_message_id))
    agrupadas = _mensagens_agrupadas_metadata(metadata_mensagem)
    for item in agrupadas:
        message_id = str(item.get("provider_message_id") or "").strip()
        if message_id and message_id not in ids:
            ids.append(message_id)
    return ids


def _registrar_mensagens_cliente(
    conversa: Mapping[str, Any],
    *,
    conteudo: str,
    provider_message_id: str,
    metadata: Mapping[str, Any] | None,
) -> None:
    if isinstance(metadata, Mapping) and metadata.get("cliente_ja_registrado"):
        _finalizar_mensagens_debounce_persistidas(metadata)
        return

    agrupadas = _mensagens_agrupadas_metadata(metadata)
    if not agrupadas:
        registrar_mensagem(
            conversa,
            remetente="cliente",
            conteudo=conteudo,
            provider_message_id=provider_message_id,
            metadata=metadata,
        )
        return

    for indice, item in enumerate(agrupadas, start=1):
        registrar_mensagem(
            conversa,
            remetente="cliente",
            conteudo=str(item.get("texto") or "").strip(),
            provider_message_id=str(item.get("provider_message_id") or "").strip(),
            metadata={
                **dict(metadata or {}),
                "agrupada": True,
                "ordem_agrupamento": indice,
                "total_agrupado": len(agrupadas),
                "timestamp_provider": item.get("timestamp", ""),
            },
        )


def _mensagens_agrupadas_metadata(metadata: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(metadata, Mapping):
        return []
    agrupadas = metadata.get("mensagens_agrupadas")
    if not isinstance(agrupadas, list):
        return []
    return [item for item in agrupadas if isinstance(item, Mapping)]


def _contexto_midia_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    contexto = metadata if isinstance(metadata, Mapping) else {}
    return {
        "mensagem_contem_midia": bool(contexto.get("mensagem_contem_midia")),
        "tipos_midia": [str(item) for item in (contexto.get("tipos_midia") or []) if str(item)],
        "media_ids": [str(item) for item in (contexto.get("media_ids") or []) if str(item)],
        "comprovante_persistido": bool(contexto.get("comprovante_persistido")),
    }


def _mensagens_debounce_persistidas_metadata(mensagens_agrupadas: Sequence[Any]) -> list[dict[str, Any]]:
    persistidas: list[dict[str, Any]] = []
    for item in mensagens_agrupadas:
        if not isinstance(item, Mapping):
            continue
        raw = item.get("raw") if isinstance(item.get("raw"), Mapping) else {}
        mensagem_id = str(raw.get("debounce_mensagem_id") or "").strip()
        if not mensagem_id:
            continue
        metadata = raw.get("debounce_metadata") if isinstance(raw.get("debounce_metadata"), Mapping) else {}
        persistidas.append(
            {
                "id": mensagem_id,
                "provider_message_id": str(item.get("provider_message_id") or metadata.get("provider_message_id_original") or ""),
                "metadata": dict(metadata),
            }
        )
    return persistidas


def _finalizar_mensagens_debounce_persistidas(metadata: Mapping[str, Any]) -> None:
    persistidas = metadata.get("mensagens_debounce_persistidas")
    if not isinstance(persistidas, list):
        return
    for item in persistidas:
        if not isinstance(item, Mapping):
            continue
        mensagem_id = str(item.get("id") or "").strip()
        if not mensagem_id:
            continue
        metadata_item = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        metadata_final = dict(metadata_item)
        metadata_final.update(
            {
                "debounce_pending": False,
                "debounce_processing": False,
                "debounce_processed": True,
                "debounce_processed_at": _agora(),
            }
        )
        provider_message_id = str(item.get("provider_message_id") or metadata_final.get("provider_message_id_original") or "").strip()
        payload: dict[str, Any] = {"metadata": metadata_final}
        if provider_message_id:
            payload["provider_message_id"] = provider_message_id
        resultado = supabase.atualizar(
            _tabela_mensagens(),
            payload,
            filtros={"id": f"eq.{mensagem_id}"},
            retornar=False,
        )
        if resultado.get("ok"):
            logger.info("Mensagem debounce persistida finalizada id=%s provider_message_id=%s.", mensagem_id, provider_message_id)
        else:
            logger.warning("Mensagem debounce persistida nao finalizada id=%s: %s", mensagem_id, resultado.get("erro"))


def _carregar_estado_reserva_conversa(conversa: Mapping[str, Any], telefone: str) -> None:
    metadata = _metadata_conversa(conversa)
    estado = metadata.get("estado_reserva")
    conversa_id = str(conversa.get("id") or "")
    logger.info(
        "DIAG_RESERVA estado_carregado_antes telefone=%s conversa_id=%s metadata_estado=%s cache_memoria=%s",
        telefone,
        conversa_id,
        estado if isinstance(estado, Mapping) else {},
        agente.obter_estado_reserva(telefone),
    )
    if isinstance(estado, Mapping):
        agente.definir_estado_reserva(
            telefone,
            {
                **dict(estado),
                "conversa_id": conversa_id,
                "origem_conversa": str(conversa.get("origem") or estado.get("origem_conversa") or ""),
            },
        )
        logger.info("Estado de reserva carregado da conversa %s para telefone=%s.", conversa_id, telefone)
        return

    estado_memoria = agente.obter_estado_reserva(telefone)
    if estado_memoria and str(estado_memoria.get("conversa_id") or "") not in {"", conversa_id}:
        agente.limpar_historico(telefone)
        estado_memoria = {}
    if not estado_memoria:
        agente.definir_estado_reserva(
            telefone,
            {
                "conversa_id": conversa_id,
                "origem_conversa": str(conversa.get("origem") or ""),
            },
        )


def _salvar_estado_reserva_conversa(
    conversa: Mapping[str, Any],
    telefone: str,
    *,
    resposta: Mapping[str, Any],
) -> None:
    estado = agente.obter_estado_reserva(telefone)
    if not estado:
        return
    estado["conversa_id"] = str(conversa.get("id") or "")
    metadata = _metadata_conversa(conversa)
    metadata["estado_reserva"] = estado
    metadata["dados_reserva"] = dict(resposta.get("dados_reserva") or {})
    metadata["status_reserva"] = resposta.get("status_reserva", "")
    logger.info(
        "DIAG_RESERVA estado_salvo_depois telefone=%s conversa_id=%s estado=%s status_reserva=%s",
        telefone,
        conversa.get("id", ""),
        estado,
        resposta.get("status_reserva", ""),
    )
    _atualizar_metadata_conversa(conversa, metadata)


def _limpar_estado_reserva_conversa(conversa: Mapping[str, Any], telefone: str) -> None:
    metadata = _metadata_conversa(conversa)
    if "estado_reserva" not in metadata and "dados_reserva" not in metadata:
        return
    metadata.pop("estado_reserva", None)
    metadata.pop("dados_reserva", None)
    metadata["estado_reserva_finalizado_em"] = _agora()
    _atualizar_metadata_conversa(conversa, metadata)
    agente.limpar_historico(telefone)


def _metadata_conversa(conversa: Mapping[str, Any]) -> dict[str, Any]:
    metadata = conversa.get("metadata")
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _atualizar_metadata_conversa(conversa: Mapping[str, Any], metadata: Mapping[str, Any]) -> None:
    conversa_id = str(conversa.get("id") or "")
    if isinstance(conversa, dict):
        conversa["metadata"] = dict(metadata)
    if not conversa_id or conversa_id.startswith("local:"):
        return

    resultado = supabase.atualizar(
        _tabela_conversas(),
        {"metadata": dict(metadata)},
        filtros={"id": f"eq.{conversa_id}"},
        retornar=False,
    )
    if resultado.get("ok"):
        logger.info("Metadata da conversa %s atualizada com estado de reserva.", conversa_id)
    else:
        logger.warning("Nao foi possivel atualizar metadata da conversa %s: %s", conversa_id, resultado.get("erro"))


def processar_mensagens_webhook(mensagens: Sequence[MensagemRecebida]) -> list[ResultadoWebhook]:
    resultados: list[ResultadoWebhook] = []
    for mensagem in _agrupar_mensagens_rapidas(mensagens):
        resultados.append(processar_mensagem_webhook(mensagem))
    return resultados


def _agrupar_mensagens_rapidas(mensagens: Sequence[MensagemRecebida]) -> list[MensagemRecebida]:
    janela = _janela_coalescencia_segundos()
    grupos: list[list[MensagemRecebida]] = []
    grupo_atual: list[MensagemRecebida] = []

    for mensagem in mensagens:
        if not grupo_atual:
            grupo_atual = [mensagem]
            continue
        if _mensagens_mesmo_grupo(grupo_atual[-1], mensagem, janela):
            grupo_atual.append(mensagem)
            continue
        grupos.append(grupo_atual)
        grupo_atual = [mensagem]

    if grupo_atual:
        grupos.append(grupo_atual)

    return [_montar_mensagem_agrupada(grupo) for grupo in grupos]


def _mensagens_mesmo_grupo(anterior: MensagemRecebida, atual: MensagemRecebida, janela: float) -> bool:
    telefone_anterior = str(anterior.get("telefone") or "").strip()
    telefone_atual = str(atual.get("telefone") or "").strip()
    if not telefone_anterior or telefone_anterior != telefone_atual:
        return False

    timestamp_anterior = _timestamp_segundos(anterior.get("timestamp"))
    timestamp_atual = _timestamp_segundos(atual.get("timestamp"))
    if timestamp_anterior is None or timestamp_atual is None:
        return True
    return abs(timestamp_atual - timestamp_anterior) <= janela


def _montar_mensagem_agrupada(grupo: Sequence[MensagemRecebida]) -> MensagemRecebida:
    if len(grupo) == 1:
        return dict(grupo[0])

    primeira = grupo[0]
    ultima = grupo[-1]
    textos = [str(item.get("texto") or "").strip() for item in grupo if str(item.get("texto") or "").strip()]
    logger.info(
        "Mensagens rapidas agrupadas para telefone=%s total=%s.",
        primeira.get("telefone", ""),
        len(grupo),
    )
    return {
        "telefone": str(primeira.get("telefone") or ""),
        "texto": "\n".join(textos),
        "remetente": str(ultima.get("remetente") or primeira.get("remetente") or ""),
        "timestamp": str(ultima.get("timestamp") or primeira.get("timestamp") or ""),
        "provider_message_id": str(primeira.get("provider_message_id") or ""),
        "raw": {
            "coalesced": True,
            "messages": [dict(item) for item in grupo],
        },
    }


def _janela_coalescencia_segundos() -> float:
    try:
        valor = float(os.getenv("RESERVABOT_COALESCENCIA_SEGUNDOS", "2"))
    except ValueError:
        return 2.0
    return max(0.0, min(valor, 10.0))


def _timestamp_segundos(valor: Any) -> float | None:
    if valor in (None, ""):
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        pass

    texto = str(valor).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(texto).timestamp()
    except ValueError:
        return None


def _deve_enfileirar_debounce(mensagem: Mapping[str, Any], raw: Mapping[str, Any]) -> bool:
    if raw.get("coalesced") or raw.get("debounce_processado"):
        return False
    if _janela_debounce_segundos() <= 0:
        return False
    return True


def _enfileirar_mensagem_debounce(mensagem: MensagemRecebida) -> ResultadoWebhook:
    telefone = str(mensagem.get("telefone") or "").strip()
    provider_message_id = str(mensagem.get("provider_message_id") or "").strip()
    janela = _janela_debounce_segundos()

    conversa = buscar_conversa_ativa_por_telefone(telefone) or buscar_conversa_por_telefone(telefone)
    conversa_id = str((conversa or {}).get("id") or "")
    chave = _debounce_key(telefone, conversa_id)
    recebido_em = _agora_preciso()
    pendente_persistido = False
    if conversa is not None:
        pendente_persistido = registrar_mensagem(
            conversa,
            remetente="cliente",
            conteudo=str(mensagem.get("texto") or ""),
            provider_message_id="",
            metadata={
                "provider": "cloud",
                "debounce_pending": True,
                "debounce_processing": False,
                "debounce_processed": False,
                "debounce_key": chave,
                "debounce_received_at": recebido_em,
                "debounce_ready_at": _iso_em_segundos(_timestamp_atual() + janela),
                "telefone": telefone,
                "provider_message_id_original": provider_message_id,
                "timestamp_provider": mensagem.get("timestamp", ""),
                "remetente_whatsapp": mensagem.get("remetente", ""),
            },
        ) is True
        if pendente_persistido:
            logger.info(
                "debounce_mensagem_registrada conversa_id=%s provider_message_id=%s chave=%s.",
                conversa_id,
                provider_message_id,
                chave,
            )

    with _debounce_lock:
        lote = _debounce_lotes.get(chave)
        if lote is None:
            lote = {"mensagens": [], "timer": None, "processando": False, "persistente": False, "telefone": telefone}
            _debounce_lotes[chave] = lote
        lote["persistente"] = bool(lote.get("persistente")) or pendente_persistido
        lote["telefone"] = telefone
        timer_antigo = lote.get("timer")
        if isinstance(timer_antigo, threading.Timer):
            timer_antigo.cancel()
        lote["mensagens"].append(dict(mensagem))
        if provider_message_id:
            _provider_ids_pendentes.add(provider_message_id)
        timer = threading.Timer(janela, _processar_lote_debounce, args=(chave,))
        timer.daemon = True
        lote["timer"] = timer
        timer.start()

    logger.info(
        "debounce_janela_renovada conversa_id=%s telefone=%s provider_message_id=%s chave=%s janela=%s.",
        conversa_id,
        telefone,
        provider_message_id,
        chave,
        janela,
    )
    return {
        "ok": True,
        "telefone": telefone,
        "conversa_id": conversa_id,
        "status": "debounce_pendente",
        "resposta_enviada": False,
    }


def _processar_lote_debounce(chave: str) -> None:
    with _debounce_lock:
        chave_lote, lote = _obter_lote_debounce(chave)
        if not lote or lote.get("processando"):
            return
        lote["processando"] = True
        telefone = str(lote.get("telefone") or chave).strip()
        mensagens = [dict(item) for item in lote.get("mensagens", []) if isinstance(item, Mapping)]
        usar_persistente = bool(lote.get("persistente"))
        _debounce_lotes.pop(chave_lote, None)

    if not mensagens and not usar_persistente:
        return

    status_persistente = "memoria"
    mensagens_persistidas: list[MensagemRecebida] = []
    if usar_persistente:
        mensagens_persistidas, status_persistente, retry_em = _carregar_lote_debounce_persistente(chave_lote, telefone)
        if status_persistente in {"aguardando", "erro", "lock_ignorado"}:
            _agendar_processamento_debounce(chave_lote, telefone=telefone, atraso=max(retry_em, DEBOUNCE_RETRY_SECONDS), persistente=True)
            return
        if status_persistente != "ok":
            return

    mensagens_processamento = mensagens_persistidas if usar_persistente else mensagens

    for mensagem in mensagens_processamento:
        provider_message_id = str(mensagem.get("provider_message_id") or "").strip()
        if provider_message_id:
            _provider_ids_pendentes.discard(provider_message_id)

    mensagem_agrupada = _montar_mensagem_agrupada(mensagens_processamento)
    raw = mensagem_agrupada.get("raw") if isinstance(mensagem_agrupada.get("raw"), Mapping) else {}
    mensagem_agrupada["raw"] = {
        **dict(raw),
        "debounce_processado": True,
        "debounce_persistente": bool(mensagens_persistidas),
    }
    raw_final = mensagem_agrupada.get("raw") if isinstance(mensagem_agrupada.get("raw"), Mapping) else {}
    mensagens_raw = raw_final.get("messages") if isinstance(raw_final.get("messages"), list) else [mensagem_agrupada]
    provider_ids = [
        str(item.get("provider_message_id") or "").strip()
        for item in mensagens_raw
        if isinstance(item, Mapping) and str(item.get("provider_message_id") or "").strip()
    ]
    conversa_id = _conversa_id_debounce(mensagens_processamento, chave_lote)
    logger.info(
        "debounce_grupo_formado conversa_id=%s quantidade=%s provider_message_ids=%s texto=%s.",
        conversa_id,
        len(mensagens_processamento),
        provider_ids,
        _texto_seguro_log(str(mensagem_agrupada.get("texto") or "")),
    )
    logger.info(
        "Processando lote debounce: telefone=%s mensagens=%s fonte=%s.",
        telefone,
        len(mensagens_processamento),
        "supabase" if mensagens_persistidas else "memoria",
    )
    try:
        resultado = processar_mensagem_webhook(mensagem_agrupada)
        logger.info(
            "debounce_agente_executado conversa_id=%s provider_message_ids=%s status=%s resposta_enviada=%s.",
            conversa_id,
            provider_ids,
            resultado.get("status", ""),
            bool(resultado.get("resposta_enviada")),
        )
        logger.info(
            "debounce_grupo_concluido conversa_id=%s quantidade=%s provider_message_ids=%s.",
            conversa_id,
            len(mensagens_processamento),
            provider_ids,
        )
    except Exception:
        logger.exception("Falha ao processar lote debounce telefone=%s.", telefone)


def _carregar_lote_debounce_persistente(chave: str, telefone: str) -> tuple[list[MensagemRecebida], str, float]:
    conversa_id = _conversa_id_debounce_chave(chave)
    if not conversa_id:
        conversa = buscar_conversa_ativa_por_telefone(telefone) or buscar_conversa_por_telefone(telefone)
        conversa_id = str((conversa or {}).get("id") or "")
    if not conversa_id or conversa_id.startswith("local:"):
        return [], "vazio", 0.0

    resultado = supabase.selecionar(
        _tabela_mensagens(),
        filtros={
            "conversa_id": f"eq.{conversa_id}",
            "remetente": "eq.cliente",
            "metadata->>debounce_pending": "eq.true",
            "metadata->>debounce_key": f"eq.{chave}",
        },
        colunas="id,conversa_id,remetente,conteudo,timestamp,provider_message_id,metadata,created_at",
        limite=20,
        order="timestamp.asc,created_at.asc",
    )
    if not resultado.get("ok"):
        logger.warning("debounce_lock_ignorado conversa_id=%s chave=%s motivo=consulta_falhou erro=%s", conversa_id, chave, resultado.get("erro"))
        return [], "erro", DEBOUNCE_RETRY_SECONDS

    dados = resultado.get("data")
    pendentes = [item for item in dados if isinstance(item, dict)] if isinstance(dados, list) else []
    if not pendentes:
        return [], "vazio", 0.0

    janela = _janela_debounce_segundos()
    ultimo_recebimento = max(_timestamp_debounce_mensagem(item) for item in pendentes)
    restante = (ultimo_recebimento + janela) - _timestamp_atual()
    if restante > 0.05:
        logger.info(
            "debounce_aguardando_nova_janela conversa_id=%s chave=%s mensagens=%s restante=%.3f.",
            conversa_id,
            chave,
            len(pendentes),
            restante,
        )
        return [], "aguardando", min(max(restante, 0.1), janela)

    lock_id = f"{_agora()}:{uuid4()}"
    travadas: list[dict[str, Any]] = []
    for item in pendentes:
        travada = _travar_mensagem_debounce_pendente(item, lock_id=lock_id, chave=chave)
        if travada is None:
            logger.info(
                "debounce_lock_ignorado conversa_id=%s chave=%s lock_id=%s motivo=mensagem_ja_travada.",
                conversa_id,
                chave,
                lock_id,
            )
            _liberar_mensagens_debounce_travadas(travadas)
            return [], "lock_ignorado", DEBOUNCE_RETRY_SECONDS
        travadas.append(travada)
    mensagens = [_mensagem_debounce_persistida_para_recebida(item) for item in travadas if item]
    if mensagens:
        logger.info(
            "debounce_lock_adquirido telefone=%s conversa_id=%s chave=%s mensagens=%s lock_id=%s.",
            telefone,
            conversa_id,
            chave,
            len(mensagens),
            lock_id,
        )
        return mensagens, "ok", 0.0
    return [], "vazio", 0.0


def _travar_mensagem_debounce_pendente(mensagem: Mapping[str, Any], *, lock_id: str, chave: str) -> dict[str, Any] | None:
    mensagem_id = str(mensagem.get("id") or "").strip()
    if not mensagem_id:
        return None
    metadata = _metadata_mensagem(mensagem)
    metadata.update(
        {
            "debounce_pending": False,
            "debounce_processing": True,
            "debounce_lock": lock_id,
            "debounce_processing_started_at": _agora(),
            "debounce_lock_expires_at": _iso_em_segundos(_timestamp_atual() + DEBOUNCE_LEASE_SECONDS),
        }
    )
    resultado = supabase.atualizar(
        _tabela_mensagens(),
        {"metadata": metadata},
        filtros={
            "id": f"eq.{mensagem_id}",
            "metadata->>debounce_pending": "eq.true",
            "metadata->>debounce_key": f"eq.{chave}",
        },
    )
    if not resultado.get("ok"):
        logger.warning("Mensagem pendente de debounce nao travada id=%s: %s", mensagem_id, resultado.get("erro"))
        return None
    atualizada = _primeiro(resultado.get("data"))
    if not atualizada:
        return None
    return dict(atualizada)


def _liberar_mensagens_debounce_travadas(mensagens: Sequence[Mapping[str, Any]]) -> None:
    for mensagem in mensagens:
        mensagem_id = str(mensagem.get("id") or "").strip()
        if not mensagem_id:
            continue
        metadata = _metadata_mensagem(mensagem)
        metadata.update(
            {
                "debounce_pending": True,
                "debounce_processing": False,
                "debounce_lock": "",
                "debounce_processing_started_at": "",
                "debounce_lock_expires_at": "",
            }
        )
        supabase.atualizar(
            _tabela_mensagens(),
            {"metadata": metadata},
            filtros={"id": f"eq.{mensagem_id}"},
            retornar=False,
        )


def _mensagem_debounce_persistida_para_recebida(mensagem: Mapping[str, Any]) -> MensagemRecebida:
    metadata = _metadata_mensagem(mensagem)
    return {
        "telefone": str(metadata.get("telefone") or ""),
        "texto": str(mensagem.get("conteudo") or ""),
        "remetente": str(metadata.get("remetente_whatsapp") or ""),
        "timestamp": str(metadata.get("timestamp_provider") or mensagem.get("timestamp") or ""),
        "provider_message_id": str(metadata.get("provider_message_id_original") or ""),
        "raw": {
            "debounce_mensagem_id": str(mensagem.get("id") or ""),
            "debounce_metadata": metadata,
            "debounce_conversa_id": str(mensagem.get("conversa_id") or ""),
        },
    }


def _metadata_mensagem(mensagem: Mapping[str, Any]) -> dict[str, Any]:
    metadata = mensagem.get("metadata")
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _debounce_key(telefone: str, conversa_id: str) -> str:
    return f"{str(conversa_id or '').strip()}:{str(telefone or '').strip()}"


def _janela_debounce_segundos() -> float:
    try:
        valor = float(os.getenv(DEBOUNCE_SECONDS_ENV, str(DEBOUNCE_SECONDS_DEFAULT)))
    except ValueError:
        return DEBOUNCE_SECONDS_DEFAULT
    return max(0.0, min(valor, 15.0))


def _obter_lote_debounce(chave: str) -> tuple[str, dict[str, Any] | None]:
    lote = _debounce_lotes.get(chave)
    if lote is not None:
        return chave, lote
    for chave_lote, lote_atual in _debounce_lotes.items():
        if str(lote_atual.get("telefone") or "") == chave:
            return chave_lote, lote_atual
    return chave, None


def _agendar_processamento_debounce(chave: str, *, telefone: str, atraso: float, persistente: bool) -> None:
    atraso_final = max(0.1, min(float(atraso or DEBOUNCE_RETRY_SECONDS), 15.0))
    with _debounce_lock:
        lote = _debounce_lotes.get(chave)
        if lote is None:
            lote = {"mensagens": [], "timer": None, "processando": False, "persistente": persistente, "telefone": telefone}
            _debounce_lotes[chave] = lote
        lote["persistente"] = bool(lote.get("persistente")) or persistente
        lote["telefone"] = telefone
        lote["processando"] = False
        timer_antigo = lote.get("timer")
        if isinstance(timer_antigo, threading.Timer):
            timer_antigo.cancel()
        timer = threading.Timer(atraso_final, _processar_lote_debounce, args=(chave,))
        timer.daemon = True
        lote["timer"] = timer
        timer.start()
    logger.info("debounce_janela_renovada telefone=%s chave=%s janela=%.3f.", telefone, chave, atraso_final)


def _timestamp_atual() -> float:
    return datetime.now(timezone.utc).timestamp()


def _agora_preciso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _iso_em_segundos(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="milliseconds")


def _timestamp_debounce_mensagem(mensagem: Mapping[str, Any]) -> float:
    metadata = _metadata_mensagem(mensagem)
    for valor in (
        metadata.get("debounce_received_at"),
        metadata.get("timestamp_provider"),
        mensagem.get("timestamp"),
        mensagem.get("created_at"),
    ):
        timestamp = _timestamp_segundos(valor)
        if timestamp is not None:
            return timestamp
    return _timestamp_atual()


def _texto_seguro_log(texto: str, *, limite: int = 500) -> str:
    normalizado = re.sub(r"\s+", " ", str(texto or "")).strip()
    if len(normalizado) <= limite:
        return normalizado
    return normalizado[:limite] + "..."


def _conversa_id_debounce(mensagens: Sequence[Mapping[str, Any]], chave: str) -> str:
    for mensagem in mensagens:
        raw = mensagem.get("raw") if isinstance(mensagem.get("raw"), Mapping) else {}
        conversa_id = str(raw.get("debounce_conversa_id") or "").strip()
        if conversa_id:
            return conversa_id
        metadata = raw.get("debounce_metadata") if isinstance(raw.get("debounce_metadata"), Mapping) else {}
        chave_metadata = str(metadata.get("debounce_key") or "").strip()
        if ":" in chave_metadata:
            return chave_metadata.split(":", 1)[0]
    return chave.split(":", 1)[0] if ":" in chave else chave


def _conversa_id_debounce_chave(chave: str) -> str:
    if ":" not in chave:
        return ""
    return chave.split(":", 1)[0].strip()


def _mensagens_originais_lote(mensagem: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = mensagem.get("raw") if isinstance(mensagem.get("raw"), Mapping) else {}
    originais = raw.get("messages") if isinstance(raw.get("messages"), list) else []
    itens = [item for item in originais if isinstance(item, Mapping)]
    return itens or [mensagem]


def _midias_lote(mensagem: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    midias: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for item in _mensagens_originais_lote(mensagem):
        media = item.get("media") if isinstance(item.get("media"), Mapping) else {}
        if media:
            midias.append((item, media))
    return midias


def _contexto_midia_lote(mensagem: Mapping[str, Any], *, comprovante_persistido: bool = False) -> dict[str, Any]:
    midias = _midias_lote(mensagem)
    return {
        "mensagem_contem_midia": bool(midias),
        "tipos_midia": [str(media.get("tipo") or "") for _, media in midias if str(media.get("tipo") or "")],
        "media_ids": [str(media.get("media_id") or "") for _, media in midias if str(media.get("media_id") or "")],
        "comprovante_persistido": comprovante_persistido,
    }


def processar_mensagem_webhook(mensagem: MensagemRecebida) -> ResultadoWebhook:
    telefone = str(mensagem.get("telefone") or "").strip()
    texto = str(mensagem.get("texto") or "").strip()
    midias_lote = _midias_lote(mensagem)
    if telefone and midias_lote:
        item_midia, media = next(
            (
                item
                for item in midias_lote
                if str(item[1].get("mime_type") or "").lower() in comprovantes_reserva.MIME_TYPES_ACEITOS
            ),
            midias_lote[0],
        )
        mensagem_midia: MensagemRecebida = {
            **dict(item_midia),
            "telefone": telefone,
            "raw": {
                **dict(item_midia.get("raw") if isinstance(item_midia.get("raw"), Mapping) else {}),
                "lote_original": [dict(item) for item in _mensagens_originais_lote(mensagem)],
                "contexto_midia": _contexto_midia_lote(mensagem),
            },
        }
        if len(midias_lote) > 1:
            logger.warning(
                "Lote contem mais de uma midia; apenas o primeiro comprovante valido sera processado. telefone=%s total=%s.",
                telefone,
                len(midias_lote),
            )
        return _processar_comprovante_webhook(mensagem_midia, media=media)
    if not telefone or not texto:
        return {
            "ok": False,
            "telefone": telefone,
            "status": "ignorada",
            "erro": "mensagem sem telefone ou texto",
        }

    raw = mensagem.get("raw") if isinstance(mensagem.get("raw"), Mapping) else {}
    provider_message_id = str(mensagem.get("provider_message_id") or "").strip()
    if (
        provider_message_id
        and not raw.get("coalesced")
        and not raw.get("debounce_processado")
    ):
        with _debounce_lock:
            pendente_memoria = provider_message_id in _provider_ids_pendentes
        if pendente_memoria or _provider_message_id_registrado(provider_message_id):
            logger.info(
                "debounce_lock_ignorado telefone=%s provider_message_id=%s motivo=provider_message_id_ja_registrado.",
                telefone,
                provider_message_id,
            )
            return {
                "ok": True,
                "telefone": telefone,
                "status": "duplicada",
                "resposta_enviada": False,
            }
    if _deve_enfileirar_debounce(mensagem, raw):
        return _enfileirar_mensagem_debounce(mensagem)

    conversa = _buscar_conversa_debounce_raw(raw, telefone) or buscar_conversa_ativa_por_telefone(telefone)
    logger.info(
        "DIAG_RESERVA webhook_mensagem_vinculo telefone=%s provider_message_id=%s conversa_ativa_id=%s conversa_status=%s",
        telefone,
        mensagem.get("provider_message_id", ""),
        (conversa or {}).get("id", ""),
        (conversa or {}).get("status", ""),
    )
    metadata = {
        "provider": "cloud",
        "timestamp_provider": mensagem.get("timestamp", ""),
        "remetente_whatsapp": mensagem.get("remetente", ""),
        "raw": raw,
        **_contexto_midia_lote(mensagem),
    }
    mensagens_agrupadas = raw.get("messages") if isinstance(raw, Mapping) and raw.get("coalesced") else None
    if isinstance(mensagens_agrupadas, list):
        metadata["mensagens_agrupadas"] = [
            {
                "texto": str(item.get("texto") or ""),
                "provider_message_id": str(item.get("provider_message_id") or ""),
                "timestamp": str(item.get("timestamp") or ""),
            }
            for item in mensagens_agrupadas
            if isinstance(item, Mapping)
        ]
    if raw.get("debounce_persistente"):
        metadata["cliente_ja_registrado"] = True
        if isinstance(mensagens_agrupadas, list):
            metadata["mensagens_debounce_persistidas"] = _mensagens_debounce_persistidas_metadata(mensagens_agrupadas)
        else:
            metadata["mensagens_debounce_persistidas"] = _mensagens_debounce_persistidas_metadata([mensagem])
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


def _processar_comprovante_webhook(
    mensagem: MensagemRecebida,
    *,
    media: Mapping[str, Any],
) -> ResultadoWebhook:
    telefone = str(mensagem.get("telefone") or "").strip()
    provider_message_id = str(mensagem.get("provider_message_id") or "").strip()
    raw = mensagem.get("raw") if isinstance(mensagem.get("raw"), Mapping) else {}
    contexto_midia = raw.get("contexto_midia") if isinstance(raw.get("contexto_midia"), Mapping) else {}
    if not contexto_midia:
        contexto_midia = _contexto_midia_lote(mensagem)
    mensagem_pre_registrada = bool(provider_message_id and _provider_message_id_registrado(provider_message_id))
    if mensagem_pre_registrada and comprovantes_reserva.obter_por_provider_message_id(provider_message_id):
        logger.info("Comprovante inbound duplicado ignorado: provider_message_id=%s.", provider_message_id)
        return {"ok": True, "telefone": telefone, "status": "duplicada", "resposta_enviada": False}
    if mensagem_pre_registrada:
        logger.info(
            "Reprocessando comprovante previamente registrado sem arquivo concluido: provider_message_id=%s.",
            provider_message_id,
        )

    conversa = buscar_conversa_ativa_por_telefone(telefone) or buscar_conversa_por_telefone(telefone)
    if not conversa:
        return {
            "ok": False,
            "telefone": telefone,
            "status": "ignorada",
            "erro": "conversa nao encontrada para a midia",
        }

    _carregar_estado_reserva_conversa(conversa, telefone)
    estado = agente.obter_estado_reserva(telefone)
    nome_arquivo = str(media.get("nome_arquivo") or "").strip()
    tipo = str(media.get("tipo") or "arquivo").strip()
    conteudo_mensagem = nome_arquivo or ("Imagem recebida" if tipo == "image" else "Documento recebido")
    if not mensagem_pre_registrada:
        registrado = registrar_mensagem(
            conversa,
            remetente="cliente",
            conteudo=conteudo_mensagem,
            provider_message_id=provider_message_id,
            metadata={
                "provider": "cloud",
                "tipo": tipo,
                "media_id": str(media.get("media_id") or ""),
                "mime_type": str(media.get("mime_type") or ""),
                "comprovante_processado": False,
                "mensagem_contem_midia": True,
                "tipos_midia": list(contexto_midia.get("tipos_midia") or [tipo]),
                "media_ids": list(contexto_midia.get("media_ids") or [str(media.get("media_id") or "")]),
                "comprovante_persistido": False,
            },
        )
        if not registrado:
            if provider_message_id and _provider_message_id_registrado(provider_message_id):
                return {"ok": True, "telefone": telefone, "status": "duplicada", "resposta_enviada": False}
            atualizar_status_conversa(conversa, status="aguardando_humano")
            return {
                "ok": False,
                "telefone": telefone,
                "status": "aguardando_humano",
                "resposta_enviada": False,
                "erro": "nao foi possivel registrar a midia recebida",
            }

    if str(estado.get("etapa") or "") != "aguardando_comprovante":
        atualizar_status_conversa(conversa, status="aguardando_humano")
        logger.warning(
            "Midia recebida fora da etapa de comprovante. telefone=%s conversa_id=%s etapa=%s.",
            telefone,
            conversa.get("id", ""),
            estado.get("etapa", ""),
        )
        return {
            "ok": True,
            "telefone": telefone,
            "conversa_id": str(conversa.get("id") or ""),
            "status": "aguardando_humano",
            "resposta_enviada": False,
        }

    reserva_id = str(estado.get("reserva_id") or "").strip()
    if not reserva_id:
        reserva = _buscar_reserva_por_conversa(str(conversa.get("id") or ""))
        reserva_id = str((reserva or {}).get("id") or "")
    resultado = comprovantes_reserva.receber_comprovante(
        media=media,
        provider_message_id=provider_message_id,
        conversa_id=str(conversa.get("id") or ""),
        reserva_id=reserva_id,
    )
    if not resultado.get("ok"):
        logger.error(
            "Falha ao receber comprovante: telefone=%s conversa_id=%s reserva_id=%s erro=%s.",
            telefone,
            conversa.get("id", ""),
            reserva_id,
            resultado.get("erro", ""),
        )
        estado["etapa"] = "aguardando_comprovante"
        estado["campo_pendente"] = "comprovante"
        estado["comprovante_status"] = "aguardando_comprovante"
        agente.definir_estado_reserva(telefone, estado)
        resposta_estado: agente.RespostaAgente = {
            "texto": "",
            "reserva_confirmada": False,
            "dados_reserva": dict((conversa.get("metadata") or {}).get("dados_reserva") or {}),
            "status_reserva": "aguardando_comprovante",
            "confianca": 1.0,
        }
        _salvar_estado_reserva_conversa(conversa, telefone, resposta=resposta_estado)
        texto_erro = "Recebi o arquivo, mas nao consegui registra-lo corretamente. Pode envia-lo novamente?"
        envio = whatsapp.enviar_com_resultado(telefone, texto_erro)
        registrar_mensagem(
            conversa,
            remetente="bot",
            conteudo=texto_erro,
            provider_message_id=str(envio.get("provider_message_id") or ""),
            metadata={"envio_ok": bool(envio.get("ok")), "falha_comprovante": True},
        )
        return {
            "ok": False,
            "telefone": telefone,
            "conversa_id": str(conversa.get("id") or ""),
            "status": "aguardando_comprovante",
            "resposta_enviada": bool(envio.get("ok")),
            "erro": str(resultado.get("erro") or "falha ao receber comprovante"),
        }

    if reserva_id:
        atualizado = supabase.atualizar(
            _tabela_reservas(),
            {"status": "aguardando_analise", "status_pagamento": "aguardando_analise"},
            filtros={"id": f"eq.{reserva_id}", "status": "eq.aguardando_comprovante"},
            retornar=False,
        )
        if not atualizado.get("ok"):
            logger.error("Reserva nao atualizada para aguardando_analise: reserva_id=%s erro=%s", reserva_id, atualizado.get("erro"))

    comprovante = resultado.get("comprovante") if isinstance(resultado.get("comprovante"), Mapping) else {}
    _marcar_mensagem_comprovante_processada(
        provider_message_id=provider_message_id,
        comprovante_id=str(comprovante.get("id") or ""),
        reserva_id=reserva_id,
        tipo=tipo,
        media_id=str(media.get("media_id") or ""),
        mime_type=str(media.get("mime_type") or ""),
    )

    estado["etapa"] = "aguardando_analise"
    estado["campo_pendente"] = "analise_comprovante"
    estado["comprovante_status"] = "aguardando_analise"
    if reserva_id:
        estado["reserva_id"] = reserva_id
    agente.definir_estado_reserva(telefone, estado)
    resposta_estado: agente.RespostaAgente = {
        "texto": "",
        "reserva_confirmada": False,
        "dados_reserva": dict((conversa.get("metadata") or {}).get("dados_reserva") or {}),
        "status_reserva": "aguardando_analise",
        "confianca": 1.0,
    }
    _salvar_estado_reserva_conversa(conversa, telefone, resposta=resposta_estado)

    texto = (
        "Comprovante recebido! A solicitacao ficou aguardando a analise da equipe. "
        "Assim que for verificado, voce recebera a confirmacao."
    )
    envio = whatsapp.enviar_com_resultado(telefone, texto)
    registrar_mensagem(
        conversa,
        remetente="bot",
        conteudo=texto,
        provider_message_id=str(envio.get("provider_message_id") or ""),
        metadata={
            "envio_ok": bool(envio.get("ok")),
            "status_reserva": "aguardando_analise",
            "reserva_id": reserva_id,
            "comprovante_id": str(comprovante.get("id") or ""),
        },
    )
    atualizar_status_conversa(conversa, status="aguardando_humano")
    logger.info(
        "Comprovante recebido e encaminhado para analise humana: telefone=%s conversa_id=%s reserva_id=%s.",
        telefone,
        conversa.get("id", ""),
        reserva_id,
    )
    return {
        "ok": True,
        "telefone": telefone,
        "conversa_id": str(conversa.get("id") or ""),
        "status": "aguardando_analise",
        "reserva_confirmada": False,
        "resposta_enviada": bool(envio.get("ok")),
    }


def _buscar_reserva_por_conversa(conversa_id: str) -> dict[str, Any] | None:
    if not conversa_id:
        return None
    resultado = supabase.selecionar(
        _tabela_reservas(),
        filtros={"conversa_id": f"eq.{conversa_id}"},
        limite=1,
        order="created_at.desc",
    )
    return _primeiro(resultado.get("data")) if resultado.get("ok") else None


def _marcar_mensagem_comprovante_processada(
    *,
    provider_message_id: str,
    comprovante_id: str,
    reserva_id: str,
    tipo: str,
    media_id: str,
    mime_type: str,
) -> None:
    if not provider_message_id:
        return
    resultado = supabase.atualizar(
        _tabela_mensagens(),
        {
            "metadata": {
                "provider": "cloud",
                "comprovante_processado": True,
                "mensagem_contem_midia": True,
                "tipos_midia": [tipo],
                "media_ids": [media_id],
                "mime_type": mime_type,
                "comprovante_persistido": True,
                "comprovante_id": comprovante_id,
                "reserva_id": reserva_id,
            }
        },
        filtros={"provider_message_id": f"eq.{provider_message_id}"},
        retornar=False,
    )
    if not resultado.get("ok"):
        logger.warning(
            "Mensagem de comprovante nao marcada como processada: provider_message_id=%s erro=%s.",
            provider_message_id,
            resultado.get("erro", ""),
        )


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


def _buscar_conversa_debounce_raw(raw: Mapping[str, Any], telefone: str) -> Conversa | None:
    if not raw.get("debounce_processado"):
        return None
    conversa_id = str(raw.get("debounce_conversa_id") or "").strip()
    mensagens = raw.get("messages")
    if not conversa_id and isinstance(mensagens, list):
        for item in mensagens:
            if not isinstance(item, Mapping):
                continue
            item_raw = item.get("raw") if isinstance(item.get("raw"), Mapping) else {}
            conversa_id = str(item_raw.get("debounce_conversa_id") or "").strip()
            if conversa_id:
                break
    if not conversa_id:
        return None
    conversa = _buscar_conversa_por_id(conversa_id)
    if conversa is not None:
        return conversa
    logger.warning("Conversa do debounce nao recuperada por id=%s; usando referencia minima.", conversa_id)
    return {"id": conversa_id, "cliente_telefone": telefone, "status": "bot_ativo", "metadata": {}}


def _buscar_conversa_por_id(conversa_id: str) -> Conversa | None:
    conversa_id = str(conversa_id or "").strip()
    if not conversa_id or conversa_id.startswith("local:"):
        return None
    resultado = supabase.selecionar(
        _tabela_conversas(),
        filtros={"id": f"eq.{conversa_id}"},
        limite=1,
    )
    if not resultado.get("ok"):
        logger.warning("Conversa nao recuperada por id=%s: %s", conversa_id, resultado.get("erro"))
        return None
    conversa = _primeiro(resultado.get("data"))
    return dict(conversa) if conversa else None


def registrar_solicitacao_reserva(
    *,
    cliente: Mapping[str, Any],
    conversa: Mapping[str, Any],
    dados_reserva: agente.DadosReserva,
    estado: Mapping[str, Any],
    config: config_restaurante.ConfigRestaurante,
) -> dict[str, Any]:
    telefone = str(cliente.get("telefone") or "").strip()
    conversa_id = str(conversa.get("id") or "").strip()
    nome = str(dados_reserva.get("nome_cliente") or cliente.get("nome") or "").strip()
    if not conversa_id or conversa_id.startswith("local:"):
        return {"ok": False, "erro": "conversa nao persistida"}
    if not agente.dados_reserva_obrigatorios_ok(dados_reserva, nome_cliente=nome, telefone=telefone):
        return {"ok": False, "erro": "dados obrigatorios invalidos"}

    existente_resultado = supabase.selecionar(
        _tabela_reservas(),
        filtros={"conversa_id": f"eq.{conversa_id}"},
        limite=1,
        order="created_at.desc",
    )
    existente = _primeiro(existente_resultado.get("data")) if existente_resultado.get("ok") else None
    espaco_id = _espaco_operacional_id(estado)
    metadata = {
        "nome": nome,
        "preferencia_espaco_id": estado.get("preferencia_espaco_id"),
        "preferencia_espaco_nome": estado.get("preferencia_espaco_nome"),
        "espaco_sugerido_id": estado.get("espaco_sugerido_id"),
        "espaco_sugerido_nome": estado.get("espaco_sugerido_nome"),
        "regra_espaco_obrigatoria": bool(estado.get("regra_espaco_obrigatoria")),
        "local_garantido": False,
        "origem_conversa": conversa.get("origem"),
    }
    payload = {
        "cliente_id": cliente.get("id"),
        "cliente_telefone": telefone,
        "conversa_id": conversa_id,
        "estabelecimento_id": config.estabelecimento_id or None,
        "espaco_id": espaco_id or None,
        "data_reserva": dados_reserva.get("data_reserva"),
        "horario": dados_reserva.get("horario"),
        "pessoas": dados_reserva.get("pessoas"),
        "observacoes": dados_reserva.get("observacoes"),
        "status": "aguardando_comprovante",
        "status_pagamento": "aguardando_comprovante",
        "metadata": metadata,
    }
    if existente:
        if str(existente.get("status") or "") == "confirmada":
            return {"ok": True, "reserva": existente, "existente": True}
        resultado = supabase.atualizar(
            _tabela_reservas(),
            _sem_vazios(payload),
            filtros={"id": f"eq.{existente.get('id')}"},
        )
    else:
        resultado = supabase.inserir(_tabela_reservas(), _sem_vazios(payload))
    if not resultado.get("ok"):
        return {
            "ok": False,
            "erro": resultado.get("detalhe") or resultado.get("erro") or "falha ao salvar solicitacao",
        }
    reserva = _primeiro(resultado.get("data")) or existente or payload
    logger.info(
        "Solicitacao de reserva persistida: reserva_id=%s conversa_id=%s status=aguardando_comprovante espaco_id=%s.",
        reserva.get("id", ""),
        conversa_id,
        espaco_id,
    )
    return {"ok": True, "reserva": reserva, "existente": bool(existente)}


def listar_comprovantes_reserva(reserva_id: str) -> list[dict[str, Any]]:
    return comprovantes_reserva.listar_por_reserva(reserva_id)


def confirmar_reserva_por_humano(reserva_id: str, *, analisado_por: str = "painel") -> dict[str, Any]:
    reserva_id_limpo = str(reserva_id or "").strip()
    resultado = supabase.selecionar(
        _tabela_reservas(),
        filtros={"id": f"eq.{reserva_id_limpo}"},
        limite=1,
    )
    reserva = _primeiro(resultado.get("data")) if resultado.get("ok") else None
    if not reserva:
        return {"ok": False, "status": 404, "erro": "reserva nao encontrada"}
    if str(reserva.get("status") or "") == "confirmada":
        return {"ok": True, "reserva": reserva, "ja_confirmada": True}
    if str(reserva.get("status") or "") != "aguardando_analise":
        return {"ok": False, "status": 409, "erro": "reserva nao esta aguardando analise"}
    comprovantes = comprovantes_reserva.listar_por_reserva(reserva_id_limpo)
    if not comprovantes:
        return {"ok": False, "status": 409, "erro": "reserva sem comprovante registrado"}

    atualizado = supabase.chamar_rpc(
        "confirmar_reserva_comprovante",
        {"p_reserva_id": reserva_id_limpo, "p_analisado_por": analisado_por},
    )
    reserva_confirmada = _primeiro(atualizado.get("data")) if atualizado.get("ok") else None
    if not reserva_confirmada:
        logger.warning(
            "Confirmacao humana transacional recusada: reserva_id=%s erro=%s detalhe=%s.",
            reserva_id_limpo,
            atualizado.get("erro", ""),
            atualizado.get("detalhe", ""),
        )
        return {"ok": False, "status": 409, "erro": "reserva foi alterada durante a analise"}
    conversa_id = str(reserva_confirmada.get("conversa_id") or "")
    conversa = _buscar_conversa_por_id(conversa_id)
    if conversa:
        finalizar_conversa(conversa, status="finalizada")

    telefone = str(reserva_confirmada.get("cliente_telefone") or "")
    texto = (
        "Comprovante aprovado! Sua reserva foi confirmada para "
        f"{reserva_confirmada.get('data_reserva')}, as {str(reserva_confirmada.get('horario') or '')[:5]}, "
        f"para {reserva_confirmada.get('pessoas')} pessoas."
    )
    envio = whatsapp.enviar_com_resultado(telefone, texto) if telefone else {"ok": False}
    if conversa:
        registrar_mensagem(
            conversa,
            remetente="bot",
            conteudo=texto,
            provider_message_id=str(envio.get("provider_message_id") or ""),
            metadata={"confirmacao_humana": True, "reserva_id": reserva_id_limpo, "envio_ok": bool(envio.get("ok"))},
        )
    logger.info(
        "Reserva confirmada por acao humana autenticada: reserva_id=%s analisado_por=%s envio_ok=%s.",
        reserva_id_limpo,
        analisado_por,
        bool(envio.get("ok")),
    )
    return {"ok": True, "reserva": reserva_confirmada, "notificacao_enviada": bool(envio.get("ok"))}


def _espaco_operacional_id(estado: Mapping[str, Any]) -> str:
    if estado.get("regra_espaco_obrigatoria") and estado.get("cliente_autorizou_espaco_direcionado"):
        return str(estado.get("espaco_sugerido_id") or estado.get("espaco_direcionado_id") or "")
    horario = str(estado.get("horario") or "")[:5]
    if horario in {"18:00", "19:00"}:
        return ""
    return str(estado.get("preferencia_espaco_id") or "")


def registrar_reserva_confirmada(
    *,
    cliente: Mapping[str, Any],
    conversa: Mapping[str, Any] | None,
    dados_reserva: agente.DadosReserva,
    autorizacao_humana: bool = False,
) -> bool:
    telefone = str(cliente.get("telefone") or "").strip()
    conversa_id = str((conversa or {}).get("id") or "")
    if not autorizacao_humana:
        logger.warning(
            "Tentativa de confirmacao automatica bloqueada. telefone=%s conversa=%s.",
            telefone,
            conversa_id,
        )
        return False
    logger.warning(
        "Registro direto de reserva confirmada bloqueado. Use confirmar_reserva_por_humano com comprovante em analise. telefone=%s conversa=%s.",
        telefone,
        conversa_id,
    )
    return False


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


def _finalizar_conversas_ativas_por_telefone(telefone: str, *, motivo: str) -> None:
    telefone_limpo = str(telefone or "").strip()
    if not telefone_limpo:
        return

    resultado = supabase.atualizar(
        _tabela_conversas(),
        {"status": "finalizada", "data_fim": _agora()},
        filtros={
            "cliente_telefone": f"eq.{telefone_limpo}",
            "status": f"in.({','.join(sorted(STATUS_BOT_ATIVO))})",
        },
        retornar=False,
    )
    if resultado.get("ok"):
        logger.info("Conversas ativas anteriores finalizadas para telefone=%s motivo=%s.", telefone_limpo, motivo)
    else:
        logger.debug("Nenhuma conversa ativa anterior finalizada para telefone=%s: %s", telefone_limpo, resultado.get("erro"))


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
        order="updated_at.desc,data_inicio.desc",
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
    if statuses and len(conversas) > 1:
        logger.warning(
            "DIAG_RESERVA multiplas_conversas_ativas telefone=%s total=%s ids=%s statuses=%s",
            telefone_limpo,
            len(conversas),
            [item.get("id") for item in conversas],
            sorted(statuses),
        )

    return dict(max(conversas, key=_chave_ordenacao_conversa))


def _chave_ordenacao_conversa(conversa: Mapping[str, Any]) -> str:
    return str(conversa.get("updated_at") or conversa.get("data_inicio") or conversa.get("created_at") or "")


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


def _provider_message_id_registrado(provider_message_id: str) -> bool:
    message_id = str(provider_message_id or "").strip()
    if not message_id:
        return False
    if _mensagem_ja_processada(message_id):
        return True
    resultado = supabase.selecionar(
        _tabela_mensagens(),
        filtros={"metadata->>provider_message_id_original": f"eq.{message_id}"},
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
