from __future__ import annotations

import json
import os
import unittest
from datetime import date
from unittest.mock import patch

from services import agente, fluxo_reservas


class AgenteGuardrailsReservaTest(unittest.TestCase):
    def setUp(self) -> None:
        agente._historicos.clear()
        agente._estados_reserva.clear()

    def tearDown(self) -> None:
        agente._historicos.clear()
        agente._estados_reserva.clear()

    def _mock_groq(self, payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False)

    def _assert_texto_preservado(self, resposta: dict, esperado: str) -> None:
        self.assertEqual(agente.remover_flag_reserva(resposta["texto"]), agente.remover_flag_reserva(esperado))

    def test_recusa_convite_reserva_finaliza_sem_chamar_groq(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {"campo_pendente": "data_reserva"}

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq") as chamar_groq:
            resposta = agente.processar_mensagem(telefone, "NÃ£o", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "sem_interesse")
        self.assertEqual(resposta["texto"], agente.MENSAGEM_RECUSA_RESERVA)
        self.assertNotIn(telefone, agente._estados_reserva)
        chamar_groq.assert_not_called()

    def test_recusa_tem_prioridade_sobre_dados_do_modelo(self) -> None:
        telefone = "5511999999999"
        interpretacao = {
            "texto": "Perfeito, para qual data voce quer fazer a reserva?",
            "reserva_confirmada": False,
            "dados_reserva": {"data_reserva": "2026-07-28", "horario": "20:00", "pessoas": 4},
            "status_reserva": "em_coleta",
            "confianca": 0.9,
        }

        resposta = agente.aplicar_guardrails_reserva(
            telefone=telefone,
            mensagem_cliente="Obrigado, nÃ£o",
            interpretacao=interpretacao,
            nome_cliente="Rodrigo Teste",
        )

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "sem_interesse")
        self.assertEqual(resposta["dados_reserva"], {})
        self.assertNotIn(telefone, agente._estados_reserva)

    def test_fallback_fixo_so_em_falha_tecnica_sem_groq(self) -> None:
        telefone = "5511999999999"

        with patch.dict(os.environ, {}, clear=True), patch.object(agente, "_chamar_groq") as chamar_groq:
            resposta = agente.processar_mensagem(telefone, "Oi, quero reservar", nome_cliente="Rodrigo Teste")

        self.assertEqual(resposta["texto"], agente._resposta_contingencia())
        self.assertEqual(resposta["status_reserva"], "aguardando_humano")
        chamar_groq.assert_not_called()

    def test_sim_sem_horario_pergunta_horario_e_nao_confirma(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-28",
            "pessoas": 10,
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "horario",
        }
        payload = {
            "resposta_cliente": "Reserva confirmada para o dia 28 para 10 pessoas.",
            "reserva": {
                "status": "confirmada",
                "data_reserva": "2026-07-28",
                "horario": None,
                "pessoas": 10,
            },
            "confianca": 1,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            resposta = agente.processar_mensagem(telefone, "Sim", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "em_coleta")
        self.assertNotIn("horario", resposta["dados_reserva"])
        self.assertNotIn("reserva confirmada", agente._normalizar_busca(resposta["texto"]))

    def test_data_passada_rejeitada_e_nao_entra_no_estado(self) -> None:
        telefone = "5511999999999"
        payload = {
            "resposta_cliente": "Essa data ja passou. Me fala uma data a partir de hoje para eu verificar a reserva.",
            "reserva": {
                "status": "em_coleta",
                "data_reserva": "2026-07-21",
                "horario": None,
                "pessoas": None,
            },
            "confianca": 0.9,
        }

        with (
            patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}),
            patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)),
        ):
            resposta = agente.processar_mensagem(telefone, "dia 21/07", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertNotIn("data_reserva", agente._estados_reserva[telefone])
        self._assert_texto_preservado(resposta, payload["resposta_cliente"])

    def test_dia_mes_passado_nao_e_assumido_como_proximo_ano(self) -> None:
        with patch.object(agente, "_hoje", return_value=date(2026, 7, 22)):
            self.assertIsNone(agente._extrair_data("dia 21/07"))
            self.assertEqual(agente._extrair_data("dia 23/07"), "2026-07-23")

    def test_pergunta_qual_data_tem_disponivel_pede_preferencia(self) -> None:
        telefone = "5511999999999"
        payload = {
            "resposta_cliente": "Consigo verificar a data que voce preferir. Me fala o dia que voce quer reservar.",
            "reserva": {"status": "nao_aplicavel"},
            "confianca": 0.5,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            resposta = agente.processar_mensagem(telefone, "Qual data tem disponivel?", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self._assert_texto_preservado(resposta, payload["resposta_cliente"])
        self.assertEqual(agente._estados_reserva[telefone]["campo_pendente"], "data_reserva")

    def test_pedido_imediato_to_indo_ai_nao_pergunta_data(self) -> None:
        telefone = "5511999999999"
        payload = {
            "resposta_cliente": "Perfeito, para qual data voce quer fazer a reserva?",
            "reserva": {"status": "em_coleta", "data_reserva": None, "horario": None, "pessoas": None},
            "confianca": 0.7,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            resposta = agente.processar_mensagem(telefone, "tÃ´ indo aÃ­", nome_cliente="Rodrigo Teste")

        texto_normalizado = agente._normalizar_busca(resposta["texto"])
        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "aguardando_humano")

    def test_pedido_hoje_agora_entende_data_atual_e_pedido_imediato(self) -> None:
        telefone = "5511999999999"
        payload = {
            "resposta_cliente": "Qual horario voce prefere?",
            "reserva": {"status": "em_coleta", "data_reserva": None, "horario": None, "pessoas": None},
            "confianca": 0.7,
        }

        with (
            patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}),
            patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)),
        ):
            resposta = agente.processar_mensagem(telefone, "hoje agora", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "aguardando_humano")
        self.assertEqual(resposta["dados_reserva"]["data_reserva"], "2026-07-22")
        self.assertEqual(resposta["texto"], payload["resposta_cliente"])

    def test_cliente_informa_26_07_e_resumo_continua_26_07(self) -> None:
        telefone = "5511999999999"
        payload = {
            "resposta_cliente": "Perfeito, sÃ³ confirmando: reserva para 28/07/2026, Ã s 20:00, para 4 pessoas. Posso confirmar?",
            "reserva": {
                "status": "confirmada",
                "data_reserva": "2026-07-28",
                "horario": "20:00",
                "pessoas": 4,
            },
            "confianca": 1,
        }

        with (
            patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}),
            patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)),
        ):
            resposta = agente.processar_mensagem(
                telefone,
                "Dia 26/07 as 20h para 4 pessoas",
                nome_cliente="Rodrigo Teste",
            )

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["dados_reserva"]["data_reserva"], "2026-07-26")
        self._assert_texto_preservado(resposta, payload["resposta_cliente"])

    def test_aguardando_quantidade_numero_29_vira_pessoas(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-26",
            "horario": "20:00",
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "pessoas",
            "etapa": "aguardando_quantidade",
        }
        payload = {
            "resposta_cliente": "Perfeito, sÃ³ confirmando: reserva para 28/07/2026, Ã s 22:00, para 29 pessoas. Posso confirmar?",
            "reserva": {
                "status": "confirmada",
                "data_reserva": "2026-07-28",
                "horario": "22:00",
                "pessoas": 29,
            },
            "confianca": 1,
        }

        with (
            patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}),
            patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)),
        ):
            resposta = agente.processar_mensagem(telefone, "29", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "aguardando_confirmacao")
        self.assertEqual(resposta["dados_reserva"]["pessoas"], 29)
        self.assertEqual(resposta["dados_reserva"]["data_reserva"], "2026-07-26")
        self.assertEqual(resposta["dados_reserva"]["horario"], "20:00")
        self._assert_texto_preservado(resposta, payload["resposta_cliente"])

    def test_nao_valida_data_durante_etapa_de_quantidade(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-26",
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "pessoas",
            "etapa": "aguardando_quantidade",
        }
        payload = {
            "resposta_cliente": "Anotei 29 pessoas. Ainda falta o horario para a reserva.",
            "reserva": {
                "status": "em_coleta",
                "data_reserva": "2026-07-21",
                "horario": None,
                "pessoas": 29,
            },
            "confianca": 0.9,
        }

        with (
            patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}),
            patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)),
        ):
            resposta = agente.processar_mensagem(telefone, "29", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(agente._estados_reserva[telefone]["data_reserva"], "2026-07-26")
        self.assertEqual(agente._estados_reserva[telefone]["pessoas"], 29)
        self._assert_texto_preservado(resposta, payload["resposta_cliente"])
        self.assertIn("horario", agente._normalizar_busca(resposta["texto"]))

    def test_explica_horario_fora_do_funcionamento(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-26",
            "pessoas": 10,
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "horario",
            "etapa": "aguardando_horario",
        }
        payload = {
            "resposta_cliente": "Esse horario fica fora do nosso funcionamento, que e das 10:00 as 23:00. Qual horario dentro desse periodo voce prefere?",
            "reserva": {
                "status": "em_coleta",
                "data_reserva": "2026-07-26",
                "horario": "04:00",
                "pessoas": 10,
            },
            "confianca": 0.9,
        }

        with (
            patch.dict(
                os.environ,
                {
                    "GROQ_API_KEY": "teste",
                    "RESERVA_HORARIO_INICIO": "10:00",
                    "RESERVA_HORARIO_FIM": "23:00",
                },
            ),
            patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)),
        ):
            resposta = agente.processar_mensagem(
                telefone,
                "Por que 4h nao esta disponivel?",
                nome_cliente="Rodrigo Teste",
            )

        texto_normalizado = agente._normalizar_busca(resposta["texto"])
        self.assertFalse(resposta["reserva_confirmada"])
        self.assertIn("funcionamento", texto_normalizado)
        self.assertIn("10:00", resposta["texto"])
        self.assertNotIn("horario", agente._estados_reserva[telefone])

    def test_pergunta_quando_abre_fecha_responde_sem_humano_e_mantem_horario(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-29",
            "pessoas": 4,
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "horario",
            "etapa": "aguardando_horario",
            "tentativas_campos": {"horario": 1},
        }
        payload = {
            "resposta_cliente": "Qual horario voce prefere?",
            "reserva": {"status": "em_coleta", "data_reserva": None, "horario": None, "pessoas": None},
            "confianca": 0.5,
        }

        with (
            patch.dict(
                os.environ,
                {
                    "GROQ_API_KEY": "teste",
                    "RESERVA_HORARIO_INICIO": "12:00",
                    "RESERVA_HORARIO_FIM": "23:00",
                },
            ),
            patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)),
        ):
            resposta = agente.processar_mensagem(telefone, "quando abre e fecha?", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "em_coleta")
        self.assertEqual(resposta["texto"], payload["resposta_cliente"])
        self.assertEqual(agente._estados_reserva[telefone]["campo_pendente"], "horario")
        self.assertEqual(agente._estados_reserva[telefone]["tentativas_campos"]["horario"], 1)

    def test_pergunta_quais_horarios_disponiveis_nao_inventa_disponibilidade(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-29",
            "pessoas": 4,
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "horario",
            "etapa": "aguardando_horario",
        }
        payload = {
            "resposta_cliente": "Temos horario disponivel.",
            "reserva": {"status": "em_coleta", "data_reserva": None, "horario": None, "pessoas": None},
            "confianca": 0.5,
        }

        with (
            patch.dict(
                os.environ,
                {
                    "GROQ_API_KEY": "teste",
                    "RESERVA_HORARIO_INICIO": "12:00",
                    "RESERVA_HORARIO_FIM": "23:00",
                },
            ),
            patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)),
        ):
            resposta = agente.processar_mensagem(telefone, "Quais horarios estao disponiveis?", nome_cliente="Rodrigo Teste")

        texto_normalizado = agente._normalizar_busca(resposta["texto"])
        self.assertEqual(resposta["status_reserva"], "em_coleta")
        self.assertEqual(resposta["texto"], payload["resposta_cliente"])

    def test_pergunta_endereco_durante_reserva_mantem_dados(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-29",
            "pessoas": 4,
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "horario",
            "etapa": "aguardando_horario",
        }
        payload = {
            "resposta_cliente": "Nao sei.",
            "reserva": {
                "status": "em_coleta",
                "data_reserva": "2026-07-28",
                "horario": "20:00",
                "pessoas": 8,
            },
            "confianca": 0.5,
        }

        with (
            patch.dict(
                os.environ,
                {
                    "GROQ_API_KEY": "teste",
                    "RESTAURANTE_ENDERECO": "Rua Teste, 123",
                },
            ),
            patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)),
        ):
            resposta = agente.processar_mensagem(telefone, "qual o endereco?", nome_cliente="Rodrigo Teste")

        estado = agente._estados_reserva[telefone]
        self.assertEqual(resposta["texto"], payload["resposta_cliente"])
        self.assertEqual(estado["data_reserva"], "2026-07-29")
        self.assertEqual(estado["pessoas"], 4)
        self.assertNotIn("horario", estado)
        self.assertEqual(estado["campo_pendente"], "horario")

    def test_duas_perguntas_seguidas_nao_aumentam_tentativas(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-29",
            "pessoas": 4,
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "horario",
            "etapa": "aguardando_horario",
            "tentativas_campos": {"horario": 1},
        }
        payload_abertura = {
            "resposta_cliente": "Abrimos das 12:00 as 23:00 nesse dia. Qual horario fica melhor para voce?",
            "reserva": {"status": "em_coleta", "data_reserva": None, "horario": None, "pessoas": None},
            "confianca": 0.5,
        }
        payload_endereco = {
            "resposta_cliente": "O endereco e Rua Teste, 123. Continuo com a data e a quantidade anotadas; falta so o horario.",
            "reserva": {"status": "em_coleta", "data_reserva": None, "horario": None, "pessoas": None},
            "confianca": 0.5,
        }

        with (
            patch.dict(
                os.environ,
                {
                    "GROQ_API_KEY": "teste",
                    "RESERVA_HORARIO_INICIO": "12:00",
                    "RESERVA_HORARIO_FIM": "23:00",
                    "RESTAURANTE_ENDERECO": "Rua Teste, 123",
                },
            ),
            patch.object(
                agente,
                "_chamar_groq",
                side_effect=[self._mock_groq(payload_abertura), self._mock_groq(payload_endereco)],
            ),
        ):
            primeira = agente.processar_mensagem(telefone, "quando abre e fecha?", nome_cliente="Rodrigo Teste")
            segunda = agente.processar_mensagem(telefone, "qual o endereco?", nome_cliente="Rodrigo Teste")

        self.assertEqual(primeira["status_reserva"], "em_coleta")
        self.assertEqual(segunda["status_reserva"], "em_coleta")
        self.assertIn("12:00", primeira["texto"])
        self.assertIn("Rua Teste, 123", segunda["texto"])
        self.assertEqual(agente._estados_reserva[telefone]["tentativas_campos"]["horario"], 1)

    def test_pergunta_como_sabe_horarios_responde_fonte_e_mantem_campo(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "campo_pendente": "data_reserva",
            "etapa": "aguardando_data",
            "tentativas_campos": {"data_reserva": 1},
        }
        payload = {
            "resposta": (
                "Esses horarios foram cadastrados pela equipe do restaurante no sistema. "
                "Voce ja tem algum dia em mente para reservar?"
            ),
            "intencao": "pergunta_fonte",
            "dados_extraidos": {"data": None, "horario": None, "quantidade": None},
            "acao": "continuar_conversa",
            "proximo_campo": "data",
            "confianca": 0.9,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            resposta = agente.processar_mensagem(telefone, "Como voce sabe esses horarios?", nome_cliente="Rodrigo Teste")

        texto_normalizado = agente._normalizar_busca(resposta["texto"])
        self.assertEqual(resposta["status_reserva"], "em_coleta")
        self.assertIn("cadastrados", texto_normalizado)
        self.assertEqual(agente._estados_reserva[telefone]["campo_pendente"], "data_reserva")
        self.assertEqual(agente._estados_reserva[telefone]["tentativas_campos"]["data_reserva"], 1)

    def test_pergunta_se_e_robo_responde_sem_pausar_bot(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "campo_pendente": "data_reserva",
            "etapa": "aguardando_data",
        }
        payload = {
            "resposta": (
                "Sou o assistente virtual do restaurante e estou aqui para ajudar com sua reserva. "
                "Se preferir falar com alguem da equipe, tambem posso encaminhar."
            ),
            "intencao": "pergunta_bot",
            "dados_extraidos": {"data": None, "horario": None, "quantidade": None},
            "acao": "continuar_conversa",
            "proximo_campo": "data",
            "confianca": 0.9,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            resposta = agente.processar_mensagem(telefone, "Voce e um robo?", nome_cliente="Rodrigo Teste")

        texto_normalizado = agente._normalizar_busca(resposta["texto"])
        self.assertEqual(resposta["status_reserva"], "em_coleta")
        self.assertIn("assistente virtual", texto_normalizado)
        self.assertEqual(agente._estados_reserva[telefone]["campo_pendente"], "data_reserva")

    def test_cliente_conversa_sem_data_nao_vira_erro(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "campo_pendente": "data_reserva",
            "etapa": "aguardando_data",
            "tentativas_campos": {"data_reserva": 2},
        }
        payload = {
            "resposta_cliente": "Nao consegui entender a data. Me passa no formato dia/mes.",
            "reserva": {"status": "em_coleta", "data_reserva": None, "horario": None, "pessoas": None},
            "confianca": 0.4,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            resposta = agente.processar_mensagem(
                telefone,
                "Nao sei ainda, estou vendo com meus amigos",
                nome_cliente="Rodrigo Teste",
            )

        texto_normalizado = agente._normalizar_busca(resposta["texto"])
        self.assertEqual(resposta["status_reserva"], "em_coleta")
        self.assertEqual(resposta["texto"], payload["resposta_cliente"])
        self.assertEqual(agente._estados_reserva[telefone]["tentativas_campos"]["data_reserva"], 2)

    def test_duas_mensagens_rapidas_nao_repetem_pergunta_de_data(self) -> None:
        telefone = "5511999999999"
        saudacao_payload = {
            "resposta": "Oi! Estou por aqui para ajudar com sua reserva.",
            "intencao": "comentario",
            "dados_extraidos": {"data": None, "horario": None, "quantidade": None},
            "acao": "continuar_conversa",
            "proximo_campo": "data",
            "confianca": 0.8,
        }
        interesse_payload = {
            "resposta": "Claro, seguimos com a reserva. Qual dia voce prefere?",
            "intencao": "fornecimento_dados",
            "dados_extraidos": {"data": None, "horario": None, "quantidade": None},
            "acao": "continuar_conversa",
            "proximo_campo": "data",
            "confianca": 0.8,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(
            agente,
            "_chamar_groq",
            side_effect=[self._mock_groq(saudacao_payload), self._mock_groq(interesse_payload)],
        ):
            primeira = agente.processar_mensagem(telefone, "Ola", nome_cliente="Rodrigo Teste")
            segunda = agente.processar_mensagem(telefone, "Quero sim", nome_cliente="Rodrigo Teste")

        self.assertNotEqual(primeira["texto"], segunda["texto"])
        self.assertNotIn("qual dia", agente._normalizar_busca(primeira["texto"]))
        self.assertIn("qual dia", agente._normalizar_busca(segunda["texto"]))
        self.assertEqual(agente._tentativas_campo(agente._estados_reserva[telefone], "data_reserva"), 0)

    def test_json_novo_aceita_11_horas_como_horario(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-29",
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "horario",
            "etapa": "aguardando_horario",
        }
        payload = {
            "resposta_natural": "Perfeito, Ã s 11h. Para quantas pessoas seria?",
            "intencao": "fornecimento_dados",
            "dados_extraidos": {"data": None, "horario": "11:00", "quantidade": None},
            "correcoes": {},
            "acao": "continuar_conversa",
            "proximo_campo": "quantidade",
            "confianca": 0.95,
        }

        with (
            patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste", "RESERVA_HORARIO_INICIO": "10:00", "RESERVA_HORARIO_FIM": "23:00"}),
            patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)),
        ):
            resposta = agente.processar_mensagem(telefone, "11 horas", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["dados_reserva"]["horario"], "11:00")
        self.assertEqual(resposta["status_reserva"], "em_coleta")
        self.assertIn("quantas pessoas", agente._normalizar_busca(resposta["texto"]))

    def test_quantidade_sem_contar_comigo_soma_cliente(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-29",
            "horario": "20:00",
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "pessoas",
            "etapa": "aguardando_quantidade",
        }
        payload = {
            "resposta_natural": "Perfeito, sÃ³ confirmando a reserva.",
            "intencao": "fornecimento_dados",
            "dados_extraidos": {"data": None, "horario": None, "quantidade": None},
            "acao": "continuar_conversa",
            "proximo_campo": "confirmacao",
            "confianca": 0.8,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            resposta = agente.processar_mensagem(telefone, "20 sem contar comigo", nome_cliente="Rodrigo Teste")

        self.assertEqual(resposta["status_reserva"], "aguardando_confirmacao")
        self.assertEqual(resposta["dados_reserva"]["pessoas"], 21)
        self.assertEqual(resposta["texto"], payload["resposta_natural"])

    def test_eu_e_mais_tres_vira_quatro_pessoas(self) -> None:
        self.assertEqual(agente._extrair_pessoas_solicitadas("eu e mais 3", permitir_numero_isolado=True), 4)
        self.assertEqual(agente._extrair_pessoas_solicitadas("vou eu, minha esposa e duas crianÃ§as", permitir_numero_isolado=True), 4)

    def test_comentario_chuva_nao_invalida_horario_nem_aumenta_erro(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-29",
            "pessoas": 4,
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "horario",
            "etapa": "aguardando_horario",
            "tentativas_campos": {"horario": 1},
        }
        payload = {
            "resposta_natural": "Combinado, tomara que o tempo ajude. Qual horÃ¡rio fica melhor para vocÃª?",
            "intencao": "comentario",
            "dados_extraidos": {"data": None, "horario": None, "quantidade": None},
            "acao": "continuar_conversa",
            "proximo_campo": "horario",
            "confianca": 0.9,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            resposta = agente.processar_mensagem(
                telefone,
                "Se nÃ£o estiver chovendo eu vou no dia 28 kkkk",
                nome_cliente="Rodrigo Teste",
            )

        estado = agente._estados_reserva[telefone]
        self.assertEqual(resposta["status_reserva"], "em_coleta")
        self.assertNotIn("horario_invalido", estado)
        self.assertEqual(estado["tentativas_campos"]["horario"], 1)

    def test_validacao_invalida_pode_usar_resposta_natural_da_ia(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-29",
            "pessoas": 4,
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "horario",
            "etapa": "aguardando_horario",
        }
        payload = {
            "resposta_natural": "Esse horÃ¡rio fica fora do nosso funcionamento, que Ã© das 10:00 Ã s 23:00. Qual horÃ¡rio dentro desse perÃ­odo fica melhor?",
            "intencao": "fornecimento_dados",
            "dados_extraidos": {"data": None, "horario": "04:00", "quantidade": None},
            "acao": "continuar_conversa",
            "proximo_campo": "horario",
            "confianca": 0.9,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste", "RESERVA_HORARIO_INICIO": "10:00", "RESERVA_HORARIO_FIM": "23:00"}), patch.object(
            agente,
            "_chamar_groq",
            return_value=self._mock_groq(payload),
        ):
            resposta = agente.processar_mensagem(telefone, "4 da manhÃ£", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertNotIn("horario", agente._estados_reserva[telefone])
        self._assert_texto_preservado(resposta, payload["resposta_natural"])

    def test_resposta_confusa_reformula_e_depois_oferece_humano(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-26",
            "pessoas": 4,
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "horario",
            "etapa": "aguardando_horario",
            "tentativas_campos": {"horario": 1},
        }
        payload = {
            "resposta_cliente": "Qual horÃ¡rio vocÃª prefere?",
            "reserva": {"status": "em_coleta", "data_reserva": None, "horario": None, "pessoas": None},
            "confianca": 0.5,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            primeira = agente.processar_mensagem(telefone, "qualquer coisa", nome_cliente="Rodrigo Teste")
            segunda = agente.processar_mensagem(telefone, "asdfgh", nome_cliente="Rodrigo Teste")

        self.assertFalse(primeira["reserva_confirmada"])
        self.assertEqual(primeira["texto"], payload["resposta_cliente"])
        self.assertFalse(segunda["reserva_confirmada"])
        self.assertEqual(segunda["status_reserva"], "em_coleta")
        self.assertEqual(segunda["texto"], payload["resposta_cliente"])

    def test_cliente_corrige_horario_e_mantem_data_quantidade(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-26",
            "horario": "19:00",
            "pessoas": 5,
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "confirmacao",
            "etapa": "aguardando_confirmacao",
            "aguardando_confirmacao": True,
        }
        payload = {
            "resposta_cliente": "Resumo errado.",
            "reserva": {
                "status": "em_coleta",
                "data_reserva": "2026-07-28",
                "horario": "21:00",
                "pessoas": 9,
            },
            "confianca": 0.9,
        }

        with (
            patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}),
            patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)),
        ):
            resposta = agente.processar_mensagem(telefone, "Na verdade, quero Ã s 20h", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "aguardando_confirmacao")
        self.assertEqual(resposta["dados_reserva"]["data_reserva"], "2026-07-26")
        self.assertEqual(resposta["dados_reserva"]["horario"], "20:00")
        self.assertEqual(resposta["dados_reserva"]["pessoas"], 5)
        self.assertEqual(resposta["texto"], payload["resposta_cliente"])

    def test_campos_validos_nao_sao_sobrescritos_em_outra_etapa(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-26",
            "horario": "20:00",
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "pessoas",
            "etapa": "aguardando_quantidade",
        }
        payload = {
            "resposta_cliente": "Resumo errado.",
            "reserva": {
                "status": "em_coleta",
                "data_reserva": "2026-07-28",
                "horario": "22:00",
                "pessoas": 29,
            },
            "confianca": 0.8,
        }

        with (
            patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}),
            patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)),
        ):
            resposta = agente.processar_mensagem(telefone, "29", nome_cliente="Rodrigo Teste")

        estado = agente._estados_reserva[telefone]
        self.assertEqual(estado["data_reserva"], "2026-07-26")
        self.assertEqual(estado["horario"], "20:00")
        self.assertEqual(estado["pessoas"], 29)
        self.assertEqual(resposta["texto"], payload["resposta_cliente"])

    def test_nao_confirma_estado_com_data_passada(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-21",
            "horario": "12:00",
            "pessoas": 10,
            "nome_cliente": "Rodrigo Teste",
            "aguardando_confirmacao": True,
            "campo_pendente": "confirmacao",
        }
        payload = {
            "resposta_cliente": "Reserva confirmada.",
            "reserva": {
                "status": "confirmada",
                "data_reserva": "2026-07-21",
                "horario": "12:00",
                "pessoas": 10,
            },
            "confianca": 1,
        }

        with (
            patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}),
            patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)),
        ):
            resposta = agente.processar_mensagem(telefone, "Sim", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertNotIn("data_reserva", agente._estados_reserva[telefone])
        self.assertEqual(resposta["texto"], agente._resposta_contingencia())

    def test_horario_invalido_nao_entra_no_estado(self) -> None:
        telefone = "5511999999999"
        payload = {
            "resposta_cliente": "Nao temos funcionamento as 4 da manha. Qual horario voce prefere?",
            "reserva": {
                "status": "em_coleta",
                "data_reserva": "2026-07-28",
                "horario": "04:00",
                "pessoas": None,
            },
            "confianca": 0.9,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            resposta = agente.processar_mensagem(telefone, "As 4 da manha", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertNotIn("horario", agente._estados_reserva[telefone])
        self.assertIn("funcionamento", agente._normalizar_busca(resposta["texto"]))

    def test_numero_isolado_corrige_pessoas_sem_virar_horario(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-28",
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "pessoas",
        }
        payload = {
            "resposta_cliente": "Dia 28, 10 pessoas, confirma?",
            "reserva": {
                "status": "confirmada",
                "data_reserva": "2026-07-28",
                "horario": "10:00",
                "pessoas": 10,
            },
            "confianca": 1,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            resposta = agente.processar_mensagem(telefone, "10", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(agente._estados_reserva[telefone]["pessoas"], 10)
        self.assertNotIn("horario", agente._estados_reserva[telefone])
        self.assertNotIn("reserva confirmada", agente._normalizar_busca(resposta["texto"]))

    def test_confirma_somente_depois_do_resumo_completo(self) -> None:
        telefone = "5511999999999"
        coleta_payload = {
            "resposta_cliente": "Anotado.",
            "reserva": {
                "status": "em_coleta",
                "data_reserva": "2026-07-28",
                "horario": "20:00",
                "pessoas": 10,
            },
            "confianca": 0.9,
        }
        confirma_payload = {
            "resposta_cliente": "Reserva confirmada.",
            "reserva": {
                "status": "confirmada",
                "data_reserva": "2026-07-28",
                "horario": "20:00",
                "pessoas": 10,
            },
            "confianca": 1,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(
            agente,
            "_chamar_groq",
            side_effect=[self._mock_groq(coleta_payload), self._mock_groq(confirma_payload)],
        ):
            previa = agente.processar_mensagem(telefone, "Dia 28/07 as 20h para 10 pessoas", nome_cliente="Rodrigo Teste")
            final = agente.processar_mensagem(telefone, "Sim", nome_cliente="Rodrigo Teste")

        self.assertFalse(previa["reserva_confirmada"])
        self.assertEqual(previa["status_reserva"], "aguardando_confirmacao")
        self.assertEqual(previa["texto"], coleta_payload["resposta_cliente"])
        self.assertTrue(final["reserva_confirmada"])
        self.assertEqual(final["dados_reserva"]["horario"], "20:00")
        self.assertEqual(final["dados_reserva"]["pessoas"], 10)

    @patch.object(fluxo_reservas.supabase, "inserir")
    @patch.object(fluxo_reservas.dados, "adicionar_reserva")
    def test_fluxo_nao_salva_reserva_sem_horario(self, adicionar_local, inserir_supabase) -> None:
        resultado = fluxo_reservas.registrar_reserva_confirmada(
            cliente={"id": "cliente-1", "telefone": "5511999999999", "nome": "Rodrigo Teste"},
            conversa={"id": "conversa-1"},
            dados_reserva={"data_reserva": "2026-07-28", "pessoas": 10},
        )

        self.assertFalse(resultado)
        inserir_supabase.assert_not_called()
        adicionar_local.assert_not_called()

    @patch.object(fluxo_reservas.supabase, "inserir")
    @patch.object(fluxo_reservas.dados, "adicionar_reserva")
    def test_fluxo_nao_salva_reserva_com_data_passada(self, adicionar_local, inserir_supabase) -> None:
        with patch.object(agente, "_hoje", return_value=date(2026, 7, 22)):
            resultado = fluxo_reservas.registrar_reserva_confirmada(
                cliente={"id": "cliente-1", "telefone": "5511999999999", "nome": "Rodrigo Teste"},
                conversa={"id": "conversa-1"},
                dados_reserva={"data_reserva": "2026-07-21", "horario": "12:00", "pessoas": 10},
            )

        self.assertFalse(resultado)
        inserir_supabase.assert_not_called()
        adicionar_local.assert_not_called()


if __name__ == "__main__":
    unittest.main()
