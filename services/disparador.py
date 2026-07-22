from __future__ import annotations

import logging
import os
import random
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypedDict

from services import agente, clientes_supabase, dados, fluxo_reservas, mensagens, perfis, planilha, whatsapp
from services.comunicacao import normalizar_telefone
from services import supabase


logger = logging.getLogger(__name__)
DEFAULT_DISPAROS_TABLE = "disparos_mensagens"


class ResultadoEnvio(TypedDict, total=False):
    telefone: str
    nome: str
    status: str
    detalhe: str
    enviado: bool
    provider_message_id: str


class ConversaAtiva(TypedDict, total=False):
    telefone: str
    nome: str
    iniciada_em: str
    ultima_mensagem_cliente: str
    remetente_whatsapp: str
    conversa_id: str
    perfil_id: str
    perfil_nome: str

_conversas_ativas: dict[str, ConversaAtiva] = {}
_lock_conversas = threading.Lock()


def executar_disparo_diario(
    caminho_planilha: str | Path = planilha.PLANILHA_PADRAO,
    data_referencia: date | None = None,
    dias: int | None = None,
    telefone: str = "",
    somente_teste: bool = False,
    modo_teste: bool = False,
    forcar_reenvio: bool = False,
) -> list[ResultadoEnvio]:
    if forcar_reenvio and not normalizar_telefone(telefone):
        raise ValueError("forcar_reenvio exige telefone especifico")

    if supabase.configurado():
        return _executar_disparo_supabase(
            data_referencia=data_referencia,
            dias=dias or 15,
            telefone=telefone,
            somente_teste=somente_teste or modo_teste or forcar_reenvio,
            forcar_reenvio=forcar_reenvio,
        )

    logger.info("Disparo usando fonte: planilha local.")
    aniversariantes = planilha.obter_aniversariantes_do_dia(
        caminho_planilha=caminho_planilha,
        data_referencia=data_referencia,
    )
    resultados: list[ResultadoEnvio] = []

    logger.info("Disparo iniciado com %s aniversariantes.", len(aniversariantes))

    for contato in aniversariantes:
        telefone = contato["telefone"]
        nome = contato["nome"]

        try:
            if dados.ja_enviado(telefone, data_referencia):
                resultados.append(
                    {
                        "telefone": telefone,
                        "nome": nome,
                        "status": "pulado",
                        "detalhe": "Contato ja recebeu mensagem hoje.",
                    }
                )
                continue

            cliente = {
                "nome": nome,
                "telefone": telefone,
                "aniversario_ddmm": contato.get("aniversario", ""),
            }
            cliente_banco = clientes_supabase.buscar_cliente_por_telefone(telefone)
            if cliente_banco:
                cliente = {
                    **cliente,
                    **cliente_banco,
                    "nome": cliente_banco.get("nome") or nome,
                    "telefone": cliente_banco.get("telefone") or telefone,
                }
            mensagem_personalizada = mensagens.gerar_mensagem_aniversario(cliente)
            mensagem = mensagem_personalizada["texto"]
            enviado = whatsapp.enviar(telefone, mensagem)
            if not enviado:
                resultados.append(
                    {
                        "telefone": telefone,
                        "nome": nome,
                        "status": "erro",
                        "detalhe": "Falha no envio pelo WhatsApp.",
                    }
                )
                continue

            dados.marcar_enviado(telefone, nome, data_referencia)
            conversa_banco = fluxo_reservas.iniciar_conversa(
                {
                    **cliente,
                    "perfil_mensagem": mensagem_personalizada["perfil"],
                    "idade": mensagem_personalizada["idade"],
                },
                origem="aniversario",
                mensagem_inicial=mensagem,
            )
            import time as _time
            _time.sleep(2)  # deixa o histórico carregar
            texto_baseline, _ = whatsapp.ler_ultima_mensagem(
                nome_bot=os.getenv("NOME_BOT_WHATSAPP")
            )
            adicionar_conversa_ativa(
                telefone=telefone,
                nome=str(cliente.get("nome") or nome),
                ultima_mensagem_cliente=texto_baseline if texto_baseline else "__INIT__",
                conversa_id=conversa_banco.get("id", ""),
                perfil_id=mensagem_personalizada.get("perfil_id", "") or str(cliente.get("perfil_id") or ""),
                perfil_nome=mensagem_personalizada.get("perfil_nome", "") or str(cliente.get("perfil_nome") or ""),
            )

            resultados.append(
                {
                    "telefone": telefone,
                    "nome": nome,
                    "status": "enviado",
                    "detalhe": "Mensagem enviada e conversa ativada.",
                }
            )
            _aguardar_intervalo_envio()
        except Exception:
            logger.exception("Erro ao processar contato %s.", telefone)
            resultados.append(
                {
                    "telefone": telefone,
                    "nome": nome,
                    "status": "erro",
                    "detalhe": "Erro inesperado no disparador.",
                }
            )

    logger.info("Disparo finalizado.")
    return resultados


def _executar_disparo_supabase(
    *,
    data_referencia: date | None = None,
    dias: int = 15,
    telefone: str = "",
    somente_teste: bool = False,
    forcar_reenvio: bool = False,
) -> list[ResultadoEnvio]:
    data_ref = data_referencia or date.today()
    dias_final = max(1, min(int(dias or 15), 60))
    telefone_filtro = normalizar_telefone(telefone)
    if forcar_reenvio and not telefone_filtro:
        raise ValueError("forcar_reenvio exige telefone especifico")
    logger.info(
        "Disparo solicitado: telefone=%s modo_teste=%s forcar_reenvio=%s.",
        telefone_filtro or "",
        somente_teste,
        forcar_reenvio,
    )
    resultado_aniversarios = clientes_supabase.listar_aniversarios_proximos(
        dias=dias_final,
        limite_clientes=2000,
        hoje=data_ref,
    )
    clientes = resultado_aniversarios["clientes"]
    logger.info("Disparo usando fonte: supabase.")
    logger.info("Clientes encontrados no Supabase: %s", resultado_aniversarios.get("analisados", 0))
    logger.info("Aniversariantes no periodo: %s", resultado_aniversarios["total"])

    if telefone_filtro:
        clientes = [
            cliente for cliente in clientes
            if normalizar_telefone(str(cliente.get("telefone") or "")) == telefone_filtro
        ]
        logger.info("Disparo filtrado para telefone de teste: encontrados=%s.", len(clientes))

    resultados: list[ResultadoEnvio] = []
    if not clientes:
        logger.info("Disparo finalizado: enviados=0 falhas=0.")
        return resultados

    for cliente in clientes:
        telefone_cliente = normalizar_telefone(str(cliente.get("telefone") or ""))
        nome = str(cliente.get("nome") or "Cliente").strip() or "Cliente"
        if not telefone_cliente:
            resultados.append(
                {
                    "telefone": str(cliente.get("telefone") or ""),
                    "nome": nome,
                    "status": "erro",
                    "detalhe": "Telefone invalido.",
                    "enviado": False,
                }
            )
            continue

        try:
            ja_enviado_hoje = _disparo_ja_registrado_supabase(telefone_cliente, data_ref)
            if ja_enviado_hoje and forcar_reenvio:
                logger.info("Ignorando bloqueio de envio diario para teste: telefone=%s.", telefone_cliente)
            if ja_enviado_hoje and not forcar_reenvio:
                logger.info("Ja enviado hoje: telefone=%s.", telefone_cliente)
                resultados.append(
                    {
                        "telefone": telefone_cliente,
                        "nome": nome,
                        "status": "pulado",
                        "detalhe": "Contato ja recebeu mensagem hoje.",
                        "enviado": False,
                        "pulado": True,
                    }
                )
                continue

            mensagem_personalizada = mensagens.gerar_mensagem_aniversario(cliente)
            texto = mensagem_personalizada["texto"]
            logger.info("Enviando mensagem de aniversario para %s.", telefone_cliente)
            envio = whatsapp.enviar_com_resultado(telefone_cliente, texto)
            logger.info(
                "Resposta WhatsApp para %s: ok=%s provider=%s erro=%s",
                telefone_cliente,
                envio.get("ok"),
                envio.get("provider"),
                envio.get("erro", ""),
            )
            if not envio.get("ok"):
                _registrar_disparo_supabase(
                    cliente=cliente,
                    telefone=telefone_cliente,
                    data_referencia=data_ref,
                    mensagem=texto,
                    status="falha",
                    envio=envio,
                    modo_teste=somente_teste or forcar_reenvio,
                )
                resultados.append(
                    {
                        "telefone": telefone_cliente,
                        "nome": nome,
                        "status": "erro",
                        "detalhe": str(envio.get("erro") or "Falha no envio pelo WhatsApp."),
                        "enviado": False,
                    }
                )
                continue

            _registrar_disparo_supabase(
                cliente=cliente,
                telefone=telefone_cliente,
                data_referencia=data_ref,
                mensagem=texto,
                status="enviado",
                envio=envio,
                modo_teste=somente_teste or forcar_reenvio,
            )
            conversa_banco = fluxo_reservas.iniciar_conversa(
                {
                    **cliente,
                    "telefone": telefone_cliente,
                    "nome": nome,
                    "perfil_mensagem": mensagem_personalizada.get("perfil", ""),
                    "idade": mensagem_personalizada.get("idade"),
                },
                origem="aniversario",
                mensagem_inicial=texto,
            )
            adicionar_conversa_ativa(
                telefone=telefone_cliente,
                nome=nome,
                ultima_mensagem_cliente="__INIT__",
                conversa_id=str(conversa_banco.get("id", "")),
                perfil_id=mensagem_personalizada.get("perfil_id", "") or str(cliente.get("perfil_id") or ""),
                perfil_nome=mensagem_personalizada.get("perfil_nome", "") or str(cliente.get("perfil_nome") or ""),
            )
            resultados.append(
                {
                    "telefone": telefone_cliente,
                    "nome": nome,
                    "status": "reenviado_teste" if forcar_reenvio else "enviado",
                    "detalhe": "Mensagem reenviada em modo de teste." if forcar_reenvio else "Mensagem enviada e conversa ativada.",
                    "enviado": True,
                    "reenviado_teste": bool(forcar_reenvio),
                    "provider_message_id": str(envio.get("provider_message_id") or ""),
                }
            )
            if not somente_teste:
                _aguardar_intervalo_envio()
        except Exception:
            logger.exception("Erro ao processar contato %s.", telefone_cliente)
            resultados.append(
                {
                    "telefone": telefone_cliente,
                    "nome": nome,
                    "status": "erro",
                    "detalhe": "Erro inesperado no disparador.",
                    "enviado": False,
                }
            )

    enviados = sum(1 for item in resultados if item.get("status") in {"enviado", "reenviado_teste"})
    falhas = sum(1 for item in resultados if item.get("status") == "erro")
    logger.info("Disparo finalizado: enviados=%s falhas=%s.", enviados, falhas)
    return resultados


def _disparo_ja_registrado_supabase(
    telefone: str,
    data_referencia: date,
    *,
    tipo_disparo: str = "aniversario",
) -> bool:
    tabela = supabase.tabela_env("SUPABASE_DISPAROS_TABLE", DEFAULT_DISPAROS_TABLE)
    resultado = supabase.selecionar(
        tabela,
        colunas="id,status",
        filtros={
            "telefone": f"eq.{telefone}",
            "tipo_disparo": f"eq.{tipo_disparo}",
            "data_referencia": f"eq.{data_referencia.isoformat()}",
            "modo_teste": "eq.false",
        },
        limite=1,
    )
    if not resultado.get("ok"):
        logger.warning("Nao foi possivel verificar controle de disparo no Supabase: %s", resultado.get("erro"))
        return False
    dados_resultado = resultado.get("data")
    return isinstance(dados_resultado, list) and bool(dados_resultado)


def _registrar_disparo_supabase(
    *,
    cliente: dict[str, Any],
    telefone: str,
    data_referencia: date,
    mensagem: str,
    status: str,
    envio: dict[str, Any],
    modo_teste: bool,
    tipo_disparo: str = "aniversario",
) -> None:
    tabela = supabase.tabela_env("SUPABASE_DISPAROS_TABLE", DEFAULT_DISPAROS_TABLE)
    payload = {
        "cliente_id": cliente.get("id") or None,
        "telefone": telefone,
        "tipo_disparo": tipo_disparo,
        "data_referencia": data_referencia.isoformat(),
        "mensagem": mensagem,
        "status": status,
        "provider": envio.get("provider"),
        "provider_message_id": envio.get("provider_message_id") or None,
        "erro": envio.get("erro") or envio.get("detalhe") or None,
        "modo_teste": bool(modo_teste),
        "metadata": {
            "cliente_nome": cliente.get("nome", ""),
            "perfil_id": cliente.get("perfil_id"),
            "perfil_nome": cliente.get("perfil_nome"),
            "dias_ate_aniversario": cliente.get("dias_ate_aniversario"),
        },
    }
    resultado = supabase.inserir(tabela, payload, retornar=False)
    if resultado.get("ok"):
        logger.info("Disparo registrado em %s para telefone=%s modo_teste=%s.", tabela, telefone, modo_teste)
    else:
        logger.warning(
            "Nao foi possivel registrar disparo em %s para telefone=%s: %s %s",
            tabela,
            telefone,
            resultado.get("erro", ""),
            resultado.get("detalhe", ""),
        )


def monitorar_respostas(parar_evento: threading.Event | None = None) -> None:
    logger.info("Monitor de respostas iniciado.")

    while parar_evento is None or not parar_evento.is_set():
        conversas = listar_conversas_ativas()

        # Log periódico — confirma que o dict não está vazio
        if conversas:
            logger.debug("[monitor] conversas ativas: %s", list(conversas.keys()))
        else:
            logger.debug("[monitor] nenhuma conversa ativa no momento.")

        for telefone, conversa in conversas.items():
            try:
                _monitorar_conversa(telefone, conversa)
            except Exception:
                logger.exception("Erro ao monitorar conversa %s.", telefone)

        _aguardar_monitoramento(parar_evento)

def adicionar_conversa_ativa(
    telefone: str,
    nome: str,
    ultima_mensagem_cliente: str | None = None,
    conversa_id: str = "",
    perfil_id: str = "",
    perfil_nome: str = "",
) -> None:
    telefone_limpo = telefone.strip()
    if not telefone_limpo:
        return

    conversa: ConversaAtiva = {
        "telefone": telefone_limpo,
        "nome": nome.strip(),
        "iniciada_em": datetime.now().isoformat(timespec="seconds"),
    }
    if conversa_id:
        conversa["conversa_id"] = conversa_id
    if perfil_id:
        conversa["perfil_id"] = perfil_id
    if perfil_nome:
        conversa["perfil_nome"] = perfil_nome

    if ultima_mensagem_cliente:
        conversa["ultima_mensagem_cliente"] = ultima_mensagem_cliente

    with _lock_conversas:
        _conversas_ativas[telefone_limpo] = conversa


def remover_conversa_ativa(telefone: str) -> None:
    with _lock_conversas:
        _conversas_ativas.pop(telefone.strip(), None)


def listar_conversas_ativas() -> dict[str, ConversaAtiva]:
    with _lock_conversas:
        return {telefone: dict(conversa) for telefone, conversa in _conversas_ativas.items()}


def resetar_enviados_do_dia() -> bool:
    logger.info("Resetando lista de enviados.")
    return dados.limpar_enviados()


def _monitorar_conversa(telefone: str, conversa: ConversaAtiva) -> None:
    ultima_salva = conversa.get("ultima_mensagem_cliente")
    remetente_conhecido = conversa.get("remetente_whatsapp")
    nome_bot = os.getenv("NOME_BOT_WHATSAPP")  # adiciona no .env

    logger.debug("[monitor] %s | ultima_salva=%r | remetente=%r", telefone, ultima_salva, remetente_conhecido)

    abriu = whatsapp.enviar(telefone, "")
    if not abriu:
        logger.warning("Nao foi possivel abrir conversa %s para leitura.", telefone)
        return

    mensagem_cliente, remetente = whatsapp.ler_ultima_mensagem(
        remetente_conhecido=remetente_conhecido,
        nome_bot=nome_bot,
    )

    logger.debug("[monitor] %s | msg_lida=%r | remetente=%r", telefone, mensagem_cliente, remetente)

    if not mensagem_cliente:
        return

    if mensagem_cliente == ultima_salva:
        logger.debug("[monitor] %s | sem novidade, pulando.", telefone)
        return

    # Salva o remetente real na primeira detecção
    if remetente and not remetente_conhecido:
        with _lock_conversas:
            if telefone in _conversas_ativas:
                _conversas_ativas[telefone]["remetente_whatsapp"] = remetente
        logger.info("[monitor] %s | remetente WhatsApp identificado: %r", telefone, remetente)

    logger.info("[monitor] %s | nova mensagem: %r", telefone, mensagem_cliente)
    _atualizar_ultima_mensagem(telefone, mensagem_cliente)
    conversa_banco = _conversa_banco(conversa)
    fluxo_reservas.registrar_mensagem(
        conversa_banco,
        remetente="cliente",
        conteudo=mensagem_cliente,
        metadata={"remetente_whatsapp": remetente},
    )

    resposta = agente.processar_mensagem(
        telefone=telefone,
        mensagem_cliente=mensagem_cliente,
        nome_cliente=conversa.get("nome", ""),
        perfil_cliente=_perfil_conversa(conversa),
    )

    texto = resposta["texto"]
    if texto:
        enviado = whatsapp.enviar(telefone, texto)
        fluxo_reservas.registrar_mensagem(
            conversa_banco,
            remetente="bot",
            conteudo=texto,
            metadata={
                "envio_ok": enviado,
                "status_reserva": resposta.get("status_reserva", ""),
                "confianca": resposta.get("confianca", 0),
            },
        )

    if resposta["reserva_confirmada"]:
        _registrar_reserva_confirmada(telefone, conversa, resposta["dados_reserva"])
        agente.limpar_historico(telefone)
        remover_conversa_ativa(telefone)
        return

    if resposta.get("status_reserva") == "sem_interesse":
        fluxo_reservas.finalizar_conversa(_conversa_banco(conversa), status="finalizada")
        agente.limpar_historico(telefone)
        remover_conversa_ativa(telefone)
        logger.info("Conversa finalizada por recusa ao convite de reserva. telefone=%s", telefone)
        return


def _registrar_reserva_confirmada(
    telefone: str,
    conversa: ConversaAtiva,
    dados_reserva: agente.DadosReserva,
) -> None:
    cliente = {
        "telefone": telefone,
        "nome": conversa.get("nome", ""),
    }
    if fluxo_reservas.registrar_reserva_confirmada(
        cliente=cliente,
        conversa=_conversa_banco(conversa),
        dados_reserva=dados_reserva,
    ):
        fluxo_reservas.finalizar_conversa(_conversa_banco(conversa))
        logger.info("Reserva confirmada registrada para %s.", telefone)
    else:
        logger.warning("Reserva confirmada nao foi salva para %s.", telefone)


def _atualizar_ultima_mensagem(telefone: str, mensagem: str) -> None:
    with _lock_conversas:
        conversa = _conversas_ativas.get(telefone)
        if conversa is not None:
            conversa["ultima_mensagem_cliente"] = mensagem


def _montar_mensagem_aniversario(nome: str) -> str:
    return mensagens.gerar_mensagem_aniversario({"nome": nome})["texto"]


def _conversa_banco(conversa: ConversaAtiva) -> dict[str, str]:
    return {
        "id": conversa.get("conversa_id", ""),
        "cliente_telefone": conversa.get("telefone", ""),
    }


def _perfil_conversa(conversa: ConversaAtiva) -> dict[str, str] | None:
    perfil_base = {
        "id": conversa.get("perfil_id", ""),
        "nome": conversa.get("perfil_nome", ""),
    }
    try:
        perfil = perfis.resolver_perfil_cliente({"perfil_id": perfil_base["id"], "perfil_nome": perfil_base["nome"]})
    except Exception:
        logger.exception("Falha ao resolver perfil da conversa %s.", conversa.get("telefone", ""))
        return perfil_base if perfil_base["id"] or perfil_base["nome"] else None
    if perfil:
        return dict(perfil)
    return perfil_base if perfil_base["id"] or perfil_base["nome"] else None


def _aguardar_intervalo_envio() -> None:
    minimo = _int_env("INTERVALO_ENVIO_MIN_SEGUNDOS", 3)
    maximo = _int_env("INTERVALO_ENVIO_MAX_SEGUNDOS", 5)
    if maximo < minimo:
        maximo = minimo

    time.sleep(random.uniform(minimo, maximo))


def _aguardar_monitoramento(parar_evento: threading.Event | None) -> None:
    intervalo = max(_int_env("INTERVALO_MONITORAMENTO_SEGUNDOS", 10), 1)
    if parar_evento is None:
        time.sleep(intervalo)
    else:
        parar_evento.wait(intervalo)


def _int_env(nome: str, padrao: int) -> int:
    try:
        return int(os.getenv(nome, str(padrao)))
    except ValueError:
        return padrao
