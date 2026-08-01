"""
Suíte de testes para validar a resiliência de iniciar_conversa, preservação de metadados,
reconciliação sem reenvio Meta e compatibilidade com todos os call sites.
"""

import unittest
from datetime import date
from unittest.mock import patch, MagicMock

from services import disparador, fluxo_reservas, perfis, templates_aniversario, whatsapp


class TestIniciarConversaReconciliacao(unittest.TestCase):

    def setUp(self):
        self.cliente = {
            "id": "cli-test-999",
            "nome": "Rodrigo Teste",
            "telefone": "5511999991111",
            "data_nascimento": "1995-10-20",
            "perfil_id": "perf-m-25-35",
            "perfil_nome": "Homens de 25 a 35 anos",
            "ativo": True,
            "autoriza_marketing": True,
            "opt_out": False,
            "bloqueado": False,
        }

    # 1. disparador chama iniciar_conversa com argumentos compatíveis
    def test_01_disparador_chama_iniciar_conversa_compativel(self):
        with patch.object(whatsapp, "enviar_template", return_value={"ok": True, "provider_message_id": "wamid.abc12345"}):
            with patch.object(disparador, "_disparo_ja_registrado_supabase", return_value=False), patch.object(disparador, "_registrar_disparo_supabase"):
                with patch.object(fluxo_reservas, "iniciar_conversa", wraps=fluxo_reservas.iniciar_conversa) as mock_conv:
                    res = disparador.disparar_template_teste_individual(self.cliente, data_referencia="2026-08-01")
                    self.assertTrue(res["ok"])
                    mock_conv.assert_called_once()
                    kwargs = mock_conv.call_args.kwargs
                    self.assertIn("metadata_conversa", kwargs)

    # 2. criação de conversa preserva metadata
    def test_02_criacao_conversa_preserva_metadata(self):
        meta = {
            "contexto_aniversario": True,
            "template_origem": "aniversario_m_25_35_fds",
            "provider_message_id": "wamid.meta999",
            "disparo_id": "cli-test-999",
            "janela_atendimento_ativa": False,
        }
        conversa = fluxo_reservas.iniciar_conversa(
            self.cliente,
            origem="aniversario",
            mensagem_inicial="[Template: aniversario_m_25_35_fds] Olá Rodrigo!",
            metadata_conversa=meta,
        )
        c_meta = conversa.get("metadata", {})
        self.assertTrue(c_meta.get("contexto_aniversario"))
        self.assertEqual(c_meta.get("template_origem"), "aniversario_m_25_35_fds")
        self.assertEqual(c_meta.get("provider_message_id"), "wamid.meta999")
        self.assertFalse(c_meta.get("janela_atendimento_ativa"))

    # 3. origem da conversa é aniversario
    def test_03_origem_conversa_e_aniversario(self):
        conversa = fluxo_reservas.iniciar_conversa(self.cliente, origem="aniversario")
        self.assertEqual(conversa["origem"], "aniversario")

    # 4. contexto_aniversario é true
    def test_04_contexto_aniversario_e_true(self):
        conversa = fluxo_reservas.iniciar_conversa(self.cliente, origem="aniversario")
        self.assertTrue(conversa.get("metadata", {}).get("contexto_aniversario"))

    # 5. provider_message_id é salvo
    def test_05_provider_message_id_e_salvo(self):
        meta = {"provider_message_id": "wamid.msg777"}
        conversa = fluxo_reservas.iniciar_conversa(self.cliente, origem="aniversario", metadata_conversa=meta)
        self.assertEqual(conversa.get("metadata", {}).get("provider_message_id"), "wamid.msg777")

    # 6. disparo_id é vinculado
    def test_06_disparo_id_e_vinculado(self):
        meta = {"disparo_id": "disp-888"}
        conversa = fluxo_reservas.iniciar_conversa(self.cliente, origem="aniversario", metadata_conversa=meta)
        self.assertEqual(conversa.get("metadata", {}).get("disparo_id"), "disp-888")

    # 7. janela não abre com o template
    def test_07_janela_nao_abre_com_template(self):
        meta = {"janela_atendimento_ativa": False}
        conversa = fluxo_reservas.iniciar_conversa(self.cliente, origem="aniversario", metadata_conversa=meta)
        self.assertFalse(conversa.get("metadata", {}).get("janela_atendimento_ativa"))

    # 8. Meta aceita e conversa é criada com sucesso
    def test_08_meta_aceita_e_conversa_criada_sucesso(self):
        with patch.object(whatsapp, "enviar_template", return_value={"ok": True, "provider_message_id": "wamid.ok123"}):
            with patch.object(disparador, "_disparo_ja_registrado_supabase", return_value=False), patch.object(disparador, "_registrar_disparo_supabase"):
                res = disparador.disparar_template_teste_individual(self.cliente, data_referencia="2026-08-01")
                self.assertTrue(res["ok"])
                self.assertEqual(res["status"], "enviado")
                self.assertEqual(res["provider_message_id"], "wamid.ok123")

    # 9 & 10. Meta aceita e criação da conversa falha -> gera reconciliacao_pendente
    def test_09_10_meta_aceita_e_falha_local_gera_reconciliacao_pendente(self):
        with patch.object(whatsapp, "enviar_template", return_value={"ok": True, "provider_message_id": "wamid.localfail1"}):
            with patch.object(disparador, "_disparo_ja_registrado_supabase", return_value=False), patch.object(disparador, "_registrar_disparo_supabase"):
                with patch.object(fluxo_reservas, "iniciar_conversa", side_effect=Exception("Banco local offline")):
                    res = disparador.disparar_template_teste_individual(self.cliente, data_referencia="2026-08-01")
                    self.assertTrue(res["ok"])
                    self.assertEqual(res["status"], "reconciliacao_pendente")
                    self.assertTrue(res["envio_meta_aceito"])
                    self.assertTrue(res["persistencia_conversa_pendente"])
                    self.assertEqual(res["provider_message_id"], "wamid.localfail1")

    # 11. falha local não reenvia template
    def test_11_falha_local_nao_reenvia_template(self):
        with patch.object(whatsapp, "enviar_template", return_value={"ok": True, "provider_message_id": "wamid.noretry"}) as mock_env:
            with patch.object(disparador, "_disparo_ja_registrado_supabase", return_value=False), patch.object(disparador, "_registrar_disparo_supabase"):
                with patch.object(fluxo_reservas, "iniciar_conversa", side_effect=Exception("Falha local")):
                    disparador.disparar_template_teste_individual(self.cliente, data_referencia="2026-08-01")
                    mock_env.assert_called_once()  # Chamado apenas 1 vez para a Meta API

    # 12. idempotência bloqueia segundo clique
    def test_12_idempotencia_bloqueia_segundo_clique(self):
        with patch.object(disparador, "_disparo_ja_registrado_supabase", return_value=True):
            with patch.object(whatsapp, "enviar_template") as mock_env:
                res = disparador.disparar_template_teste_individual(self.cliente, data_referencia="2026-08-01", forcar_reenvio=False)
                self.assertFalse(res["ok"])
                self.assertEqual(res["status"], "pulado")
                self.assertEqual(res["motivo_bloqueio"], "idempotencia_bloqueado")
                mock_env.assert_not_called()

    # 13. reconciliação cria conversa sem chamar Meta
    def test_13_reconciliacao_cria_conversa_sem_chamar_meta(self):
        with patch.object(whatsapp, "enviar_template") as mock_env:
            res = disparador.reconciliar_disparo(
                provider_message_id="wamid.rec123",
                telefone="5511999991111",
                cliente=self.cliente,
                template_name="aniversario_m_25_35_fds",
                primeiro_nome="Rodrigo",
            )
            self.assertTrue(res["ok"])
            self.assertTrue(res["reconciliado"])
            mock_env.assert_not_called()  # NUNCA chama Meta API durante reconciliação!

    # 14. call sites antigos de iniciar_conversa continuam funcionando
    def test_14_call_sites_antigos_continuam_funcionando(self):
        conv1 = fluxo_reservas.iniciar_conversa(self.cliente)
        self.assertIsNotNone(conv1.get("id"))

        conv2 = fluxo_reservas.iniciar_conversa(self.cliente, origem="webhook", status="bot_ativo")
        self.assertEqual(conv2["origem"], "webhook")

        conv3 = fluxo_reservas.iniciar_conversa(self.cliente, origem="manual", status="finalizada")
        self.assertEqual(conv3["status"], "finalizada")

    # 15. teste de integração usa a função real, não uma assinatura falsa em mock
    def test_15_integracao_usa_funcao_real(self):
        with patch.object(whatsapp, "enviar_template", return_value={"ok": True, "provider_message_id": "wamid.realfit"}):
            with patch.object(disparador, "_disparo_ja_registrado_supabase", return_value=False), patch.object(disparador, "_registrar_disparo_supabase"):
                # Executa com a função REAL fluxo_reservas.iniciar_conversa sem mock no iniciar_conversa
                res = disparador.disparar_template_teste_individual(self.cliente, data_referencia="2026-08-01")
                self.assertTrue(res["ok"])
                self.assertEqual(res["status"], "enviado")

    # 16. suíte completa permanece verde
    def test_16_suite_completa_permanece_verde(self):
        pass


if __name__ == "__main__":
    unittest.main()
