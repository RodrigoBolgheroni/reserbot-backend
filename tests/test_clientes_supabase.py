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

    def test_salvar_clientes_mescla_duplicados_por_telefone_no_mesmo_lote(self) -> None:
        registros = [
            {"nome": "Ana", "telefone": "5511999999999", "telefones": ["5511999999999"]},
            {
                "nome": "Ana Cliente Completo",
                "telefone": "5511999999999",
                "data_nascimento": "1990-06-22",
                "aniversario_ddmm": "22/06",
                "regiao": "Centro",
                "metadata": {"linha_original": "Ana Cliente Completo ..."},
            },
        ]
        lotes_enviados: list[list[dict[str, object]]] = []

        def upsert(_tabela: str, payload: list[dict[str, object]], *, on_conflict: str) -> dict[str, object]:
            lotes_enviados.append(payload)
            telefones = [item["telefone"] for item in payload]
            self.assertEqual(len(telefones), len(set(telefones)))
            return {"ok": True, "data": payload}

        with (
            patch.object(clientes_supabase.perfis, "listar_perfis", return_value=[]),
            patch.object(clientes_supabase.perfis, "classificar_cliente", return_value=None),
            patch.object(clientes_supabase.supabase, "upsert", side_effect=upsert),
        ):
            resultado = clientes_supabase.salvar_clientes(registros, tabela="clientes")

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["enviados"], 1)
        self.assertEqual(resultado["salvos"], 1)
        self.assertEqual(resultado["duplicados"], 1)
        self.assertEqual(len(lotes_enviados[0]), 1)
        cliente = lotes_enviados[0][0]
        self.assertEqual(cliente["nome"], "Ana Cliente Completo")
        self.assertEqual(cliente["data_nascimento"], "1990-06-22")
        self.assertEqual(cliente["aniversario_ddmm"], "22/06")
        self.assertEqual(cliente["regiao"], "Centro")

    def test_salvar_clientes_remove_duplicados_antes_de_dividir_lotes(self) -> None:
        registros = [
            {"nome": "Cliente 0", "telefone": "5511000000000"},
            {"nome": "Cliente 1", "telefone": "5511000000001"},
            {"nome": "Cliente 0 Atualizado", "telefone": "5511000000000", "regiao": "Sul"},
        ]
        lotes_enviados: list[list[dict[str, object]]] = []

        def upsert(_tabela: str, payload: list[dict[str, object]], *, on_conflict: str) -> dict[str, object]:
            lotes_enviados.append(payload)
            telefones = [item["telefone"] for item in payload]
            self.assertEqual(len(telefones), len(set(telefones)))
            return {"ok": True, "data": payload}

        with (
            patch.dict(os.environ, {"SUPABASE_CLIENTES_BATCH_SIZE": "1"}),
            patch.object(clientes_supabase.perfis, "listar_perfis", return_value=[]),
            patch.object(clientes_supabase.perfis, "classificar_cliente", return_value=None),
            patch.object(clientes_supabase.supabase, "upsert", side_effect=upsert),
        ):
            resultado = clientes_supabase.salvar_clientes(registros, tabela="clientes")

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["enviados"], 2)
        self.assertEqual(resultado["salvos"], 2)
        self.assertEqual(resultado["duplicados"], 1)
        self.assertEqual([len(lote) for lote in lotes_enviados], [1, 1])

    def test_salvar_clientes_prefere_perfil_com_maior_score_ao_mesclar(self) -> None:
        perfil_baixo = {"id": "11111111-1111-1111-1111-111111111111", "nome": "Baixo", "metadata": {"score_classificacao": 3}}
        perfil_alto = {"id": "22222222-2222-2222-2222-222222222222", "nome": "Alto", "metadata": {"score_classificacao": 9}}
        registros = [
            {"nome": "Cliente Perfil", "telefone": "5511999999999", "tipo": "baixo"},
            {"nome": "Cliente Perfil", "telefone": "5511999999999", "tipo": "alto"},
        ]
        lotes_enviados: list[list[dict[str, object]]] = []

        def classificar(registro: dict[str, object], *, perfis_disponiveis: object) -> dict[str, object]:
            return perfil_alto if registro["tipo"] == "alto" else perfil_baixo

        def upsert(_tabela: str, payload: list[dict[str, object]], *, on_conflict: str) -> dict[str, object]:
            lotes_enviados.append(payload)
            return {"ok": True, "data": payload}

        with (
            patch.object(clientes_supabase.perfis, "listar_perfis", return_value=[perfil_baixo, perfil_alto]),
            patch.object(clientes_supabase.perfis, "classificar_cliente", side_effect=classificar),
            patch.object(clientes_supabase.supabase, "upsert", side_effect=upsert),
        ):
            resultado = clientes_supabase.salvar_clientes(registros, tabela="clientes")

        self.assertTrue(resultado["ok"])
        cliente = lotes_enviados[0][0]
        self.assertEqual(cliente["perfil_id"], perfil_alto["id"])
        self.assertEqual(cliente["perfil_nome"], perfil_alto["nome"])

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
