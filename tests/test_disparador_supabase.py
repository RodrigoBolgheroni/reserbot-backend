from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from services import disparador


class DisparadorSupabaseTest(unittest.TestCase):
    def test_disparo_usa_supabase_quando_configurado(self) -> None:
        clientes = [
            {
                "nome": "Rodrigo Teste",
                "telefone": "5511999999999",
                "data_nascimento": "1990-06-25",
                "aniversario_ddmm": "25/06",
                "perfil_nome": "VIP",
            }
        ]
        with (
            patch.object(disparador.supabase, "configurado", return_value=True),
            patch.object(disparador.planilha, "obter_aniversariantes_do_dia") as planilha,
            patch.object(
                disparador.clientes_supabase,
                "listar_aniversarios_proximos",
                return_value={"dias": 15, "total": 1, "clientes": clientes, "analisados": 200},
            ) as listar,
            patch.object(disparador.dados, "ja_enviado", return_value=False),
            patch.object(disparador.dados, "marcar_enviado", return_value=True),
            patch.object(
                disparador.mensagens,
                "gerar_mensagem_aniversario",
                return_value={"texto": "Feliz aniversario!", "perfil": "VIP", "idade": 36},
            ),
            patch.object(
                disparador.whatsapp,
                "enviar_com_resultado",
                return_value={"ok": True, "provider": "cloud", "provider_message_id": "wamid.1"},
            ) as enviar,
            patch.object(disparador.fluxo_reservas, "iniciar_conversa", return_value={"id": "conversa-1"}),
        ):
            resultados = disparador.executar_disparo_diario(dias=15, data_referencia=date(2026, 6, 22))

        planilha.assert_not_called()
        listar.assert_called_once()
        enviar.assert_called_once_with("5511999999999", "Feliz aniversario!")
        self.assertEqual(resultados[0]["status"], "enviado")
        self.assertTrue(resultados[0]["enviado"])

    def test_disparo_com_telefone_envia_somente_para_cliente_filtrado(self) -> None:
        clientes = [
            {"nome": "Ana", "telefone": "5511000000001"},
            {"nome": "Rodrigo Teste", "telefone": "5511999999999"},
        ]
        with (
            patch.object(disparador.supabase, "configurado", return_value=True),
            patch.object(
                disparador.clientes_supabase,
                "listar_aniversarios_proximos",
                return_value={"dias": 15, "total": 2, "clientes": clientes, "analisados": 200},
            ),
            patch.object(disparador.dados, "ja_enviado", return_value=False),
            patch.object(disparador.dados, "marcar_enviado", return_value=True),
            patch.object(
                disparador.mensagens,
                "gerar_mensagem_aniversario",
                return_value={"texto": "Feliz aniversario!", "perfil": "padrao", "idade": None},
            ),
            patch.object(disparador.whatsapp, "enviar_com_resultado", return_value={"ok": True, "provider": "cloud"}) as enviar,
            patch.object(disparador.fluxo_reservas, "iniciar_conversa", return_value={"id": "conversa-1"}),
        ):
            resultados = disparador.executar_disparo_diario(
                dias=15,
                telefone="5511999999999",
                somente_teste=True,
                data_referencia=date(2026, 6, 22),
            )

        enviar.assert_called_once_with("5511999999999", "Feliz aniversario!")
        self.assertEqual(len(resultados), 1)
        self.assertEqual(resultados[0]["telefone"], "5511999999999")


if __name__ == "__main__":
    unittest.main()
