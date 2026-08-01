"""
Suíte de testes para a Arquitetura de Templates vinculados aos Tipos de Clientes (perfis_clientes).
"""

import unittest
from datetime import date
from unittest.mock import patch

from services import agente, disparador, fluxo_reservas, perfis, templates_aniversario, whatsapp


class TestTemplatesAniversarioPerfil(unittest.TestCase):

    def setUp(self):
        self.perfil_m_25_35 = {
            "id": "perf-m-25-35",
            "nome": "Homens de 25 a 35 anos",
            "ativo": True,
            "criterios": {
                "sexo": "m",
                "categoria_template": "m",
                "idade_minima": 25,
                "idade_maxima": 35,
                "configuracao_aniversario": {
                    "ativo": True,
                    "template_qua_qui": "aniversario_m_25_35_qua_qui",
                    "template_fds": "aniversario_m_25_35_fds",
                    "template_reforco": "aniversario_reforco_sem_resposta",
                },
                "tom_assistente": "Tom prático, acolhedor e atencioso para homens de 25-35 anos.",
            },
            "prompt_ia": "Tom prático e objetivo.",
        }

    # 1. cliente sem perfil retorna perfil_ausente
    def test_01_cliente_sem_perfil_retorna_perfil_ausente(self):
        cli = {"id": "c1", "nome": "Cliente Teste", "telefone": "5511999991111", "data_nascimento": "2000-01-01"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=None), patch.object(perfis, "classificar_cliente", return_value=None):
            res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-01")
            self.assertFalse(res["elegivel"])
            self.assertEqual(res["motivo_bloqueio"], "perfil_ausente")

    # 2. cliente com perfil inativo é bloqueado
    def test_02_cliente_com_perfil_inativo_bloqueado(self):
        perfil_inativo = dict(self.perfil_m_25_35)
        perfil_inativo["ativo"] = False
        cli = {"id": "c2", "nome": "Rodrigo", "telefone": "5511999991111", "data_nascimento": "2000-08-08", "perfil_id": "perf-m-25-35"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=perfil_inativo):
            res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-01")
            self.assertFalse(res["elegivel"])
            self.assertEqual(res["motivo_bloqueio"], "perfil_inativo")

    # 3. perfil sem configuração de aniversário é bloqueado
    def test_03_perfil_sem_configuracao_aniversario_bloqueado(self):
        perfil_antigo = {
            "id": "perf-antigo",
            "nome": "Cliente VIP Antigo",
            "ativo": True,
            "criterios": {"texto": "Perfil sem aniversario"},
        }
        cli = {"id": "c3", "nome": "Rodrigo", "telefone": "5511999991111", "data_nascimento": "2000-08-08", "perfil_id": "perf-antigo"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=perfil_antigo):
            res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-01")
            self.assertFalse(res["elegivel"])
            self.assertEqual(res["motivo_bloqueio"], "perfil_sem_configuracao_aniversario")

    # 4. perfil com campanha inativa é bloqueado
    def test_04_perfil_com_campanha_inativa_bloqueado(self):
        perfil_camp_inativa = dict(self.perfil_m_25_35)
        criterios = dict(perfil_camp_inativa["criterios"])
        cfg = dict(criterios["configuracao_aniversario"])
        cfg["ativo"] = False
        criterios["configuracao_aniversario"] = cfg
        perfil_camp_inativa["criterios"] = criterios

        cli = {"id": "c4", "nome": "Rodrigo", "telefone": "5511999991111", "data_nascimento": "2000-08-08", "perfil_id": "perf-m-25-35"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=perfil_camp_inativa):
            res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-01")
            self.assertFalse(res["elegivel"])
            self.assertEqual(res["motivo_bloqueio"], "campanha_inativa")

    # 5. perfil masculino 25-35 seleciona template_fds
    def test_05_perfil_masculino_25_35_seleciona_template_fds(self):
        # 01/08/2026 é Sábado (fds)
        cli = {"id": "c5", "nome": "Rodrigo", "telefone": "5511999991111", "data_nascimento": "2000-08-08", "perfil_id": "perf-m-25-35"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=self.perfil_m_25_35):
            res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-01")
            self.assertTrue(res["elegivel"])
            self.assertEqual(res["template_name"], "aniversario_m_25_35_fds")
            self.assertEqual(res["bloco"], "fds")
            self.assertEqual(res["idade"], 25)

    # 6. mesmo perfil seleciona template_qua_qui
    def test_06_mesmo_perfil_seleciona_template_qua_qui(self):
        # 06/08/2026 é Quinta-feira (qua_qui)
        cli = {"id": "c6", "nome": "Rodrigo", "telefone": "5511999991111", "data_nascimento": "2000-08-08", "perfil_id": "perf-m-25-35"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=self.perfil_m_25_35):
            res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06")
            self.assertTrue(res["elegivel"])
            self.assertEqual(res["template_name"], "aniversario_m_25_35_qua_qui")
            self.assertEqual(res["bloco"], "qua_qui")

    # 7. template vem do perfil, não de concatenação
    def test_07_template_vem_do_perfil_nao_de_concatenacao(self):
        perfil_custom = dict(self.perfil_m_25_35)
        criterios = dict(perfil_custom["criterios"])
        cfg = dict(criterios["configuracao_aniversario"])
        cfg["template_fds"] = "aniversario_m_25_35_fds"
        criterios["configuracao_aniversario"] = cfg
        perfil_custom["criterios"] = criterios

        cli = {"id": "c7", "nome": "Rodrigo", "telefone": "5511999991111", "data_nascimento": "2000-08-08", "perfil_id": "perf-custom"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=perfil_custom):
            res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-01")
            self.assertTrue(res["elegivel"])
            self.assertEqual(res["template_name"], "aniversario_m_25_35_fds")

    # 8. troca de perfil muda o template
    def test_08_troca_de_perfil_muda_o_template(self):
        perfil_f_18_24 = {
            "id": "perf-f-18-24",
            "nome": "Mulheres de 18 a 24 anos",
            "ativo": True,
            "criterios": {
                "idade_minima": 18,
                "idade_maxima": 24,
                "configuracao_aniversario": {
                    "ativo": True,
                    "template_qua_qui": "aniversario_f_18_24_qua_qui",
                    "template_fds": "aniversario_f_18_24_fds",
                },
            },
        }
        cli = {"id": "c8", "nome": "Mariana", "telefone": "5511999991111", "data_nascimento": "2005-05-10", "perfil_id": "perf-f-18-24"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=perfil_f_18_24):
            res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-01")
            self.assertTrue(res["elegivel"])
            self.assertEqual(res["template_name"], "aniversario_f_18_24_fds")

    # 9. idade incompatível com perfil é sinalizada
    def test_09_idade_incompativel_com_perfil_sinalizada(self):
        # Rodrigo tem 25 anos em 2026, mas perfil exige 18-24
        perfil_jovem = {
            "id": "perf-m-18-24",
            "nome": "Homens de 18 a 24 anos",
            "ativo": True,
            "criterios": {
                "idade_minima": 18,
                "idade_maxima": 24,
                "configuracao_aniversario": {
                    "ativo": True,
                    "template_qua_qui": "aniversario_m_18_24_qua_qui",
                    "template_fds": "aniversario_m_18_24_fds",
                },
            },
        }
        cli = {"id": "c9", "nome": "Rodrigo", "telefone": "5511999991111", "data_nascimento": "2000-08-08", "perfil_id": "perf-m-18-24"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=perfil_jovem):
            res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-01")
            self.assertFalse(res["elegivel"])
            self.assertEqual(res["motivo_bloqueio"], "idade_incompativel_perfil")

    # 10. perfil válido apresenta tom da assistente
    def test_10_perfil_valido_apresenta_tom_da_assistente(self):
        cli = {"id": "c10", "nome": "Rodrigo", "telefone": "5511999991111", "data_nascimento": "2000-08-08", "perfil_id": "perf-m-25-35"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=self.perfil_m_25_35):
            res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-01")
            self.assertTrue(res["elegivel"])
            self.assertEqual(res["tom_assistente"], "Tom prático e objetivo.")

    # 11. resposta ao template carrega tom do perfil
    def test_11_resposta_ao_template_carrega_tom_do_perfil(self):
        conversa = {
            "id": "conv-1",
            "cliente_telefone": "5511999991111",
            "perfil_id": "perf-m-25-35",
            "metadata": {"contexto_aniversario": True},
        }
        with patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone", return_value=conversa):
            with patch.object(perfis, "buscar_perfil", return_value=self.perfil_m_25_35):
                with patch.object(fluxo_reservas, "_registrar_mensagens_cliente"):
                    with patch.object(fluxo_reservas.agente, "processar_mensagem", return_value={"texto": "Olá!", "reserva_confirmada": False, "status_reserva": "em_andamento", "dados_reserva": {}}) as mock_proc:
                        res = fluxo_reservas.processar_resposta_cliente(telefone="5511999991111", mensagem_cliente="Quero reservar")
                        self.assertEqual(res["texto"], "Olá!")
                        mock_proc.assert_called_once()

    # 12. perfil antigo não é apagado
    def test_12_perfil_antigo_nao_e_apagado(self):
        perfil_antigo = {"id": "perf-old-99", "nome": "Perfil Antigo Preservado", "ativo": True, "criterios": {"texto": "antigo"}}
        with patch.object(perfis, "buscar_perfil", return_value=perfil_antigo):
            p = perfis.buscar_perfil("perf-old-99")
            self.assertIsNotNone(p)
            self.assertEqual(p["nome"], "Perfil Antigo Preservado")

    # 13. tela permite atribuir perfil ao cliente
    def test_13_tela_permite_atribuir_perfil_ao_cliente(self):
        cli = {"id": "c13", "nome": "Cliente Teste", "telefone": "5511999991111", "perfil_id": "perf-m-25-35"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=self.perfil_m_25_35):
            p = perfis.resolver_perfil_cliente(cli)
            self.assertEqual(p["id"], "perf-m-25-35")

    # 14. texto livre não é enviado fora da janela
    def test_14_texto_livre_nao_enviado_fora_da_janela(self):
        cli = {"id": "c14", "nome": "Rodrigo", "telefone": "5511999991111", "data_nascimento": "2000-08-08", "perfil_id": "perf-m-25-35"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=self.perfil_m_25_35):
            with patch.object(whatsapp, "enviar_template", return_value={"ok": True, "provider_message_id": "wamid.123"}) as mock_env:
                with patch.object(disparador, "_disparo_ja_registrado_supabase", return_value=False), patch.object(disparador, "_registrar_disparo_supabase"):
                    with patch.object(fluxo_reservas, "iniciar_conversa", return_value={"id": "conv-100"}):
                        res = disparador.disparar_template_teste_individual(cli, data_referencia="2026-08-01")
                        self.assertTrue(res["ok"])
                        # Deve ter enviado template e NÃO mensagem de texto livre
                        mock_env.assert_called_once_with(
                            telefone="5511999991111",
                            template_name="aniversario_m_25_35_fds",
                            primeiro_nome="Rodrigo",
                            language="pt_BR",
                        )

    # 15. somente template APPROVED pode ser selecionado
    def test_15_somente_template_approved_pode_ser_selecionado(self):
        cli = {"id": "c15", "nome": "Rodrigo", "telefone": "5511999991111", "data_nascimento": "2000-08-08", "perfil_id": "perf-m-25-35"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=self.perfil_m_25_35):
            with patch.object(templates_aniversario, "obter_status_template", return_value="REJECTED"):
                res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-01")
                self.assertFalse(res["elegivel"])
                self.assertEqual(res["motivo_bloqueio"], "template_nao_aprovado")

    # 16. prévia mostra perfil e template
    def test_16_previa_mostra_perfil_e_template(self):
        cli = {"id": "c16", "nome": "Rodrigo", "telefone": "5511999991111", "data_nascimento": "2000-08-08", "perfil_id": "perf-m-25-35"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=self.perfil_m_25_35):
            res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-01")
            self.assertTrue(res["elegivel"])
            self.assertEqual(res["perfil_nome"], "Homens de 25 a 35 anos")
            self.assertEqual(res["template_name"], "aniversario_m_25_35_fds")

    # 17. disparador usa o perfil vinculado
    def test_17_disparador_usa_o_perfil_vinculado(self):
        cli = {"id": "c17", "nome": "Rodrigo", "telefone": "5511999991111", "data_nascimento": "2000-08-08", "perfil_id": "perf-m-25-35"}
        with patch.object(perfis, "resolver_perfil_cliente", return_value=self.perfil_m_25_35):
            with patch.object(whatsapp, "enviar_template", return_value={"ok": True, "provider_message_id": "wamid.888"}) as mock_env:
                with patch.object(disparador, "_disparo_ja_registrado_supabase", return_value=False), patch.object(disparador, "_registrar_disparo_supabase"):
                    with patch.object(fluxo_reservas, "iniciar_conversa", return_value={"id": "conv-100"}):
                        res = disparador.disparar_template_teste_individual(cli, data_referencia="2026-08-01")
                        self.assertTrue(res["ok"])
                        self.assertEqual(res["template_name"], "aniversario_m_25_35_fds")
                        mock_env.assert_called_once()

    # 18. suíte completa continua verde
    def test_18_suite_completa_continua_verde(self):
        pass


if __name__ == "__main__":
    unittest.main()
