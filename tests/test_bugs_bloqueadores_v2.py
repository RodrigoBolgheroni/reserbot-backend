"""
Suíte de Testes dos 9 Bugs Bloqueadores e Diretrizes Mandatórias de Arquitetura.
"""

import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from services import agente, config_restaurante, fluxo_reservas, ia_fallback

TELEFONE = "5511999999999"


def _config_teste() -> config_restaurante.ConfigRestaurante:
    base = config_restaurante._obter_config_env()
    return replace(
        base,
        nome="Restaurante Praia do Sol",
        estabelecimento_id="est-praia",
        fonte="supabase",
        quantidade_minima_reserva=11,
        taxa_valor=50.0,
        taxa_convertida_consumacao=True,
        prazo_cancelamento_horas=24,
        pix_chave="00.000.000/0001-00",
        pix_titular="Restaurante Praia do Sol",
        exige_comprovante=True,
        horarios_permitidos_reserva=("12:00", "13:00", "14:00", "18:00", "19:00"),
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
        faq_conteudos=(
            config_restaurante.FaqConteudo(
                id="faq-1",
                titulo="Regra Finais de Semana",
                conteudo="Em sabados e domingos grupos acima de 25 pessoas sao direcionados para a Areia.",
                categoria="espaco",
                tags=("espaco",),
                ativo=True,
            ),
        ),
    )


class BugsBloqueadoresV2Test(unittest.TestCase):
    """Testes unitários dos 9 bugs e 5 diretrizes mandatórias de arquitetura."""

    def setUp(self):
        agente._historicos.clear()
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._historicos.clear()
        agente._estados_reserva.clear()

    # -------------------------------------------------------------------------
    # 1. ANALISADOR ÚNICO DE ESPAÇO E NEGAÇÃO VS. RECUSA DE RESERVA
    # -------------------------------------------------------------------------

    def test_analisador_unico_retorna_resultados_consistentes(self):
        cfg = _config_teste()
        analise = agente._analisar_intencao_espaco("Não quero Areia, prefiro Salão", {}, cfg)
        self.assertEqual(analise["espaco_escolhido_id"], "salao")
        self.assertIn("areia", analise["espacos_negados"])
        self.assertFalse(analise["recusa_reserva"])
        self.assertFalse(analise["recusa_espaco_obrigatorio"])

    def test_pergunta_e_escolha_na_mesma_frase(self):
        cfg = _config_teste()
        analise = agente._analisar_intencao_espaco("Prefiro Salão, mas a Areia é coberta?", {}, cfg)
        self.assertEqual(analise["espaco_escolhido_id"], "salao")
        self.assertTrue(analise["pergunta_sobre_espaco"])
        self.assertFalse(analise["recusa_reserva"])

    def test_negacao_de_um_espaco_e_escolha_de_outro(self):
        cfg = _config_teste()
        res1 = agente._analisar_intencao_espaco("Não quero Areia, prefiro Salão", {}, cfg)
        self.assertEqual(res1["espaco_escolhido_id"], "salao")
        self.assertFalse(res1["recusa_reserva"])

        res2 = agente._analisar_intencao_espaco("Salão, não Areia", {}, cfg)
        self.assertEqual(res2["espaco_escolhido_id"], "salao")

        res3 = agente._analisar_intencao_espaco("Não quero Salão, pode ser Areia", {}, cfg)
        self.assertEqual(res3["espaco_escolhido_id"], "areia")

    def test_recusa_real_da_reserva(self):
        cfg = _config_teste()
        analise = agente._analisar_intencao_espaco("Não quero mais fazer a reserva", {}, cfg)
        self.assertTrue(analise["recusa_reserva"])
        self.assertIsNone(analise["espaco_escolhido_id"])

    # -------------------------------------------------------------------------
    # 2. RECUSA DE ESPAÇO OBRIGATÓRIO
    # -------------------------------------------------------------------------

    def test_recusa_espaco_obrigatorio_27_pessoas_sabado(self):
        cfg = _config_teste()
        estado = {
            "data_reserva": "2026-08-08",  # Sábado
            "horario": "13:00",
            "pessoas": 27,
            "etapa": "aguardando_espaco",
            "campo_pendente": "espaco",
        }
        with patch.object(config_restaurante, "obter_config", return_value=cfg):
            agente._aplicar_regras_operacionais_espaco_estado(estado)
            self.assertTrue(estado.get("regra_espaco_obrigatoria"))
            self.assertEqual(estado.get("espaco_direcionado_id"), "areia")

            analise = agente._analisar_intencao_espaco("Não quero a Areia", estado, cfg)
            self.assertTrue(analise["recusa_espaco_obrigatorio"])

            resp = agente._responder_direcionamento_espaco_se_necessario(
                telefone=TELEFONE,
                mensagem_cliente="Não quero a Areia",
                estado=estado,
                interpretacao={"texto": "..."}
            )
            self.assertIsNotNone(resp)
            self.assertIn("Para 27 pessoas no sabado", resp["texto"])
            self.assertIn("Podemos reduzir para até 25 pessoas", resp["texto"])
            self.assertEqual(resp["status_reserva"], "em_coleta")
            self.assertTrue(estado.get("recusa_espaco_direcionado"))

    # -------------------------------------------------------------------------
    # 3. RECÁLCULO ATÔMICO DE REGRAS DERIVADAS (27 -> 20 PESSOAS)
    # -------------------------------------------------------------------------

    def test_correcao_27_para_20_pessoas_limpa_direcionamento(self):
        cfg = _config_teste()
        estado = {
            "data_reserva": "2026-08-08",  # Sábado
            "horario": "13:00",
            "pessoas": 27,
            "regra_espaco_obrigatoria": True,
            "espaco_direcionado_id": "areia",
            "espaco_direcionado_nome": "Areia",
            "etapa": "aguardando_espaco",
            "campo_pendente": "espaco",
        }
        estado["pessoas"] = 20
        with patch.object(config_restaurante, "obter_config", return_value=cfg):
            agente._limpar_e_recalcular_regras_derivadas(estado, cfg)
            agente._atualizar_preferencia_espaco_estado(estado, mensagem_cliente="Então coloca 20 pessoas, com preferência pelo Salão")

            self.assertFalse(estado.get("regra_espaco_obrigatoria"))
            self.assertIsNone(estado.get("espaco_direcionado_id"))
            self.assertEqual(estado.get("preferencia_espaco_id"), "salao")
            self.assertEqual(estado.get("preferencia_espaco_nome"), "Salão")

    # -------------------------------------------------------------------------
    # 4. GROUNDING DETERMINÍSTICO PARA ESPAÇOS SEM IA
    # -------------------------------------------------------------------------

    def test_descricao_de_espaco_nao_passa_pela_ia(self):
        cfg = _config_teste()
        texto = agente._resposta_pergunta_espacos_deterministica(cfg)
        self.assertIn("Salão é Área interna", texto)
        self.assertIn("comporta até 25 pessoas", texto)
        self.assertIn("Areia é Área externa", texto)
        self.assertIn("não possui limite máximo definido no cadastro", texto)
        self.assertIn("Qual deles você prefere?", texto)
        self.assertNotIn("ar-condicionado", texto.lower())
        self.assertNotIn("praia", texto.lower())
        self.assertNotIn("vista", texto.lower())

    def test_palavra_praia_no_nome_do_restaurante_nao_causa_falso_positivo(self):
        cfg = _config_teste()
        self.assertEqual(cfg.nome, "Restaurante Praia do Sol")
        analise = agente._analisar_intencao_espaco("Quero reservar no Restaurante Praia do Sol", {}, cfg)
        self.assertFalse(analise["recusa_reserva"])
        self.assertFalse(analise["pergunta_sobre_espaco"])
        self.assertNotIn("salao", analise["espacos_negados"])
        self.assertNotIn("areia", analise["espacos_negados"])

    # -------------------------------------------------------------------------
    # 5. HORÁRIO INVÁLIDO (03h)
    # -------------------------------------------------------------------------

    def test_horario_invalido_03h_rejeitado_deterministicamente(self):
        cfg = _config_teste()
        valido = agente._horario_reserva_valido("03:00", data_reserva="2026-08-06", config=cfg)
        self.assertFalse(valido)

        with patch.object(config_restaurante, "obter_config", return_value=cfg):
            msg = agente._mensagem_validacao_falhou(
                "horario",
                {"horario": "03:00"},
                TELEFONE,
                {"texto": "..."},
                mensagem_cliente="06/08/2026 às 03h",
            )
            self.assertIn("As reservas são feitas às 12h, 13h, 14h, 18h ou 19h", msg)
            self.assertNotIn("sábado", msg.lower())

    # -------------------------------------------------------------------------
    # 6. PIPELINE DETERMINÍSTICO E CORREÇÕES SEM IA
    # -------------------------------------------------------------------------

    def test_correcao_deterministica_ocorre_sem_chamar_ia(self):
        cfg = _config_teste()
        estado = {"data_reserva": "2026-08-06", "horario": "13:00", "pessoas": 15}
        with patch.object(config_restaurante, "obter_config", return_value=cfg):
            alterou = agente._tentar_correcao_deterministica("coloca 20 pessoas", estado, cfg)
            self.assertTrue(alterou)
            self.assertEqual(estado.get("pessoas"), 20)

    def test_muda_para_sabado_nao_inventa_data(self):
        res = agente._extrair_data_relativa("muda para sabado")
        self.assertIsNotNone(res)

    # -------------------------------------------------------------------------
    # 7. SNAPSHOT PERSISTENTE PARA RETOMADA (SUPABASE METADATA)
    # -------------------------------------------------------------------------

    def test_retomada_funciona_apos_limpar_completamente_o_cache_em_memoria(self):
        cfg = _config_teste()
        conversa = {
            "id": "conv-retomada",
            "origem": "aniversario",
            "status": "bot_ativo",
            "metadata": {
                "estado_reserva": {
                    "data_reserva": "2026-08-06",
                    "horario": "13:00",
                    "pessoas": 15,
                    "preferencia_espaco_id": "salao",
                    "preferencia_espaco_nome": "Salão",
                    "etapa": "aguardando_comprovante",
                    "campo_pendente": "comprovante",
                }
            },
        }

        agente._estados_reserva.clear()
        agente._historicos.clear()

        with patch.object(config_restaurante, "obter_config", return_value=cfg):
            fluxo_reservas._carregar_estado_reserva_conversa(conversa, TELEFONE)
            est = agente.obter_estado_reserva(TELEFONE)
            self.assertEqual(est.get("data_reserva"), "2026-08-06")
            self.assertEqual(est.get("horario"), "13:00")
            self.assertEqual(est.get("pessoas"), 15)
            self.assertEqual(est.get("preferencia_espaco_id"), "salao")

    def test_retomada_funciona_simulando_restart_do_worker(self):
        cfg = _config_teste()
        conversa = {
            "id": "conv-restart",
            "origem": "aniversario",
            "status": "bot_ativo",
            "metadata": {
                "snapshot_estado_reserva": {
                    "data_reserva": "2026-08-06",
                    "horario": "18:00",
                    "pessoas": 12,
                    "etapa": "aguardando_espaco",
                }
            },
        }

        agente._estados_reserva.clear()
        with patch.object(config_restaurante, "obter_config", return_value=cfg):
            fluxo_reservas._carregar_estado_reserva_conversa(conversa, TELEFONE)
            est = agente.obter_estado_reserva(TELEFONE)
            self.assertEqual(est.get("data_reserva"), "2026-08-06")
            self.assertEqual(est.get("horario"), "18:00")
            self.assertEqual(est.get("pessoas"), 12)

    def test_reiniciar_do_zero_realmente_descarta_o_snapshot_somente_apos_acao_explicita(self):
        meta = {"estado_reserva": {"data_reserva": "2026-08-06", "pessoas": 15}}
        conversa = {
            "id": "conv-reset",
            "origem": "aniversario",
            "metadata": meta,
        }
        agente.definir_estado_reserva(TELEFONE, {"data_reserva": "2026-08-06", "pessoas": 15})

        with patch.object(fluxo_reservas, "_atualizar_metadata_conversa") as mock_upd:
            fluxo_reservas._limpar_estado_reserva_conversa(conversa, TELEFONE)
            self.assertNotIn("estado_reserva", conversa["metadata"])
            self.assertNotIn("snapshot_estado_reserva", conversa["metadata"])
            self.assertIn("estado_reserva_finalizado_em", conversa["metadata"])
            mock_upd.assert_called_once()

    def test_fallback_com_429_preserva_o_estado(self):
        cfg = _config_teste()
        est = {"data_reserva": "2026-08-06", "horario": "13:00", "pessoas": 15}
        agente.definir_estado_reserva(TELEFONE, est)

        with patch.object(ia_fallback, "executar_ia_com_fallback", return_value={"ok": False, "erro_codigo": "rate_limit_exceeded", "encaminhar_humano": False}):
            res = agente._chamar_groq([], "llama-3.3-70b-versatile")
            self.assertIn("oscilacao temporaria", res)
            est_depois = agente.obter_estado_reserva(TELEFONE)
            self.assertEqual(est_depois.get("data_reserva"), "2026-08-06")
            self.assertEqual(est_depois.get("pessoas"), 15)


if __name__ == "__main__":
    unittest.main()
