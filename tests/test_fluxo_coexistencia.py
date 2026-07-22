from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services import fluxo_reservas


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

    def tearDown(self) -> None:
        fluxo_reservas.agente._historicos.clear()
        fluxo_reservas.agente._estados_reserva.clear()

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
            mensagem_cliente="Quero falar com atendente",
            provider_message_id="wamid.3",
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


if __name__ == "__main__":
    unittest.main()
