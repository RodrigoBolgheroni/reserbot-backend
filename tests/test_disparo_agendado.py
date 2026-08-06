from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from services import disparador


DATA = date(2026, 8, 6)
CHAVE = "disparo-2026-08-06-19h-3-clientes"


def _cliente(indice: int) -> dict[str, str]:
    return {
        "id": f"00000000-0000-0000-0000-00000000000{indice}",
        "nome": f"Cliente {indice}",
        "telefone": f"551199999000{indice}",
        "perfil_id": f"perfil-{indice}",
        "perfil_nome": "Perfil aprovado",
    }


def _preparado(cliente: dict[str, str]) -> dict[str, object]:
    indice = cliente["telefone"][-1]
    return {
        "ok": True,
        "cliente": cliente,
        "telefone": cliente["telefone"],
        "template_name": f"aniversario_m_25_35_qua_qui_{indice}",
        "language": "pt_BR",
        "primeiro_nome": cliente["nome"],
        "selecao": {
            "categoria": "m",
            "faixa": "25_35",
            "bloco": "qua_qui",
            "perfil_nome": cliente["perfil_nome"],
        },
    }


class DisparoAgendadoTest(unittest.TestCase):
    def test_dry_run_valida_exatamente_tres_sem_enviar(self) -> None:
        clientes = [_cliente(1), _cliente(2), _cliente(3)]
        with (
            patch.object(disparador, "_preparar_template_agendado", side_effect=lambda cliente, **_: _preparado(cliente)),
            patch.object(disparador.whatsapp, "enviar_template") as enviar,
            patch.object(disparador.supabase, "inserir") as inserir,
        ):
            resultado = disparador.executar_disparo_agendado(
                clientes,
                data_referencia=DATA,
                chave_idempotencia=CHAVE,
                horario_programado="2026-08-06T19:00:00-03:00",
                dry_run=True,
            )

        self.assertTrue(resultado["ok"])
        self.assertTrue(resultado["dry_run"])
        self.assertEqual(resultado["total_destinatarios"], 3)
        self.assertEqual(len(resultado["validacoes"]), 3)
        self.assertEqual(resultado["confirmacao"], "nenhum envio ocorreu")
        self.assertTrue(all("****" in item["cliente_id_mascarado"] for item in resultado["validacoes"]))
        enviar.assert_not_called()
        inserir.assert_not_called()

    def test_quantidade_diferente_de_tres_aborta_antes_de_validar(self) -> None:
        with patch.object(disparador, "_preparar_template_agendado") as preparar:
            resultado = disparador.executar_disparo_agendado(
                [_cliente(1), _cliente(2)],
                data_referencia=DATA,
                chave_idempotencia=CHAVE,
                horario_programado="2026-08-06T19:00:00-03:00",
                dry_run=True,
            )

        self.assertFalse(resultado["ok"])
        self.assertTrue(resultado["aborted"])
        preparar.assert_not_called()

    def test_claim_real_e_segunda_execucao_nao_reenvia(self) -> None:
        clientes = [_cliente(1), _cliente(2), _cliente(3)]
        contador_claim = 0

        def inserir(*args, **kwargs):
            nonlocal contador_claim
            contador_claim += 1
            if contador_claim <= 3:
                return {"ok": True, "data": [{"id": f"disp-{contador_claim}"}]}
            return {"ok": False, "status": 409, "erro": "conflito de unicidade"}

        with (
            patch.object(disparador, "_preparar_template_agendado", side_effect=lambda cliente, **_: _preparado(cliente)),
            patch.object(disparador.supabase, "configurado", return_value=True),
            patch.object(disparador.supabase, "inserir", side_effect=inserir) as inserir_mock,
            patch.object(disparador.supabase, "atualizar", return_value={"ok": True}) as atualizar,
            patch.object(disparador.whatsapp, "enviar_template", return_value={"ok": True, "provider": "cloud", "provider_message_id": "wamid.1"}) as enviar,
            patch.object(disparador.fluxo_reservas, "iniciar_conversa", side_effect=lambda *args, **kwargs: {"id": "conv-1"}),
        ):
            primeira = disparador.executar_disparo_agendado(
                clientes,
                data_referencia=DATA,
                chave_idempotencia=CHAVE,
                horario_programado="2026-08-06T19:00:00-03:00",
                dry_run=False,
            )
            segunda = disparador.executar_disparo_agendado(
                clientes,
                data_referencia=DATA,
                chave_idempotencia=CHAVE,
                horario_programado="2026-08-06T19:00:00-03:00",
                dry_run=False,
            )

        self.assertTrue(primeira["ok"])
        self.assertEqual(primeira["enviados"], 3)
        self.assertTrue(segunda["ok"])
        self.assertTrue(all(item["status"] == "pulado" for item in segunda["resultados"]))
        self.assertEqual(inserir_mock.call_count, 6)
        self.assertEqual(enviar.call_count, 3)
        self.assertGreaterEqual(atualizar.call_count, 6)
        primeiro_payload = inserir_mock.call_args_list[0].args[1]
        self.assertFalse(primeiro_payload["modo_teste"])
        self.assertEqual(primeiro_payload["status"], "pendente")
        self.assertEqual(primeiro_payload["metadata"]["chave_idempotencia"], CHAVE)


if __name__ == "__main__":
    unittest.main()
