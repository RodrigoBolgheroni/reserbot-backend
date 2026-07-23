from __future__ import annotations

import json
import os
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from services import agente, config_restaurante, repositorio_config_restaurante as repositorio


ESTABELECIMENTO_ID = "00000000-0000-0000-0000-000000000001"


def _config_bruta(**overrides: object) -> dict:
    dados = {
        "ok": True,
        "estabelecimento": {
            "id": ESTABELECIMENTO_ID,
            "slug": "praia-da-radial",
            "nome": "Praia da Radial",
            "telefone": None,
            "whatsapp": None,
            "endereco": "Rua Guapeperuvu, 56 - Vila Aricanduva, Sao Paulo - SP",
            "ponto_referencia": "proximo a Estacao Penha do Metro",
            "timezone": "America/Sao_Paulo",
            "ativo": True,
        },
        "horarios": [
            {"dia_semana": 1, "fechado": True, "horario_abertura": None, "horario_fechamento": None, "observacao": "Fechado", "ativo": True},
            {"dia_semana": 2, "fechado": True, "horario_abertura": None, "horario_fechamento": None, "observacao": "Fechado", "ativo": True},
            {"dia_semana": 3, "fechado": False, "horario_abertura": "17:00:00", "horario_fechamento": "22:00:00", "observacao": None, "ativo": True},
            {"dia_semana": 4, "fechado": False, "horario_abertura": "12:00:00", "horario_fechamento": "23:00:00", "observacao": None, "ativo": True},
            {"dia_semana": 5, "fechado": False, "horario_abertura": "12:00:00", "horario_fechamento": "00:00:00", "observacao": "Fecha a meia-noite", "ativo": True},
            {"dia_semana": 6, "fechado": False, "horario_abertura": "12:00:00", "horario_fechamento": "01:00:00", "observacao": "Fecha no dia seguinte", "ativo": True},
            {"dia_semana": 0, "fechado": False, "horario_abertura": "12:00:00", "horario_fechamento": "22:00:00", "observacao": None, "ativo": True},
        ],
        "configuracao_reserva": {
            "quantidade_minima": 11,
            "quantidade_maxima_automatica": 30,
            "horarios_permitidos": ["12:00", "13:00", "14:00", "18:00", "19:00"],
            "taxa_valor": "50.00",
            "taxa_convertida_consumacao": True,
            "prazo_cancelamento_horas": 24,
            "pix_chave": "",
            "pix_titular": "",
            "exige_comprovante": True,
            "tolerancia_atraso_minutos": 15,
            "politica_cancelamento": "Cancelamento com estorno ate 24 horas antes da reserva.",
            "instrucoes_reserva": "Reservas acima de 10 pessoas exigem comprovante Pix.",
            "ativo": True,
        },
        "espacos": [
            {
                "id": "espaco-1",
                "nome": "Salao",
                "descricao": "Area interna.",
                "capacidade_maxima": 25,
                "permite_preferencia": True,
                "regras": "Limite operacional do salao.",
                "ativo": True,
            },
            {
                "id": "espaco-2",
                "nome": "Areia",
                "descricao": "Area externa.",
                "capacidade_maxima": None,
                "permite_preferencia": True,
                "regras": "Direcionamento operacional para areia.",
                "ativo": True,
            },
        ],
        "faq_conteudos": [
            {
                "categoria": "estacionamento",
                "titulo": "Estacionamento",
                "conteudo": "Informacao de estacionamento ainda precisa ser confirmada com a equipe.",
                "tags": ["estacionamento"],
                "ativo": True,
            },
            {
                "categoria": "aniversario",
                "titulo": "Bolo e decoracao",
                "conteudo": "Informacoes sobre bolo e decoracao devem ser confirmadas com a equipe.",
                "tags": ["bolo", "decoracao"],
                "ativo": True,
            },
        ],
        "erros_parciais": [],
    }
    dados.update(overrides)
    return dados


def _resposta_supabase_repo(tabela: str, **_: object) -> dict:
    if tabela == repositorio.TABELA_ESTABELECIMENTOS:
        return {
            "ok": True,
            "data": [
                {
                    "id": ESTABELECIMENTO_ID,
                    "slug": "praia-da-radial",
                    "nome": "Praia da Radial",
                    "telefone": None,
                    "whatsapp": None,
                    "endereco": "Rua Guapeperuvu, 56 - Vila Aricanduva, Sao Paulo - SP",
                    "ponto_referencia": "proximo a Estacao Penha do Metro",
                    "timezone": "America/Sao_Paulo",
                    "ativo": True,
                }
            ],
        }
    if tabela == repositorio.TABELA_HORARIOS_FUNCIONAMENTO:
        return {"ok": True, "data": _config_bruta()["horarios"]}
    if tabela == repositorio.TABELA_CONFIGURACOES_RESERVA:
        return {"ok": True, "data": [_config_bruta()["configuracao_reserva"]]}
    if tabela == repositorio.TABELA_ESPACOS:
        return {"ok": True, "data": _config_bruta()["espacos"]}
    if tabela == repositorio.TABELA_FAQ_CONTEUDOS:
        return {"ok": True, "data": _config_bruta()["faq_conteudos"]}
    return {"ok": False, "erro": f"tabela inesperada: {tabela}"}


class ConfigRestauranteTest(unittest.TestCase):
    def setUp(self) -> None:
        config_restaurante.limpar_cache_config()
        agente._historicos.clear()
        agente._estados_reserva.clear()

    def tearDown(self) -> None:
        config_restaurante.limpar_cache_config()
        agente._historicos.clear()
        agente._estados_reserva.clear()

    def test_carrega_configuracao_estruturada_do_repositorio(self) -> None:
        with patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta()):
            config = config_restaurante.obter_config()

        self.assertEqual(config.fonte, "supabase")
        self.assertEqual(config.nome, "Praia da Radial")
        self.assertIn("Guapeperuvu", config.endereco)
        self.assertEqual(config.timezone, "America/Sao_Paulo")
        self.assertEqual(config.quantidade_minima_reserva, 11)
        self.assertEqual(config.limite_pessoas_reserva, 30)
        self.assertEqual(config.horarios_permitidos_reserva, ("12:00", "13:00", "14:00", "18:00", "19:00"))
        self.assertEqual(config.taxa_valor, 50.0)
        self.assertTrue(config.taxa_convertida_consumacao)
        self.assertTrue(config.exige_comprovante)
        self.assertEqual(config.tolerancia_atraso_minutos, 15)
        self.assertIn("sexta: 12:00-00:00", config.dias_funcionamento)
        self.assertIn("sabado: 12:00-01:00", config.dias_funcionamento)
        self.assertEqual(len(config.espacos), 2)
        self.assertEqual(config.espacos[0].capacidade_maxima, 25)
        self.assertIn("estacionamento", config.estacionamento.lower())

    def test_cache_sucesso_evita_nova_consulta_dentro_do_ttl(self) -> None:
        carregar = Mock(return_value=_config_bruta())
        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", carregar),
            patch.object(config_restaurante.time, "monotonic", return_value=100.0),
        ):
            config_1 = config_restaurante.obter_config()
            config_2 = config_restaurante.obter_config()

        self.assertIs(config_1, config_2)
        self.assertEqual(carregar.call_count, 1)

    def test_cache_usa_lock_para_evitar_montagens_simultaneas(self) -> None:
        carregar = Mock(return_value=_config_bruta())
        resultados = []

        def worker() -> None:
            resultados.append(config_restaurante.obter_config())

        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", carregar),
            patch.object(config_restaurante.time, "monotonic", return_value=100.0),
        ):
            threads = [threading.Thread(target=worker) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(resultados), 5)
        self.assertEqual(carregar.call_count, 1)

    def test_cache_sucesso_expira_apos_60_segundos(self) -> None:
        carregar = Mock(return_value=_config_bruta())
        with patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", carregar):
            with patch.object(config_restaurante.time, "monotonic", return_value=100.0):
                config_restaurante.obter_config()
            with patch.object(config_restaurante.time, "monotonic", return_value=159.0):
                config_restaurante.obter_config()
            with patch.object(config_restaurante.time, "monotonic", return_value=161.0):
                config_restaurante.obter_config()

        self.assertEqual(carregar.call_count, 2)

    def test_fallback_env_fica_em_cache_por_no_maximo_5_segundos(self) -> None:
        carregar = Mock(return_value={"ok": False, "erro_tipo": "timeout", "erro": "request timed out"})
        env = {"NOME_RESTAURANTE": "Fallback", "RESERVA_HORARIO_INICIO": "09:00", "RESERVA_HORARIO_FIM": "22:00"}
        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", carregar),
            patch.dict(os.environ, env, clear=False),
        ):
            with patch.object(config_restaurante.time, "monotonic", return_value=100.0):
                config_1 = config_restaurante.obter_config()
            with patch.object(config_restaurante.time, "monotonic", return_value=104.0):
                config_2 = config_restaurante.obter_config()
            with patch.object(config_restaurante.time, "monotonic", return_value=106.0):
                config_3 = config_restaurante.obter_config()

        self.assertEqual(config_1.fonte, "env")
        self.assertIs(config_1, config_2)
        self.assertEqual(config_3.nome, "Fallback")
        self.assertEqual(carregar.call_count, 2)

    def test_limpar_cache_forca_nova_consulta(self) -> None:
        carregar = Mock(return_value=_config_bruta())
        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", carregar),
            patch.object(config_restaurante.time, "monotonic", return_value=100.0),
        ):
            config_restaurante.obter_config()
            config_restaurante.limpar_cache_config()
            config_restaurante.obter_config()

        self.assertEqual(carregar.call_count, 2)

    def test_configuracao_parcial_nao_derruba_agente(self) -> None:
        parcial = _config_bruta(
            horarios=[],
            configuracao_reserva={},
            espacos=[],
            faq_conteudos=[],
            erros_parciais=[{"tabela": "espacos", "erro_tipo": "timeout", "erro": "timeout"}],
        )
        env = {"RESERVA_HORARIO_INICIO": "10:00", "RESERVA_HORARIO_FIM": "23:00"}
        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=parcial),
            patch.dict(os.environ, env, clear=False),
        ):
            config = config_restaurante.obter_config()

        self.assertEqual(config.fonte, "supabase")
        self.assertEqual(config.horario_abertura, "10:00")
        self.assertEqual(config.horario_fechamento, "23:00")
        self.assertEqual(config.espacos, ())

    def test_estabelecimento_inativo_usa_fallback_env(self) -> None:
        env = {"NOME_RESTAURANTE": "Fallback"}
        with (
            patch.object(
                config_restaurante.repositorio,
                "carregar_configuracao_bruta",
                return_value={"ok": False, "erro_tipo": "sem_estabelecimento_ativo", "erro": "Nenhum ativo"},
            ),
            patch.dict(os.environ, env, clear=False),
        ):
            config = config_restaurante.obter_config()

        self.assertEqual(config.fonte, "env")
        self.assertEqual(config.nome, "Fallback")

    def test_horarios_permitidos_json_invalido_nao_quebra_config(self) -> None:
        configuracao = dict(_config_bruta()["configuracao_reserva"])
        configuracao["horarios_permitidos"] = {"invalido": True}
        with patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta(configuracao_reserva=configuracao)):
            config = config_restaurante.obter_config()

        self.assertEqual(config.horarios_permitidos_reserva, ())
        self.assertIn("entre", config.horarios_aceitos_reserva)

    def test_dia_sem_linha_de_funcionamento_nao_quebra_consulta(self) -> None:
        horarios = [item for item in _config_bruta()["horarios"] if item["dia_semana"] != 1]
        with patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta(horarios=horarios)):
            config = config_restaurante.obter_config()

        self.assertFalse(config_restaurante.fechado_na_data("2026-07-27", config=config))
        self.assertEqual(config_restaurante.horario_funcionamento("2026-07-27", config=config), ("12:00", "01:00"))

    def test_horario_reserva_valida_data_real_de_chegada_e_extensao_da_madrugada(self) -> None:
        with patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta()):
            config = config_restaurante.obter_config()

        casos = [
            ("23:30", "2026-07-24", True, "sexta 23:30 dentro da sexta"),
            ("00:30", "2026-07-25", False, "sabado 00:30 passa pela extensao da sexta, mas sexta fecha 00:00"),
            ("23:30", "2026-07-25", True, "sabado 23:30 dentro do sabado"),
            ("00:30", "2026-07-26", True, "domingo 00:30 como extensao do sabado"),
            ("01:30", "2026-07-26", False, "domingo 01:30 depois do fechamento de sabado"),
            ("00:30", "2026-07-27", False, "segunda 00:30 sem extensao do domingo"),
            ("19:00", "2026-07-27", False, "segunda durante o dia fechado"),
        ]
        for horario, data_reserva, esperado, descricao in casos:
            with self.subTest(descricao=descricao):
                self.assertEqual(
                    agente._horario_reserva_valido(horario, data_reserva=data_reserva, config=config),
                    esperado,
                )

    def test_processamento_de_mensagem_carrega_configuracao_uma_vez(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-30",
            "pessoas": 4,
            "nome_cliente": "Rodrigo",
            "campo_pendente": "horario",
            "etapa": "aguardando_horario",
        }
        payload = {
            "resposta": "20h, anotado. Ficou para 30/07 as 20h, para 4 pessoas. Posso confirmar?",
            "intencao": "fornecimento_dados",
            "dados_confirmados": {"data": None, "horario": "20:00", "quantidade": None, "nome": None},
            "dados_mencionados": {"data": None, "horario": None, "quantidade": None},
            "dados_incertos": {},
            "correcoes": {},
            "acao": "pedir_confirmacao",
            "deve_avancar_estado": True,
            "campo_sugerido": "confirmacao",
            "confianca": 0.9,
        }
        carregar = Mock(return_value=_config_bruta())
        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", carregar),
            patch.object(agente, "_hoje", return_value=date(2026, 7, 23)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}, clear=False),
            patch.object(agente, "_chamar_groq", return_value=json.dumps(payload)),
        ):
            resposta = agente.processar_mensagem(telefone, "20h", nome_cliente="Rodrigo")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(agente._estados_reserva[telefone]["horario"], "20:00")
        self.assertEqual(carregar.call_count, 1)

    def test_migration_preserva_pix_em_reexecucao(self) -> None:
        sql = Path("supabase/20260723_estabelecimentos_mvp.sql").read_text(encoding="utf-8")

        self.assertIn("slug text", sql)
        self.assertIn("on conflict (slug) do update", sql)
        self.assertIn("pix_chave = coalesce(nullif(public.configuracoes_reserva.pix_chave, ''), excluded.pix_chave)", sql)
        self.assertIn("pix_titular = coalesce(nullif(public.configuracoes_reserva.pix_titular, ''), excluded.pix_titular)", sql)
        self.assertIn("on conflict (estabelecimento_id, categoria, titulo) do nothing", sql)
        self.assertIn("reservas acima de 25 pessoas devem ser direcionadas para a Areia", sql)


class RepositorioConfigRestauranteTest(unittest.TestCase):
    def test_repositorio_monta_dados_brutos_e_usa_primeiro_estabelecimento_ativo(self) -> None:
        def selecionar(tabela: str, **kwargs: object) -> dict:
            if tabela == repositorio.TABELA_ESTABELECIMENTOS:
                estabelecimento_1 = dict(_config_bruta()["estabelecimento"])
                estabelecimento_2 = dict(estabelecimento_1)
                estabelecimento_2["id"] = "00000000-0000-0000-0000-000000000002"
                return {"ok": True, "data": [estabelecimento_1, estabelecimento_2]}
            return _resposta_supabase_repo(tabela, **kwargs)

        with (
            patch.object(repositorio.supabase, "configurado", return_value=True),
            patch.object(repositorio.supabase, "selecionar", side_effect=selecionar) as selecionar_mock,
        ):
            resultado = repositorio.carregar_configuracao_bruta()

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["estabelecimento"]["id"], ESTABELECIMENTO_ID)
        self.assertEqual(selecionar_mock.call_count, 5)

    def test_repositorio_diferencia_401_404_timeout_e_erro_generico(self) -> None:
        casos = [
            ({"ok": False, "status": 401, "detalhe": "invalid api key"}, "autenticacao"),
            ({"ok": False, "status": 404, "detalhe": "relation does not exist"}, "tabela_inexistente"),
            ({"ok": False, "detalhe": "request timed out"}, "timeout"),
            ({"ok": False, "status": 500, "detalhe": "erro qualquer"}, "erro_generico"),
        ]
        for resposta, tipo in casos:
            with self.subTest(tipo=tipo):
                with (
                    patch.object(repositorio.supabase, "configurado", return_value=True),
                    patch.object(repositorio.supabase, "selecionar", return_value=resposta),
                ):
                    resultado = repositorio.carregar_configuracao_bruta()

                self.assertFalse(resultado["ok"])
                self.assertEqual(resultado["erro_tipo"], tipo)

    def test_repositorio_404_parcial_nao_derruba_estabelecimento(self) -> None:
        def selecionar(tabela: str, **kwargs: object) -> dict:
            if tabela == repositorio.TABELA_ESPACOS:
                return {"ok": False, "status": 404, "detalhe": "relation does not exist"}
            return _resposta_supabase_repo(tabela, **kwargs)

        with (
            patch.object(repositorio.supabase, "configurado", return_value=True),
            patch.object(repositorio.supabase, "selecionar", side_effect=selecionar),
        ):
            resultado = repositorio.carregar_configuracao_bruta()

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["espacos"], [])
        self.assertEqual(resultado["erros_parciais"][0]["erro_tipo"], "tabela_inexistente")


if __name__ == "__main__":
    unittest.main()
