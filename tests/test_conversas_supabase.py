from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from services import conversas_supabase


class ConversasSupabaseTest(unittest.TestCase):
    def test_listar_conversas_monta_itens_com_ultima_mensagem_e_cliente(self) -> None:
        chamadas: list[tuple[str, dict[str, Any]]] = []

        def selecionar(tabela: str, **kwargs: Any) -> dict[str, Any]:
            chamadas.append((tabela, kwargs))
            if tabela == "conversas":
                return {
                    "ok": True,
                    "total": 2,
                    "data": [
                        {
                            "id": "conv-1",
                            "cliente_id": "cli-1",
                            "cliente_telefone": "5511991111111",
                            "status": "bot_ativo",
                            "origem": "aniversario",
                            "data_inicio": "2026-07-23T10:00:00Z",
                            "created_at": "2026-07-23T10:00:00Z",
                            "updated_at": "2026-07-23T10:00:00Z",
                            "metadata": {"nao_lidas": 2},
                        },
                        {
                            "id": "conv-2",
                            "cliente_id": "cli-2",
                            "cliente_telefone": "5511992222222",
                            "status": "humano",
                            "origem": "webhook",
                            "data_inicio": "2026-07-23T09:00:00Z",
                            "created_at": "2026-07-23T09:00:00Z",
                            "updated_at": "2026-07-23T09:00:00Z",
                            "metadata": {},
                        },
                    ],
                }
            if tabela == "mensagens":
                return {
                    "ok": True,
                    "data": [
                        {
                            "id": "msg-2",
                            "conversa_id": "conv-2",
                            "remetente": "bot",
                            "conteudo": "Confirmado.",
                            "timestamp": "2026-07-23T10:07:00Z",
                            "provider_message_id": "wamid.2",
                            "metadata": {"status_entrega": "entregue"},
                        },
                        {
                            "id": "msg-1",
                            "conversa_id": "conv-1",
                            "remetente": "cliente",
                            "conteudo": "Agora pode confirmar",
                            "timestamp": "2026-07-23T10:05:00Z",
                            "provider_message_id": "wamid.1",
                            "metadata": {},
                        },
                    ],
                }
            if tabela == "clientes":
                return {
                    "ok": True,
                    "data": [
                        {"id": "cli-1", "nome": "Rodrigo", "telefone": "5511991111111"},
                        {"id": "cli-2", "nome": "Maria", "telefone": "5511992222222"},
                    ],
                }
            raise AssertionError(tabela)

        with patch.object(conversas_supabase.supabase, "selecionar", side_effect=selecionar):
            resultado = conversas_supabase.listar_conversas(page=1, page_size=30)

        self.assertEqual(resultado["total"], 2)
        self.assertEqual([item["id"] for item in resultado["items"]], ["conv-2", "conv-1"])
        self.assertEqual(resultado["items"][0]["cliente"]["nome"], "Maria")
        self.assertEqual(resultado["items"][0]["ultima_mensagem"]["status"], "entregue")
        self.assertEqual(resultado["items"][1]["nao_lidas"], 2)
        self.assertTrue(any(tabela == "mensagens" for tabela, _ in chamadas))
        self.assertTrue(any(tabela == "clientes" for tabela, _ in chamadas))

    def test_listar_mensagens_conversa_responde_no_formato_do_front(self) -> None:
        def selecionar(tabela: str, **kwargs: Any) -> dict[str, Any]:
            if tabela == "conversas":
                return {
                    "ok": True,
                    "data": [
                        {
                            "id": "conv-1",
                            "cliente_id": "cli-1",
                            "cliente_telefone": "5511991111111",
                            "status": "bot_ativo",
                            "origem": "aniversario",
                            "data_inicio": "2026-07-23T10:00:00Z",
                            "created_at": "2026-07-23T10:00:00Z",
                            "updated_at": "2026-07-23T10:00:00Z",
                        }
                    ],
                }
            if tabela == "clientes":
                return {"ok": True, "data": [{"id": "cli-1", "nome": "Rodrigo", "telefone": "5511991111111"}]}
            if tabela == "mensagens":
                return {
                    "ok": True,
                    "data": [
                        {
                            "id": "msg-1",
                            "conversa_id": "conv-1",
                            "remetente": "cliente",
                            "conteudo": "Quero reservar",
                            "timestamp": "2026-07-23T10:01:00Z",
                            "provider_message_id": None,
                            "metadata": {},
                        },
                        {
                            "id": "msg-2",
                            "conversa_id": "conv-1",
                            "remetente": "bot",
                            "conteudo": "Qual dia?",
                            "timestamp": "2026-07-23T10:02:00Z",
                            "provider_message_id": "wamid.2",
                            "metadata": {"status_entrega": "lido"},
                        },
                    ],
                }
            raise AssertionError(tabela)

        with patch.object(conversas_supabase.supabase, "selecionar", side_effect=selecionar):
            resultado = conversas_supabase.listar_mensagens_conversa("conv-1")

        assert resultado is not None
        self.assertEqual(resultado["cliente"]["nome"], "Rodrigo")
        self.assertEqual(resultado["conversa"]["status"], "bot_ativo")
        self.assertEqual([item["texto"] for item in resultado["mensagens"]], ["Quero reservar", "Qual dia?"])
        self.assertEqual(resultado["mensagens"][1]["status"], "lido")


if __name__ == "__main__":
    unittest.main()
