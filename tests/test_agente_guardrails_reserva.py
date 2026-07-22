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

    def test_recusa_convite_reserva_finaliza_sem_chamar_groq(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {"campo_pendente": "data_reserva"}

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq") as chamar_groq:
            resposta = agente.processar_mensagem(telefone, "Não", nome_cliente="Rodrigo Teste")

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
            mensagem_cliente="Obrigado, não",
            interpretacao=interpretacao,
            nome_cliente="Rodrigo Teste",
        )

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "sem_interesse")
        self.assertEqual(resposta["dados_reserva"], {})
        self.assertNotIn(telefone, agente._estados_reserva)

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
        self.assertIn("falta o horario", agente._normalizar_busca(resposta["texto"]))

    def test_data_passada_rejeitada_e_nao_entra_no_estado(self) -> None:
        telefone = "5511999999999"
        payload = {
            "resposta_cliente": "Perfeito, tenho a data. Qual horario voce prefere?",
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
        self.assertIn("essa data ja passou", agente._normalizar_busca(resposta["texto"]))

    def test_dia_mes_passado_nao_e_assumido_como_proximo_ano(self) -> None:
        with patch.object(agente, "_hoje", return_value=date(2026, 7, 22)):
            self.assertIsNone(agente._extrair_data("dia 21/07"))
            self.assertEqual(agente._extrair_data("dia 23/07"), "2026-07-23")

    def test_pergunta_qual_data_tem_disponivel_pede_preferencia(self) -> None:
        telefone = "5511999999999"
        payload = {
            "resposta_cliente": "Temos algumas datas disponiveis.",
            "reserva": {"status": "nao_aplicavel"},
            "confianca": 0.5,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            resposta = agente.processar_mensagem(telefone, "Qual data tem disponivel?", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(
            resposta["texto"],
            "Você tem alguma data em mente? Me fala o dia e eu verifico para você.",
        )

    def test_pedido_imediato_to_indo_ai_nao_pergunta_data(self) -> None:
        telefone = "5511999999999"
        payload = {
            "resposta_cliente": "Perfeito, para qual data voce quer fazer a reserva?",
            "reserva": {"status": "em_coleta", "data_reserva": None, "horario": None, "pessoas": None},
            "confianca": 0.7,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            resposta = agente.processar_mensagem(telefone, "tô indo aí", nome_cliente="Rodrigo Teste")

        texto_normalizado = agente._normalizar_busca(resposta["texto"])
        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "aguardando_humano")
        self.assertIn("agora", texto_normalizado)
        self.assertIn("equipe", texto_normalizado)
        self.assertNotIn("qual data", texto_normalizado)

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
        self.assertIn("em cima da hora", agente._normalizar_busca(resposta["texto"]))

    def test_cliente_informa_26_07_e_resumo_continua_26_07(self) -> None:
        telefone = "5511999999999"
        payload = {
            "resposta_cliente": "Perfeito, só confirmando: reserva para 28/07/2026, às 20:00, para 4 pessoas. Posso confirmar?",
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
        self.assertIn("26/07/2026", resposta["texto"])
        self.assertNotIn("28/07/2026", resposta["texto"])

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
            "resposta_cliente": "Perfeito, só confirmando: reserva para 28/07/2026, às 22:00, para 29 pessoas. Posso confirmar?",
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
        self.assertIn("26/07/2026", resposta["texto"])
        self.assertIn("20:00", resposta["texto"])
        self.assertIn("29 pessoas", resposta["texto"])

    def test_nao_valida_data_durante_etapa_de_quantidade(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-26",
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "pessoas",
            "etapa": "aguardando_quantidade",
        }
        payload = {
            "resposta_cliente": "Essa data já passou.",
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
        self.assertNotIn("data ja passou", agente._normalizar_busca(resposta["texto"]))
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
            "resposta_cliente": "Qual horário você prefere?",
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
        self.assertIn("23:00", resposta["texto"])
        self.assertNotIn("horario", agente._estados_reserva[telefone])

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
            "resposta_cliente": "Qual horário você prefere?",
            "reserva": {"status": "em_coleta", "data_reserva": None, "horario": None, "pessoas": None},
            "confianca": 0.5,
        }

        with patch.dict(os.environ, {"GROQ_API_KEY": "teste"}), patch.object(agente, "_chamar_groq", return_value=self._mock_groq(payload)):
            primeira = agente.processar_mensagem(telefone, "qualquer coisa", nome_cliente="Rodrigo Teste")
            segunda = agente.processar_mensagem(telefone, "não sei", nome_cliente="Rodrigo Teste")

        self.assertFalse(primeira["reserva_confirmada"])
        self.assertIn("horario aproximado", agente._normalizar_busca(primeira["texto"]))
        self.assertFalse(segunda["reserva_confirmada"])
        self.assertEqual(segunda["status_reserva"], "aguardando_humano")
        self.assertIn("chamar alguem da equipe", agente._normalizar_busca(segunda["texto"]))

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
            resposta = agente.processar_mensagem(telefone, "Na verdade, quero às 20h", nome_cliente="Rodrigo Teste")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "aguardando_confirmacao")
        self.assertEqual(resposta["dados_reserva"]["data_reserva"], "2026-07-26")
        self.assertEqual(resposta["dados_reserva"]["horario"], "20:00")
        self.assertEqual(resposta["dados_reserva"]["pessoas"], 5)
        self.assertIn("26/07/2026", resposta["texto"])
        self.assertIn("20:00", resposta["texto"])
        self.assertIn("5 pessoas", resposta["texto"])
        self.assertNotIn("28/07/2026", resposta["texto"])

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
        self.assertIn("26/07/2026", resposta["texto"])
        self.assertIn("20:00", resposta["texto"])
        self.assertNotIn("28/07/2026", resposta["texto"])
        self.assertNotIn("22:00", resposta["texto"])

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
        self.assertIn("essa data ja passou", agente._normalizar_busca(resposta["texto"]))

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
        self.assertIn("falta o horario", agente._normalizar_busca(resposta["texto"]))

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
        self.assertIn("posso confirmar", agente._normalizar_busca(previa["texto"]))
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
