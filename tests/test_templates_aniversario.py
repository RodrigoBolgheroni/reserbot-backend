"""
Suíte de testes obrigatória para seleção de templates de aniversário.
"""

import unittest
from datetime import date
from unittest.mock import patch

from services import templates_aniversario


class TestTemplatesAniversario(unittest.TestCase):

    def _base_cliente(self, **kwargs):
        base = {
            "id": "cli-123",
            "nome": "Maria Silva",
            "telefone": "5511999998888",
            "data_nascimento": "1998-05-15",
            "categoria": "f",
            "ativo": True,
            "autoriza_marketing": True,
            "opt_out": False,
            "bloqueado": False,
        }
        base.update(kwargs)
        return base

    # 1. feminino 18–24 qua_qui
    def test_01_feminino_18_24_qua_qui(self):
        # 20 anos na data de referencia
        cli = self._base_cliente(categoria="feminino", data_nascimento="2006-05-15")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertTrue(res["elegivel"])
        self.assertEqual(res["template_name"], "aniversario_f_18_24_qua_qui")
        self.assertEqual(res["variaveis"]["1"], "Maria")

    # 2. feminino 18–24 fds
    def test_02_feminino_18_24_fds(self):
        cli = self._base_cliente(categoria="mulher", data_nascimento="2006-05-15")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-07", bloco_campanha="fds")
        self.assertTrue(res["elegivel"])
        self.assertEqual(res["template_name"], "aniversario_f_18_24_fds")

    # 3. feminino 25–35 qua_qui
    def test_03_feminino_25_35_qua_qui(self):
        cli = self._base_cliente(categoria="f", data_nascimento="1998-05-15")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertTrue(res["elegivel"])
        self.assertEqual(res["template_name"], "aniversario_f_25_35_qua_qui")

    # 4. feminino 25–35 fds
    def test_04_feminino_25_35_fds(self):
        cli = self._base_cliente(categoria="f", data_nascimento="1998-05-15")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-07", bloco_campanha="fds")
        self.assertTrue(res["elegivel"])
        self.assertEqual(res["template_name"], "aniversario_f_25_35_fds")

    # 5. feminino 36+ qua_qui
    def test_05_feminino_36_mais_qua_qui(self):
        cli = self._base_cliente(categoria="feminino", data_nascimento="1980-01-10")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertTrue(res["elegivel"])
        self.assertEqual(res["template_name"], "aniversario_f_36_mais_qua_qui")

    # 6. feminino 36+ fds
    def test_06_feminino_36_mais_fds(self):
        cli = self._base_cliente(categoria="f", data_nascimento="1980-01-10")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-07", bloco_campanha="fds")
        self.assertTrue(res["elegivel"])
        self.assertEqual(res["template_name"], "aniversario_f_36_mais_fds")

    # 7. masculino 18–24 qua_qui
    def test_07_masculino_18_24_qua_qui(self):
        cli = self._base_cliente(nome="Carlos Souza", categoria="masculino", data_nascimento="2005-02-10")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertTrue(res["elegivel"])
        self.assertEqual(res["template_name"], "aniversario_m_18_24_qua_qui")
        self.assertEqual(res["variaveis"]["1"], "Carlos")

    # 8. masculino 18–24 fds
    def test_08_masculino_18_24_fds(self):
        cli = self._base_cliente(nome="Carlos Souza", categoria="homem", data_nascimento="2005-02-10")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-07", bloco_campanha="fds")
        self.assertTrue(res["elegivel"])
        self.assertEqual(res["template_name"], "aniversario_m_18_24_fds")

    # 9. masculino 25–35 qua_qui
    def test_09_masculino_25_35_qua_qui(self):
        cli = self._base_cliente(nome="Rodrigo Silva", categoria="m", data_nascimento="1995-10-20")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertTrue(res["elegivel"])
        self.assertEqual(res["template_name"], "aniversario_m_25_35_qua_qui")

    # 10. masculino 25–35 fds
    def test_10_masculino_25_35_fds(self):
        cli = self._base_cliente(nome="Rodrigo Silva", categoria="m", data_nascimento="1995-10-20")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-07", bloco_campanha="fds")
        self.assertTrue(res["elegivel"])
        self.assertEqual(res["template_name"], "aniversario_m_25_35_fds")

    # 11. masculino 36+ qua_qui
    def test_11_masculino_36_mais_qua_qui(self):
        cli = self._base_cliente(nome="Roberto Bolgheroni", categoria="m", data_nascimento="1975-03-30")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertTrue(res["elegivel"])
        self.assertEqual(res["template_name"], "aniversario_m_36_mais_qua_qui")

    # 12. masculino 36+ fds
    def test_12_masculino_36_mais_fds(self):
        cli = self._base_cliente(nome="Roberto Bolgheroni", categoria="m", data_nascimento="1975-03-30")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-07", bloco_campanha="fds")
        self.assertTrue(res["elegivel"])
        self.assertEqual(res["template_name"], "aniversario_m_36_mais_fds")

    # 13. cálculo de idade antes do aniversário
    def test_13_calculo_idade_antes_aniversario(self):
        dob = "2000-08-15"
        ref = date(2026, 8, 1)  # Aniversário em 15 de agosto ainda não ocorreu
        idade = templates_aniversario.calcular_idade_campanha(dob, ref)
        self.assertEqual(idade, 25)

    # 14. cálculo no dia do aniversário
    def test_14_calculo_no_dia_do_aniversario(self):
        dob = "2000-08-01"
        ref = date(2026, 8, 1)  # Hoje é o aniversário
        idade = templates_aniversario.calcular_idade_campanha(dob, ref)
        self.assertEqual(idade, 26)

    # 15. data 29 de fevereiro
    def test_15_data_29_fevereiro(self):
        dob = "2004-02-29"
        ref = date(2026, 3, 1)  # 2026 não é bissexto
        idade = templates_aniversario.calcular_idade_campanha(dob, ref)
        self.assertEqual(idade, 22)

    # 16. menor de 18 bloqueado
    def test_16_menor_de_18_bloqueado(self):
        cli = self._base_cliente(data_nascimento="2010-05-15")  # 16 anos em 2026
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertFalse(res["elegivel"])
        self.assertEqual(res["motivo_bloqueio"], "menor_de_18")
        self.assertIsNone(res["template_name"])

    # 17. nome completo gera primeiro nome
    def test_17_nome_completo_gera_primeiro_nome(self):
        self.assertEqual(templates_aniversario.obter_primeiro_nome("   Maria   Silva   "), "Maria")
        self.assertEqual(templates_aniversario.obter_primeiro_nome("Rodrigo Bolgheroni"), "Rodrigo")

    # 18. nome vazio bloqueado
    def test_18_nome_vazio_bloqueado(self):
        cli = self._base_cliente(nome="   ")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertFalse(res["elegivel"])
        self.assertEqual(res["motivo_bloqueio"], "nome_ausente")

    # 19. telefone inválido bloqueado
    def test_19_telefone_invalido_bloqueado(self):
        cli = self._base_cliente(telefone="abc")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertFalse(res["elegivel"])
        self.assertEqual(res["motivo_bloqueio"], "telefone_invalido")

    # 20. categoria ausente bloqueada
    def test_20_categoria_ausente_bloqueada(self):
        cli = self._base_cliente(categoria="")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertFalse(res["elegivel"])
        self.assertEqual(res["motivo_bloqueio"], "categoria_ausente")

    # 21. categoria inválida bloqueada
    def test_21_categoria_invalida_bloqueada(self):
        cli = self._base_cliente(categoria="desconhecido")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertFalse(res["elegivel"])
        self.assertEqual(res["motivo_bloqueio"], "categoria_invalida")

    # 22. perfil inativo bloqueado
    def test_22_perfil_inativo_bloqueado(self):
        cli = self._base_cliente(ativo=False)
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertFalse(res["elegivel"])
        self.assertEqual(res["motivo_bloqueio"], "cliente_inativo")

    # 23. opt-out bloqueia elegibilidade
    def test_23_opt_out_bloqueia_elegibilidade(self):
        cli = self._base_cliente(opt_out=True)
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertFalse(res["elegivel"])
        self.assertEqual(res["motivo_bloqueio"], "opt_out")

    # 24. marketing não autorizado bloqueia
    def test_24_marketing_nao_autorizado_bloqueia(self):
        cli = self._base_cliente(autoriza_marketing=False)
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertFalse(res["elegivel"])
        self.assertEqual(res["motivo_bloqueio"], "marketing_nao_autorizado")

    # 25. bloco ausente não escolhe template
    def test_25_bloco_ausente_nao_escolhe_template(self):
        # Segunda-feira (2026-08-03) não possui bloco definido
        cli = self._base_cliente()
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-03")
        self.assertFalse(res["elegivel"])
        self.assertEqual(res["motivo_bloqueio"], "bloco_nao_definido")

    # 26. nenhum template é escolhido aleatoriamente
    def test_26_nenhum_template_escolhido_aleatoriamente(self):
        cli = self._base_cliente(data_nascimento="")
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertFalse(res["elegivel"])
        self.assertIsNone(res["template_name"])

    # 27. prévia não chama WhatsApp
    def test_27_previa_nao_chama_whatsapp(self):
        from scripts.config_server import ConfigHandler
        from services import whatsapp
        with patch.object(whatsapp, "enviar") as mock_env, patch.object(whatsapp, "enviar_com_resultado") as mock_env_res:
            cli = self._base_cliente()
            res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
            self.assertTrue(res["elegivel"])
            mock_env.assert_not_called()
            mock_env_res.assert_not_called()

    # 28. prévia não registra disparo
    def test_28_previa_nao_registra_disparo(self):
        from services import disparador
        with patch.object(disparador, "_registrar_disparo_supabase") as mock_reg:
            cli = self._base_cliente()
            res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
            self.assertTrue(res["elegivel"])
            mock_reg.assert_not_called()

    # 29. dados existentes do perfil são reutilizados
    def test_29_dados_existentes_do_perfil_sao_reutilizados(self):
        cli = {
            "id": "cli-888",
            "nome": "Fernanda Lima",
            "telefone": "5511988887777",
            "metadata": {
                "categoria": "feminino",
                "data_nascimento": "1990-12-05",
            }
        }
        res = templates_aniversario.selecionar_template_aniversario(cli, data_referencia="2026-08-06", bloco_campanha="qua_qui")
        self.assertTrue(res["elegivel"])
        self.assertEqual(res["categoria"], "f")
        self.assertEqual(res["faixa"], "25_35")
        self.assertEqual(res["template_name"], "aniversario_f_25_35_qua_qui")

    # 30. reforço de aniversário está mapeado como constante
    def test_30_reforco_aniversario_mapeado(self):
        self.assertEqual(templates_aniversario.TEMPLATE_REFORCO_ANIVERSARIO, "aniversario_reforco_sem_resposta")


if __name__ == "__main__":
    unittest.main()
