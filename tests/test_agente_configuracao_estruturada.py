from __future__ import annotations

import json
import os
import unittest
from datetime import date
from unittest.mock import patch

from services import agente, config_restaurante


ESTABELECIMENTO_ID = "00000000-0000-0000-0000-000000000001"


def _config_bruta_reserva() -> dict:
    return {
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
            {"dia_semana": 5, "fechado": False, "horario_abertura": "12:00:00", "horario_fechamento": "00:00:00", "observacao": None, "ativo": True},
            {"dia_semana": 6, "fechado": False, "horario_abertura": "12:00:00", "horario_fechamento": "01:00:00", "observacao": None, "ativo": True},
            {"dia_semana": 0, "fechado": False, "horario_abertura": "12:00:00", "horario_fechamento": "22:00:00", "observacao": None, "ativo": True},
        ],
        "configuracao_reserva": {
            "quantidade_minima": 11,
            "quantidade_maxima_automatica": 30,
            "horarios_permitidos": ["12:00", "13:00", "14:00", "18:00", "19:00"],
            "taxa_valor": "50.00",
            "taxa_convertida_consumacao": True,
            "prazo_cancelamento_horas": 24,
            "pix_chave": "pix-nao-deve-aparecer-em-log-ou-prompt",
            "pix_titular": "Titular Teste",
            "exige_comprovante": True,
            "tolerancia_atraso_minutos": 15,
            "politica_cancelamento": "Cancelamento com estorno ate 24 horas antes da reserva.",
            "instrucoes_reserva": "Reservas acima de 10 pessoas exigem comprovante Pix.",
            "ativo": True,
        },
        "espacos": [
            {
                "id": "espaco-salao",
                "nome": "Salao",
                "descricao": "Area interna.",
                "capacidade_maxima": 25,
                "permite_preferencia": True,
                "regras": "Sabados e domingos, reservas acima de 25 pessoas devem ser direcionadas para a Areia. As 18h e 19h nao ha garantia de preferencia de local.",
                "ativo": True,
            },
            {
                "id": "espaco-areia",
                "nome": "Areia",
                "descricao": "Area externa.",
                "capacidade_maxima": None,
                "permite_preferencia": True,
                "regras": "Para 20 pessoas as 19h, Salao e Areia podem ser possiveis conforme disponibilidade.",
                "ativo": True,
            },
        ],
        "faq_conteudos": [
            {
                "categoria": "esportes",
                "titulo": "Locacao de quadras",
                "conteudo": "Temos locacao de quadras para esportes e Day Use conforme programacao.",
                "tags": ["quadra", "quadras", "locacao", "esportes", "day use"],
                "ativo": True,
            },
            {
                "categoria": "aniversario",
                "titulo": "Bolo e utensilios",
                "conteudo": "Pode levar bolo, mas utensilios e detalhes precisam ser combinados com a equipe.",
                "tags": ["bolo", "aniversario", "decoracao"],
                "ativo": True,
            },
            {
                "categoria": "entrada",
                "titulo": "Valores de entrada",
                "conteudo": "Os valores de entrada variam por dia e condicao. Consulte a equipe para o valor atualizado.",
                "tags": ["entrada", "valor", "domingo", "criancas"],
                "ativo": True,
            },
            {
                "categoria": "espacos",
                "titulo": "Regras dos espacos",
                "conteudo": "Salao e areia dependem de disponibilidade; em horarios de 18h e 19h nao ha garantia de preferencia.",
                "tags": ["salao", "areia", "preferencia", "local"],
                "ativo": True,
            },
            {
                "categoria": "musica",
                "titulo": "Programacao musical",
                "conteudo": "A programacao musical muda conforme a agenda da casa.",
                "tags": ["musica", "programacao"],
                "ativo": True,
            },
            {
                "categoria": "gympass",
                "titulo": "Gympass",
                "conteudo": "Informacoes sobre Gympass devem ser confirmadas com a equipe.",
                "tags": ["gympass"],
                "ativo": True,
            },
        ],
        "erros_parciais": [],
    }


def _payload_ia(texto: str) -> str:
    return json.dumps(
        {
            "resposta": texto,
            "intencao": "pergunta_restaurante",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "dados_incertos": {},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "data",
            "confianca": 0.9,
        },
        ensure_ascii=False,
    )


def _payload_custom(
    texto: str,
    *,
    intencao: str = "pergunta_restaurante",
    dados_confirmados: dict | None = None,
    dados_mencionados: dict | None = None,
    dados_incertos: dict | None = None,
    correcoes: dict | None = None,
    deve_avancar_estado: bool = False,
    campo_sugerido: str | None = None,
) -> str:
    return json.dumps(
        {
            "resposta": texto,
            "intencao": intencao,
            "dados_confirmados": dados_confirmados or {},
            "dados_mencionados": dados_mencionados or {},
            "dados_incertos": dados_incertos or {},
            "correcoes": correcoes or {},
            "acao": "responder",
            "deve_avancar_estado": deve_avancar_estado,
            "campo_sugerido": campo_sugerido,
            "confianca": 0.9,
        },
        ensure_ascii=False,
    )


class AgenteConfiguracaoEstruturadaTest(unittest.TestCase):
    def setUp(self) -> None:
        agente._historicos.clear()
        agente._estados_reserva.clear()
        config_restaurante.limpar_cache_config()

    def tearDown(self) -> None:
        agente._historicos.clear()
        agente._estados_reserva.clear()
        config_restaurante.limpar_cache_config()

    def _processar(self, mensagem: str, resposta_ia: str) -> agente.RespostaAgente:
        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta_reserva()),
            patch.object(agente, "_hoje", return_value=date(2026, 7, 23)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}, clear=False),
            patch.object(agente, "_chamar_groq", return_value=_payload_ia(resposta_ia)),
        ):
            return agente.processar_mensagem("5511993680823", mensagem, nome_cliente="Rodrigo")

    def _capturar_prompt(self, mensagem: str) -> str:
        capturado: dict[str, str] = {}

        def chamar_groq_fake(*, mensagens: list[agente.Mensagem], modelo: str, response_format_json: bool) -> str:
            capturado["prompt"] = mensagens[0]["content"]
            return _payload_ia("Certo, sigo com voce.")

        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta_reserva()),
            patch.object(agente, "_hoje", return_value=date(2026, 7, 23)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}, clear=False),
            patch.object(agente, "_chamar_groq", side_effect=chamar_groq_fake),
        ):
            agente.processar_mensagem("5511993680823", mensagem, nome_cliente="Rodrigo")

        return capturado["prompt"]

    def _config(self) -> config_restaurante.ConfigRestaurante:
        config_restaurante.limpar_cache_config()
        with patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta_reserva()):
            return config_restaurante.obter_config()

    def test_pergunta_valor_reserva_nao_pode_negar_taxa_configurada(self) -> None:
        resposta_ia = "A reserva tem taxa de R$ 50,00, convertida em consumacao, e o comprovante Pix e obrigatorio."
        resposta = self._processar(
            "tem algum valor para reservar?",
            resposta_ia,
        )

        texto = resposta["texto"]
        texto_normalizado = agente._normalizar_busca(texto)
        self.assertEqual(texto, resposta_ia)
        self.assertIn("R$ 50,00", texto)
        self.assertIn("consumacao", texto_normalizado)
        self.assertIn("comprovante pix", texto_normalizado)

    def test_confirmacao_taxa_cinquenta_nao_pode_ser_negada(self) -> None:
        resposta_ia = "Sim, a taxa da reserva e R$ 50,00 e esse valor vira consumacao. O comprovante Pix e obrigatorio."
        resposta = self._processar(
            "vi que voces cobram R$ 50 pela reserva, certo?",
            resposta_ia,
        )

        texto = resposta["texto"]
        texto_normalizado = agente._normalizar_busca(texto)
        self.assertEqual(texto, resposta_ia)
        self.assertIn("R$ 50,00", texto)
        self.assertIn("consumacao", texto_normalizado)
        self.assertIn("comprovante pix", texto_normalizado)

    def test_contexto_enviado_para_ia_contem_configuracao_critica_sem_pix(self) -> None:
        capturado: dict[str, str] = {}

        def chamar_groq_fake(*, mensagens: list[agente.Mensagem], modelo: str, response_format_json: bool) -> str:
            capturado["prompt"] = mensagens[0]["content"]
            return _payload_ia("A reserva tem taxa de R$ 50,00, convertida em consumacao, com comprovante Pix obrigatorio.")

        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta_reserva()),
            patch.object(agente, "_hoje", return_value=date(2026, 7, 23)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}, clear=False),
            patch.object(agente, "_chamar_groq", side_effect=chamar_groq_fake),
            self.assertLogs("services.agente", level="INFO") as logs,
        ):
            agente.processar_mensagem("5511993680823", "tem algum valor para reservar?", nome_cliente="Rodrigo")

        prompt = capturado["prompt"]
        logs_texto = "\n".join(logs.output)
        self.assertIn("Taxa de reserva", prompt)
        self.assertIn("R$ 50,00", prompt)
        self.assertIn("Quantidade minima", prompt)
        self.assertIn("Comprovante de Pix", prompt)
        self.assertIn("Salao", prompt)
        self.assertIn("capacidade maxima 25", prompt)
        self.assertIn("Areia", prompt)
        self.assertNotIn("pix-nao-deve-aparecer", prompt)
        self.assertIn("source=supabase", logs_texto)
        self.assertIn("estabelecimento_id=" + ESTABELECIMENTO_ID, logs_texto)
        self.assertIn("configuracoes_reserva=True", logs_texto)
        self.assertIn("taxa_valor=50.00", logs_texto)
        self.assertIn("quantidade_minima=11", logs_texto)
        self.assertIn("horarios_permitidos=['12:00', '13:00', '14:00', '18:00', '19:00']", logs_texto)
        self.assertNotIn("pix-nao-deve-aparecer", logs_texto)

    def test_faqs_nao_entram_todas_e_pergunta_quadra_seleciona_esportes(self) -> None:
        prompt = self._capturar_prompt("Voces alugam quadra?")

        self.assertIn("Locacao de quadras", prompt)
        self.assertIn("esportes", agente._normalizar_busca(prompt))
        self.assertNotIn("Bolo e utensilios", prompt)
        self.assertNotIn("Programacao musical", prompt)

    def test_pergunta_bolo_seleciona_faq_de_bolo(self) -> None:
        prompt = self._capturar_prompt("Pode levar bolo?")

        self.assertIn("Bolo e utensilios", prompt)
        self.assertNotIn("Locacao de quadras", prompt)

    def test_pergunta_entrada_seleciona_faq_de_entrada(self) -> None:
        prompt = self._capturar_prompt("Quanto custa para entrar domingo?")

        self.assertIn("Valores de entrada", prompt)
        self.assertNotIn("Locacao de quadras", prompt)

    def test_contexto_limita_faqs_relevantes_no_prompt(self) -> None:
        prompt = self._capturar_prompt("Quero saber sobre entrada, bolo, quadra, gympass, musica e areia")

        total_titulos = sum(
            1
            for titulo in (
                "Locacao de quadras",
                "Bolo e utensilios",
                "Valores de entrada",
                "Regras dos espacos",
                "Programacao musical",
                "Gympass",
            )
            if titulo in prompt
        )
        self.assertLessEqual(total_titulos, 5)

    def test_estado_recalcula_proximo_campo_apos_horario_confirmado_em_pergunta(self) -> None:
        telefone = "5511993680823"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-25",
            "nome_cliente": "Rodrigo",
            "campo_pendente": "horario",
            "etapa": "aguardando_horario",
        }
        payload = {
            "resposta": "Certo. Sobre o valor, a reserva tem taxa de R$ 50,00. Para quantas pessoas fica?",
            "intencao": "pergunta_restaurante",
            "dados_confirmados": {"horario": "19:00"},
            "dados_mencionados": {},
            "dados_incertos": {},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": True,
            "campo_sugerido": "pessoas",
            "confianca": 0.9,
        }
        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta_reserva()),
            patch.object(agente, "_hoje", return_value=date(2026, 7, 23)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}, clear=False),
            patch.object(agente, "_chamar_groq", return_value=json.dumps(payload)),
        ):
            agente.processar_mensagem(telefone, "Pode ser as 19h. Tem algum valor?", nome_cliente="Rodrigo")

        estado = agente._estados_reserva[telefone]
        self.assertEqual(estado["data_reserva"], "2026-07-25")
        self.assertEqual(estado["horario"], "19:00")
        self.assertEqual(estado["campo_pendente"], "pessoas")
        self.assertEqual(estado["etapa"], "aguardando_quantidade")

    def test_dado_confirmado_nao_muda_sem_correcao_explicita(self) -> None:
        telefone = "5511993680823"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-25",
            "horario": "19:00",
            "nome_cliente": "Rodrigo",
            "campo_pendente": "pessoas",
            "etapa": "aguardando_quantidade",
        }
        payload = {
            "resposta": "Perfeito, deixo 20 pessoas por enquanto. Sobre o local, salao e areia dependem de disponibilidade.",
            "intencao": "pergunta_restaurante",
            "dados_confirmados": {"data": "2026-07-26", "horario": "18:00", "quantidade": 20},
            "dados_mencionados": {},
            "dados_incertos": {},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": True,
            "campo_sugerido": "confirmacao",
            "confianca": 0.9,
        }
        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta_reserva()),
            patch.object(agente, "_hoje", return_value=date(2026, 7, 23)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}, clear=False),
            patch.object(agente, "_chamar_groq", return_value=json.dumps(payload)),
        ):
            agente.processar_mensagem(
                telefone,
                "Pode colocar 20 pessoas por enquanto. A reserva e no salao ou na areia?",
                nome_cliente="Rodrigo",
            )

        estado = agente._estados_reserva[telefone]
        self.assertEqual(estado["data_reserva"], "2026-07-25")
        self.assertEqual(estado["horario"], "19:00")
        self.assertEqual(estado["pessoas"], 20)

    def test_pergunta_generica_de_horario_nao_dispara_guardrail(self) -> None:
        config = self._config()
        texto, motivos = agente._corrigir_texto_por_configuracao_estruturada(
            texto="A reserva e feita para grupos a partir de 11 pessoas. Qual horario voce gostaria de fazer a reserva no sabado?",
            mensagem_cliente="A reserva e no salao ou na areia?",
            config=config,
        )

        self.assertEqual(motivos, [])
        self.assertIn("Qual horario", texto)

    def test_resposta_sobre_taxa_nao_dispara_guardrail_de_horario(self) -> None:
        config = self._config()
        texto_ia = (
            "A reserva tem taxa de R$ 50,00, que e convertida em consumacao. "
            "O comprovante Pix e obrigatorio. Posso considerar 20 pessoas por enquanto."
        )
        texto, motivos = agente._corrigir_texto_por_configuracao_estruturada(
            texto=texto_ia,
            mensagem_cliente="Pode ser as 19h. Tem algum valor para reservar?",
            config=config,
        )

        self.assertEqual(motivos, [])
        self.assertEqual(texto, texto_ia)

    def test_palavra_horario_sem_valor_numerico_nao_dispara_guardrail(self) -> None:
        config = self._config()
        texto, motivos = agente._corrigir_texto_por_configuracao_estruturada(
            texto="A reserva depende do horario escolhido. Qual horario voce prefere?",
            mensagem_cliente="Como funciona a reserva?",
            config=config,
        )

        self.assertEqual(motivos, [])
        self.assertIn("Qual horario", texto)

    def test_horario_valido_dezenove_nao_dispara_guardrail(self) -> None:
        config = self._config()
        texto, motivos = agente._corrigir_texto_por_configuracao_estruturada(
            texto="Pode reservar as 19h. A taxa e R$ 50,00.",
            mensagem_cliente="Pode ser as 19h?",
            config=config,
        )

        self.assertEqual(motivos, [])
        self.assertIn("19h", texto)

    def test_horario_invalido_oferecido_pelo_bot_dispara_guardrail(self) -> None:
        config = self._config()
        texto, motivos = agente._corrigir_texto_por_configuracao_estruturada(
            texto="Pode reservar as 20h.",
            mensagem_cliente="Quero reservar.",
            config=config,
        )

        self.assertIn("horarios_reserva_fora_config", motivos)
        self.assertEqual(texto, "Pode reservar as 20h.")

    def test_horario_invalido_rejeitado_pelo_bot_nao_substitui_resposta(self) -> None:
        config = self._config()
        texto_ia = "Voce mencionou 20h, mas esse horario nao esta entre os aceitos. Posso verificar 19:00."
        texto, motivos = agente._corrigir_texto_por_configuracao_estruturada(
            texto=texto_ia,
            mensagem_cliente="Pode ser as 20h?",
            config=config,
        )

        self.assertEqual(motivos, [])
        self.assertEqual(texto, texto_ia)

    def test_correcao_localizada_preserva_restante_da_resposta(self) -> None:
        config = self._config()
        texto, motivos = agente._corrigir_texto_por_configuracao_estruturada(
            texto="Pode reservar as 20h. A taxa e R$ 50,00, convertida em consumacao.",
            mensagem_cliente="Tem taxa?",
            config=config,
        )

        self.assertIn("horarios_reserva_fora_config", motivos)
        self.assertIn("A taxa e R$ 50,00", texto)
        self.assertEqual(texto, "Pode reservar as 20h. A taxa e R$ 50,00, convertida em consumacao.")

    def test_sabado_e_normalizado_para_data_absoluta(self) -> None:
        telefone = "5511993680823"
        payload = _payload_custom(
            "Sabado anotado. Qual horario voce prefere?",
            intencao="fornecimento_dados",
            dados_confirmados={"data": "sabado"},
            deve_avancar_estado=True,
            campo_sugerido="horario",
        )
        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta_reserva()),
            patch.object(agente, "_hoje", return_value=date(2026, 7, 23)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}, clear=False),
            patch.object(agente, "_chamar_groq", return_value=payload),
        ):
            agente.processar_mensagem(telefone, "Queria reservar no sabado", nome_cliente="Rodrigo")

        estado = agente._estados_reserva[telefone]
        self.assertEqual(estado["data_reserva"], "2026-07-25")
        self.assertEqual(estado["campo_pendente"], "horario")

    def test_data_confirmada_remove_etapa_aguardando_data(self) -> None:
        telefone = "5511993680823"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-25",
            "nome_cliente": "Rodrigo",
            "campo_pendente": "data_reserva",
            "etapa": "aguardando_data",
        }
        payload = _payload_custom(
            "Tenho a data. Qual horario voce prefere?",
            intencao="pergunta_restaurante",
        )
        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta_reserva()),
            patch.object(agente, "_hoje", return_value=date(2026, 7, 23)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}, clear=False),
            patch.object(agente, "_chamar_groq", return_value=payload),
        ):
            agente.processar_mensagem(telefone, "Como funciona a reserva?", nome_cliente="Rodrigo")

        estado = agente._estados_reserva[telefone]
        self.assertEqual(estado["campo_pendente"], "horario")
        self.assertEqual(estado["etapa"], "aguardando_horario")

    def test_data_e_horario_confirmados_levam_para_pessoas(self) -> None:
        telefone = "5511993680823"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-25",
            "horario": "19:00",
            "nome_cliente": "Rodrigo",
            "campo_pendente": "data_reserva",
            "etapa": "aguardando_data",
        }
        payload = _payload_custom("Certo, quantas pessoas vao?", intencao="pergunta_restaurante")
        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta_reserva()),
            patch.object(agente, "_hoje", return_value=date(2026, 7, 23)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}, clear=False),
            patch.object(agente, "_chamar_groq", return_value=payload),
        ):
            agente.processar_mensagem(telefone, "Tem taxa?", nome_cliente="Rodrigo")

        estado = agente._estados_reserva[telefone]
        self.assertEqual(estado["campo_pendente"], "pessoas")
        self.assertEqual(estado["etapa"], "aguardando_quantidade")

    def test_quantidade_aproximada_mantem_pessoas_pendente(self) -> None:
        telefone = "5511993680823"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-25",
            "horario": "19:00",
            "nome_cliente": "Rodrigo",
            "campo_pendente": "pessoas",
            "etapa": "aguardando_quantidade",
        }
        payload = _payload_custom(
            "Posso considerar 20 pessoas por enquanto e ajustar depois.",
            intencao="pergunta_restaurante",
            dados_confirmados={"quantidade": 20},
            dados_incertos={"quantidade": "18, 20 ou 22"},
            deve_avancar_estado=True,
            campo_sugerido="confirmacao",
        )
        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta_reserva()),
            patch.object(agente, "_hoje", return_value=date(2026, 7, 23)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}, clear=False),
            patch.object(agente, "_chamar_groq", return_value=payload),
        ):
            agente.processar_mensagem(
                telefone,
                "Ainda nao sei se serao 18, 20 ou 22 pessoas.",
                nome_cliente="Rodrigo",
            )

        estado = agente._estados_reserva[telefone]
        self.assertNotIn("pessoas", estado)
        self.assertEqual(estado["campo_pendente"], "pessoas")

    def test_conversa_real_taxa_quantidade_e_horario_nao_sao_sobrescritos(self) -> None:
        telefone = "5511993680823"
        respostas = [
            _payload_custom(
                "A reserva funciona a partir de 11 pessoas, com taxa de R$ 50,00 convertida em consumacao "
                "e comprovante Pix obrigatorio. Posso considerar 20 pessoas por enquanto; qual horario voce prefere?",
                intencao="pergunta_restaurante",
                dados_confirmados={"data": "sabado"},
                dados_mencionados={"quantidade": 20},
                dados_incertos={"quantidade": "entre 18 e 22"},
                deve_avancar_estado=True,
                campo_sugerido="horario",
            ),
            _payload_custom(
                "A reserva tem taxa de R$ 50,00, convertida em consumacao, e o comprovante Pix e obrigatorio. "
                "Posso considerar 20 pessoas por enquanto e ajustar depois.",
                intencao="pergunta_restaurante",
                dados_confirmados={"horario": "19:00"},
                dados_mencionados={"quantidade": 20},
                dados_incertos={"quantidade": "18, 20 ou 22"},
                deve_avancar_estado=True,
                campo_sugerido="pessoas",
            ),
        ]
        with (
            patch.object(config_restaurante.repositorio, "carregar_configuracao_bruta", return_value=_config_bruta_reserva()),
            patch.object(agente, "_hoje", return_value=date(2026, 7, 23)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste"}, clear=False),
            patch.object(agente, "_chamar_groq", side_effect=respostas),
        ):
            primeira = agente.processar_mensagem(
                telefone,
                "Oi, vi a mensagem. Queria comemorar meu aniversario ai no sabado. "
                "Acho que vao umas 20 pessoas, mas ainda pode variar entre 18 e 22. Como funciona a reserva?",
                nome_cliente="Rodrigo",
            )
            segunda = agente.processar_mensagem(
                telefone,
                "Pode ser as 19h. Mas antes queria entender: tem algum valor para reservar? "
                "E como ainda nao sei se serao 18, 20 ou 22 pessoas, isso e um problema?",
                nome_cliente="Rodrigo",
            )

        self.assertIn("a partir de 11", agente._normalizar_busca(primeira["texto"]))
        self.assertIn("R$ 50,00", segunda["texto"])
        self.assertIn("consumacao", agente._normalizar_busca(segunda["texto"]))
        self.assertIn("comprovante Pix", segunda["texto"])
        self.assertIn("20 pessoas por enquanto", segunda["texto"])
        self.assertNotIn("Os horarios aceitos para reserva", segunda["texto"])
        estado = agente._estados_reserva[telefone]
        self.assertEqual(estado["data_reserva"], "2026-07-25")
        self.assertEqual(estado["horario"], "19:00")
        self.assertNotIn("pessoas", estado)
        self.assertEqual(estado["campo_pendente"], "pessoas")

    def test_contexto_de_espaco_e_mantido_em_pergunta_com_isso_as_19h(self) -> None:
        telefone = "5511993680823"
        agente._historicos[telefone] = [
            {"role": "user", "content": "A reserva e no salao ou na areia?"},
            {"role": "assistant", "content": "Depende da disponibilidade e do horario."},
        ]
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-25",
            "horario": "19:00",
            "pessoas": 20,
            "nome_cliente": "Rodrigo",
            "assunto_atual": "salao ou areia",
            "pergunta_aberta": "local da reserva",
            "campo_pendente": "confirmacao",
            "etapa": "aguardando_confirmacao",
        }
        prompt = self._capturar_prompt("Como funciona isso as 19h?")

        self.assertIn("Salao", prompt)
        self.assertIn("Areia", prompt)
        self.assertIn("Regras dos espacos", prompt)


if __name__ == "__main__":
    unittest.main()
