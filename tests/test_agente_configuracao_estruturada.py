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
        "espacos": [],
        "faq_conteudos": [],
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

    def test_pergunta_valor_reserva_nao_pode_negar_taxa_configurada(self) -> None:
        resposta = self._processar(
            "tem algum valor para reservar?",
            "Nao temos um valor fixo para reservar.",
        )

        texto = resposta["texto"]
        texto_normalizado = agente._normalizar_busca(texto)
        self.assertIn("R$ 50,00", texto)
        self.assertIn("consumacao", texto_normalizado)
        self.assertIn("comprovante pix", texto_normalizado)
        self.assertNotIn("nao temos um valor fixo", texto_normalizado)
        self.assertNotIn("sem custo", texto_normalizado)

    def test_confirmacao_taxa_cinquenta_nao_pode_ser_negada(self) -> None:
        resposta = self._processar(
            "vi que voces cobram R$ 50 pela reserva, certo?",
            "Nao cobramos R$ 50 pela reserva. Voce pode fazer a reserva sem custo adicional.",
        )

        texto = resposta["texto"]
        texto_normalizado = agente._normalizar_busca(texto)
        self.assertIn("R$ 50,00", texto)
        self.assertIn("consumacao", texto_normalizado)
        self.assertIn("comprovante pix", texto_normalizado)
        self.assertNotIn("nao cobramos", texto_normalizado)
        self.assertNotIn("sem custo adicional", texto_normalizado)

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
        self.assertNotIn("pix-nao-deve-aparecer", prompt)
        self.assertIn("source=supabase", logs_texto)
        self.assertIn("estabelecimento_id=" + ESTABELECIMENTO_ID, logs_texto)
        self.assertIn("configuracoes_reserva=True", logs_texto)
        self.assertIn("taxa_valor=50.00", logs_texto)
        self.assertIn("quantidade_minima=11", logs_texto)
        self.assertIn("horarios_permitidos=['12:00', '13:00', '14:00', '18:00', '19:00']", logs_texto)
        self.assertNotIn("pix-nao-deve-aparecer", logs_texto)


if __name__ == "__main__":
    unittest.main()
