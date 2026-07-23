from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import config_server


class ConfigServerClientesTest(unittest.TestCase):
    def test_paginacao_clientes_page_1(self) -> None:
        page, page_size, offset = config_server._paginacao_clientes("/api/clientes?page=1&page_size=100")

        self.assertEqual(page, 1)
        self.assertEqual(page_size, 100)
        self.assertEqual(offset, 0)

    def test_paginacao_clientes_page_2(self) -> None:
        page, page_size, offset = config_server._paginacao_clientes("/api/clientes?page=2&page_size=100")

        self.assertEqual(page, 2)
        self.assertEqual(page_size, 100)
        self.assertEqual(offset, 100)

    def test_paginacao_clientes_limita_page_size_maximo(self) -> None:
        page, page_size, offset = config_server._paginacao_clientes("/api/clientes?page=1&page_size=9999")

        self.assertEqual(page, 1)
        self.assertEqual(page_size, 500)
        self.assertEqual(offset, 0)

    def test_paginacao_clientes_aceita_limit_e_offset(self) -> None:
        page, page_size, offset = config_server._paginacao_clientes("/api/clientes?limit=200&offset=400")

        self.assertEqual(page, 3)
        self.assertEqual(page_size, 200)
        self.assertEqual(offset, 400)

    def test_listar_clientes_responde_metadados_de_paginacao(self) -> None:
        handler = object.__new__(config_server.ConfigHandler)
        handler.path = "/api/clientes?page=2&page_size=100"
        respostas: list[dict[str, object]] = []

        with (
            patch.object(
                config_server.clientes_supabase,
                "listar_clientes",
                return_value={"clientes": [{"nome": "Cliente"}], "total": 1234},
            ) as listar,
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: respostas.append(payload)),
        ):
            config_server.ConfigHandler._listar_clientes(handler)

        listar.assert_called_once_with(limite=100, offset=100, contar=True)
        self.assertEqual(respostas[0]["page"], 2)
        self.assertEqual(respostas[0]["page_size"], 100)
        self.assertEqual(respostas[0]["total"], 1234)
        self.assertEqual(respostas[0]["total_pages"], 13)
        self.assertTrue(respostas[0]["has_next"])
        self.assertTrue(respostas[0]["has_prev"])

    def test_listar_aniversarios_proximos_responde_dados_globais(self) -> None:
        handler = object.__new__(config_server.ConfigHandler)
        handler.path = "/api/clientes/aniversarios-proximos?dias=15"
        respostas: list[dict[str, object]] = []

        with (
            patch.object(
                config_server.clientes_supabase,
                "listar_aniversarios_proximos",
                return_value={"dias": 15, "total": 42, "clientes": [{"nome": "Maria"}], "analisados": 1234},
            ) as listar,
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: respostas.append(payload)),
        ):
            config_server.ConfigHandler._listar_aniversarios_proximos(handler)

        listar.assert_called_once_with(dias=15, limite_clientes=50)
        self.assertEqual(respostas[0]["ok"], True)
        self.assertEqual(respostas[0]["dias"], 15)
        self.assertEqual(respostas[0]["total"], 42)
        self.assertEqual(respostas[0]["clientes"], [{"nome": "Maria"}])

    def test_disparar_aniversarios_aceita_payload_de_teste(self) -> None:
        handler = object.__new__(config_server.ConfigHandler)
        respostas: list[dict[str, object]] = []

        with (
            patch.object(
                config_server.ConfigHandler,
                "_ler_json_body",
                return_value={"dias": 15, "telefone": "5511999999999", "modo_teste": True, "forcar_reenvio": True},
            ),
            patch.object(
                config_server.disparador,
                "executar_disparo_diario",
                return_value=[{"nome": "Rodrigo Teste", "telefone": "5511999999999", "status": "reenviado_teste", "enviado": True, "reenviado_teste": True}],
            ) as executar,
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: respostas.append(payload)),
        ):
            config_server.ConfigHandler._disparar_aniversarios(handler)

        executar.assert_called_once_with(
            dias=15,
            telefone="5511999999999",
            somente_teste=True,
            modo_teste=True,
            forcar_reenvio=True,
        )
        self.assertEqual(respostas[0]["ok"], True)
        self.assertEqual(respostas[0]["total_encontrados"], 1)
        self.assertEqual(respostas[0]["total_enviados"], 1)
        self.assertEqual(respostas[0]["falhas"], 0)
        self.assertEqual(respostas[0]["reenviados_teste"], 1)

    def test_disparar_aniversarios_nao_forca_reenvio_sem_telefone(self) -> None:
        handler = object.__new__(config_server.ConfigHandler)
        respostas: list[tuple[dict[str, object], object]] = []

        with (
            patch.object(config_server.ConfigHandler, "_ler_json_body", return_value={"forcar_reenvio": True}),
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: respostas.append((payload, status))),
        ):
            config_server.ConfigHandler._disparar_aniversarios(handler)

        self.assertEqual(respostas[0][0]["ok"], False)
        self.assertEqual(respostas[0][1], config_server.HTTPStatus.BAD_REQUEST)

    def test_paginacao_conversas_limita_page_size(self) -> None:
        page, page_size = config_server._paginacao_conversas("/api/conversas?page=2&page_size=999")

        self.assertEqual(page, 2)
        self.assertEqual(page_size, 100)

    def test_listar_conversas_repassa_filtros_para_servico(self) -> None:
        handler = object.__new__(config_server.ConfigHandler)
        handler.path = "/api/conversas?page=2&page_size=30&search=rodrigo&status=bot_ativo"
        respostas: list[dict[str, object]] = []

        with (
            patch.object(
                config_server.conversas_supabase,
                "listar_conversas",
                return_value={"items": [{"id": "conv-1"}], "page": 2, "page_size": 30, "total": 40, "has_next": True, "has_prev": True},
            ) as listar,
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: respostas.append(payload)),
        ):
            config_server.ConfigHandler._listar_conversas(handler)

        listar.assert_called_once_with(page=2, page_size=30, search="rodrigo", status="bot_ativo")
        self.assertEqual(respostas[0]["ok"], True)
        self.assertEqual(respostas[0]["items"], [{"id": "conv-1"}])

    def test_listar_mensagens_conversa_responde_404_quando_nao_existe(self) -> None:
        handler = object.__new__(config_server.ConfigHandler)
        respostas: list[tuple[config_server.HTTPStatus, str]] = []

        with (
            patch.object(config_server.conversas_supabase, "listar_mensagens_conversa", return_value=None),
            patch.object(config_server.ConfigHandler, "_responder_erro", lambda _self, status, mensagem: respostas.append((status, mensagem))),
        ):
            config_server.ConfigHandler._obter_conversa_ou_mensagens(handler, "/api/conversas/conv-1/mensagens")

        self.assertEqual(respostas[0][0], config_server.HTTPStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
