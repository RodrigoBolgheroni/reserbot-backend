"""
Suíte de testes para o fluxo de preferência de espaço (Salão / Areia),
diferenciação de negações direcionadas, controle seguro de histórico por turno,
e remoção de instruções de confirmação automática.
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from services import agente, config_restaurante, fluxo_reservas


TELEFONE = "5511777777777"


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
                id="salao-1",
                nome="Salão",
                descricao="Área interna",
                capacidade_maxima=25,
                permite_preferencia=True,
                regras="",
            ),
            config_restaurante.EspacoRestaurante(
                id="areia-1",
                nome="Areia",
                descricao="Área externa",
                capacidade_maxima=None,
                permite_preferencia=True,
                regras="",
            ),
        ),
    )


def _conversa_aniversario(**extras):
    base = {"id": "conv-aniv-esp", "origem": "aniversario", "status": "bot_ativo"}
    base.update(extras)
    return base


def _estado_coleta(**extras):
    estado = {
        "origem_conversa": "aniversario",
        "contexto_aniversario": True,
        "data_reserva": "2026-08-06",
        "horario": "13:00",
        "pessoas": 15,
        "nome_cliente": "Rodrigo",
        "etapa": "aguardando_espaco",
        "campo_pendente": "espaco",
    }
    estado.update(extras)
    return estado


class EscolhaEspacoNegacaoTest(unittest.TestCase):
    """Testes para a função _analisar_escolha_espaco_na_mensagem."""

    def setUp(self):
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._estados_reserva.clear()

    def test_selecao_direta_salao(self):
        config = _config_padrao()
        estado = _estado_coleta()
        for msg in ("Salão", "Salao", "prefiro o salão", "eu já disse salão", "quero salao"):
            with self.subTest(msg=msg):
                espaco = agente._analisar_escolha_espaco_na_mensagem(config, msg, estado)
                self.assertIsNotNone(espaco, f"Falhou para a mensagem: {msg}")
                self.assertEqual(espaco.nome, "Salão")

    def test_selecao_direta_areia(self):
        config = _config_padrao()
        estado = _estado_coleta()
        for msg in ("Areia", "prefiro areia", "pode ser na areia"):
            with self.subTest(msg=msg):
                espaco = agente._analisar_escolha_espaco_na_mensagem(config, msg, estado)
                self.assertIsNotNone(espaco, f"Falhou para a mensagem: {msg}")
                self.assertEqual(espaco.nome, "Areia")

    def test_negacao_direta_salao_nao_seleciona(self):
        config = _config_padrao()
        estado = _estado_coleta()
        for msg in ("Não quero Salão", "não gosto do Salão", "Salão não"):
            with self.subTest(msg=msg):
                espaco = agente._analisar_escolha_espaco_na_mensagem(config, msg, estado)
                self.assertIsNone(espaco, f"Não deveria selecionar para: {msg}")

    def test_negacao_areia_com_escolha_salao(self):
        """Casos obrigatórios: Negação de Areia + Escolha de Salão."""
        config = _config_padrao()
        estado = _estado_coleta()
        for msg in ("Não quero Areia, prefiro Salão", "Salão, não Areia"):
            with self.subTest(msg=msg):
                espaco = agente._analisar_escolha_espaco_na_mensagem(config, msg, estado)
                self.assertIsNotNone(espaco, f"Deveria escolher Salão para: {msg}")
                self.assertEqual(espaco.nome, "Salão")

    def test_pergunta_comparativa_sem_escolha_nao_seleciona(self):
        config = _config_padrao()
        estado = _estado_coleta()
        for msg in ("Areia ou Salão?", "qual a diferença entre salão e areia?", "o salão fica onde?"):
            with self.subTest(msg=msg):
                espaco = agente._analisar_escolha_espaco_na_mensagem(config, msg, estado)
                self.assertIsNone(espaco, f"Não deveria selecionar para pergunta: {msg}")

    def test_escolha_com_pergunta_contextual_preserva_escolha(self):
        """'Prefiro Salão, mas a Areia é coberta?' -> deve escolher Salão."""
        config = _config_padrao()
        estado = _estado_coleta()
        espaco = agente._analisar_escolha_espaco_na_mensagem(
            config, "Prefiro Salão, mas a Areia é coberta?", estado
        )
        self.assertIsNotNone(espaco)
        self.assertEqual(espaco.nome, "Salão")


class HistoricoPorTurnoTest(unittest.TestCase):
    """Testes para registro de histórico vinculado a turn_id e envio do WhatsApp."""

    def setUp(self):
        agente._historicos.clear()
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._historicos.clear()
        agente._estados_reserva.clear()

    @patch.object(agente, "processar_mensagem")
    def test_historico_salva_texto_definitivo_apenas_em_envio_sucesso(self, mock_proc):
        mock_proc.return_value = {
            "texto": "Perfeito.",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "em_coleta",
            "confianca": 0.9,
        }
        agente.definir_estado_reserva(TELEFONE, _estado_coleta())
        conversa = _conversa_aniversario()

        with patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado", return_value={"ok": True, "provider_message_id": "msg-123"}), \
             patch.object(fluxo_reservas, "registrar_mensagem") as mock_reg, \
             patch.object(fluxo_reservas, "_salvar_estado_reserva_conversa"):

            res = fluxo_reservas.processar_resposta_cliente(
                telefone=TELEFONE,
                mensagem_cliente="Salão",
                conversa=conversa,
                provider_message_id="msg-in-1",
            )
            self.assertTrue(res.get("texto"))
            historico = agente._historicos.get(TELEFONE, [])
            self.assertGreater(len(historico), 0)
            self.assertEqual(historico[-1]["role"], "assistant")
            self.assertEqual(historico[-1]["content"], res["texto"])
            self.assertEqual(historico[-1].get("turn_id"), "msg-in-1")

    @patch.object(agente, "processar_mensagem")
    def test_historico_nao_salva_em_falha_envio(self, mock_proc):
        mock_proc.return_value = {
            "texto": "Perfeito.",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "em_coleta",
            "confianca": 0.9,
        }
        agente.definir_estado_reserva(TELEFONE, _estado_coleta())
        conversa = _conversa_aniversario()

        with patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado", return_value={"ok": False, "erro": "API Error"}), \
             patch.object(fluxo_reservas, "registrar_mensagem"):

            res = fluxo_reservas.processar_resposta_cliente(
                telefone=TELEFONE,
                mensagem_cliente="Salão",
                conversa=conversa,
                provider_message_id="msg-in-err",
            )
            self.assertEqual(res.get("status_reserva"), "falha_envio")
            historico = agente._historicos.get(TELEFONE, [])
            # Nenhuma mensagem assistente foi registrada como entregue
            assistants = [m for m in historico if m.get("role") == "assistant"]
            self.assertEqual(len(assistants), 0)

    @patch.object(agente, "processar_mensagem")
    def test_duas_mensagens_rapidas_atualizam_turnos_corretos(self, mock_proc):
        mock_proc.return_value = {
            "texto": "Perfeito.",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "em_coleta",
            "confianca": 0.9,
        }
        agente.definir_estado_reserva(TELEFONE, _estado_coleta())
        conversa = _conversa_aniversario()

        with patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado", return_value={"ok": True, "provider_message_id": "out-1"}), \
             patch.object(fluxo_reservas, "registrar_mensagem"), \
             patch.object(fluxo_reservas, "_salvar_estado_reserva_conversa"):

            fluxo_reservas.processar_resposta_cliente(
                telefone=TELEFONE,
                mensagem_cliente="Salão",
                conversa=conversa,
                provider_message_id="turn-1",
            )
            fluxo_reservas.processar_resposta_cliente(
                telefone=TELEFONE,
                mensagem_cliente="Ok",
                conversa=conversa,
                provider_message_id="turn-2",
            )

            historico = agente._historicos.get(TELEFONE, [])
            assistants = [m for m in historico if m.get("role") == "assistant"]
            self.assertEqual(len(assistants), 2)
            self.assertEqual(assistants[0].get("turn_id"), "turn-1")
            self.assertEqual(assistants[1].get("turn_id"), "turn-2")


class RegraPromptOrigemTest(unittest.TestCase):
    """Verifica remoção total de instruções sobre confirmação automática."""

    def test_prompt_nao_contem_confirmacao_automatica(self):
        config = _config_padrao()
        texto_min = agente._texto_quantidade_minima_config(config)
        self.assertNotIn("confirmar automaticamente", texto_min.lower())
        self.assertIn("analise humana", texto_min.lower())

        with patch.object(agente, "_config_restaurante_atual", return_value=config):
            prompt = agente._mensagem_sistema("Rodrigo", telefone=TELEFONE)["content"]
            self.assertNotIn("limite de pessoas por reserva automatica", prompt.lower())
            self.assertNotIn("confirmar automaticamente ate 30 pessoas", prompt.lower())


class FluxoCompletoCenarioRealTest(unittest.TestCase):
    """Reproduz o fluxo real: Aniversário, 06/08/2026, 13h, 15 pessoas, 'Salão'."""

    def setUp(self):
        agente._historicos.clear()
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._historicos.clear()
        agente._estados_reserva.clear()

    @patch.object(agente, "interpretar_resposta_modelo")
    @patch.object(fluxo_reservas.agente, "dados_reserva_obrigatorios_ok", return_value=True)
    @patch.object(fluxo_reservas, "registrar_solicitacao_reserva", return_value={"ok": True, "reserva": {"id": "res-15"}})
    @patch.object(config_restaurante, "obter_config")
    def test_escolha_salao_avanca_para_aniversario_e_comprovante(
        self, mock_cfg, mock_reg, _val, mock_interp
    ):
        cfg = _config_padrao()
        mock_cfg.return_value = cfg
        mock_interp.return_value = {
            "texto": "Perfeito.",
            "dados_reserva": {"data_reserva": "2026-08-06", "horario": "13:00", "pessoas": 15, "nome_cliente": "Rodrigo"},
            "dados_confirmados": {"data_reserva": "2026-08-06", "horario": "13:00", "pessoas": 15, "nome_cliente": "Rodrigo"},
            "dados_mencionados": {},
            "dados_incertos": {},
            "correcoes": {},
            "status_reserva": "em_coleta",
            "intencao": "fornecimento_dados",
            "acao": "coletar",
            "confianca": 0.9,
        }

        est_inicial = _estado_coleta()
        agente.definir_estado_reserva(TELEFONE, est_inicial)
        conversa = {**_conversa_aniversario(), "metadata": {"estado_reserva": est_inicial}}

        with patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado", return_value={"ok": True, "provider_message_id": "out-real"}), \
             patch.object(fluxo_reservas, "registrar_mensagem"), \
             patch.object(fluxo_reservas, "_salvar_estado_reserva_conversa"):

            res = fluxo_reservas.processar_resposta_cliente(
                telefone=TELEFONE,
                mensagem_cliente="Salão",
                conversa=conversa,
                provider_message_id="msg-salao",
            )

            texto = res["texto"]
            self.assertIn("Salão", texto)
            self.assertIn("não trabalhamos com lista", texto.lower())
            self.assertIn("R$ 50,00", texto)
            # Não deve repetir a pergunta de escolha de espaço
            self.assertNotIn("Voces preferem Areia ou Salão?", texto)
            # Não promete disponibilidade real
            self.assertNotIn("vou verificar agora", texto.lower())
            self.assertNotIn("está disponível", texto.lower())

            estado = agente.obter_estado_reserva(TELEFONE)
            self.assertEqual(estado["preferencia_espaco_nome"], "Salão")
            self.assertEqual(estado["etapa"], "aguardando_comprovante")


if __name__ == "__main__":
    unittest.main()



if __name__ == "__main__":
    unittest.main()
