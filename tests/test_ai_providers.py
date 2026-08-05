from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import ia_fallback
from services.ai_providers.base import AIProviderError
from services.ai_providers.gemini_provider import GeminiProvider


class FakeGeminiModels:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeGeminiClient:
    def __init__(self, models: FakeGeminiModels) -> None:
        self.models = models


class GeminiProviderTest(unittest.TestCase):
    def _response(self, text: str):
        return SimpleNamespace(
            text=text,
            candidates=[SimpleNamespace(finish_reason="STOP")],
            usage_metadata=SimpleNamespace(
                prompt_token_count=10,
                candidates_token_count=20,
                total_token_count=30,
            ),
        )

    def test_gemini_retorna_json_e_separa_system_instruction(self) -> None:
        models = FakeGeminiModels(self._response('{"resposta":"ok"}'))
        provider = GeminiProvider(
            api_key="nao-usada-no-teste",
            model="gemini-test",
            client=FakeGeminiClient(models),
        )

        resultado = provider.generate(
            [
                {"role": "system", "content": "Regras"},
                {"role": "user", "content": "Oi"},
                {"role": "assistant", "content": "Ola"},
            ],
            response_schema={"type": "object", "properties": {"resposta": {"type": "string"}}},
        )

        self.assertEqual(resultado.provider, "gemini")
        self.assertEqual(resultado.parsed_json, {"resposta": "ok"})
        chamada = models.calls[0]
        self.assertEqual([item.role for item in chamada["contents"]], ["user", "model"])
        self.assertEqual(chamada["config"].system_instruction, "Regras")
        self.assertEqual(chamada["config"].response_mime_type, "application/json")

    def test_json_embutido_e_normalizado_pelo_extrator_existente(self) -> None:
        models = FakeGeminiModels(self._response('Resposta natural. {"resposta":"ok"}'))
        provider = GeminiProvider(api_key="teste", model="gemini-test", client=FakeGeminiClient(models))

        resultado = provider.generate([{"role": "user", "content": "Oi"}])

        self.assertIsNone(resultado.parsed_json)
        self.assertEqual(ia_fallback.extrair_json_resposta(resultado.content)["resposta"], "ok")

    def test_structured_output_textual_e_reparado_antes_do_fallback(self) -> None:
        models = FakeGeminiModels(self._response('Antes do JSON {"resposta":"ok"} depois'))
        provider = GeminiProvider(api_key="teste", model="gemini-test", client=FakeGeminiClient(models))
        with patch.object(ia_fallback, "GeminiProvider", return_value=provider):
            resultado = ia_fallback._executar_provider_normalizado(
                provider="gemini",
                modelo="gemini-test",
                mensagens=[{"role": "user", "content": "Oi"}],
                response_format_json=True,
            )

        self.assertEqual(resultado.parsed_json["resposta"], "ok")
        self.assertEqual(resultado.parsed_json["acao"], "responder")
        self.assertEqual(json.loads(resultado.content)["resposta"], "ok")

    def test_resposta_vazia_e_timeout_sao_normalizados(self) -> None:
        vazio = FakeGeminiModels(self._response(""))
        provider_vazio = GeminiProvider(api_key="teste", model="gemini-test", client=FakeGeminiClient(vazio))
        with self.assertRaises(AIProviderError) as erro_vazio:
            provider_vazio.generate([{"role": "user", "content": "Oi"}])
        self.assertEqual(erro_vazio.exception.error_code, "content_empty")

        timeout = FakeGeminiModels(error=TimeoutError("timeout"))
        provider_timeout = GeminiProvider(api_key="teste", model="gemini-test", client=FakeGeminiClient(timeout))
        with self.assertRaises(AIProviderError) as erro_timeout:
            provider_timeout.generate([{"role": "user", "content": "Oi"}])
        self.assertEqual(erro_timeout.exception.error_code, "timeout")
        self.assertEqual(len(timeout.calls), 2)

    def test_chave_ausente_e_autenticacao_nao_fazem_retry(self) -> None:
        provider_sem_chave = GeminiProvider(api_key="", model="gemini-test", client=object())
        with self.assertRaises(AIProviderError) as erro_chave:
            provider_sem_chave.generate([{"role": "user", "content": "Oi"}])
        self.assertEqual(erro_chave.exception.error_code, "missing_api_key")

        class Unauthorized(Exception):
            status_code = 401

        models = FakeGeminiModels(error=Unauthorized("invalid api key"))
        provider_auth = GeminiProvider(api_key="teste", model="gemini-test", client=FakeGeminiClient(models))
        with self.assertRaises(AIProviderError) as erro_auth:
            provider_auth.generate([{"role": "user", "content": "Oi"}])
        self.assertEqual(erro_auth.exception.error_code, "authentication")
        self.assertEqual(len(models.calls), 1)

    def test_rate_limit_faz_no_maximo_um_retry(self) -> None:
        class RateLimit(Exception):
            status_code = 429

        models = FakeGeminiModels(error=RateLimit("rate limit"))
        provider = GeminiProvider(api_key="teste", model="gemini-test", client=FakeGeminiClient(models))
        with self.assertRaises(AIProviderError) as erro:
            provider.generate([{"role": "user", "content": "Oi"}])
        self.assertEqual(erro.exception.error_code, "rate_limit")
        self.assertEqual(len(models.calls), 2)


class ProviderChainTest(unittest.TestCase):
    def test_cadeia_remove_modelo_duplicado(self) -> None:
        env = {
            "AI_PRIMARY_PROVIDER": "gemini",
            "AI_FALLBACK_PROVIDER": "gemini",
            "GEMINI_API_KEY": "gemini-test-key",
            "GEMINI_PRIMARY_MODEL": "gemini-primary",
            "GEMINI_FALLBACK_MODEL": "gemini-primary",
            "GROQ_API_KEY": "",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(ia_fallback._candidatos_providers(None), [("gemini", "gemini-primary")])

    def test_dois_modelos_gemini_falham_e_groq_responde(self) -> None:
        class GeminiFailure:
            def generate(self, *args, **kwargs):
                raise AIProviderError("falha", error_code="provider_error")

        env = {
            "AI_PRIMARY_PROVIDER": "gemini",
            "AI_FALLBACK_PROVIDER": "groq",
            "GEMINI_API_KEY": "gemini-test-key",
            "GEMINI_PRIMARY_MODEL": "gemini-primary",
            "GEMINI_FALLBACK_MODEL": "gemini-secondary",
            "GROQ_API_KEY": "groq-test-key",
            "GROQ_PRIMARY_MODEL": "groq-primary",
            "GROQ_FALLBACK_MODEL": "",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(ia_fallback, "_cooldown_ativo", return_value=False),
            patch.object(ia_fallback, "GeminiProvider", side_effect=[GeminiFailure(), GeminiFailure()]),
            patch.object(ia_fallback, "_executar_groq_modelo", return_value=json.dumps({"resposta": "ok"})),
        ):
            resultado = ia_fallback.executar_ia_com_fallback([{"role": "user", "content": "Oi"}])

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["provider"], "groq")
        self.assertEqual(resultado["model"], "groq-primary")

    def test_contrato_reduzido_recebe_defaults_e_ignora_extras_no_agente(self) -> None:
        payload = ia_fallback.extrair_json_resposta('{"resposta":"ok","campo_novo":"ignorar"}')
        self.assertIsNotNone(payload)
        self.assertEqual(payload["resposta"], "ok")
        self.assertEqual(payload["acao"], "responder")
        self.assertEqual(payload["dados_confirmados"], {})
        self.assertEqual(payload["campo_novo"], "ignorar")

    def test_gemini_falha_e_groq_responde(self) -> None:
        class GeminiFailure:
            def generate(self, *args, **kwargs):
                raise AIProviderError("falha", error_code="provider_error")

        env = {
            "AI_PRIMARY_PROVIDER": "gemini",
            "AI_FALLBACK_PROVIDER": "groq",
            "GEMINI_API_KEY": "gemini-test-key",
            "GEMINI_PRIMARY_MODEL": "gemini-primary",
            "GEMINI_FALLBACK_MODEL": "",
            "GROQ_API_KEY": "groq-test-key",
            "GROQ_PRIMARY_MODEL": "groq-primary",
            "GROQ_FALLBACK_MODEL": "",
            "AI_FALLBACK_API_KEY": "",
            "AI_FALLBACK_MODEL": "",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(ia_fallback, "_cooldown_ativo", return_value=False),
            patch.object(ia_fallback, "GeminiProvider", return_value=GeminiFailure()),
            patch.object(ia_fallback, "_executar_groq_modelo", return_value=json.dumps({"resposta": "ok"})),
        ):
            resultado = ia_fallback.executar_ia_com_fallback(
                [{"role": "user", "content": "Oi"}],
                response_format_json=True,
            )

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["provider"], "groq")
        self.assertTrue(resultado["usou_fallback"])

    def test_groq_legado_funciona_sem_gemini(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "",
                    "GROQ_API_KEY": "groq-test-key",
                    "GROQ_PRIMARY_MODEL": "groq-primary",
                    "GROQ_FALLBACK_MODEL": "",
                    "AI_PRIMARY_PROVIDER": "",
                    "AI_FALLBACK_PROVIDER": "",
                    "AI_FALLBACK_API_KEY": "",
                    "AI_FALLBACK_MODEL": "",
                },
                clear=True,
            ),
            patch.object(ia_fallback, "_cooldown_ativo", return_value=False),
            patch.object(ia_fallback, "_executar_groq_modelo", return_value='{"resposta":"ok"}'),
        ):
            resultado = ia_fallback.executar_ia_com_fallback([{"role": "user", "content": "Oi"}])

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["provider"], "groq")
