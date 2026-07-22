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
            "Consigo verificar a data que voce preferir. Me fala o dia que voce quer reservar.",
        )

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
        self.assertIn("horario nao esta disponivel", agente._normalizar_busca(resposta["texto"]))

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
