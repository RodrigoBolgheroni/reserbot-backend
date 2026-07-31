"""
Testes de Sanitização do Histórico para Modelos de IA e Preservação de Turnos
"""

import os
import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from services import agente, config_restaurante, fluxo_reservas, ia_fallback

TELEFONE = "5511888888888"


def _config_padrao() -> config_restaurante.ConfigRestaurante:
    base = config_restaurante._obter_config_env()
    return replace(
        base,
        nome="Restaurante Teste",
        estabelecimento_id="est-t",
        fonte="supabase",
        quantidade_minima_reserva=11,
        taxa_valor=50.0,
        taxa_convertida_consumacao=True,
        prazo_cancelamento_horas=24,
        pix_chave="00.000.000/0001-00",
        pix_titular="Restaurante Teste",
        exige_comprovante=True,
        espacos=(
            config_restaurante.EspacoRestaurante(
                id="salao",
                nome="Salão",
                descricao="Área interna",
                capacidade_maxima=25,
                permite_preferencia=True,
                regras="",
            ),
            config_restaurante.EspacoRestaurante(
                id="areia",
                nome="Areia",
                descricao="Área externa",
                capacidade_maxima=None,
                permite_preferencia=True,
                regras="",
            ),
        ),
    )


class SanitizacaoHistoricoModeloTest(unittest.TestCase):
    """Verifica regras de sanitização de mensagens para o modelo LLM e isolamento de metadados internos."""

    def setUp(self):
        agente._historicos.clear()
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._historicos.clear()
        agente._estados_reserva.clear()

    def test_historico_interno_contem_turn_id_e_conversa_id(self):
        agente._historicos[TELEFONE] = [
            {"role": "user", "content": "Quero reservar meu aniversário"},
            {
                "role": "assistant",
                "content": "Qual dia e horário?",
                "turn_id": "turn-101",
                "conversa_id": "conv-202",
                "provider_message_id_saida": "wamid.out1",
            },
        ]
        historico = agente._historicos[TELEFONE]
        self.assertEqual(historico[1]["turn_id"], "turn-101")
        self.assertEqual(historico[1]["conversa_id"], "conv-202")
        self.assertEqual(historico[1]["provider_message_id_saida"], "wamid.out1")

    def test_payload_enviado_ao_modelo_nao_contem_esses_campos(self):
        mensagens_internas = [
            {"role": "system", "content": "Prompt do sistema"},
            {"role": "user", "content": "Quero reservar meu aniversário"},
            {
                "role": "assistant",
                "content": "Qual dia e horário?",
                "turn_id": "turn-101",
                "conversa_id": "conv-202",
                "provider_message_id_saida": "wamid.out1",
            },
        ]
        sanitizado = ia_fallback.sanitizar_mensagens_para_modelo(mensagens_internas)
        self.assertEqual(len(sanitizado), 3)
        for msg in sanitizado:
            self.assertNotIn("turn_id", msg)
            self.assertNotIn("conversa_id", msg)
            self.assertNotIn("provider_message_id_saida", msg)

    def test_payload_contem_somente_role_e_content_no_fluxo_atual(self):
        mensagens_internas = [
            {"role": "system", "content": "Prompt do sistema"},
            {"role": "user", "content": "Quero reservar meu aniversário"},
            {
                "role": "assistant",
                "content": "Qual dia e horário?",
                "turn_id": "turn-101",
                "conversa_id": "conv-202",
            },
        ]
        sanitizado = ia_fallback.sanitizar_mensagens_para_modelo(mensagens_internas)
        for msg in sanitizado:
            self.assertEqual(set(msg.keys()), {"role", "content"})

    def test_modelo_principal_recebe_payload_sanitizado(self):
        mensagens_internas = [
            {"role": "user", "content": "Mensagem 1"},
            {
                "role": "assistant",
                "content": "Resposta 1",
                "turn_id": "turn-1",
                "conversa_id": "conv-1",
            },
        ]
        with patch.object(ia_fallback, "_executar_groq_modelo") as mock_exec, \
             patch.dict(os.environ, {"GROQ_API_KEY": "gkey"}):

            mock_exec.return_value = '{"resposta":"ok"}'
            ia_fallback.executar_ia_com_fallback(
                mensagens_internas,
                telefone=TELEFONE,
                modelo_preferido="llama-3.3-70b-versatile",
            )
            self.assertTrue(mock_exec.called)
            mensagens_recebidas = mock_exec.call_args[0][0]
            for msg in mensagens_recebidas:
                self.assertNotIn("turn_id", msg)
                self.assertNotIn("conversa_id", msg)
                self.assertEqual(set(msg.keys()), {"role", "content"})

    def test_fallback_recebe_payload_sanitizado(self):
        mensagens_internas = [
            {"role": "user", "content": "Mensagem 1"},
            {
                "role": "assistant",
                "content": "Resposta 1",
                "turn_id": "turn-1",
                "conversa_id": "conv-1",
            },
        ]
        with patch.object(ia_fallback, "_executar_groq_modelo", side_effect=Exception("Groq error")), \
             patch.object(ia_fallback, "_executar_fallback_provider") as mock_fb, \
             patch.dict(
                 os.environ,
                 {
                     "GROQ_API_KEY": "gkey",
                     "AI_FALLBACK_PROVIDER": "openai",
                     "AI_FALLBACK_API_KEY": "fkey",
                     "AI_FALLBACK_MODEL": "gpt-4o",
                 },
             ):

            mock_fb.return_value = '{"resposta":"ok"}'
            ia_fallback.executar_ia_com_fallback(
                mensagens_internas,
                telefone=TELEFONE,
                modelo_preferido="llama-3.3-70b-versatile",
            )
            self.assertTrue(mock_fb.called)
            kwargs_fb = mock_fb.call_args[1]
            mensagens_recebidas = kwargs_fb["mensagens"]
            for msg in mensagens_recebidas:
                self.assertNotIn("turn_id", msg)
                self.assertNotIn("conversa_id", msg)
                self.assertEqual(set(msg.keys()), {"role", "content"})

    def test_o_historico_interno_continua_guardando_turn_id(self):
        agente.registrar_resposta_assistente_historico(
            telefone=TELEFONE,
            conversa_id="conv-1",
            turn_id="turn-55",
            texto_final="Resposta de teste",
        )
        historico = agente._historicos.get(TELEFONE, [])
        self.assertEqual(len(historico), 1)
        self.assertEqual(historico[0]["turn_id"], "turn-55")
        self.assertEqual(historico[0]["conversa_id"], "conv-1")

    def test_duas_mensagens_proximas_permanecem_associadas_aos_turnos_corretos(self):
        cfg = _config_padrao()
        conversa = {"id": "conv-seq", "origem": "aniversario", "status": "bot_ativo"}
        resp_mock = {
            "texto": "Anotado.",
            "dados_reserva": {"data_reserva": "2026-08-06", "horario": "13:00", "pessoas": 15},
            "dados_confirmados": {"data_reserva": "2026-08-06", "horario": "13:00", "pessoas": 15},
            "status_reserva": "em_coleta",
            "confianca": 0.9,
        }

        with patch.object(config_restaurante, "obter_config", return_value=cfg), \
             patch.object(agente, "interpretar_resposta_modelo", return_value=resp_mock), \
             patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado", return_value={"ok": True, "provider_message_id": "out-seq"}), \
             patch.object(fluxo_reservas, "registrar_mensagem"), \
             patch.object(fluxo_reservas, "_salvar_estado_reserva_conversa"):

            fluxo_reservas.processar_resposta_cliente(
                telefone=TELEFONE,
                mensagem_cliente="06/08/2026 às 13h",
                conversa=conversa,
                provider_message_id="turn-req-1",
            )
            fluxo_reservas.processar_resposta_cliente(
                telefone=TELEFONE,
                mensagem_cliente="15 pessoas",
                conversa=conversa,
                provider_message_id="turn-req-2",
            )

            historico = agente._historicos.get(TELEFONE, [])
            assistants = [m for m in historico if m.get("role") == "assistant"]
            self.assertEqual(len(assistants), 2)
            self.assertEqual(assistants[0].get("turn_id"), "turn-req-1")
            self.assertEqual(assistants[1].get("turn_id"), "turn-req-2")

    def test_falha_400_nao_apaga_dados_ja_coletados(self):
        cfg = _config_padrao()
        est = {
            "origem_conversa": "aniversario",
            "data_reserva": "2026-08-06",
            "horario": "13:00",
            "pessoas": 15,
            "preferencia_espaco_id": "salao",
            "preferencia_espaco_nome": "Salão",
            "etapa": "aguardando_espaco",
        }
        agente.definir_estado_reserva(TELEFONE, est)
        conversa = {"id": "conv-400", "origem": "aniversario", "status": "bot_ativo", "metadata": {"estado_reserva": est}}

        with patch.object(config_restaurante, "obter_config", return_value=cfg), \
             patch.object(ia_fallback, "tem_provedor_configurado", return_value=True), \
             patch.object(ia_fallback, "executar_ia_com_fallback", return_value={"ok": False, "erro_codigo": "bad_request", "encaminhar_humano": False}), \
             patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado", return_value={"ok": True, "provider_message_id": "out-400"}), \
             patch.object(fluxo_reservas, "registrar_mensagem"), \
             patch.object(fluxo_reservas, "_salvar_estado_reserva_conversa"):

            res = fluxo_reservas.processar_resposta_cliente(
                telefone=TELEFONE,
                mensagem_cliente="Salão",
                conversa=conversa,
                provider_message_id="turn-400",
            )

            estado = agente.obter_estado_reserva(TELEFONE)
            self.assertEqual(estado.get("data_reserva"), "2026-08-06")
            self.assertEqual(estado.get("horario"), "13:00")
            self.assertEqual(estado.get("pessoas"), 15)
            self.assertEqual(estado.get("preferencia_espaco_id"), "salao")

    def test_conversa_nao_fica_pausada_por_payload_local_invalido(self):
        cfg = _config_padrao()
        est = {"origem_conversa": "aniversario", "data_reserva": "2026-08-06", "pessoas": 15}
        agente.definir_estado_reserva(TELEFONE, est)
        conversa = {"id": "conv-400-pause", "origem": "aniversario", "status": "bot_ativo", "metadata": {"estado_reserva": est}}

        with patch.object(config_restaurante, "obter_config", return_value=cfg), \
             patch.object(ia_fallback, "tem_provedor_configurado", return_value=True), \
             patch.object(ia_fallback, "executar_ia_com_fallback", return_value={"ok": False, "erro_codigo": "bad_request", "encaminhar_humano": False}), \
             patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado", return_value={"ok": True, "provider_message_id": "out-400-p"}), \
             patch.object(fluxo_reservas, "registrar_mensagem"), \
             patch.object(fluxo_reservas, "atualizar_status_conversa") as mock_status:

            res = fluxo_reservas.processar_resposta_cliente(
                telefone=TELEFONE,
                mensagem_cliente="Salão",
                conversa=conversa,
                provider_message_id="turn-400-p",
            )

            self.assertNotEqual(res.get("status_reserva"), "aguardando_humano")
            for call_item in mock_status.call_args_list:
                self.assertNotEqual(call_item.kwargs.get("status"), "aguardando_humano")

    def test_primeira_mensagem_funciona(self):
        cfg = _config_padrao()
        conversa = {"id": "conv-m1", "origem": "aniversario", "status": "bot_ativo"}
        resp_mock = {
            "texto": "Qual dia você quer reservar?",
            "dados_reserva": {},
            "dados_confirmados": {},
            "status_reserva": "em_coleta",
            "confianca": 0.9,
        }

        with patch.object(config_restaurante, "obter_config", return_value=cfg), \
             patch.object(agente, "interpretar_resposta_modelo", return_value=resp_mock), \
             patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado", return_value={"ok": True, "provider_message_id": "out-m1"}), \
             patch.object(fluxo_reservas, "registrar_mensagem"), \
             patch.object(fluxo_reservas, "_salvar_estado_reserva_conversa"):

            res = fluxo_reservas.processar_resposta_cliente(
                telefone=TELEFONE,
                mensagem_cliente="Quero fazer uma reserva de aniversário",
                conversa=conversa,
                provider_message_id="turn-m1",
            )
            self.assertEqual(res.get("status_reserva"), "em_coleta")
            self.assertIn("dia", res.get("texto", "").lower())

    def test_segunda_mensagem_funciona_com_historico(self):
        cfg = _config_padrao()
        conversa = {"id": "conv-m2", "origem": "aniversario", "status": "bot_ativo"}
        resp_mock = {
            "texto": "Quantas pessoas serão?",
            "dados_reserva": {"data_reserva": "2026-08-06", "horario": "13:00"},
            "dados_confirmados": {"data_reserva": "2026-08-06", "horario": "13:00"},
            "status_reserva": "em_coleta",
            "confianca": 0.9,
        }

        agente._historicos[TELEFONE] = [
            {"role": "user", "content": "Quero fazer uma reserva de aniversário"},
            {"role": "assistant", "content": "Qual dia você quer reservar?", "turn_id": "turn-m1", "conversa_id": "conv-m2"},
        ]

        with patch.object(config_restaurante, "obter_config", return_value=cfg), \
             patch.object(agente, "interpretar_resposta_modelo", return_value=resp_mock), \
             patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado", return_value={"ok": True, "provider_message_id": "out-m2"}), \
             patch.object(fluxo_reservas, "registrar_mensagem"), \
             patch.object(fluxo_reservas, "_salvar_estado_reserva_conversa"):

            res = fluxo_reservas.processar_resposta_cliente(
                telefone=TELEFONE,
                mensagem_cliente="06/08/2026 às 13h",
                conversa=conversa,
                provider_message_id="turn-m2",
            )
            self.assertEqual(res.get("status_reserva"), "em_coleta")

    def test_terceira_mensagem_funciona_com_historico_maior(self):
        cfg = _config_padrao()
        conversa = {"id": "conv-m3", "origem": "aniversario", "status": "bot_ativo"}
        resp_mock = {
            "texto": "Vocês preferem Salão ou Areia?",
            "dados_reserva": {"data_reserva": "2026-08-06", "horario": "13:00", "pessoas": 15},
            "dados_confirmados": {"data_reserva": "2026-08-06", "horario": "13:00", "pessoas": 15},
            "status_reserva": "em_coleta",
            "confianca": 0.9,
        }

        agente._historicos[TELEFONE] = [
            {"role": "user", "content": "Quero fazer uma reserva de aniversário"},
            {"role": "assistant", "content": "Qual dia você quer reservar?", "turn_id": "turn-m1", "conversa_id": "conv-m3"},
            {"role": "user", "content": "06/08/2026 às 13h"},
            {"role": "assistant", "content": "Quantas pessoas serão?", "turn_id": "turn-m2", "conversa_id": "conv-m3"},
        ]

        with patch.object(config_restaurante, "obter_config", return_value=cfg), \
             patch.object(agente, "interpretar_resposta_modelo", return_value=resp_mock), \
             patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado", return_value={"ok": True, "provider_message_id": "out-m3"}), \
             patch.object(fluxo_reservas, "registrar_mensagem"), \
             patch.object(fluxo_reservas, "_salvar_estado_reserva_conversa"):

            res = fluxo_reservas.processar_resposta_cliente(
                telefone=TELEFONE,
                mensagem_cliente="15 pessoas",
                conversa=conversa,
                provider_message_id="turn-m3",
            )
            self.assertEqual(res.get("status_reserva"), "em_coleta")


if __name__ == "__main__":
    unittest.main()
