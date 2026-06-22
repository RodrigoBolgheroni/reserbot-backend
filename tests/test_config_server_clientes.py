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


if __name__ == "__main__":
    unittest.main()
