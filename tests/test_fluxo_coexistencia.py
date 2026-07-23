from __future__ import annotations

import json
import os
import unittest
from datetime import date
from unittest.mock import patch

from services import fluxo_reservas


def json_payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


class FluxoCoexistenciaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resposta_agente = {
            "texto": "Claro, vou ajudar com a reserva.",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "em_coleta",
            "confianca": 0.8,
        }
        fluxo_reservas.agente._historicos.clear()
        fluxo_reservas.agente._estados_reserva.clear()
        fluxo_reservas._debounce_lotes.clear()
        fluxo_reservas._provider_ids_pendentes.clear()

    def tearDown(self) -> None:
        fluxo_reservas.agente._historicos.clear()
        fluxo_reservas.agente._estados_reserva.clear()
        fluxo_reservas._debounce_lotes.clear()
        fluxo_reservas._provider_ids_pendentes.clear()

    @patch.object(fluxo_reservas, "_mensagem_ja_processada", return_value=False)
    @patch.object(fluxo_reservas, "registrar_mensagem")
    @patch.object(fluxo_reservas, "iniciar_conversa")
    @patch.object(fluxo_reservas, "buscar_conversa_por_telefone", return_value=None)
    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone", return_value=None)
    @patch.object(fluxo_reservas.clientes_supabase, "buscar_cliente_por_telefone")
    @patch.object(fluxo_reservas.agente, "processar_mensagem")
    @patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado")
    def test_mensagem_fora_de_fluxo_nao_chama_groq_nem_responde(
        self,
        enviar,
        processar,
        buscar_cliente,
        buscar_ativa,
        buscar_qualquer,
        iniciar,
        registrar,
        _ja_processada,
    ) -> None:
        buscar_cliente.return_value = {"id": "cliente-1", "telefone": "5511999999999", "nome": "Cliente"}
        iniciar.return_value = {
            "id": "conv-1",
            "cliente_telefone": "5511999999999",
            "status": "aguardando_humano",
        }

        resposta = fluxo_reservas.processar_resposta_cliente(
            telefone="5511999999999",
            mensagem_cliente="Oi",
            provider_message_id="wamid.1",
        )

        self.assertEqual(resposta["status_reserva"], "aguardando_humano")
        iniciar.assert_called_once()
        self.assertEqual(iniciar.call_args.kwargs["status"], "aguardando_humano")
        registrar.assert_called_once()
        processar.assert_not_called()
        enviar.assert_not_called()

    @patch.object(fluxo_reservas, "_mensagem_ja_processada", return_value=False)
    @patch.object(fluxo_reservas, "registrar_mensagem")
    @patch.object(fluxo_reservas, "atualizar_status_conversa")
    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone")
    @patch.object(fluxo_reservas.clientes_supabase, "buscar_cliente_por_telefone")
    @patch.object(fluxo_reservas.agente, "processar_mensagem")
    @patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado")
    def test_conversa_bot_ativa_responde(
        self,
        enviar,
        processar,
        buscar_cliente,
        buscar_ativa,
        atualizar,
        registrar,
        _ja_processada,
    ) -> None:
        buscar_cliente.return_value = {"id": "cliente-1", "telefone": "5511999999999", "nome": "Cliente"}
        buscar_ativa.return_value = {"id": "conv-1", "cliente_telefone": "5511999999999", "status": "bot_ativo"}
        processar.return_value = self.resposta_agente
        enviar.return_value = {"ok": True, "provider_message_id": "wamid.bot"}

        resposta = fluxo_reservas.processar_resposta_cliente(
            telefone="5511999999999",
            mensagem_cliente="Quero reservar",
            provider_message_id="wamid.2",
        )

        self.assertEqual(resposta["texto"], self.resposta_agente["texto"])
        processar.assert_called_once()
        enviar.assert_called_once()
        atualizar.assert_called_with(buscar_ativa.return_value, status="bot_ativo")

    @patch.object(fluxo_reservas, "_mensagem_ja_processada", return_value=False)
    @patch.object(fluxo_reservas, "registrar_mensagem")
    @patch.object(fluxo_reservas, "atualizar_status_conversa")
    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone")
    @patch.object(fluxo_reservas.clientes_supabase, "buscar_cliente_por_telefone")
    @patch.object(fluxo_reservas.agente, "processar_mensagem")
    @patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado")
    def test_pedido_de_humano_pausa_sem_chamar_groq(
        self,
        enviar,
        processar,
        buscar_cliente,
        buscar_ativa,
        atualizar,
        registrar,
        _ja_processada,
    ) -> None:
        conversa = {"id": "conv-1", "cliente_telefone": "5511999999999", "status": "bot_ativo"}
        buscar_cliente.return_value = {"id": "cliente-1", "telefone": "5511999999999", "nome": "Cliente"}
        buscar_ativa.return_value = conversa

        resposta = fluxo_reservas.processar_resposta_cliente(
            telefone="5511999999999",
            mensagem_cliente="Quero falar com alguém",
            provider_message_id="wamid.3",
        )

        self.assertEqual(resposta["status_reserva"], "humano")
        atualizar.assert_called_once_with(conversa, status="humano")
        processar.assert_not_called()
        enviar.assert_not_called()
        registrar.assert_called_once()

    @patch.object(fluxo_reservas, "_mensagem_ja_processada", return_value=False)
    @patch.object(fluxo_reservas, "registrar_mensagem")
    @patch.object(fluxo_reservas, "atualizar_status_conversa")
    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone")
    @patch.object(fluxo_reservas.clientes_supabase, "buscar_cliente_por_telefone")
    @patch.object(fluxo_reservas.agente, "processar_mensagem")
    @patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado")
    def test_pare_agora_interrompe_sem_chamar_ia(
        self,
        enviar,
        processar,
        buscar_cliente,
        buscar_ativa,
        atualizar,
        registrar,
        _ja_processada,
    ) -> None:
        conversa = {"id": "conv-1", "cliente_telefone": "5511999999999", "status": "bot_ativo"}
        buscar_cliente.return_value = {"id": "cliente-1", "telefone": "5511999999999", "nome": "Cliente"}
        buscar_ativa.return_value = conversa

        resposta = fluxo_reservas.processar_resposta_cliente(
            telefone="5511999999999",
            mensagem_cliente="pare agora",
            provider_message_id="wamid.pare",
        )

        self.assertEqual(resposta["status_reserva"], "humano")
        atualizar.assert_called_once_with(conversa, status="humano")
        processar.assert_not_called()
        enviar.assert_not_called()
        registrar.assert_called_once()

    @patch.object(fluxo_reservas, "_mensagem_ja_processada", return_value=False)
    @patch.object(fluxo_reservas, "registrar_reserva_confirmada")
    @patch.object(fluxo_reservas, "registrar_mensagem")
    @patch.object(fluxo_reservas, "finalizar_conversa")
    @patch.object(fluxo_reservas, "atualizar_status_conversa")
    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone")
    @patch.object(fluxo_reservas.clientes_supabase, "buscar_cliente_por_telefone")
    @patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado")
    def test_recusa_convite_finaliza_conversa_sem_chamar_groq(
        self,
        enviar,
        buscar_cliente,
        buscar_ativa,
        atualizar,
        finalizar,
        registrar,
        registrar_reserva,
        _ja_processada,
    ) -> None:
        conversa = {"id": "conv-1", "cliente_telefone": "5511999999999", "status": "bot_ativo"}
        buscar_cliente.return_value = {"id": "cliente-1", "telefone": "5511999999999", "nome": "Cliente"}
        buscar_ativa.return_value = conversa
        enviar.return_value = {"ok": True, "provider_message_id": "wamid.bot"}

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(fluxo_reservas.agente, "_chamar_groq") as chamar_groq:
            resposta = fluxo_reservas.processar_resposta_cliente(
                telefone="5511999999999",
                mensagem_cliente="Não",
                provider_message_id="wamid.4",
            )

        self.assertEqual(resposta["status_reserva"], "sem_interesse")
        self.assertEqual(resposta["texto"], fluxo_reservas.agente.MENSAGEM_RECUSA_RESERVA)
        enviar.assert_called_once_with("5511999999999", fluxo_reservas.agente.MENSAGEM_RECUSA_RESERVA)
        finalizar.assert_called_once_with(conversa, status="finalizada")
        registrar_reserva.assert_not_called()
        chamar_groq.assert_not_called()
        self.assertEqual(registrar.call_count, 2)
        atualizar.assert_called_once_with(conversa, status="bot_ativo")

    @patch.object(fluxo_reservas, "_mensagem_ja_processada", return_value=False)
    @patch.object(fluxo_reservas, "registrar_mensagem")
    @patch.object(fluxo_reservas, "atualizar_status_conversa")
    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone")
    @patch.object(fluxo_reservas.clientes_supabase, "buscar_cliente_por_telefone")
    @patch.object(fluxo_reservas.agente, "processar_mensagem")
    @patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado")
    def test_agente_pode_pausar_conversa_para_humano(
        self,
        enviar,
        processar,
        buscar_cliente,
        buscar_ativa,
        atualizar,
        registrar,
        _ja_processada,
    ) -> None:
        conversa = {"id": "conv-1", "cliente_telefone": "5511999999999", "status": "bot_ativo"}
        buscar_cliente.return_value = {"id": "cliente-1", "telefone": "5511999999999", "nome": "Cliente"}
        buscar_ativa.return_value = conversa
        processar.return_value = {
            "texto": "Entendi, você quer para agora. Como é em cima da hora, preciso que a equipe confirme.",
            "reserva_confirmada": False,
            "dados_reserva": {"data_reserva": "2026-07-22"},
            "status_reserva": "aguardando_humano",
            "confianca": 1.0,
        }
        enviar.return_value = {"ok": True, "provider_message_id": "wamid.bot"}

        resposta = fluxo_reservas.processar_resposta_cliente(
            telefone="5511999999999",
            mensagem_cliente="hoje agora",
            provider_message_id="wamid.6",
        )

        self.assertEqual(resposta["status_reserva"], "aguardando_humano")
        enviar.assert_called_once()
        self.assertEqual(atualizar.call_args_list[-1].kwargs["status"], "aguardando_humano")
        self.assertEqual(registrar.call_count, 2)

    @patch.object(fluxo_reservas, "_mensagem_ja_processada", return_value=False)
    @patch.object(fluxo_reservas, "registrar_mensagem")
    @patch.object(fluxo_reservas, "atualizar_status_conversa")
    @patch.object(fluxo_reservas, "iniciar_conversa")
    @patch.object(fluxo_reservas, "buscar_conversa_por_telefone")
    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone", return_value=None)
    @patch.object(fluxo_reservas.clientes_supabase, "buscar_cliente_por_telefone")
    @patch.object(fluxo_reservas.agente, "processar_mensagem")
    @patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado")
    def test_interesse_posterior_reabre_conversa_finalizada(
        self,
        enviar,
        processar,
        buscar_cliente,
        _buscar_ativa,
        buscar_qualquer,
        iniciar,
        atualizar,
        registrar,
        _ja_processada,
    ) -> None:
        conversa_finalizada = {"id": "conv-antiga", "cliente_telefone": "5511999999999", "status": "finalizada"}
        conversa_nova = {"id": "conv-nova", "cliente_telefone": "5511999999999", "status": "bot_ativo"}
        buscar_cliente.return_value = {"id": "cliente-1", "telefone": "5511999999999", "nome": "Cliente"}
        buscar_qualquer.return_value = conversa_finalizada
        iniciar.return_value = conversa_nova
        processar.return_value = self.resposta_agente
        enviar.return_value = {"ok": True, "provider_message_id": "wamid.bot"}

        resposta = fluxo_reservas.processar_resposta_cliente(
            telefone="5511999999999",
            mensagem_cliente="Mudei de ideia, quero reservar",
            provider_message_id="wamid.5",
        )

        self.assertEqual(resposta["texto"], self.resposta_agente["texto"])
        iniciar.assert_called_once()
        self.assertEqual(iniciar.call_args.kwargs["status"], "bot_ativo")
        processar.assert_called_once()
        enviar.assert_called_once()
        atualizar.assert_called_once_with(conversa_nova, status="bot_ativo")
        self.assertEqual(registrar.call_count, 2)

    @patch.object(fluxo_reservas, "atualizar_status_conversa")
    @patch.object(fluxo_reservas, "buscar_conversa_por_telefone")
    @patch.object(fluxo_reservas.clientes_supabase, "buscar_cliente_por_telefone", return_value=None)
    def test_atualizacao_manual_muda_status_da_conversa(self, _buscar_cliente, buscar, atualizar) -> None:
        buscar.return_value = {"id": "conv-1", "cliente_telefone": "5511999999999", "status": "bot_ativo"}

        resultado = fluxo_reservas.definir_status_conversa_por_telefone(
            telefone="5511999999999",
            status="humano",
        )

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["status"], "humano")
        atualizar.assert_called_once_with(buscar.return_value, status="humano")

    @patch.object(fluxo_reservas, "_mensagem_ja_processada", return_value=False)
    @patch.object(fluxo_reservas, "registrar_mensagem")
    @patch.object(fluxo_reservas, "atualizar_status_conversa")
    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone")
    @patch.object(fluxo_reservas.clientes_supabase, "buscar_cliente_por_telefone")
    @patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado")
    @patch.object(fluxo_reservas.supabase, "atualizar")
    def test_estado_reserva_e_carregado_e_salvo_no_metadata_da_conversa(
        self,
        atualizar_supabase,
        enviar,
        buscar_cliente,
        buscar_ativa,
        atualizar_status,
        registrar,
        _ja_processada,
    ) -> None:
        conversa = {
            "id": "conv-estado",
            "cliente_telefone": "5511999999999",
            "status": "bot_ativo",
            "metadata": {
                "estado_reserva": {
                    "data_reserva": "2026-07-29",
                    "pessoas": 4,
                    "nome_cliente": "Rodrigo Teste",
                    "campo_pendente": "horario",
                    "etapa": "aguardando_horario",
                }
            },
        }
        payload = {
            "resposta_natural": "Perfeito, às 20h.",
            "intencao": "fornecimento_dados",
            "dados_extraidos": {"data": None, "horario": "20:00", "quantidade": None},
            "acao": "continuar_conversa",
            "proximo_campo": "confirmacao",
            "confianca": 0.9,
        }
        buscar_cliente.return_value = {"id": "cliente-1", "telefone": "5511999999999", "nome": "Rodrigo Teste"}
        buscar_ativa.return_value = conversa
        enviar.return_value = {"ok": True, "provider_message_id": "wamid.bot"}
        atualizar_supabase.return_value = {"ok": True}

        with (
            patch.object(fluxo_reservas.agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}),
            patch.object(fluxo_reservas.agente, "_chamar_groq", return_value=json_payload(payload)),
        ):
            resposta = fluxo_reservas.processar_resposta_cliente(
                telefone="5511999999999",
                mensagem_cliente="20h",
                provider_message_id="wamid.estado",
            )

        self.assertEqual(resposta["status_reserva"], "aguardando_confirmacao")
        payload_metadata = atualizar_supabase.call_args_list[-1].args[1]
        estado_salvo = payload_metadata["metadata"]["estado_reserva"]
        self.assertEqual(estado_salvo["data_reserva"], "2026-07-29")
        self.assertEqual(estado_salvo["horario"], "20:00")
        self.assertEqual(estado_salvo["pessoas"], 4)
        self.assertEqual(estado_salvo["campo_pendente"], "confirmacao")
        enviar.assert_called_once()
        self.assertEqual(registrar.call_count, 2)

    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone", return_value={"id": "conv-1", "status": "bot_ativo"})
    @patch.object(fluxo_reservas, "processar_resposta_cliente")
    def test_mensagens_rapidas_no_mesmo_payload_viram_uma_chamada(self, processar, _buscar_ativa) -> None:
        processar.return_value = {
            "texto": "Claro, qual dia você prefere?",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "em_coleta",
            "confianca": 0.9,
        }

        resultados = fluxo_reservas.processar_mensagens_webhook(
            [
                {
                    "telefone": "5511999999999",
                    "texto": "Olá",
                    "remetente": "Rodrigo",
                    "timestamp": "2026-07-22T20:00:00+00:00",
                    "provider_message_id": "wamid.ola",
                },
                {
                    "telefone": "5511999999999",
                    "texto": "Quero sim",
                    "remetente": "Rodrigo",
                    "timestamp": "2026-07-22T20:00:01+00:00",
                    "provider_message_id": "wamid.quero",
                },
            ]
        )

        self.assertEqual(len(resultados), 1)
        processar.assert_called_once()
        chamada = processar.call_args.kwargs
        self.assertEqual(chamada["mensagem_cliente"], "Olá\nQuero sim")
        self.assertEqual(chamada["provider_message_id"], "wamid.ola")
        self.assertEqual(len(chamada["metadata_mensagem"]["mensagens_agrupadas"]), 2)

    @patch.object(fluxo_reservas, "_mensagem_ja_processada", return_value=False)
    @patch.object(fluxo_reservas, "registrar_mensagem")
    @patch.object(fluxo_reservas, "buscar_conversa_por_telefone")
    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone")
    @patch.object(fluxo_reservas, "processar_resposta_cliente")
    def test_debounce_em_webhooks_separados_gera_uma_chamada_ao_agente(
        self,
        processar,
        buscar_ativa,
        buscar_por_telefone,
        registrar,
        _ja_processada,
    ) -> None:
        class TimerFake:
            def __init__(self, intervalo, funcao, args=()):
                self.funcao = funcao
                self.args = args
                self.cancelado = False

            def start(self):
                return None

            def cancel(self):
                self.cancelado = True

        conversa = {"id": "conv-1", "status": "bot_ativo", "cliente_telefone": "5511999999999"}
        buscar_ativa.return_value = conversa
        buscar_por_telefone.return_value = conversa
        processar.return_value = {
            "texto": "Claro, qual dia voce prefere?",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "em_coleta",
            "confianca": 0.9,
        }

        with (
            patch.dict(os.environ, {"WHATSAPP_DEBOUNCE_SECONDS": "3"}),
            patch.object(fluxo_reservas.threading, "Timer", TimerFake),
        ):
            primeira = fluxo_reservas.processar_mensagem_webhook(
                {
                    "telefone": "5511999999999",
                    "texto": "Ola",
                    "remetente": "Rodrigo",
                    "timestamp": "2026-07-22T20:00:00+00:00",
                    "provider_message_id": "wamid.ola",
                }
            )
            segunda = fluxo_reservas.processar_mensagem_webhook(
                {
                    "telefone": "5511999999999",
                    "texto": "Quero sim",
                    "remetente": "Rodrigo",
                    "timestamp": "2026-07-22T20:00:02+00:00",
                    "provider_message_id": "wamid.quero",
                }
            )
            fluxo_reservas._processar_lote_debounce("5511999999999")

        self.assertEqual(primeira["status"], "debounce_pendente")
        self.assertEqual(segunda["status"], "debounce_pendente")
        self.assertEqual(registrar.call_count, 2)
        processar.assert_called_once()
        chamada = processar.call_args.kwargs
        self.assertEqual(chamada["mensagem_cliente"], "Ola\nQuero sim")
        self.assertEqual(chamada["provider_message_id"], "wamid.ola")
        self.assertEqual(len(chamada["metadata_mensagem"]["mensagens_agrupadas"]), 2)

    @patch.object(fluxo_reservas, "_mensagem_ja_processada", return_value=False)
    @patch.object(fluxo_reservas, "registrar_mensagem")
    @patch.object(fluxo_reservas, "buscar_conversa_por_telefone")
    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone")
    @patch.object(fluxo_reservas, "processar_resposta_cliente")
    def test_provider_message_id_duplicado_pendente_nao_gera_novo_processamento(
        self,
        processar,
        buscar_ativa,
        buscar_por_telefone,
        registrar,
        _ja_processada,
    ) -> None:
        class TimerFake:
            def __init__(self, intervalo, funcao, args=()):
                self.funcao = funcao
                self.args = args

            def start(self):
                return None

            def cancel(self):
                return None

        conversa = {"id": "conv-1", "status": "bot_ativo", "cliente_telefone": "5511999999999"}
        buscar_ativa.return_value = conversa
        buscar_por_telefone.return_value = conversa

        mensagem = {
            "telefone": "5511999999999",
            "texto": "Ola",
            "remetente": "Rodrigo",
            "timestamp": "2026-07-22T20:00:00+00:00",
            "provider_message_id": "wamid.duplicada",
        }
        with (
            patch.dict(os.environ, {"WHATSAPP_DEBOUNCE_SECONDS": "3"}),
            patch.object(fluxo_reservas.threading, "Timer", TimerFake),
        ):
            primeira = fluxo_reservas.processar_mensagem_webhook(mensagem)
            segunda = fluxo_reservas.processar_mensagem_webhook(mensagem)

        self.assertEqual(primeira["status"], "debounce_pendente")
        self.assertEqual(segunda["status"], "duplicada_pendente")
        self.assertEqual(registrar.call_count, 1)
        processar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
