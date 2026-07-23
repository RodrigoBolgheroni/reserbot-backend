from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services import agente, ia_fallback


class RateLimitError(Exception):
    def __init__(self, message: str = "rate_limit_exceeded", headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status_code = 429
        self.headers = headers or {}


class InternalServerError(Exception):
    status_code = 500


class IaFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        ia_fallback.limpar_cooldowns_memoria()
        agente._historicos.clear()
        agente._estados_reserva.clear()

    def tearDown(self) -> None:
        ia_fallback.limpar_cooldowns_memoria()
        agente._historicos.clear()
        agente._estados_reserva.clear()

    def _env(self, **valores: str) -> dict[str, str]:
        env = {
            "GROQ_API_KEY": "groq-key",
            "GROQ_PRIMARY_MODEL": "modelo-principal",
            "GROQ_FALLBACK_MODEL": "",
            "AI_FALLBACK_PROVIDER": "",
            "AI_FALLBACK_API_KEY": "",
            "AI_FALLBACK_MODEL": "",
        }
        env.update(valores)
        return env

    def _sem_cooldown_supabase(self):
        return patch.object(ia_fallback.supabase, "selecionar", return_value={"ok": True, "data": []})

    def test_modelo_principal_responde_normalmente(self) -> None:
        with (
            patch.dict(os.environ, self._env(), clear=True),
            self._sem_cooldown_supabase(),
            patch.object(ia_fallback, "_executar_groq_modelo", return_value='{"resposta":"ok"}') as executar,
        ):
            resultado = ia_fallback.executar_ia_com_fallback([{"role": "user", "content": "oi"}])

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["provider"], "groq")
        self.assertEqual(resultado["model"], "modelo-principal")
        self.assertFalse(resultado["usou_fallback"])
        executar.assert_called_once()

    def test_principal_429_e_secundario_responde(self) -> None:
        agora = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        with (
            patch.dict(os.environ, self._env(GROQ_FALLBACK_MODEL="modelo-secundario"), clear=True),
            patch.object(ia_fallback, "_agora", return_value=agora),
            self._sem_cooldown_supabase(),
            patch.object(ia_fallback.supabase, "upsert", return_value={"ok": True}) as upsert,
            patch.object(
                ia_fallback,
                "_executar_groq_modelo",
                side_effect=[RateLimitError("rate_limit_exceeded"), '{"resposta":"fallback"}'],
            ) as executar,
        ):
            resultado = ia_fallback.executar_ia_com_fallback([{"role": "user", "content": "oi"}])

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["model"], "modelo-secundario")
        self.assertTrue(resultado["usou_fallback"])
        self.assertEqual(executar.call_count, 2)
        upsert.assert_called_once()

    def test_modelo_em_cooldown_nao_e_chamado_novamente(self) -> None:
        agora = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        ia_fallback._registrar_cooldown_memoria(
            "groq",
            "modelo-principal",
            {
                "provider": "groq",
                "model": "modelo-principal",
                "indisponivel_ate": (agora + timedelta(minutes=5)).isoformat(),
                "motivo": "rate_limit",
                "metadata": {},
            },
        )

        with (
            patch.dict(os.environ, self._env(GROQ_FALLBACK_MODEL="modelo-secundario"), clear=True),
            patch.object(ia_fallback, "_agora", return_value=agora),
            self._sem_cooldown_supabase(),
            patch.object(ia_fallback, "_executar_groq_modelo", return_value='{"resposta":"fallback"}') as executar,
        ):
            resultado = ia_fallback.executar_ia_com_fallback([{"role": "user", "content": "oi"}])

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["model"], "modelo-secundario")
        executar.assert_called_once()
        self.assertEqual(executar.call_args.kwargs["modelo"], "modelo-secundario")

    def test_retry_after_header_e_respeitado(self) -> None:
        agora = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        with (
            patch.dict(os.environ, self._env(), clear=True),
            patch.object(ia_fallback, "_agora", return_value=agora),
            self._sem_cooldown_supabase(),
            patch.object(ia_fallback.supabase, "upsert", return_value={"ok": False, "erro": "sem banco"}),
            patch.object(ia_fallback, "_executar_groq_modelo", side_effect=RateLimitError(headers={"Retry-After": "120"})),
        ):
            resultado = ia_fallback.executar_ia_com_fallback([{"role": "user", "content": "oi"}])

        self.assertFalse(resultado["ok"])
        cooldown = ia_fallback.cooldown_memoria("groq", "modelo-principal")
        self.assertIsNotNone(cooldown)
        self.assertEqual(cooldown["indisponivel_ate"], (agora + timedelta(seconds=120)).isoformat())

    def test_tempo_retry_after_da_mensagem_e_interpretado(self) -> None:
        segundos = ia_fallback.parse_tempo_retry_after("Please try again in 41m4.128s.")
        self.assertEqual(segundos, 2465)

    def test_principal_secundario_e_provider_falham_encaminha_humano(self) -> None:
        with (
            patch.dict(
                os.environ,
                self._env(
                    GROQ_FALLBACK_MODEL="modelo-secundario",
                    AI_FALLBACK_PROVIDER="openai",
                    AI_FALLBACK_API_KEY="fallback-key",
                    AI_FALLBACK_MODEL="modelo-alt",
                ),
                clear=True,
            ),
            self._sem_cooldown_supabase(),
            patch.object(ia_fallback.supabase, "upsert", return_value={"ok": True}),
            patch.object(ia_fallback, "_executar_groq_modelo", side_effect=[RateLimitError(), InternalServerError()]),
            patch.object(ia_fallback, "_executar_fallback_provider", side_effect=InternalServerError()),
        ):
            resultado = ia_fallback.executar_ia_com_fallback([{"role": "user", "content": "oi"}])

        self.assertFalse(resultado["ok"])
        self.assertTrue(resultado["encaminhar_humano"])
        self.assertEqual(resultado["erro_codigo"], "todos_provedores_indisponiveis")

    def test_provider_alternativo_responde_quando_groq_falha(self) -> None:
        with (
            patch.dict(
                os.environ,
                self._env(
                    AI_FALLBACK_PROVIDER="openai",
                    AI_FALLBACK_API_KEY="fallback-key",
                    AI_FALLBACK_MODEL="modelo-alt",
                ),
                clear=True,
            ),
            self._sem_cooldown_supabase(),
            patch.object(ia_fallback.supabase, "upsert", return_value={"ok": True}),
            patch.object(ia_fallback, "_executar_groq_modelo", side_effect=RateLimitError()),
            patch.object(ia_fallback, "_executar_fallback_provider", return_value='{"resposta":"provider"}') as provider,
        ):
            resultado = ia_fallback.executar_ia_com_fallback([{"role": "user", "content": "oi"}])

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["provider"], "openai")
        self.assertEqual(resultado["model"], "modelo-alt")
        provider.assert_called_once()

    def test_fallback_modelo_vazio_nao_quebra(self) -> None:
        with (
            patch.dict(os.environ, self._env(GROQ_FALLBACK_MODEL=""), clear=True),
            self._sem_cooldown_supabase(),
            patch.object(ia_fallback, "_executar_groq_modelo", return_value='{"resposta":"ok"}') as executar,
        ):
            resultado = ia_fallback.executar_ia_com_fallback([{"role": "user", "content": "oi"}])

        self.assertTrue(resultado["ok"])
        executar.assert_called_once()

    def test_falha_ao_salvar_cooldown_no_supabase_usa_memoria(self) -> None:
        agora = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        with (
            patch.dict(os.environ, self._env(), clear=True),
            patch.object(ia_fallback, "_agora", return_value=agora),
            self._sem_cooldown_supabase(),
            patch.object(ia_fallback.supabase, "upsert", return_value={"ok": False, "erro": "timeout"}),
            patch.object(ia_fallback, "_executar_groq_modelo", side_effect=RateLimitError("Please try again in 41m4.128s.")),
        ):
            resultado = ia_fallback.executar_ia_com_fallback([{"role": "user", "content": "oi"}])

        self.assertFalse(resultado["ok"])
        cooldown = ia_fallback.cooldown_memoria("groq", "modelo-principal")
        self.assertIsNotNone(cooldown)
        self.assertEqual(cooldown["motivo"], "rate_limit")

    def test_apos_cooldown_modelo_principal_pode_ser_usado(self) -> None:
        agora = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        ia_fallback._registrar_cooldown_memoria(
            "groq",
            "modelo-principal",
            {
                "provider": "groq",
                "model": "modelo-principal",
                "indisponivel_ate": (agora - timedelta(seconds=1)).isoformat(),
                "motivo": "rate_limit",
                "metadata": {},
            },
        )

        with (
            patch.dict(os.environ, self._env(), clear=True),
            patch.object(ia_fallback, "_agora", return_value=agora),
            self._sem_cooldown_supabase(),
            patch.object(ia_fallback, "_executar_groq_modelo", return_value='{"resposta":"ok"}') as executar,
        ):
            resultado = ia_fallback.executar_ia_com_fallback([{"role": "user", "content": "oi"}])

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["model"], "modelo-principal")
        executar.assert_called_once()

    def test_falha_total_preserva_estado_e_nao_confirma_reserva(self) -> None:
        telefone = "5511999999999"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-30",
            "horario": "20:00",
            "pessoas": 4,
            "nome_cliente": "Rodrigo",
            "campo_pendente": "confirmacao",
            "aguardando_confirmacao": True,
        }

        with (
            patch.dict(os.environ, self._env(), clear=True),
            self._sem_cooldown_supabase(),
            patch.object(ia_fallback.supabase, "upsert", return_value={"ok": True}),
            patch.object(ia_fallback, "_executar_groq_modelo", side_effect=RateLimitError()),
        ):
            resposta = agente.processar_mensagem(telefone, "sim", nome_cliente="Rodrigo")

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "aguardando_humano")
        self.assertEqual(resposta["texto"], ia_fallback.MENSAGEM_HANDOFF_IA)
        estado = agente._estados_reserva[telefone]
        self.assertEqual(estado["data_reserva"], "2026-07-30")
        self.assertEqual(estado["horario"], "20:00")
        self.assertEqual(estado["pessoas"], 4)

    def test_erro_tecnico_nao_aparece_para_cliente(self) -> None:
        with (
            patch.dict(os.environ, self._env(), clear=True),
            self._sem_cooldown_supabase(),
            patch.object(ia_fallback.supabase, "upsert", return_value={"ok": True}),
            patch.object(ia_fallback, "_executar_groq_modelo", side_effect=RateLimitError("HTTP 429 rate_limit_exceeded")),
        ):
            resposta = agente.processar_mensagem("5511999999999", "Oi", nome_cliente="Rodrigo")

        self.assertNotIn("429", resposta["texto"])
        self.assertNotIn("rate_limit", resposta["texto"])
        self.assertEqual(resposta["texto"], ia_fallback.MENSAGEM_HANDOFF_IA)


if __name__ == "__main__":
    unittest.main()
