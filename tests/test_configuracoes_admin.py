from __future__ import annotations

import copy
import logging
import os
import unittest
from http import HTTPStatus
from unittest.mock import patch

from scripts import config_server
from services import configuracoes_admin


class SupabaseAdminFake:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, object]]] = {
            configuracoes_admin.TABELA_ESTABELECIMENTOS: [
                {
                    "id": "est-1",
                    "slug": "praia-da-radial",
                    "nome": "Praia da Radial",
                    "telefone": None,
                    "whatsapp": None,
                    "endereco": "Rua Guapeperuvu, 56",
                    "ponto_referencia": "Proximo a Estacao Penha",
                    "timezone": "America/Sao_Paulo",
                    "ativo": True,
                    "created_at": "2026-07-23T00:00:00Z",
                }
            ],
            configuracoes_admin.TABELA_HORARIOS_FUNCIONAMENTO: [
                {"id": f"hor-{dia}", "estabelecimento_id": "est-1", "dia_semana": dia, "fechado": dia in {1, 2}, "horario_abertura": None if dia in {1, 2} else "12:00:00", "horario_fechamento": None if dia in {1, 2} else "22:00:00", "observacao": None, "ativo": True}
                for dia in range(7)
            ],
            configuracoes_admin.TABELA_CONFIGURACOES_RESERVA: [
                {
                    "id": "res-1",
                    "estabelecimento_id": "est-1",
                    "quantidade_minima": 11,
                    "quantidade_maxima_automatica": 30,
                    "horarios_permitidos": ["12:00", "18:00", "19:00"],
                    "taxa_valor": "50.00",
                    "taxa_convertida_consumacao": True,
                    "prazo_cancelamento_horas": 24,
                    "pix_chave": None,
                    "pix_titular": None,
                    "exige_comprovante": True,
                    "tolerancia_atraso_minutos": 15,
                    "politica_cancelamento": "Estorno ate 24h antes.",
                    "instrucoes_reserva": "Comprovante Pix obrigatorio.",
                    "ativo": True,
                }
            ],
            configuracoes_admin.TABELA_ESPACOS: [
                {"id": "esp-1", "estabelecimento_id": "est-1", "nome": "Salao", "descricao": "Area interna", "capacidade_maxima": 25, "permite_preferencia": True, "regras": "Limite 25", "ativo": True},
                {"id": "esp-2", "estabelecimento_id": "est-2", "nome": "Outro", "descricao": None, "capacidade_maxima": None, "permite_preferencia": True, "regras": None, "ativo": True},
            ],
            configuracoes_admin.TABELA_FAQ_CONTEUDOS: [
                {"id": "faq-1", "estabelecimento_id": "est-1", "categoria": "estacionamento", "titulo": "Estacionamento", "conteudo": "Tem vagas proximas.", "tags": ["carro"], "ativo": True},
                {"id": "faq-2", "estabelecimento_id": "est-1", "categoria": "kids", "titulo": "Espaco kids", "conteudo": "Confirmar com a equipe.", "tags": ["criancas"], "ativo": False},
                {"id": "faq-3", "estabelecimento_id": "est-2", "categoria": "outro", "titulo": "Outro", "conteudo": "Outro", "tags": [], "ativo": True},
            ],
        }
        self.upsert_calls = 0
        self.update_calls = 0

    def selecionar(self, tabela: str, *, filtros=None, colunas="*", limite=None, offset=None, contar=False, order=None):
        items = [copy.deepcopy(item) for item in self.tables.get(tabela, [])]
        for campo, filtro in (filtros or {}).items():
            if not isinstance(filtro, str) or not filtro.startswith("eq."):
                continue
            esperado = filtro[3:]
            if esperado == "true":
                items = [item for item in items if item.get(campo) is True]
            elif esperado == "false":
                items = [item for item in items if item.get(campo) is False]
            else:
                items = [item for item in items if str(item.get(campo) or "") == esperado]
        if order:
            for trecho in reversed(order.split(",")):
                campo = trecho.split(".", 1)[0]
                items.sort(key=lambda item: (item.get(campo) is None, item.get(campo)))
        if offset:
            items = items[offset:]
        if limite is not None:
            items = items[:limite]
        return {"ok": True, "data": items, "total": len(items) if contar else None}

    def atualizar(self, tabela: str, payload: dict, *, filtros: dict, retornar: bool = True):
        self.update_calls += 1
        atualizados = []
        for item in self.tables.get(tabela, []):
            if self._match(item, filtros):
                item.update(copy.deepcopy(payload))
                atualizados.append(copy.deepcopy(item))
        return {"ok": True, "data": atualizados}

    def inserir(self, tabela: str, payload: dict, *, retornar: bool = True):
        item = copy.deepcopy(payload)
        item.setdefault("id", f"new-{len(self.tables.get(tabela, [])) + 1}")
        self.tables.setdefault(tabela, []).append(item)
        return {"ok": True, "data": [copy.deepcopy(item)]}

    def upsert(self, tabela: str, payload: list[dict], *, on_conflict: str, retornar: bool = True):
        self.upsert_calls += 1
        chaves = [chave.strip() for chave in on_conflict.split(",")]
        for novo in payload:
            existente = next(
                (
                    item
                    for item in self.tables.setdefault(tabela, [])
                    if all(item.get(chave) == novo.get(chave) for chave in chaves)
                ),
                None,
            )
            if existente is None:
                item = copy.deepcopy(novo)
                item.setdefault("id", f"upsert-{len(self.tables[tabela]) + 1}")
                self.tables[tabela].append(item)
            else:
                existente.update(copy.deepcopy(novo))
        return {"ok": True, "data": copy.deepcopy(payload)}

    def _match(self, item: dict, filtros: dict) -> bool:
        for campo, filtro in filtros.items():
            if not str(filtro).startswith("eq."):
                continue
            if str(item.get(campo) or "") != str(filtro)[3:]:
                return False
        return True


class ConfiguracoesAdminServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = SupabaseAdminFake()
        self.patches = [
            patch.object(configuracoes_admin.supabase, "selecionar", side_effect=self.fake.selecionar),
            patch.object(configuracoes_admin.supabase, "atualizar", side_effect=self.fake.atualizar),
            patch.object(configuracoes_admin.supabase, "inserir", side_effect=self.fake.inserir),
            patch.object(configuracoes_admin.supabase, "upsert", side_effect=self.fake.upsert),
        ]
        for patcher in self.patches:
            patcher.start()
        self.limpar_cache = patch.object(configuracoes_admin.config_restaurante, "limpar_cache_config")
        self.limpar_cache_mock = self.limpar_cache.start()

    def tearDown(self) -> None:
        self.limpar_cache.stop()
        for patcher in reversed(self.patches):
            patcher.stop()

    def test_estabelecimento_leitura_e_atualizacao_valida_limpa_cache(self) -> None:
        leitura = configuracoes_admin.obter_estabelecimento()
        self.assertTrue(leitura["ok"])
        self.assertEqual(leitura["data"]["nome"], "Praia da Radial")

        atualizado = configuracoes_admin.atualizar_estabelecimento({"nome": "Praia Nova", "timezone": "America/Sao_Paulo"})

        self.assertTrue(atualizado["ok"])
        self.assertEqual(atualizado["data"]["nome"], "Praia Nova")
        self.limpar_cache_mock.assert_called_once()

    def test_estabelecimento_rejeita_campos_imutaveis_timezone_invalido_e_inexistente(self) -> None:
        self.assertFalse(configuracoes_admin.atualizar_estabelecimento({"id": "novo"})["ok"])
        self.assertEqual(configuracoes_admin.atualizar_estabelecimento({"timezone": "Mars/Olympus"})["erro"], "timezone_invalido")
        self.fake.tables[configuracoes_admin.TABELA_ESTABELECIMENTOS][0]["ativo"] = False
        inexistente = configuracoes_admin.obter_estabelecimento()
        self.assertEqual(inexistente["_status"], 404)

    def test_horarios_leitura_sete_dias_ordenados_e_atualizacao_valida(self) -> None:
        leitura = configuracoes_admin.listar_horarios()
        self.assertEqual([item["dia_semana"] for item in leitura["items"]], list(range(7)))

        items = [
            {"dia_semana": dia, "fechado": dia in {1, 2}, "horario_abertura": None if dia in {1, 2} else "12:00", "horario_fechamento": None if dia in {1, 2} else ("01:00" if dia == 6 else "22:00"), "observacao": None, "ativo": True}
            for dia in range(7)
        ]
        resposta = configuracoes_admin.atualizar_horarios({"items": items})

        self.assertTrue(resposta["ok"])
        self.assertEqual(self.fake.upsert_calls, 1)
        self.limpar_cache_mock.assert_called_once()

    def test_horarios_validam_dia_duplicado_fora_aberto_sem_horario_e_horario_invalido(self) -> None:
        base = [
            {"dia_semana": dia, "fechado": False, "horario_abertura": "12:00", "horario_fechamento": "22:00", "ativo": True}
            for dia in range(7)
        ]
        duplicado = copy.deepcopy(base)
        duplicado[1]["dia_semana"] = 0
        self.assertEqual(configuracoes_admin.atualizar_horarios({"items": duplicado})["erro"], "dia_semana_duplicado")
        fora = copy.deepcopy(base)
        fora[0]["dia_semana"] = 9
        self.assertEqual(configuracoes_admin.atualizar_horarios({"items": fora})["erro"], "dia_semana_invalido")
        sem_horario = copy.deepcopy(base)
        sem_horario[0]["horario_abertura"] = None
        self.assertEqual(configuracoes_admin.atualizar_horarios({"items": sem_horario})["erro"], "horario_obrigatorio")
        invalido = copy.deepcopy(base)
        invalido[0]["horario_abertura"] = "25:00"
        self.assertEqual(configuracoes_admin.atualizar_horarios({"items": invalido})["erro"], "horario_invalido")
        self.fake.upsert = lambda *args, **kwargs: {"ok": False, "status": 500, "tabela": "horarios_funcionamento", "detalhe": "falha"}
        with patch.object(configuracoes_admin.supabase, "upsert", side_effect=self.fake.upsert):
            resposta = configuracoes_admin.atualizar_horarios({"items": base})
        self.assertFalse(resposta["ok"])
        self.limpar_cache_mock.assert_not_called()

    def test_reserva_leitura_atualizacao_normaliza_horarios_e_valida_regras(self) -> None:
        leitura = configuracoes_admin.obter_configuracao_reserva()
        self.assertTrue(leitura["ok"])

        atualizado = configuracoes_admin.atualizar_configuracao_reserva(
            {
                "quantidade_minima": 10,
                "quantidade_maxima_automatica": 30,
                "horarios_permitidos": ["19:00", "12:00", "19:00"],
                "taxa_valor": 60,
                "pix_chave": "pix-secreto",
            }
        )

        self.assertTrue(atualizado["ok"])
        self.assertEqual(atualizado["data"]["horarios_permitidos"], ["12:00", "19:00"])
        self.assertEqual(atualizado["data"]["pix_chave"], "pix-secreto")
        self.limpar_cache_mock.assert_called_once()

    def test_reserva_rejeita_quantidade_valor_horario_e_maximo_menor_que_minimo(self) -> None:
        self.assertEqual(configuracoes_admin.atualizar_configuracao_reserva({"quantidade_minima": -1})["erro"], "quantidade_minima_invalido")
        self.assertEqual(
            configuracoes_admin.atualizar_configuracao_reserva({"quantidade_minima": 20, "quantidade_maxima_automatica": 10})["erro"],
            "quantidade_maxima_invalida",
        )
        self.assertEqual(configuracoes_admin.atualizar_configuracao_reserva({"horarios_permitidos": ["99:00"]})["erro"], "horario_invalido")
        self.assertEqual(configuracoes_admin.atualizar_configuracao_reserva({"taxa_valor": -10})["erro"], "taxa_valor_invalida")

    def test_pix_nao_aparece_em_logs(self) -> None:
        logger = logging.getLogger(configuracoes_admin.__name__)
        with patch.object(logger, "warning") as warning:
            resposta = configuracoes_admin.atualizar_configuracao_reserva({"pix_chave": "pix-super-secreto"})

        self.assertTrue(resposta["ok"])
        textos = " ".join(str(arg) for chamada in warning.call_args_list for arg in chamada.args)
        self.assertNotIn("pix-super-secreto", textos)

    def test_espacos_criacao_edicao_exclusao_logica_e_validacoes(self) -> None:
        criado = configuracoes_admin.criar_espaco({"nome": "Areia", "capacidade_maxima": None})
        self.assertEqual(criado["_status"], 201)
        self.assertEqual(criado["data"]["nome"], "Areia")

        editado = configuracoes_admin.atualizar_espaco(criado["data"]["id"], {"descricao": "Area externa", "capacidade_maxima": 40})
        self.assertTrue(editado["ok"])
        self.assertEqual(editado["data"]["capacidade_maxima"], 40)

        excluido = configuracoes_admin.excluir_espaco(criado["data"]["id"])
        self.assertTrue(excluido["ok"])
        self.assertFalse(excluido["data"]["ativo"])

        self.assertEqual(configuracoes_admin.criar_espaco({"nome": " salao "})["erro"], "espaco_duplicado")
        self.assertEqual(configuracoes_admin.criar_espaco({"nome": "Novo", "capacidade_maxima": 0})["erro"], "capacidade_maxima_invalido")
        self.assertEqual(configuracoes_admin.atualizar_espaco("esp-2", {"nome": "Outro"})["_status"], 404)
        self.assertEqual(configuracoes_admin.excluir_espaco("nao-existe")["_status"], 404)

    def test_faq_listagem_filtros_criacao_edicao_exclusao_tags_e_validacoes(self) -> None:
        lista = configuracoes_admin.listar_faqs({"page": 1, "page_size": 1})
        self.assertEqual(lista["page_size"], 1)
        self.assertEqual(lista["total"], 2)
        self.assertTrue(lista["has_next"])

        por_categoria = configuracoes_admin.listar_faqs({"categoria": "estacionamento"})
        self.assertEqual(por_categoria["total"], 1)
        inativos = configuracoes_admin.listar_faqs({"ativo": "false"})
        self.assertEqual(inativos["items"][0]["id"], "faq-2")
        busca = configuracoes_admin.listar_faqs({"search": "vagas"})
        self.assertEqual(busca["items"][0]["id"], "faq-1")

        criado = configuracoes_admin.criar_faq(
            {
                "categoria": "bolo",
                "titulo": "Bolo",
                "conteudo": "Pode levar bolo?",
                "tags": [" aniversario ", "Aniversario", "bolo"],
            }
        )
        self.assertEqual(criado["_status"], 201)
        self.assertEqual(criado["data"]["tags"], ["aniversario", "bolo"])

        editado = configuracoes_admin.atualizar_faq(criado["data"]["id"], {"conteudo": "Confirmar com a equipe."})
        self.assertEqual(editado["data"]["conteudo"], "Confirmar com a equipe.")
        excluido = configuracoes_admin.excluir_faq(criado["data"]["id"])
        self.assertFalse(excluido["data"]["ativo"])

        self.assertEqual(configuracoes_admin.criar_faq({"titulo": "Sem categoria", "conteudo": "Texto"})["erro"], "categoria_obrigatorio")
        self.assertEqual(configuracoes_admin.criar_faq({"categoria": "x", "titulo": "x", "conteudo": "x", "tags": "tag"})["erro"], "tags_invalidas")
        self.assertEqual(configuracoes_admin.atualizar_faq("faq-3", {"titulo": "Outro"})["_status"], 404)

    def test_proxima_leitura_reflete_dados_atualizados(self) -> None:
        configuracoes_admin.atualizar_estabelecimento({"nome": "Praia Atualizada"})
        leitura = configuracoes_admin.obter_estabelecimento()

        self.assertEqual(leitura["data"]["nome"], "Praia Atualizada")


class ConfigServerConfiguracoesAdminTest(unittest.TestCase):
    def _handler(self, *, path: str = "/api/configuracoes/estabelecimento", headers: dict[str, str] | None = None):
        handler = object.__new__(config_server.ConfigHandler)
        handler.path = path
        handler.headers = headers or {}
        return handler

    def test_auth_sem_token_configurado_token_invalido_e_valido(self) -> None:
        sem_config: list[tuple[dict, object]] = []
        handler = self._handler(headers={"Authorization": "Bearer recebido"})
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: sem_config.append((payload, status))),
        ):
            config_server.ConfigHandler._config_admin_ler(handler, lambda: {"ok": True})
        self.assertEqual(sem_config[0][1], HTTPStatus.FORBIDDEN)

        sem_header: list[tuple[dict, object]] = []
        handler = self._handler()
        with (
            patch.dict(os.environ, {"CONFIG_ADMIN_TOKEN": "correto"}, clear=True),
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: sem_header.append((payload, status))),
        ):
            config_server.ConfigHandler._config_admin_ler(handler, lambda: {"ok": True})
        self.assertEqual(sem_header[0][1], HTTPStatus.UNAUTHORIZED)

        invalido: list[tuple[dict, object]] = []
        handler = self._handler(headers={"X-Config-Admin-Token": "errado"})
        with (
            patch.dict(os.environ, {"CONFIG_ADMIN_TOKEN": "correto"}, clear=True),
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: invalido.append((payload, status))),
        ):
            config_server.ConfigHandler._config_admin_ler(handler, lambda: {"ok": True})
        self.assertEqual(invalido[0][1], HTTPStatus.FORBIDDEN)

        valido: list[tuple[dict, object]] = []
        handler = self._handler(headers={"Authorization": "Bearer correto"})
        with (
            patch.dict(os.environ, {"CONFIG_ADMIN_TOKEN": "correto"}, clear=True),
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: valido.append((payload, status))),
        ):
            config_server.ConfigHandler._config_admin_ler(handler, lambda: {"ok": True, "data": {"id": "est-1"}})
        self.assertEqual(valido[0][0]["ok"], True)
        self.assertNotIn("correto", str(valido[0][0]))

        supabase_auth: list[tuple[dict, object]] = []
        handler = self._handler(headers={"Authorization": "Bearer jwt-supabase"})
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(config_server, "_validar_token_supabase", return_value=True) as validar,
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: supabase_auth.append((payload, status))),
        ):
            config_server.ConfigHandler._config_admin_ler(handler, lambda: {"ok": True, "data": {"id": "est-1"}})
        validar.assert_called_once_with("jwt-supabase")
        self.assertEqual(supabase_auth[0][0]["ok"], True)

    def test_rotas_chamam_services_e_respeitam_status(self) -> None:
        respostas: list[tuple[dict, object]] = []
        handler = self._handler(path="/api/configuracoes/faqs?page=1&page_size=30", headers={"Authorization": "Bearer token"})

        with (
            patch.dict(os.environ, {"CONFIG_ADMIN_TOKEN": "token"}, clear=True),
            patch.object(config_server.configuracoes_admin, "listar_faqs", return_value={"ok": True, "items": []}) as listar,
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: respostas.append((payload, status))),
        ):
            config_server.ConfigHandler.do_GET(handler)

        listar.assert_called_once_with({"page": "1", "page_size": "30"})
        self.assertEqual(respostas[0][0]["items"], [])

        respostas.clear()
        handler = self._handler(path="/api/configuracoes/espacos", headers={"X-Config-Admin-Token": "token"})
        with (
            patch.dict(os.environ, {"CONFIG_ADMIN_TOKEN": "token"}, clear=True),
            patch.object(config_server.ConfigHandler, "_ler_json_body", return_value={"nome": "Areia"}),
            patch.object(config_server.configuracoes_admin, "criar_espaco", return_value={"ok": True, "data": {"id": "esp-1"}, "_status": 201}),
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: respostas.append((payload, status))),
        ):
            config_server.ConfigHandler.do_POST(handler)
        self.assertEqual(respostas[0][1], HTTPStatus.CREATED)

        respostas.clear()
        handler = self._handler(path="/api/configuracoes/espacos/esp-1", headers={"Authorization": "Bearer token"})
        with (
            patch.dict(os.environ, {"CONFIG_ADMIN_TOKEN": "token"}, clear=True),
            patch.object(config_server.ConfigHandler, "_ler_json_body", return_value={"nome": "Salao"}),
            patch.object(config_server.configuracoes_admin, "atualizar_espaco", return_value={"ok": True, "data": {"id": "esp-1"}}) as atualizar,
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: respostas.append((payload, status))),
        ):
            config_server.ConfigHandler.do_PUT(handler)
        atualizar.assert_called_once_with("esp-1", {"nome": "Salao"})

        respostas.clear()
        handler = self._handler(path="/api/configuracoes/faqs/faq-1", headers={"Authorization": "Bearer token"})
        with (
            patch.dict(os.environ, {"CONFIG_ADMIN_TOKEN": "token"}, clear=True),
            patch.object(config_server.configuracoes_admin, "excluir_faq", return_value={"ok": True, "data": {"id": "faq-1", "ativo": False}}) as excluir,
            patch.object(config_server.ConfigHandler, "_responder_json", lambda _self, payload, status=None: respostas.append((payload, status))),
        ):
            config_server.ConfigHandler.do_DELETE(handler)
        excluir.assert_called_once_with("faq-1")
        self.assertFalse(respostas[0][0]["data"]["ativo"])


if __name__ == "__main__":
    unittest.main()
