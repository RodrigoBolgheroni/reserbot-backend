from __future__ import annotations

import json
import os
import unittest
from datetime import date
from unittest.mock import patch

from services import agente


class AgenteIAOrquestradoraTest(unittest.TestCase):
    def setUp(self) -> None:
        agente._historicos.clear()
        agente._estados_reserva.clear()

    def tearDown(self) -> None:
        agente._historicos.clear()
        agente._estados_reserva.clear()

    def _json(self, payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False)

    def _processar(self, telefone: str, mensagem: str, payload: dict | list[dict]) -> agente.RespostaAgente:
        respostas = [self._json(item) for item in payload] if isinstance(payload, list) else self._json(payload)
        with (
            patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste", "RESERVA_HORARIO_INICIO": "10:00", "RESERVA_HORARIO_FIM": "23:59"}),
            patch.object(agente, "_chamar_groq", side_effect=respostas if isinstance(respostas, list) else None, return_value=respostas if isinstance(respostas, str) else None),
        ):
            return agente.processar_mensagem(telefone, mensagem, nome_cliente="Rodrigo Teste")

    def test_comentario_nao_avanca_estado_automaticamente(self) -> None:
        telefone = "5511990000001"
        agente._estados_reserva[telefone] = {"data_reserva": "2026-07-30", "campo_pendente": "horario", "etapa": "aguardando_horario"}
        payload = {
            "resposta": "Kkkkk tento não te deixar esperando. Você estava pensando em algum horário para o dia 30?",
            "intencao": "comentario",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "horario",
            "confianca": 0.96,
        }

        resposta = self._processar(telefone, "Você responde rápido hein kkkkk", payload)

        self.assertEqual(resposta["texto"], payload["resposta"])
        self.assertNotIn("horario", agente._estados_reserva[telefone])
        self.assertEqual(agente._estados_reserva[telefone]["campo_pendente"], "horario")

    def test_numero_mencionado_nao_vira_horario_confirmado(self) -> None:
        telefone = "5511990000002"
        agente._estados_reserva[telefone] = {"data_reserva": "2026-07-30", "campo_pendente": "horario", "etapa": "aguardando_horario"}
        payload = {
            "resposta": "Dá sim, porque fechamos à meia-noite. Você pretende chegar logo depois do trabalho?",
            "intencao": "pergunta_contextual",
            "dados_confirmados": {},
            "dados_mencionados": {"horario": "20:00"},
            "dados_incertos": {},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "horario",
            "confianca": 0.95,
        }

        resposta = self._processar(telefone, "Eu saio do trabalho às 20h, será que dá tempo?", payload)

        estado = agente._estados_reserva[telefone]
        self.assertEqual(resposta["texto"], payload["resposta"])
        self.assertNotIn("horario", estado)
        self.assertEqual(estado["dados_mencionados"]["horario"], "20:00")

    def test_contexto_expandido_fica_serializavel_para_supabase(self) -> None:
        telefone = "5511990000999"
        payload = {
            "resposta": "Dá tempo sim. Você pretende chegar logo depois do trabalho?",
            "intencao": "pergunta_contextual",
            "dados_confirmados": {},
            "dados_mencionados": {"horario": "20:00"},
            "dados_incertos": {"chegada": "depois do trabalho"},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "horario",
            "assunto_atual": "tempo para chegar depois do trabalho",
            "pergunta_aberta": "cliente pretende chegar logo depois do trabalho?",
            "tom_cliente": "descontraido",
            "resumo_conversa": "Cliente avalia se consegue chegar apos sair do trabalho.",
            "confianca": 0.94,
        }

        self._processar(telefone, "saio às 20h, será que dá tempo?", payload)
        estado = agente.obter_estado_reserva(telefone)

        self.assertEqual(estado["dados_mencionados"]["horario"], "20:00")
        self.assertEqual(estado["dados_incertos"]["chegada"], "depois do trabalho")
        self.assertIn("trabalho", estado["assunto_atual"])
        self.assertEqual(estado["tom_cliente"], "descontraido")

    def test_correcao_explicita_de_horario_altera_estado(self) -> None:
        telefone = "5511990000003"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-30",
            "horario": "19:00",
            "pessoas": 4,
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "confirmacao",
            "etapa": "aguardando_confirmacao",
            "aguardando_confirmacao": True,
        }
        payload = {
            "resposta": "Fechado, 20h30. Só confirmando: dia 30/07/2026 às 20:30 para 4 pessoas, certo?",
            "intencao": "corrigir_horario",
            "dados_confirmados": {"horario": "20:30"},
            "dados_mencionados": {},
            "correcoes": {"horario": "20:30"},
            "acao": "pedir_confirmacao",
            "deve_avancar_estado": True,
            "campo_sugerido": "confirmacao",
            "confianca": 0.97,
        }

        resposta = self._processar(telefone, "Então coloca 20h30", payload)

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["dados_reserva"]["horario"], "20:30")
        self.assertEqual(resposta["dados_reserva"]["data_reserva"], "2026-07-30")

    def test_quantidade_incerta_nao_vira_quantidade_confirmada(self) -> None:
        telefone = "5511990000004"
        agente._estados_reserva[telefone] = {"data_reserva": "2026-07-30", "horario": "20:30", "campo_pendente": "pessoas", "etapa": "aguardando_quantidade"}
        payload = {
            "resposta": "Entendi: 3 pessoas confirmadas e talvez uma quarta, certo?",
            "intencao": "fornecimento_dados",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "dados_incertos": {"quantidade": "3 confirmadas e talvez 4"},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "quantidade",
            "confianca": 0.9,
        }

        resposta = self._processar(telefone, "Vai eu, minha mãe, meu pai e talvez minha irmã", payload)

        estado = agente._estados_reserva[telefone]
        self.assertEqual(resposta["texto"], payload["resposta"])
        self.assertNotIn("pessoas", estado)
        self.assertIn("talvez", estado["dados_incertos"]["quantidade"])

    def test_pergunta_fora_do_campo_pendente_nao_vira_erro(self) -> None:
        telefone = "5511990000005"
        agente._estados_reserva[telefone] = {"data_reserva": "2026-07-30", "campo_pendente": "horario", "etapa": "aguardando_horario", "tentativas_campos": {"horario": 1}}
        payload = {
            "resposta": "Essa informação ainda não está cadastrada por aqui. Posso continuar sua reserva enquanto isso.",
            "intencao": "pergunta_restaurante",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "horario",
            "confianca": 0.9,
        }

        resposta = self._processar(telefone, "Aliás, vocês têm estacionamento?", payload)

        self.assertEqual(resposta["texto"], payload["resposta"])
        self.assertEqual(agente._estados_reserva[telefone]["tentativas_campos"]["horario"], 1)

    def test_duas_perguntas_seguidas_nao_aumentam_erro(self) -> None:
        telefone = "5511990000006"
        agente._estados_reserva[telefone] = {"campo_pendente": "horario", "etapa": "aguardando_horario", "tentativas_campos": {"horario": 1}}
        payload_1 = {
            "resposta": "Funcionamos das 10:00 à meia-noite.",
            "intencao": "pergunta_restaurante",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "horario",
            "confianca": 0.9,
        }
        payload_2 = {**payload_1, "resposta": "As formas de pagamento cadastradas ficam com a equipe do restaurante."}

        primeira = self._processar(telefone, "que horas fecha?", payload_1)
        segunda = self._processar(telefone, "e aceita pix?", payload_2)

        self.assertIn("10:00", primeira["texto"])
        self.assertIn("pagamento", agente._normalizar_busca(segunda["texto"]))
        self.assertEqual(agente._estados_reserva[telefone]["tentativas_campos"]["horario"], 1)

    def test_bincadeira_usa_resposta_natural_sem_frase_de_formulario(self) -> None:
        telefone = "5511990000007"
        agente._estados_reserva[telefone] = {"campo_pendente": "quantidade", "etapa": "aguardando_quantidade"}
        payload = {
            "resposta": "Kkkkk boa. Quando você souber certinho o total, eu anoto por aqui.",
            "intencao": "brincadeira",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "quantidade",
            "confianca": 0.9,
        }

        resposta = self._processar(telefone, "se couber todo mundo kkkk", payload)

        self.assertEqual(resposta["texto"], payload["resposta"])
        self.assertNotIn("quantas pessoas", agente._normalizar_busca(resposta["texto"]))

    def test_resumo_da_reserva_pode_vir_da_ia_quando_confere(self) -> None:
        telefone = "5511990000008"
        agente._estados_reserva[telefone] = {"data_reserva": "2026-07-30", "horario": "20:30", "pessoas": 4, "nome_cliente": "Rodrigo Teste"}
        payload = {
            "resposta": "Fechado: 30/07/2026, às 20:30, para 4 pessoas. Posso confirmar?",
            "intencao": "resumo_reserva",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "acao": "pedir_confirmacao",
            "deve_avancar_estado": False,
            "campo_sugerido": "confirmacao",
            "confianca": 0.95,
        }

        resposta = self._processar(telefone, "resume pra mim", payload)

        self.assertEqual(resposta["texto"], payload["resposta"])
        self.assertEqual(resposta["status_reserva"], "aguardando_confirmacao")

    def test_mudanca_de_assunto_e_retorno_mantem_estado(self) -> None:
        telefone = "5511990000009"
        agente._estados_reserva[telefone] = {"data_reserva": "2026-07-30", "campo_pendente": "horario", "etapa": "aguardando_horario"}
        pergunta = {
            "resposta": "Sobre pagamento, posso seguir com a reserva por aqui e a equipe confirma detalhes se precisar.",
            "intencao": "pergunta_restaurante",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "horario",
            "confianca": 0.9,
        }
        retorno = {
            "resposta": "Fechado, 20h30. Para quantas pessoas?",
            "intencao": "fornecimento_dados",
            "dados_confirmados": {"horario": "20:30"},
            "dados_mencionados": {},
            "acao": "continuar_conversa",
            "deve_avancar_estado": True,
            "campo_sugerido": "quantidade",
            "confianca": 0.95,
        }

        self._processar(telefone, "mudando de assunto, como paga?", pergunta)
        resposta = self._processar(telefone, "voltando, coloca 20h30", retorno)

        self.assertEqual(agente._estados_reserva[telefone]["data_reserva"], "2026-07-30")
        self.assertEqual(resposta["dados_reserva"]["horario"], "20:30")

    def test_nao_confirma_sem_autorizacao_do_cliente(self) -> None:
        telefone = "5511990010000"
        agente._estados_reserva[telefone] = {"data_reserva": "2026-07-30", "horario": "20:30", "pessoas": 4, "nome_cliente": "Rodrigo Teste"}
        payload = {
            "resposta": "Isso: 30/07/2026, às 20:30, para 4 pessoas. Posso confirmar?",
            "intencao": "resumo_reserva",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "acao": "pedir_confirmacao",
            "deve_avancar_estado": False,
            "campo_sugerido": "confirmacao",
            "confianca": 0.95,
        }

        resposta = self._processar(telefone, "ficou como?", payload)

        self.assertFalse(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "aguardando_confirmacao")

    def test_resposta_natural_apos_validacao_invalida(self) -> None:
        telefone = "5511990010001"
        agente._estados_reserva[telefone] = {"data_reserva": "2026-07-30", "pessoas": 4, "nome_cliente": "Rodrigo Teste", "campo_pendente": "horario", "etapa": "aguardando_horario"}
        primeira = {
            "resposta": "Anotei 9h.",
            "intencao": "fornecimento_dados",
            "dados_confirmados": {"horario": "09:00"},
            "dados_mencionados": {},
            "acao": "continuar_conversa",
            "deve_avancar_estado": True,
            "campo_sugerido": "horario",
            "confianca": 0.9,
        }
        segunda = {"resposta": "Às 9h ainda estaremos fechados. A partir das 10h já funciona — qual horário seria melhor?"}

        resposta = self._processar(telefone, "pode ser 9h", [primeira, segunda])

        self.assertEqual(resposta["texto"], segunda["resposta"])
        self.assertNotIn("horario", agente._estados_reserva[telefone])

    def test_estado_pendente_nao_dispara_frase_fixa_sozinho(self) -> None:
        telefone = "5511990010002"
        agente._estados_reserva[telefone] = {"campo_pendente": "pessoas", "etapa": "aguardando_quantidade"}
        payload = {
            "resposta": "Boa pergunta. Dá para ajustar o total depois se alguém confirmar em cima da hora.",
            "intencao": "pergunta_contextual",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "quantidade",
            "confianca": 0.92,
        }

        resposta = self._processar(telefone, "e se mais alguém resolver ir depois?", payload)

        self.assertEqual(resposta["texto"], payload["resposta"])
        self.assertNotIn("me manda so", agente._normalizar_busca(resposta["texto"]))


if __name__ == "__main__":
    unittest.main()
