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

    def test_comentario_rapidez_nao_e_sobrescrito_por_campo_pendente(self) -> None:
        telefone = "5511990000032"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-30",
            "campo_pendente": "horario",
            "etapa": "aguardando_horario",
            "tentativas_campos": {"horario": 2},
        }
        payload = {
            "resposta": "Sim, e para te ajudar o mais rapido possivel. Agora, sobre o horario da reserva, voce ja tem alguma ideia?",
            "intencao": "fornecimento_dados",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "dados_incertos": {},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "horario",
            "confianca": 0.9,
        }

        resposta = self._processar(telefone, "Você responde rápido hein kkkkk", payload)

        estado = agente._estados_reserva[telefone]
        self.assertEqual(resposta["texto"], payload["resposta"])
        self.assertEqual(resposta["status_reserva"], "em_coleta")
        self.assertEqual(estado["campo_pendente"], "horario")
        self.assertEqual(estado["tentativas_campos"]["horario"], 2)

    def test_pergunta_contextual_com_horario_mencionado_nao_e_sobrescrita_nem_handoff(self) -> None:
        telefone = "5511990000033"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-30",
            "campo_pendente": "horario",
            "etapa": "aguardando_horario",
            "tentativas_campos": {"horario": 2},
        }
        payload = {
            "resposta": "Você sai do trabalho às 20h, mas não confirmou se vai chegar aqui às 20h. Como fechamos à meia-noite, dá tempo; qual horário você acha que vai chegar?",
            "intencao": "fornecimento_dados",
            "dados_confirmados": {},
            "dados_mencionados": {"horario": "20:00"},
            "dados_incertos": {"chegada": "cliente sai do trabalho às 20h"},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "horario",
            "confianca": 0.9,
        }

        resposta = self._processar(telefone, "Eu saio do trabalho às 20h, será que dá tempo?", payload)

        estado = agente._estados_reserva[telefone]
        self.assertEqual(resposta["texto"], payload["resposta"])
        self.assertEqual(resposta["status_reserva"], "em_coleta")
        self.assertNotIn("horario", estado)
        self.assertEqual(estado["dados_mencionados"]["horario"], "20:00")
        self.assertEqual(estado["tentativas_campos"]["horario"], 2)
        self.assertNotIn("nao consegui entender", agente._normalizar_busca(resposta["texto"]))

    def test_horarios_texto_simples_sao_reparados_e_validados(self) -> None:
        casos = [
            ("Então coloca umas 20h30", "20:30", True, "20h30, anotado! Agora, quantas pessoas vão estar com você no dia 30/07?"),
            ("20:30", "20:30", True, "20h30, anotado! Agora, quantas pessoas vão estar com você no dia 30/07?"),
            ("Oito horas", "08:00", False, "Às 8h ainda estamos fechados, porque abrimos às 10h. Qual horário dentro do funcionamento fica melhor?"),
            ("19:30", "19:30", True, "19h30, anotado! Agora, quantas pessoas vão estar com você no dia 30/07?"),
        ]

        for indice, (mensagem, horario, valido, resposta_natural) in enumerate(casos, 1):
            with self.subTest(mensagem=mensagem):
                telefone = f"55119900010{indice:02d}"
                agente._estados_reserva[telefone] = {
                    "data_reserva": "2026-07-30",
                    "campo_pendente": "horario",
                    "etapa": "aguardando_horario",
                }
                reparo = {
                    "resposta": resposta_natural,
                    "intencao": "fornecimento_dados",
                    "dados_confirmados": {"horario": horario},
                    "dados_mencionados": {},
                    "dados_incertos": {},
                    "correcoes": {},
                    "acao": "continuar_conversa",
                    "deve_avancar_estado": False,
                    "campo_sugerido": "quantidade" if valido else "horario",
                    "confianca": 0.92,
                }
                chamadas = [resposta_natural, self._json(reparo)]
                if not valido:
                    chamadas.append(self._json({"resposta": resposta_natural}))
                estado_antes = dict(agente._estados_reserva[telefone])

                with (
                    patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
                    patch.dict(os.environ, {"GROQ_API_KEY": "teste", "RESERVA_HORARIO_INICIO": "10:00", "RESERVA_HORARIO_FIM": "23:59"}),
                    patch.object(agente, "_chamar_groq", side_effect=chamadas),
                ):
                    resposta = agente.processar_mensagem(telefone, mensagem, nome_cliente="Rodrigo Teste")

                estado_depois = agente._estados_reserva[telefone]
                self.assertEqual(estado_antes["campo_pendente"], "horario")
                if valido:
                    self.assertEqual(resposta["texto"], resposta_natural)
                    self.assertEqual(estado_depois["horario"], horario)
                    self.assertEqual(estado_depois["campo_pendente"], "pessoas")
                else:
                    self.assertNotIn("horario", estado_depois)
                    self.assertEqual(estado_depois["horario_invalido"], horario)
                    self.assertEqual(estado_depois["campo_pendente"], "horario")
                    self.assertIn("fechados", agente._normalizar_busca(resposta["texto"]))

    def test_horarios_json_sao_validados_sem_deve_avancar(self) -> None:
        casos = [
            ("Então coloca umas 20h30", "20:30", True),
            ("20:30", "20:30", True),
            ("Oito horas", "08:00", False),
            ("19:30", "19:30", True),
        ]

        for indice, (mensagem, horario, valido) in enumerate(casos, 1):
            with self.subTest(mensagem=mensagem):
                telefone = f"55119900011{indice:02d}"
                agente._estados_reserva[telefone] = {
                    "data_reserva": "2026-07-30",
                    "campo_pendente": "horario",
                    "etapa": "aguardando_horario",
                }
                resposta_natural = (
                    f"{horario}, anotado! Agora, quantas pessoas vão estar com você no dia 30/07?"
                    if valido
                    else "Às 8h ainda estamos fechados, porque abrimos às 10h. Qual horário dentro do funcionamento fica melhor?"
                )
                payload = {
                    "resposta": resposta_natural,
                    "intencao": "fornecimento_dados",
                    "dados_confirmados": {"horario": horario},
                    "dados_mencionados": {},
                    "dados_incertos": {},
                    "correcoes": {},
                    "acao": "continuar_conversa",
                    "deve_avancar_estado": False,
                    "campo_sugerido": "quantidade" if valido else "horario",
                    "confianca": 0.92,
                }
                chamadas = [self._json(payload)]
                if not valido:
                    chamadas.append(self._json({"resposta": resposta_natural}))
                estado_antes = dict(agente._estados_reserva[telefone])

                with (
                    patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
                    patch.dict(os.environ, {"GROQ_API_KEY": "teste", "RESERVA_HORARIO_INICIO": "10:00", "RESERVA_HORARIO_FIM": "23:59"}),
                    patch.object(agente, "_chamar_groq", side_effect=chamadas),
                ):
                    resposta = agente.processar_mensagem(telefone, mensagem, nome_cliente="Rodrigo Teste")

                estado_depois = agente._estados_reserva[telefone]
                self.assertEqual(estado_antes["campo_pendente"], "horario")
                if valido:
                    self.assertEqual(resposta["texto"], resposta_natural)
                    self.assertEqual(estado_depois["horario"], horario)
                    self.assertEqual(estado_depois["campo_pendente"], "pessoas")
                else:
                    self.assertNotIn("horario", estado_depois)
                    self.assertEqual(estado_depois["horario_invalido"], horario)
                    self.assertEqual(estado_depois["campo_pendente"], "horario")
                    self.assertIn("fechados", agente._normalizar_busca(resposta["texto"]))

    def test_parser_horario_aceita_formatos_basicos(self) -> None:
        casos = {
            "20:30": "20:30",
            "20h30": "20:30",
            "20h": "20:00",
            "20 horas": "20:00",
            "às 20": "20:00",
            "umas 20h30": "20:30",
            "então coloca 20h30": "20:30",
            "pode ser 20:30": "20:30",
            "oito horas": "08:00",
            "oito e meia": "08:30",
            "meia-noite": "00:00",
            "meio-dia": "12:00",
        }

        for frase, esperado in casos.items():
            with self.subTest(frase=frase):
                self.assertEqual(agente._extrair_horario(frase, permitir_numero_isolado=True), esperado)

    def test_comentario_indecisao_usa_resposta_natural_da_ia(self) -> None:
        telefone = "5511990000034"
        payload = {
            "resposta": "Qual dia e horário você está pensando para a reserva, caso consiga combinar com os outros?",
            "intencao": "comentario_indecisao",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "dados_incertos": {},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "data",
            "confianca": 0.9,
        }

        resposta = self._processar(telefone, "Quero reservar, mas ainda estou vendo com o pessoal", payload)

        estado = agente._estados_reserva[telefone]
        self.assertEqual(resposta["texto"], payload["resposta"])
        self.assertEqual(resposta["status_reserva"], "em_coleta")
        self.assertEqual(estado["campo_pendente"], "data_reserva")
        self.assertNotIn("tentativas_campos", estado)
        self.assertNotIn("quando decidir", agente._normalizar_busca(resposta["texto"]))

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

    def test_incidente_real_data_repetida_nao_e_perdida(self) -> None:
        telefone = "5511990010030"
        payloads = [
            {
                "resposta": "Sem problema. Quando decidir, me chama por aqui que eu continuo a reserva com voce.",
                "intencao": "comentario",
                "dados_confirmados": {},
                "dados_mencionados": {},
                "acao": "responder",
                "deve_avancar_estado": False,
                "campo_sugerido": "data",
                "confianca": 0.9,
            },
            {
                "resposta": "Certo, para qual data você quer fazer a reserva?",
                "intencao": "comentario",
                "dados_confirmados": {},
                "dados_mencionados": {"data": "2026-07-30"},
                "acao": "responder",
                "deve_avancar_estado": False,
                "campo_sugerido": "data",
                "confianca": 0.8,
            },
            {
                "resposta": "Desculpa, tenho o dia 30/07 anotado. Qual horário você prefere?",
                "intencao": "comentario",
                "dados_confirmados": {},
                "dados_mencionados": {},
                "acao": "responder",
                "deve_avancar_estado": False,
                "campo_sugerido": "horario",
                "confianca": 0.9,
            },
            {
                "resposta": "Sim, dia 30/07 está anotado. Agora só falta o horário.",
                "intencao": "comentario",
                "dados_confirmados": {},
                "dados_mencionados": {"data": "2026-07-30"},
                "acao": "responder",
                "deve_avancar_estado": False,
                "campo_sugerido": "horario",
                "confianca": 0.9,
            },
            {
                "resposta": "Dia 30/07 continua anotado. Qual horário fica melhor?",
                "intencao": "comentario",
                "dados_confirmados": {},
                "dados_mencionados": {"data": "2026-07-30"},
                "acao": "responder",
                "deve_avancar_estado": False,
                "campo_sugerido": "horario",
                "confianca": 0.9,
            },
        ]

        respostas_modelo = [self._json(payload) for payload in payloads]
        mensagens = [
            "Quero reservar, mas ainda estou vendo com o pessoal",
            "Acho que dia 30/07 é bom",
            "Ja falei",
            "Acho que dia 30/07",
            "30/07",
        ]
        respostas: list[agente.RespostaAgente] = []

        with (
            self.assertLogs("services.agente", level="INFO") as logs,
            patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste", "RESERVA_HORARIO_INICIO": "10:00", "RESERVA_HORARIO_FIM": "23:59"}),
            patch.object(agente, "_chamar_groq", side_effect=respostas_modelo),
        ):
            for mensagem in mensagens:
                respostas.append(agente.processar_mensagem(telefone, mensagem, nome_cliente="Rodrigo Teste"))

        estado_apos_segunda = respostas[1]["dados_reserva"]
        texto_apos_segunda = agente._normalizar_busca(respostas[1]["texto"])
        self.assertEqual(estado_apos_segunda["data_reserva"], "2026-07-30")
        self.assertEqual(agente._estados_reserva[telefone]["data_reserva"], "2026-07-30")
        self.assertEqual(agente._estados_reserva[telefone]["campo_pendente"], "horario")
        self.assertEqual(texto_apos_segunda, agente._normalizar_busca(payloads[1]["resposta"]))
        self.assertNotIn("formato dia", agente._normalizar_busca(" ".join(resposta["texto"] for resposta in respostas)))
        self.assertTrue(any("dados_confirmados_promovidos campo=data_reserva valor=2026-07-30" in linha for linha in logs.output))

    def test_parser_data_aceita_frases_obvias(self) -> None:
        with patch.object(agente, "_hoje", return_value=date(2026, 7, 22)):
            self.assertEqual(agente._extrair_data("dia 30/07", permitir_dia_isolado=True), "2026-07-30")
            self.assertEqual(agente._extrair_data("acho que dia 30/07", permitir_dia_isolado=True), "2026-07-30")
            self.assertEqual(agente._extrair_data("30/07 é bom", permitir_dia_isolado=True), "2026-07-30")
            self.assertEqual(agente._extrair_data("pode ser 30/07", permitir_dia_isolado=True), "2026-07-30")
            self.assertEqual(agente._extrair_data("talvez 30/07", permitir_dia_isolado=True), "2026-07-30")
            self.assertEqual(agente._extrair_data("dia 30", permitir_dia_isolado=True), "2026-07-30")

    def test_parser_data_aceita_mes_extenso_iso_e_rejeita_data_impossivel(self) -> None:
        with patch.object(agente, "_hoje", return_value=date(2026, 7, 22)):
            self.assertEqual(agente._extrair_data("entao coloca 28 de fevereiro de 2030"), "2030-02-28")
            self.assertEqual(agente._extrair_data("25 de julho de 2026"), "2026-07-25")
            self.assertEqual(agente._extrair_data("sábado, 25 de julho de 2026"), "2026-07-25")
            self.assertEqual(agente._extrair_data("28/02/2030"), "2030-02-28")
            self.assertEqual(agente._extrair_data("2030-02-28"), "2030-02-28")
            self.assertIsNone(agente._extrair_data("31/02/2030"))

    def test_data_iso_mencionada_pela_ia_tem_prioridade_mesmo_sem_parser_textual(self) -> None:
        telefone = "5511990010032"
        agente._estados_reserva[telefone] = {
            "horario": "18:00",
            "pessoas": 12,
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "data_reserva",
            "etapa": "aguardando_data",
        }
        payload = {
            "resposta": "Resumo: 28/02/2030, 18:00, 12 pessoas. Posso confirmar?",
            "intencao": "fornecimento_dados",
            "dados_confirmados": {},
            "dados_mencionados": {"data": "2030-02-28", "horario": "18:00", "quantidade": 12},
            "correcoes": {},
            "acao": "pedir_confirmacao",
            "deve_avancar_estado": False,
            "campo_sugerido": "confirmacao",
            "confianca": 0.95,
        }

        with patch.object(agente, "_extrair_data", return_value=None):
            resposta = self._processar(telefone, "entao coloca 28 de fevereiro de 2030", payload)

        estado = agente._estados_reserva[telefone]
        self.assertEqual(resposta["texto"], payload["resposta"])
        self.assertEqual(estado["data_reserva"], "2030-02-28")
        self.assertEqual(estado["horario"], "18:00")
        self.assertEqual(estado["pessoas"], 12)
        self.assertEqual(estado["campo_pendente"], "confirmacao")

    def test_data_valida_nao_chama_ia_pos_validacao(self) -> None:
        telefone = "5511990010033"
        agente._estados_reserva[telefone] = {
            "horario": "18:00",
            "pessoas": 12,
            "nome_cliente": "Rodrigo Teste",
            "campo_pendente": "data_reserva",
            "etapa": "aguardando_data",
        }
        payload = {
            "resposta": "Resumo: 28/02/2030, 18:00, 12 pessoas. Posso confirmar?",
            "intencao": "fornecimento_dados",
            "dados_confirmados": {},
            "dados_mencionados": {"data": "2030-02-28", "horario": "18:00", "quantidade": 12},
            "correcoes": {},
            "acao": "pedir_confirmacao",
            "deve_avancar_estado": False,
            "campo_sugerido": "confirmacao",
            "confianca": 0.95,
        }
        retorno = self._json(payload)

        with (
            patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste", "RESERVA_HORARIO_INICIO": "10:00", "RESERVA_HORARIO_FIM": "23:59"}),
            patch.object(agente, "_chamar_groq", return_value=retorno) as chamar,
        ):
            resposta = agente.processar_mensagem(telefone, "entao coloca 28 de fevereiro de 2030", nome_cliente="Rodrigo Teste")

        self.assertEqual(resposta["texto"], payload["resposta"])
        self.assertEqual(chamar.call_count, 1)
        self.assertEqual(agente._estados_reserva[telefone]["data_reserva"], "2030-02-28")

    def test_data_invalida_chama_ia_pos_validacao_uma_vez(self) -> None:
        telefone = "5511990010034"
        agente._estados_reserva[telefone] = {"campo_pendente": "data_reserva", "etapa": "aguardando_data"}
        primeira = {
            "resposta": "Resumo: 31/02/2030, 18:00, 12 pessoas. Posso confirmar?",
            "intencao": "fornecimento_dados",
            "dados_confirmados": {"data": "2030-02-31", "horario": "18:00", "quantidade": 12},
            "dados_mencionados": {},
            "correcoes": {},
            "acao": "pedir_confirmacao",
            "deve_avancar_estado": True,
            "campo_sugerido": "confirmacao",
            "confianca": 0.95,
        }
        segunda = {"resposta": "31 de fevereiro não existe. Me fala outra data válida para eu verificar."}

        with (
            patch.object(agente, "_hoje", return_value=date(2026, 7, 22)),
            patch.dict(os.environ, {"GROQ_API_KEY": "teste", "RESERVA_HORARIO_INICIO": "10:00", "RESERVA_HORARIO_FIM": "23:59"}),
            patch.object(agente, "_chamar_groq", side_effect=[self._json(primeira), self._json(segunda)]) as chamar,
        ):
            resposta = agente.processar_mensagem(telefone, "entao coloca 31/02/2030", nome_cliente="Rodrigo Teste")

        self.assertEqual(resposta["texto"], segunda["resposta"])
        self.assertEqual(chamar.call_count, 2)
        self.assertNotIn("data_reserva", agente._estados_reserva[telefone])

    def test_ia_informar_data_confirmada_e_salva_sem_regex(self) -> None:
        telefone = "5511990010031"
        agente._estados_reserva[telefone] = {"campo_pendente": "data_reserva", "etapa": "aguardando_data"}
        payload = {
            "resposta": "Perfeito, dia 30/07. Qual horário fica melhor?",
            "intencao": "informar_data",
            "dados_confirmados": {"data": "2026-07-30"},
            "dados_mencionados": {},
            "correcoes": {},
            "acao": "continuar_conversa",
            "campo_sugerido": "horario",
            "confianca": 0.95,
        }

        resposta = self._processar(telefone, "isso", payload)

        self.assertEqual(resposta["dados_reserva"]["data_reserva"], "2026-07-30")
        self.assertEqual(agente._estados_reserva[telefone]["campo_pendente"], "horario")


    def test_confirmacao_pendente_responde_contexto_pausa_resumo_correcao_e_confirma(self) -> None:
        telefone = "5511990010040"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2026-07-30",
            "data_reserva_original": "30/07",
            "horario": "20:30",
            "pessoas": 4,
            "nome_cliente": "Rodrigo",
            "campo_pendente": "confirmacao",
            "etapa": "aguardando_confirmacao",
            "aguardando_confirmacao": True,
            "ultima_confirmacao_oferecida_em": "2026-07-23T02:00:00+00:00",
        }

        atraso = {
            "resposta": "Se voce se atrasar um pouco, a equipe pode orientar no momento. Sigo com a reserva anotada.",
            "intencao": "pergunta_contextual",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "dados_incertos": {},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "confirmacao",
            "confianca": 0.93,
        }
        pausa = {
            "resposta": "Tranquilo, nao vou confirmar ainda.",
            "intencao": "comentario",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "confirmacao",
            "confianca": 0.9,
        }
        resumo = {
            "resposta": "Ficou para 30/07/2026, as 20:30, para 4 pessoas, no nome de Rodrigo. Posso confirmar?",
            "intencao": "resumo_reserva",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "correcoes": {},
            "acao": "pedir_confirmacao",
            "deve_avancar_estado": False,
            "campo_sugerido": "confirmacao",
            "confianca": 0.95,
        }
        correcao = {
            "resposta": "Claro, ajustei para 21h.",
            "intencao": "correcao",
            "dados_confirmados": {"horario": "21:00"},
            "dados_mencionados": {},
            "correcoes": {"horario": "21:00"},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "confirmacao",
            "confianca": 0.96,
        }
        comentario = {
            "resposta": "Kkkkk tomara que o tempo ajude. Sigo com os dados da reserva anotados.",
            "intencao": "comentario",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "correcoes": {},
            "acao": "responder",
            "deve_avancar_estado": False,
            "campo_sugerido": "confirmacao",
            "confianca": 0.91,
        }
        confirmar = {
            "resposta": "Reserva confirmada para 30/07/2026, as 21:00, para 4 pessoas, no nome de Rodrigo. Obrigado!",
            "intencao": "comentario",
            "dados_confirmados": {"data": "2026-07-30", "horario": "21:00", "quantidade": 4, "nome": "Rodrigo"},
            "dados_mencionados": {},
            "correcoes": {},
            "acao": "confirmar_reserva",
            "deve_avancar_estado": True,
            "campo_sugerido": "confirmacao",
            "confianca": 0.98,
        }

        resposta_atraso = self._processar(telefone, "E se eu chegar atrasado?", atraso)
        self.assertEqual(resposta_atraso["texto"], atraso["resposta"])
        self.assertNotIn("posso confirmar", agente._normalizar_busca(resposta_atraso["texto"]))
        self.assertEqual(agente._estados_reserva[telefone]["campo_pendente"], "confirmacao")
        self.assertEqual(agente._estados_reserva[telefone]["horario"], "20:30")

        resposta_pausa = self._processar(telefone, "Calma, ainda nao confirma", pausa)
        self.assertEqual(resposta_pausa["texto"], pausa["resposta"])
        self.assertTrue(agente._estados_reserva[telefone]["confirmacao_pausada"])
        self.assertFalse(agente._estados_reserva[telefone]["cliente_autorizou_confirmacao"])

        resposta_resumo = self._processar(telefone, "Me lembra como ficou?", resumo)
        texto_resumo = agente._normalizar_busca(resposta_resumo["texto"])
        self.assertIn("30/07/2026", texto_resumo)
        self.assertIn("20:30", texto_resumo)
        self.assertIn("4 pessoas", texto_resumo)
        self.assertIn("rodrigo", texto_resumo)
        self.assertEqual(resposta_resumo["texto"], resumo["resposta"])

        resposta_correcao = self._processar(telefone, "Na verdade muda para 21h", correcao)
        self.assertEqual(resposta_correcao["texto"], correcao["resposta"])
        self.assertEqual(agente._estados_reserva[telefone]["data_reserva"], "2026-07-30")
        self.assertEqual(agente._estados_reserva[telefone]["horario"], "21:00")
        self.assertTrue(agente._estados_reserva[telefone]["confirmacao_pausada"])
        self.assertNotIn("posso confirmar", agente._normalizar_busca(resposta_correcao["texto"]))

        resposta_comentario = self._processar(telefone, "Se nao estiver chovendo eu vou kkkkk", comentario)
        self.assertEqual(resposta_comentario["texto"], comentario["resposta"])
        self.assertEqual(agente._estados_reserva[telefone]["horario"], "21:00")
        self.assertNotIn("posso confirmar", agente._normalizar_busca(resposta_comentario["texto"]))

        resposta_confirmar = self._processar(telefone, "Agora pode confirmar", confirmar)
        self.assertTrue(resposta_confirmar["reserva_confirmada"])
        self.assertEqual(resposta_confirmar["status_reserva"], "confirmada")
        self.assertEqual(resposta_confirmar["dados_reserva"]["data_reserva"], "2026-07-30")
        self.assertEqual(resposta_confirmar["dados_reserva"]["horario"], "21:00")
        self.assertEqual(resposta_confirmar["dados_reserva"]["pessoas"], 4)
        self.assertNotIn("agora", agente._normalizar_busca(resposta_confirmar["texto"]))

    def test_agora_pode_confirmar_nao_e_pedido_imediato(self) -> None:
        self.assertFalse(agente._eh_pedido_imediato("Agora pode confirmar"))
        self.assertFalse(agente._eh_pedido_imediato("pode confirmar agora"))
        self.assertTrue(agente._eh_pedido_imediato("quero reservar agora"))

    def test_pode_seguir_confirma_reserva_pendente(self) -> None:
        telefone = "5511990010041"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2030-02-28",
            "data_reserva_original": "28 de fevereiro de 2030",
            "horario": "18:00",
            "pessoas": 14,
            "nome_cliente": "Rodrigo",
            "preferencia_local": "Salao",
            "campo_pendente": "confirmacao",
            "etapa": "aguardando_confirmacao",
            "aguardando_confirmacao": True,
        }
        payload = {
            "resposta": "28 de fevereiro de 2030 as 18h, 14 pessoas, preferencia pelo salao. Confirma?",
            "intencao": "pedir_confirmacao",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "correcoes": {},
            "acao": "pedir_confirmacao",
            "deve_avancar_estado": True,
            "campo_sugerido": "confirmacao",
            "confianca": 0.91,
        }

        resposta = self._processar(telefone, "pode seguir", payload)

        self.assertTrue(resposta["reserva_confirmada"])
        self.assertEqual(resposta["status_reserva"], "confirmada")
        self.assertEqual(resposta["dados_reserva"]["data_reserva"], "2030-02-28")
        self.assertEqual(resposta["dados_reserva"]["horario"], "18:00")
        self.assertEqual(resposta["dados_reserva"]["pessoas"], 14)

    def test_prefiro_salao_nao_pausa_por_pedido_humano_falso_da_ia(self) -> None:
        telefone = "5511990010042"
        agente._estados_reserva[telefone] = {
            "data_reserva": "2030-02-28",
            "horario": "18:00",
            "pessoas": 14,
            "nome_cliente": "Rodrigo",
            "campo_pendente": "confirmacao",
            "etapa": "aguardando_confirmacao",
            "aguardando_confirmacao": True,
        }
        payload = {
            "resposta": "Entendido, prefere o salao. 28/02/2030, 18:00, 14 pessoas — confirma?",
            "intencao": "pedido_humano",
            "dados_confirmados": {"data": "2030-02-28", "horario": "18:00", "quantidade": 14, "nome": "Rodrigo"},
            "dados_mencionados": {"data": "2030-02-28", "horario": "18:00", "quantidade": 14},
            "correcoes": {},
            "acao": "pedir_confirmacao",
            "deve_avancar_estado": True,
            "campo_sugerido": "confirmacao",
            "confianca": 0.91,
        }

        resposta = self._processar(telefone, "prefiro o salao", payload)

        self.assertEqual(resposta["texto"], payload["resposta"])
        self.assertEqual(resposta["status_reserva"], "aguardando_confirmacao")
        self.assertEqual(agente._estados_reserva[telefone]["horario"], "18:00")
        self.assertTrue(agente._estados_reserva[telefone]["aguardando_confirmacao"])


if __name__ == "__main__":
    unittest.main()
