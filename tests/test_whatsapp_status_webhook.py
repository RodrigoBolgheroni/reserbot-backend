from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import config_server
from services import fluxo_reservas, whatsapp_cloud


class WhatsAppStatusWebhookTest(unittest.TestCase):
    def test_extrai_status_failed_do_payload_meta(self) -> None:
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": "wamid.123",
                                        "status": "failed",
                                        "timestamp": "1710000000",
                                        "recipient_id": "5511999999999",
                                        "errors": [
                                            {
                                                "code": 131026,
                                                "title": "Message undeliverable",
                                                "message": "Message undeliverable",
                                                "error_data": {"details": "Recipient not reachable"},
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }

        statuses = whatsapp_cloud.extrair_status_webhook(payload)

        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["message_id"], "wamid.123")
        self.assertEqual(statuses[0]["status"], "failed")
        self.assertEqual(statuses[0]["recipient_id"], "5511999999999")
        self.assertEqual(statuses[0]["errors"][0]["code"], 131026)

    def test_processa_status_atualiza_disparo_e_mensagem(self) -> None:
        with (
            patch.object(fluxo_reservas, "_atualizar_disparo_status", return_value=True) as atualizar_disparo,
            patch.object(fluxo_reservas, "_atualizar_mensagem_status", return_value=True) as atualizar_mensagem,
        ):
            resultado = fluxo_reservas.processar_status_whatsapp(
                {
                    "message_id": "wamid.123",
                    "status": "delivered",
                    "timestamp": "2026-06-22T10:00:00+00:00",
                    "recipient_id": "5511999999999",
                    "errors": [],
                }
            )

        atualizar_disparo.assert_called_once()
        atualizar_mensagem.assert_called_once()
        self.assertEqual(resultado["status_interno"], "entregue")
        self.assertEqual(resultado["atualizacoes"], 2)

    def test_webhook_de_status_nao_chama_processamento_de_mensagem(self) -> None:
        handler = object.__new__(config_server.ConfigHandler)
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": "wamid.123",
                                        "status": "read",
                                        "timestamp": "1710000000",
                                        "recipient_id": "5511999999999",
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        respostas: list[dict[str, object]] = []

        with (
            patch.object(config_server.ConfigHandler, "_ler_json_body", return_value=payload),
            patch.object(config_server.fluxo_reservas, "processar_status_whatsapp", return_value={"ok": True, "message_id": "wamid.123"}),
            patch.object(config_server.fluxo_reservas, "processar_mensagem_webhook") as processar_mensagem,
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: respostas.append(payload)),
        ):
            config_server.ConfigHandler._receber_webhook_whatsapp(handler)

        processar_mensagem.assert_not_called()
        self.assertEqual(respostas[0]["statuses_recebidos"], 1)
        self.assertEqual(respostas[0]["recebidas"], 0)


if __name__ == "__main__":
    unittest.main()
