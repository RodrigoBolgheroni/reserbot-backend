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

    def test_listar_mensagens_expoe_imagem_e_pdf_sem_dados_privados(self) -> None:
        def selecionar(tabela: str, **kwargs: Any) -> dict[str, Any]:
            if tabela == "conversas":
                return {
                    "ok": True,
                    "data": [{"id": "conv-1", "cliente_id": "cli-1", "status": "aguardando_humano"}],
                }
            if tabela == "clientes":
                return {"ok": True, "data": [{"id": "cli-1", "nome": "Rodrigo", "telefone": "5511991111111"}]}
            if tabela == "mensagens":
                return {
                    "ok": True,
                    "data": [
                        {
                            "id": "msg-img",
                            "conversa_id": "conv-1",
                            "remetente": "cliente",
                            "conteudo": "Imagem recebida",
                            "provider_message_id": "wamid.img",
                            "metadata": {
                                "tipo": "image",
                                "comprovante_id": "comp-img",
                                "bucket": "nao-expor",
                                "storage_path": "privado/imagem.jpg",
                            },
                        },
                        {
                            "id": "msg-pdf",
                            "conversa_id": "conv-1",
                            "remetente": "cliente",
                            "conteudo": "Documento recebido",
                            "provider_message_id": "wamid.pdf",
                            "metadata": {"tipo": "document", "comprovante_id": "comp-pdf"},
                        },
                    ],
                }
            raise AssertionError(tabela)

        comprovantes = [
            {
                "id": "comp-img",
                "provider_message_id": "wamid.img",
                "mime_type": "image/jpeg",
                "nome_original": "pix.jpg",
                "tamanho_bytes": 1234,
                "status_analise": "aguardando_analise",
                "disponivel": True,
            },
            {
                "id": "comp-pdf",
                "provider_message_id": "wamid.pdf",
                "mime_type": "application/pdf",
                "nome_original": "pix.pdf",
                "tamanho_bytes": 4321,
                "status_analise": "aprovado",
                "disponivel": True,
            },
        ]
        with (
            patch.object(conversas_supabase.supabase, "selecionar", side_effect=selecionar),
            patch.object(conversas_supabase.comprovantes_reserva, "listar_por_conversa", return_value=comprovantes),
        ):
            resultado = conversas_supabase.listar_mensagens_conversa("conv-1")

        assert resultado is not None
        imagem, pdf = resultado["mensagens"]
        self.assertEqual(imagem["tipo"], "image")
        self.assertEqual(imagem["media"]["mime_type"], "image/jpeg")
        self.assertEqual(imagem["media"]["filename"], "pix.jpg")
        self.assertEqual(imagem["media"]["tamanho"], 1234)
        self.assertEqual(imagem["media"]["comprovante_status"], "aguardando_analise")
        self.assertEqual(imagem["media"]["url_endpoint"], "/api/mensagens/msg-img/midia?conversa_id=conv-1")
        self.assertEqual(pdf["tipo"], "document")
        self.assertEqual(pdf["media"]["mime_type"], "application/pdf")
        self.assertEqual(pdf["media"]["comprovante_status"], "aprovado")
        serializado = str(resultado)
        self.assertNotIn("nao-expor", serializado)
        self.assertNotIn("privado/imagem.jpg", serializado)

    def test_mensagem_comum_nao_dispara_consulta_de_comprovantes(self) -> None:
        mensagem = {"id": "msg-1", "conversa_id": "conv-1", "conteudo": "Ola", "metadata": {}}
        resumo = conversas_supabase._resumir_mensagem(mensagem)
        self.assertNotIn("media", resumo)
        self.assertNotIn("tipo", resumo)


if __name__ == "__main__":
    unittest.main()
