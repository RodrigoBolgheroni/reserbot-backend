from __future__ import annotations

import logging
import os
import random
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import TypedDict

from services import agente, clientes_supabase, dados, fluxo_reservas, mensagens, perfis, planilha, whatsapp


logger = logging.getLogger(__name__)


class ResultadoEnvio(TypedDict):
    telefone: str
    nome: str
    status: str
    detalhe: str


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
) -> list[ResultadoEnvio]:
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
