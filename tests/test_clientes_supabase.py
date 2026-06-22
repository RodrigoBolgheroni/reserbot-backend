from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services import clientes_supabase


class ClientesSupabaseTest(unittest.TestCase):
    def test_preparar_registros_mantem_mesmas_chaves_com_e_sem_perfil(self) -> None:
        perfil = {"id": "11111111-1111-1111-1111-111111111111", "nome": "VIP", "metadata": {"score_classificacao": 9}}
        registros = [
            {
                "nome": "Ana Cliente",
                "telefone": "5511999999999",
                "telefones": ["5511999999999"],
                "data_nascimento": "1990-06-22",
                "campo_extra_parser": "nao deve virar coluna",
                "metadata": {"linha_original": "Ana Cliente ..."},
            },
            {
                "nome": "Bruno Cliente",
                "telefone": "5511888888888",
                "data_nascimento": "1985-06-22",
            },
        ]
        lotes_enviados: list[list[dict[str, object]]] = []

        def classificar(registro: dict[str, object], *, perfis_disponiveis: object) -> dict[str, object] | None:
            return perfil if registro["nome"] == "Ana Cliente" else None

        def upsert(_tabela: str, payload: list[dict[str, object]], *, on_conflict: str) -> dict[str, object]:
            lotes_enviados.append(payload)
            return {"ok": True, "data": payload}

        with (
            patch.object(clientes_supabase.perfis, "listar_perfis", return_value=[perfil]),
            patch.object(clientes_supabase.perfis, "classificar_cliente", side_effect=classificar),
            patch.object(clientes_supabase.supabase, "upsert", side_effect=upsert),
        ):
            resultado = clientes_supabase.salvar_clientes(registros, tabela="clientes")

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["salvos"], 2)
        self.assertEqual(len(lotes_enviados), 1)
        primeiro, segundo = lotes_enviados[0]
        self.assertEqual(set(primeiro), set(segundo))
        self.assertEqual(tuple(primeiro), clientes_supabase.CAMPOS_CLIENTES)
        self.assertNotIn("campo_extra_parser", primeiro)
        self.assertEqual(primeiro["perfil_id"], perfil["id"])
        self.assertEqual(primeiro["perfil_nome"], perfil["nome"])
        self.assertIsNone(segundo["perfil_id"])
        self.assertIsNone(segundo["perfil_nome"])

    def test_salvar_clientes_envia_em_lotes_normalizados(self) -> None:
        registros = [
            {"nome": f"Cliente {indice}", "telefone": f"55110000000{indice}", "data_nascimento": "1990-01-01"}
            for indice in range(3)
        ]
        tamanhos: list[int] = []

        def upsert(_tabela: str, payload: list[dict[str, object]], *, on_conflict: str) -> dict[str, object]:
            tamanhos.append(len(payload))
            for item in payload:
                self.assertEqual(tuple(item), clientes_supabase.CAMPOS_CLIENTES)
            return {"ok": True, "data": payload}

        with (
            patch.dict(os.environ, {"SUPABASE_CLIENTES_BATCH_SIZE": "2"}),
            patch.object(clientes_supabase.perfis, "listar_perfis", return_value=[]),
            patch.object(clientes_supabase.perfis, "classificar_cliente", return_value=None),
            patch.object(clientes_supabase.supabase, "upsert", side_effect=upsert),
        ):
            resultado = clientes_supabase.salvar_clientes(registros, tabela="clientes")

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["salvos"], 3)
        self.assertEqual(tamanhos, [2, 1])

    def test_salvar_clientes_retorna_erro_claro_do_supabase(self) -> None:
        with (
            patch.object(clientes_supabase.perfis, "listar_perfis", return_value=[]),
            patch.object(clientes_supabase.perfis, "classificar_cliente", return_value=None),
            patch.object(
                clientes_supabase.supabase,
                "upsert",
                return_value={"ok": False, "erro": "Supabase retornou HTTP 400", "detalhe": "All object keys must match"},
            ),
        ):
            resultado = clientes_supabase.salvar_clientes(
                [{"nome": "Ana Cliente", "telefone": "5511999999999"}],
                tabela="clientes",
            )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["erro"], "Nao foi possivel salvar os clientes importados.")
        self.assertEqual(resultado["detalhe"], "All object keys must match")


if __name__ == "__main__":
    unittest.main()
