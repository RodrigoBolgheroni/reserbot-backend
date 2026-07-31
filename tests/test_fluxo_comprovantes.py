from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from scripts import config_server
from services import agente, comprovantes_reserva, config_restaurante, fluxo_reservas, whatsapp_cloud


TELEFONE = "5511999999999"


def _config_praia() -> config_restaurante.ConfigRestaurante:
    base = config_restaurante._obter_config_env()
    return replace(
        base,
        nome="Praia da Radial",
        estabelecimento_id="est-1",
        fonte="supabase",
        quantidade_minima_reserva=11,
        taxa_valor=50.0,
        taxa_convertida_consumacao=True,
        prazo_cancelamento_horas=24,
        pix_chave="42.538.063/0001-46",
        pix_titular="Praia da Radial",
        exige_comprovante=True,
        espacos=(
            config_restaurante.EspacoRestaurante(
                id="salao-1",
                nome="Salao",
                descricao="Area interna",
                capacidade_maxima=25,
                permite_preferencia=True,
                regras="",
            ),
            config_restaurante.EspacoRestaurante(
                id="areia-1",
                nome="Areia",
                descricao="Area externa",
                capacidade_maxima=None,
                permite_preferencia=True,
                regras="",
            ),
        ),
        faq_conteudos=(
            config_restaurante.FaqConteudo(
                id="faq-bolo",
                categoria="aniversario",
                titulo="Bolo e aniversario",
                conteudo=(
                    "Como e aniversario, nao trabalhamos com lista. Pode trazer bolo, e conseguimos guarda-lo "
                    "na geladeira ate o parabens. Recomendamos trazer pratos e garfos para servir."
                ),
                tags=("bolo", "aniversario"),
            ),
        ),
    )


def _estado_completo(**extras):
    estado = {
        "data_reserva": "2030-08-03",
        "horario": "13:00",
        "pessoas": 14,
        "nome_cliente": "Rodrigo",
        "preferencia_espaco_id": "salao-1",
        "preferencia_espaco_nome": "Salao",
        "origem_conversa": "aniversario",
        "campo_pendente": "comprovante",
        "etapa": "dados_completos",
    }
    estado.update(extras)
    return estado


def _resposta(dados=None, *, texto="Dados completos. Posso confirmar?"):
    return {
        "texto": texto,
        "reserva_confirmada": True,
        "dados_reserva": dados
        or {
            "data_reserva": "2030-08-03",
            "horario": "13:00",
            "pessoas": 14,
            "nome_cliente": "Rodrigo",
        },
        "status_reserva": "confirmada",
        "confianca": 0.9,
    }


def _interpretacao_pergunta(texto: str):
    return {
        "texto": texto,
        "reserva_confirmada": False,
        "dados_reserva": {},
        "dados_confirmados": {},
        "dados_mencionados": {},
        "dados_incertos": {},
        "status_reserva": "em_coleta",
        "confianca": 0.9,
        "intencao": "pergunta_restaurante",
        "acao": "responder",
        "proximo_campo": "",
        "deve_avancar_estado": False,
        "correcoes": {},
        "assunto_atual": "",
        "pergunta_aberta": "",
        "tom_cliente": "",
        "resumo_conversa": "",
        "contrato_novo": True,
    }


class FluxoConclusaoReservaTest(unittest.TestCase):
    def setUp(self) -> None:
        agente._estados_reserva.clear()

    def tearDown(self) -> None:
        agente._estados_reserva.clear()

    def test_quantidade_minima_e_capacidades_vem_da_configuracao(self) -> None:
        config = _config_praia()
        self.assertFalse(agente._pessoas_atende_quantidade_minima(10, config))
        self.assertTrue(agente._pessoas_atende_quantidade_minima(11, config))
        espacos = {espaco.nome: espaco for espaco in config.espacos}
        self.assertEqual(espacos["Salao"].capacidade_maxima, 25)
        self.assertIsNone(espacos["Areia"].capacidade_maxima)

    @patch.object(fluxo_reservas, "registrar_solicitacao_reserva")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config", return_value=_config_praia())
    def test_a_direcionamento_obrigatorio_exige_aceite_antes_do_comprovante(
        self,
        _config,
        registrar_solicitacao,
    ) -> None:
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                data_reserva="2026-08-08",
                horario="14:00",
                pessoas=26,
                preferencia_espaco_id="salao-1",
                preferencia_espaco_nome="Salao",
                espaco_direcionado_id="areia-1",
                espaco_direcionado_nome="Areia",
                regra_espaco_obrigatoria=True,
                cliente_autorizou_espaco_direcionado=False,
                etapa="aguardando_comprovante",
                campo_pendente="comprovante",
            ),
        )

        resposta = fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="08/08/2026 as 14h para 26 pessoas",
            cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Rodrigo"},
            conversa={"id": "conv-a", "origem": "aniversario"},
            resposta=_resposta(
                dados={
                    "data_reserva": "2026-08-08",
                    "horario": "14:00",
                    "pessoas": 26,
                    "nome_cliente": "Rodrigo",
                },
                texto="Perfeito. Envie o comprovante do Pix.",
            ),
        )

        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertEqual(resposta["status_reserva"], "em_coleta")
        self.assertIn("areia", resposta["texto"].lower())
        self.assertIn("posso seguir", resposta["texto"].lower())
        self.assertNotIn("pix", resposta["texto"].lower())
        self.assertEqual(estado["etapa"], "aguardando_confirmacao_espaco")
        self.assertEqual(estado["campo_pendente"], "espaco")
        self.assertFalse(estado["cliente_autorizou_espaco_direcionado"])
        for aceite in ("pode", "tudo bem", "aceito", "pode continuar com a Areia"):
            with self.subTest(aceite=aceite):
                self.assertTrue(agente._eh_aceite_espaco_direcionado(aceite, estado))
        self.assertFalse(agente._eh_aceite_espaco_direcionado("Enviei", estado))
        registrar_solicitacao.assert_not_called()

    @patch.object(fluxo_reservas, "registrar_solicitacao_reserva")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config", return_value=_config_praia())
    @patch.object(fluxo_reservas.agente, "dados_reserva_obrigatorios_ok", return_value=True)
    def test_dados_resolvidos_apresentam_aniversario_pagamento_e_cancelamento_uma_vez(
        self,
        _validar,
        _config,
        registrar_solicitacao,
    ) -> None:
        registrar_solicitacao.return_value = {"ok": True, "reserva": {"id": "res-1"}}
        agente.definir_estado_reserva(TELEFONE, _estado_completo(etapa="dados_completos", campo_pendente="comprovante"))

        resposta = fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="prefiro o salao",
            cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Rodrigo"},
            conversa={"id": "conv-1", "origem": "aniversario"},
            resposta=_resposta(),
        )

        texto = resposta["texto"].lower()
        self.assertEqual(resposta["status_reserva"], "aguardando_comprovante")
        self.assertFalse(resposta["reserva_confirmada"])
        self.assertIn("não trabalhamos com lista", texto)
        self.assertIn("bolo", texto)
        self.assertIn("geladeira", texto)
        self.assertIn("pratos e garfos", texto)
        self.assertIn("r$ 50,00", texto)
        self.assertIn("42.538.063/0001-46", texto)
        self.assertIn("24 horas", texto)
        self.assertEqual(
            resposta["texto"].count(
                "Como é aniversário, não trabalhamos com lista. Pode trazer bolo, e conseguimos guardá-lo "
                "na geladeira até a hora do parabéns. Recomendamos trazer pratos e garfos para servir."
            ),
            1,
        )
        self.assertNotIn("posso confirmar", texto)
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertTrue(estado["informacoes_aniversario_apresentadas"])
        self.assertTrue(estado["informacoes_pagamento_apresentadas"])
        self.assertTrue(estado["informacoes_cancelamento_apresentadas"])
        self.assertEqual(estado["reserva_id"], "res-1")

        segunda = fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="ja paguei",
            cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Rodrigo"},
            conversa={"id": "conv-1", "origem": "aniversario"},
            resposta=_resposta(texto="Sua reserva esta confirmada."),
        )
        self.assertEqual(segunda["status_reserva"], "aguardando_comprovante")
        self.assertFalse(segunda["reserva_confirmada"])
        self.assertIn("imagem ou o pdf", segunda["texto"].lower())
        self.assertNotIn("r$ 50,00", segunda["texto"].lower())
        self.assertNotIn("Como é aniversário", segunda["texto"])

    @patch.object(fluxo_reservas, "registrar_solicitacao_reserva")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config", return_value=_config_praia())
    @patch.object(agente.config_restaurante, "obter_config", return_value=_config_praia())
    def test_fluxo_real_salao_areia_ciente_obrigado_e_sem_envio_nao_confirma(
        self,
        _config_agente,
        _config_fluxo,
        registrar_solicitacao,
    ) -> None:
        registrar_solicitacao.return_value = {"ok": True, "reserva": {"id": "res-real"}}
        agente.definir_estado_reserva(
            TELEFONE,
            {
                "data_reserva": "2026-08-08",
                "horario": "14:00",
                "pessoas": 26,
                "nome_cliente": "Rodrigo",
                "preferencia_espaco_id": "salao-1",
                "preferencia_espaco_nome": "Salao",
                "espaco_direcionado_id": "areia-1",
                "espaco_direcionado_nome": "Areia",
                "espaco_sugerido_id": "areia-1",
                "espaco_sugerido_nome": "Areia",
                "regra_espaco_obrigatoria": True,
                "aguardando_confirmacao_espaco": True,
                "cliente_autorizou_espaco_direcionado": False,
                "origem_conversa": "aniversario",
                "campo_pendente": "espaco",
                "etapa": "aguardando_espaco",
            },
        )
        interpretacao_legada = {
            "texto": "Para confirmar, preciso que voce confirme que esta ciente da taxa de reserva de R$50,00.",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "dados_confirmados": {},
            "dados_mencionados": {},
            "dados_incertos": {},
            "status_reserva": "aguardando_confirmacao",
            "confianca": 0.92,
            "intencao": "pedir_confirmacao",
            "acao": "pedir_confirmacao",
            "proximo_campo": "confirmacao",
            "deve_avancar_estado": False,
            "correcoes": {},
            "assunto_atual": "",
            "pergunta_aberta": "",
            "tom_cliente": "",
            "resumo_conversa": "",
            "contrato_novo": True,
        }

        aceite_areia = agente.aplicar_guardrails_reserva(
            telefone=TELEFONE,
            mensagem_cliente="pode seguir com a Areia",
            interpretacao=interpretacao_legada,
            nome_cliente="Rodrigo",
        )
        self.assertFalse(aceite_areia["reserva_confirmada"])
        self.assertEqual(aceite_areia["status_reserva"], "aguardando_comprovante")
        estado_apos_aceite = agente.obter_estado_reserva(TELEFONE)
        self.assertEqual(estado_apos_aceite["campo_pendente"], "comprovante")
        self.assertFalse(estado_apos_aceite["aguardando_confirmacao"])
        self.assertFalse(estado_apos_aceite["cliente_autorizou_confirmacao"])
        self.assertTrue(estado_apos_aceite["cliente_autorizou_espaco_direcionado"])

        primeira = fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="pode seguir com a Areia",
            cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Rodrigo"},
            conversa={"id": "conv-real", "origem": "aniversario"},
            resposta=aceite_areia,
        )
        texto_primeira = primeira["texto"].lower()
        self.assertEqual(primeira["status_reserva"], "aguardando_comprovante")
        self.assertFalse(primeira["reserva_confirmada"])
        self.assertIn("não trabalhamos com lista", texto_primeira)
        self.assertIn("bolo", texto_primeira)
        self.assertIn("geladeira", texto_primeira)
        self.assertIn("pratos e garfos", texto_primeira)
        self.assertIn("r$ 50,00", texto_primeira)
        self.assertIn("42.538.063/0001-46", texto_primeira)
        self.assertIn("praia da radial", texto_primeira)
        self.assertIn("24 horas", texto_primeira)
        self.assertIn("imagem ou o pdf", texto_primeira)
        self.assertNotIn("posso confirmar", texto_primeira)
        self.assertNotIn("reserva confirmada", texto_primeira)
        estado_informacoes = agente.obter_estado_reserva(TELEFONE)
        self.assertTrue(estado_informacoes["informacoes_aniversario_apresentadas"])
        self.assertTrue(estado_informacoes["informacoes_pagamento_apresentadas"])
        self.assertTrue(estado_informacoes["informacoes_cancelamento_apresentadas"])

        respostas = [
            ("Estou ciente", "imagem ou o pdf"),
            ("Certo, obrigado", "aguardando o comprovante"),
            ("Nao preciso enviar nada nao?", "precisa sim"),
            ("sim", "imagem ou o pdf"),
            ("ja paguei", "imagem ou o pdf"),
        ]
        for mensagem, trecho in respostas:
            with self.subTest(mensagem=mensagem):
                resposta = fluxo_reservas._aplicar_fluxo_comprovante(
                    telefone=TELEFONE,
                    mensagem_cliente=mensagem,
                    cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Rodrigo"},
                    conversa={"id": "conv-real", "origem": "aniversario"},
                    resposta=_resposta(texto="Tudo confirmado entao."),
                )
                texto = resposta["texto"].lower()
                self.assertEqual(resposta["status_reserva"], "aguardando_comprovante")
                self.assertFalse(resposta["reserva_confirmada"])
                self.assertIn(trecho, texto)
                self.assertNotIn("tudo confirmado", texto)
                self.assertNotIn("reserva confirmada", texto)
                estado = agente.obter_estado_reserva(TELEFONE)
                self.assertEqual(estado["etapa"], "aguardando_comprovante")
                self.assertEqual(estado["campo_pendente"], "comprovante")

    @patch.object(fluxo_reservas.comprovantes_reserva, "receber_comprovante")
    @patch.object(fluxo_reservas.supabase, "inserir")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config", return_value=_config_praia())
    def test_c_texto_enviei_nao_cria_comprovante_nem_avanca_analise(
        self,
        _config,
        inserir,
        receber,
    ) -> None:
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                reserva_id="res-c",
                etapa="aguardando_comprovante",
                campo_pendente="comprovante",
                comprovante_status="aguardando_comprovante",
                informacoes_pagamento_apresentadas=True,
                informacoes_cancelamento_apresentadas=True,
                informacoes_aniversario_apresentadas=True,
            ),
        )

        resposta = fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="Enviei",
            cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Rodrigo"},
            conversa={"id": "conv-c", "origem": "aniversario"},
            resposta=_resposta(texto="Comprovante recebido! A equipe vai analisar o comprovante."),
        )

        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertIn("nao apareceu nenhum arquivo", resposta["texto"].lower())
        self.assertNotIn("comprovante recebido", resposta["texto"].lower())
        self.assertEqual(resposta["status_reserva"], "aguardando_comprovante")
        self.assertEqual(estado["etapa"], "aguardando_comprovante")
        self.assertEqual(estado["comprovante_status"], "aguardando_comprovante")
        receber.assert_not_called()
        inserir.assert_not_called()

    def test_g_guardrail_final_substitui_afirmacao_sem_comprovante_persistido(self) -> None:
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                etapa="aguardando_comprovante",
                campo_pendente="comprovante",
                comprovante_status="aguardando_comprovante",
            ),
        )
        for texto_ia in (
            "Comprovante recebido! A equipe vai analisar o comprovante.",
            "A equipe ja recebeu.",
            "O arquivo foi enviado.",
        ):
            with self.subTest(texto_ia=texto_ia):
                resposta = fluxo_reservas._bloquear_afirmacao_comprovante_backend(
                    telefone=TELEFONE,
                    resposta=_resposta(texto=texto_ia),
                    comprovante_persistido=False,
                )

                self.assertIn("nao apareceu nenhum arquivo", resposta["texto"].lower())
                self.assertNotIn("comprovante recebido", resposta["texto"].lower())
                self.assertEqual(resposta["status_reserva"], "aguardando_comprovante")

    @patch.object(fluxo_reservas, "registrar_solicitacao_reserva")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config", return_value=_config_praia())
    @patch.object(fluxo_reservas.agente, "dados_reserva_obrigatorios_ok", return_value=True)
    def test_18h_nao_garante_espaco_e_nao_pede_preferencia(
        self,
        _validar,
        _config,
        registrar_solicitacao,
    ) -> None:
        registrar_solicitacao.return_value = {"ok": True, "reserva": {"id": "res-18"}}
        estado = _estado_completo(
            horario="18:00",
            etapa="dados_completos",
            campo_pendente="comprovante",
            origem_conversa="webhook",
        )
        agente.definir_estado_reserva(TELEFONE, estado)
        dados = dict(_resposta()["dados_reserva"])
        dados["horario"] = "18:00"

        resposta = fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="18h",
            cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Rodrigo"},
            conversa={"id": "conv-18", "origem": "webhook"},
            resposta=_resposta(dados=dados),
        )

        self.assertIn("nao conseguimos garantir", resposta["texto"].lower())
        self.assertNotIn("voces preferem", resposta["texto"].lower())
        self.assertNotIn("preferencia_espaco_id", agente.obter_estado_reserva(TELEFONE))

    @patch.object(fluxo_reservas, "registrar_solicitacao_reserva")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config", return_value=_config_praia())
    @patch.object(fluxo_reservas.agente, "dados_reserva_obrigatorios_ok", return_value=True)
    def test_sem_espaco_em_horario_com_preferencia_permitida_pergunta_uma_vez(
        self,
        _validar,
        _config,
        registrar_solicitacao,
    ) -> None:
        estado = _estado_completo(etapa="dados_completos")
        estado.pop("preferencia_espaco_id")
        estado.pop("preferencia_espaco_nome")
        agente.definir_estado_reserva(TELEFONE, estado)
        resposta = fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="14 pessoas",
            cliente={"telefone": TELEFONE, "nome": "Rodrigo"},
            conversa={"id": "conv-1", "origem": "webhook"},
            resposta=_resposta(texto="Certo."),
        )
        self.assertEqual(resposta["status_reserva"], "em_coleta")
        self.assertIn("salao ou areia", resposta["texto"].lower())
        registrar_solicitacao.assert_not_called()

    def test_registrar_confirmada_sem_acao_humana_e_bloqueado(self) -> None:
        self.assertFalse(
            fluxo_reservas.registrar_reserva_confirmada(
                cliente={"telefone": TELEFONE, "nome": "Rodrigo"},
                conversa={"id": "conv-1"},
                dados_reserva={"data_reserva": "2030-08-03", "horario": "13:00", "pessoas": 14},
            )
        )

    @patch.object(fluxo_reservas.supabase, "inserir")
    @patch.object(fluxo_reservas.dados, "adicionar_reserva")
    def test_registrar_confirmada_direta_mesmo_com_autorizacao_humana_e_bloqueada(
        self,
        adicionar_local,
        inserir_supabase,
    ) -> None:
        resultado = fluxo_reservas.registrar_reserva_confirmada(
            cliente={"telefone": TELEFONE, "nome": "Rodrigo"},
            conversa={"id": "conv-1"},
            dados_reserva={"data_reserva": "2030-08-03", "horario": "13:00", "pessoas": 14},
            autorizacao_humana=True,
        )

        self.assertFalse(resultado)
        inserir_supabase.assert_not_called()
        adicionar_local.assert_not_called()


class ComprovanteWebhookTest(unittest.TestCase):
    def setUp(self) -> None:
        agente._estados_reserva.clear()

    def tearDown(self) -> None:
        agente._estados_reserva.clear()

    def test_parser_whatsapp_aceita_imagem_e_pdf_sem_texto(self) -> None:
        for tipo, corpo, mime in (
            ("image", {"id": "media-img", "mime_type": "image/jpeg"}, "image/jpeg"),
            ("document", {"id": "media-pdf", "mime_type": "application/pdf", "filename": "pix.pdf"}, "application/pdf"),
        ):
            with self.subTest(tipo=tipo):
                payload = {
                    "entry": [
                        {
                            "changes": [
                                {
                                    "value": {
                                        "messages": [{"id": f"wamid-{tipo}", "from": TELEFONE, "type": tipo, tipo: corpo}],
                                        "contacts": [{"wa_id": TELEFONE, "profile": {"name": "Rodrigo"}}],
                                    }
                                }
                            ]
                        }
                    ]
                }
                mensagens = whatsapp_cloud.extrair_mensagens_webhook(payload)
                self.assertEqual(len(mensagens), 1)
                self.assertEqual(mensagens[0]["media"]["mime_type"], mime)
                self.assertEqual(mensagens[0].get("texto", ""), "")

    @patch.object(fluxo_reservas, "atualizar_status_conversa")
    @patch.object(fluxo_reservas, "registrar_mensagem", return_value=True)
    @patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado", return_value={"ok": True, "provider_message_id": "wamid-bot"})
    @patch.object(fluxo_reservas.supabase, "atualizar", return_value={"ok": True})
    @patch.object(fluxo_reservas.comprovantes_reserva, "receber_comprovante")
    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone")
    @patch.object(fluxo_reservas, "_provider_message_id_registrado", return_value=False)
    def test_imagem_e_pdf_mudam_para_aguardando_analise_sem_confirmar(
        self,
        _duplicado,
        buscar_conversa,
        receber,
        _atualizar_supabase,
        _enviar,
        _registrar,
        atualizar_status,
    ) -> None:
        conversa = {
            "id": "conv-1",
            "cliente_telefone": TELEFONE,
            "status": "bot_ativo",
            "origem": "aniversario",
            "metadata": {
                "estado_reserva": _estado_completo(
                    reserva_id="res-1",
                    etapa="aguardando_comprovante",
                    campo_pendente="comprovante",
                    comprovante_status="aguardando_comprovante",
                )
            },
        }
        buscar_conversa.return_value = conversa
        receber.return_value = {"ok": True, "comprovante": {"id": "comp-1"}}

        for indice, mime in enumerate(("image/jpeg", "application/pdf"), start=1):
            with self.subTest(mime=mime):
                conversa["metadata"]["estado_reserva"] = _estado_completo(
                    reserva_id="res-1",
                    etapa="aguardando_comprovante",
                    campo_pendente="comprovante",
                    comprovante_status="aguardando_comprovante",
                )
                resultado = fluxo_reservas.processar_mensagem_webhook(
                    {
                        "telefone": TELEFONE,
                        "provider_message_id": f"wamid-media-{indice}",
                        "media": {
                            "media_id": f"media-{indice}",
                            "tipo": "document" if mime == "application/pdf" else "image",
                            "mime_type": mime,
                            "nome_arquivo": "pix.pdf" if mime == "application/pdf" else "",
                        },
                        "raw": {},
                    }
                )
                self.assertEqual(resultado["status"], "aguardando_analise")
                self.assertFalse(resultado["reserva_confirmada"])
        self.assertEqual(receber.call_count, 2)
        self.assertTrue(all(call.kwargs["status"] == "aguardando_humano" for call in atualizar_status.call_args_list))

    @patch.object(fluxo_reservas, "atualizar_status_conversa")
    @patch.object(fluxo_reservas, "registrar_mensagem", return_value=True)
    @patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado", return_value={"ok": True, "provider_message_id": "wamid-bot"})
    @patch.object(fluxo_reservas.supabase, "atualizar", return_value={"ok": True})
    @patch.object(fluxo_reservas.comprovantes_reserva, "receber_comprovante")
    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone")
    @patch.object(fluxo_reservas, "_provider_message_id_registrado", return_value=False)
    @patch.object(fluxo_reservas.agente, "processar_mensagem")
    def test_f_imagem_e_enviei_no_mesmo_lote_registram_e_respondem_uma_vez(
        self,
        processar_ia,
        _duplicado,
        buscar_conversa,
        receber,
        _atualizar_supabase,
        enviar,
        _registrar,
        _atualizar_status,
    ) -> None:
        buscar_conversa.return_value = {
            "id": "conv-f",
            "cliente_telefone": TELEFONE,
            "status": "bot_ativo",
            "origem": "aniversario",
            "metadata": {
                "estado_reserva": _estado_completo(
                    reserva_id="res-f",
                    etapa="aguardando_comprovante",
                    campo_pendente="comprovante",
                    comprovante_status="aguardando_comprovante",
                )
            },
        }
        receber.return_value = {"ok": True, "comprovante": {"id": "comp-f"}}

        resultados = fluxo_reservas.processar_mensagens_webhook(
            [
                {
                    "telefone": TELEFONE,
                    "timestamp": "2026-08-08T17:00:00+00:00",
                    "provider_message_id": "wamid-f-media",
                    "media": {
                        "media_id": "media-f",
                        "tipo": "image",
                        "mime_type": "image/jpeg",
                        "nome_arquivo": "",
                    },
                    "raw": {},
                },
                {
                    "telefone": TELEFONE,
                    "texto": "Enviei",
                    "timestamp": "2026-08-08T17:00:00.500000+00:00",
                    "provider_message_id": "wamid-f-texto",
                    "raw": {},
                },
            ]
        )

        self.assertEqual(len(resultados), 1)
        self.assertEqual(resultados[0]["status"], "aguardando_analise")
        self.assertFalse(resultados[0]["reserva_confirmada"])
        receber.assert_called_once()
        enviar.assert_called_once()
        processar_ia.assert_not_called()

    @patch.object(fluxo_reservas, "atualizar_status_conversa")
    @patch.object(fluxo_reservas, "registrar_mensagem", return_value=True)
    @patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado", return_value={"ok": True, "provider_message_id": "wamid-bot"})
    @patch.object(fluxo_reservas.comprovantes_reserva, "receber_comprovante", return_value={"ok": False, "erro": "upload falhou"})
    @patch.object(fluxo_reservas, "buscar_conversa_ativa_por_telefone")
    @patch.object(fluxo_reservas, "_provider_message_id_registrado", return_value=False)
    def test_falha_ao_persistir_midia_pede_reenvio_e_mantem_estado_seguro(
        self,
        _duplicado,
        buscar_conversa,
        _receber,
        enviar,
        _registrar,
        atualizar_status,
    ) -> None:
        conversa = {
            "id": "conv-erro",
            "cliente_telefone": TELEFONE,
            "status": "bot_ativo",
            "metadata": {
                "estado_reserva": _estado_completo(
                    reserva_id="res-erro",
                    etapa="aguardando_comprovante",
                    campo_pendente="comprovante",
                    comprovante_status="aguardando_comprovante",
                )
            },
        }
        buscar_conversa.return_value = conversa

        resultado = fluxo_reservas.processar_mensagem_webhook(
            {
                "telefone": TELEFONE,
                "provider_message_id": "wamid-erro",
                "media": {
                    "media_id": "media-erro",
                    "tipo": "image",
                    "mime_type": "image/png",
                },
                "raw": {},
            }
        )

        self.assertEqual(resultado["status"], "aguardando_comprovante")
        self.assertIn("pode envia-lo novamente", enviar.call_args.args[1].lower())
        self.assertEqual(agente.obter_estado_reserva(TELEFONE)["etapa"], "aguardando_comprovante")
        atualizar_status.assert_not_called()

    @patch.object(comprovantes_reserva, "_upload_privado", return_value={"ok": True})
    @patch.object(comprovantes_reserva.whatsapp_cloud, "baixar_midia")
    @patch.object(comprovantes_reserva.supabase, "inserir")
    @patch.object(comprovantes_reserva.supabase, "selecionar", return_value={"ok": True, "data": []})
    def test_servico_armazena_comprovante_privado_sem_url_publica(
        self,
        _selecionar,
        inserir,
        baixar,
        _upload,
    ) -> None:
        baixar.return_value = {"ok": True, "conteudo": b"pdf", "mime_type": "application/pdf", "tamanho": 3}
        inserir.return_value = {"ok": True, "data": [{"id": "comp-1"}]}
        resultado = comprovantes_reserva.receber_comprovante(
            media={"media_id": "media-1", "tipo": "document", "mime_type": "application/pdf", "nome_arquivo": "pix.pdf"},
            provider_message_id="wamid-1",
            conversa_id="conv-1",
            reserva_id="res-1",
        )
        self.assertTrue(resultado["ok"])
        payload = inserir.call_args.args[1]
        self.assertEqual(payload["status_analise"], "aguardando_analise")
        self.assertNotIn("url", payload)
        self.assertEqual(payload["bucket"], "reserva-comprovantes")

    @patch.object(comprovantes_reserva, "_download_privado", return_value={"ok": True, "conteudo": b"imagem"})
    @patch.object(comprovantes_reserva.supabase, "selecionar")
    def test_stream_por_mensagem_confere_conversa_e_preserva_mime(self, selecionar, download) -> None:
        selecionar.side_effect = [
            {
                "ok": True,
                "data": [
                    {
                        "id": "msg-1",
                        "conversa_id": "conv-1",
                        "provider_message_id": "wamid-1",
                        "metadata": {"comprovante_id": "comp-1"},
                    }
                ],
            },
            {
                "ok": True,
                "data": [
                    {
                        "id": "comp-1",
                        "conversa_id": "conv-1",
                        "mime_type": "image/png",
                        "nome_original": "pix.png",
                        "bucket": "privado",
                        "storage_path": "conv-1/pix.png",
                    }
                ],
            },
        ]
        resultado = comprovantes_reserva.baixar_arquivo_mensagem(mensagem_id="msg-1", conversa_id="conv-1")
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["mime_type"], "image/png")
        self.assertEqual(resultado["nome_arquivo"], "pix.png")
        download.assert_called_once_with(bucket="privado", caminho="conv-1/pix.png")
        self.assertEqual(selecionar.call_args_list[0].kwargs["filtros"]["conversa_id"], "eq.conv-1")
        self.assertEqual(selecionar.call_args_list[1].kwargs["filtros"]["conversa_id"], "eq.conv-1")

    @patch.object(comprovantes_reserva, "_download_privado")
    @patch.object(comprovantes_reserva.supabase, "selecionar", return_value={"ok": True, "data": []})
    def test_stream_por_mensagem_recusa_conversa_incorreta(self, _selecionar, download) -> None:
        resultado = comprovantes_reserva.baixar_arquivo_mensagem(mensagem_id="msg-1", conversa_id="outra-conversa")
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["status"], 404)
        download.assert_not_called()

    def test_texto_operacional_de_aniversario_nao_usa_faq_generica(self) -> None:
        config = replace(
            _config_praia(),
            faq_conteudos=(
                config_restaurante.FaqConteudo(
                    id="faq-generica",
                    categoria="aniversario",
                    titulo="Bolo e decoracao",
                    conteudo="Informacoes sobre bolo e decoracao devem ser confirmadas com a equipe.",
                    tags=("bolo", "decoracao"),
                ),
            ),
        )
        self.assertEqual(
            fluxo_reservas._texto_aniversario_config(config),
            "Como é aniversário, não trabalhamos com lista. Pode trazer bolo, e conseguimos guardá-lo "
            "na geladeira até a hora do parabéns. Recomendamos trazer pratos e garfos para servir.",
        )


class InformacoesAniversarioTest(unittest.TestCase):
    def setUp(self) -> None:
        agente._estados_reserva.clear()

    def tearDown(self) -> None:
        agente._estados_reserva.clear()

    def test_contexto_de_campanha_persiste_sem_repetir_aniversario_na_mensagem(self) -> None:
        conversa = {
            "id": "conv-campanha",
            "origem": "whatsapp",
            "metadata": {"campanha": "Campanha de aniversário 2026"},
        }
        fluxo_reservas._carregar_estado_reserva_conversa(conversa, TELEFONE)

        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertTrue(estado["contexto_aniversario"])
        self.assertTrue(fluxo_reservas._conversa_de_aniversario(conversa, estado))

    def test_flag_aniversario_recarrega_do_metadata_persistido(self) -> None:
        conversa = {
            "id": "conv-flag",
            "origem": "aniversario",
            "metadata": {
                "contexto_aniversario": True,
                "informacoes_aniversario_apresentadas": True,
                "estado_reserva": {"etapa": "aguardando_comprovante", "campo_pendente": "comprovante"},
            },
        }
        fluxo_reservas._carregar_estado_reserva_conversa(conversa, TELEFONE)

        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertTrue(estado["contexto_aniversario"])
        self.assertTrue(estado["informacoes_aniversario_apresentadas"])

    @patch.object(agente.config_restaurante, "obter_config", return_value=_config_praia())
    def test_pergunta_bolo_responde_e_retoma_data(self, _config) -> None:
        agente.definir_estado_reserva(
            TELEFONE,
            {"contexto_aniversario": True, "etapa": "aguardando_data", "campo_pendente": "data_reserva"},
        )
        resposta = agente.aplicar_guardrails_reserva(
            telefone=TELEFONE,
            mensagem_cliente="Posso levar bolo?",
            interpretacao=_interpretacao_pergunta("Confirme com a equipe se pode levar bolo."),
            nome_cliente="Rodrigo",
        )

        self.assertEqual(
            resposta["texto"],
            "Pode sim! Conseguimos guardar na geladeira até a hora do parabéns. "
            "Recomendamos trazer pratos e garfos para servir. Me fala o dia que voce quer reservar.",
        )
        self.assertEqual(agente.obter_estado_reserva(TELEFONE)["etapa"], "aguardando_data")

    @patch.object(agente.config_restaurante, "obter_config", return_value=_config_praia())
    def test_pergunta_lista_preserva_dados_e_retoma_quantidade(self, _config) -> None:
        agente.definir_estado_reserva(
            TELEFONE,
            {
                "contexto_aniversario": True,
                "data_reserva": "2030-08-03",
                "horario": "13:00",
                "etapa": "aguardando_quantidade",
                "campo_pendente": "pessoas",
            },
        )
        resposta = agente.aplicar_guardrails_reserva(
            telefone=TELEFONE,
            mensagem_cliente="Vocês trabalham com lista?",
            interpretacao=_interpretacao_pergunta("Detalhes devem ser confirmados com a equipe."),
            nome_cliente="Rodrigo",
        )

        self.assertEqual(
            resposta["texto"],
            "Não trabalhamos com lista de aniversário. Para quantas pessoas sera a reserva?",
        )
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertEqual(estado["data_reserva"], "2030-08-03")
        self.assertEqual(estado["horario"], "13:00")
        self.assertEqual(estado["campo_pendente"], "pessoas")

    @patch.object(agente.config_restaurante, "obter_config", return_value=_config_praia())
    def test_pergunta_geladeira_preserva_etapa_comprovante(self, _config) -> None:
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                contexto_aniversario=True,
                etapa="aguardando_comprovante",
                campo_pendente="comprovante",
                informacoes_aniversario_apresentadas=True,
            ),
        )
        resposta = agente.aplicar_guardrails_reserva(
            telefone=TELEFONE,
            mensagem_cliente="Vocês guardam o bolo?",
            interpretacao=_interpretacao_pergunta("Vou verificar a geladeira com a equipe."),
            nome_cliente="Rodrigo",
        )

        self.assertEqual(resposta["texto"], "Sim, conseguimos guardar na geladeira até a hora do parabéns.")
        self.assertEqual(resposta["status_reserva"], "aguardando_comprovante")
        self.assertEqual(agente.obter_estado_reserva(TELEFONE)["campo_pendente"], "comprovante")

    @patch.object(agente.config_restaurante, "obter_config", return_value=_config_praia())
    def test_pergunta_prato_preserva_dados_e_retoma_horario(self, _config) -> None:
        agente.definir_estado_reserva(
            TELEFONE,
            {
                "contexto_aniversario": True,
                "data_reserva": "2030-08-03",
                "etapa": "aguardando_horario",
                "campo_pendente": "horario",
            },
        )
        resposta = agente.aplicar_guardrails_reserva(
            telefone=TELEFONE,
            mensagem_cliente="Preciso levar prato?",
            interpretacao=_interpretacao_pergunta("Confirme os utensílios com a equipe."),
            nome_cliente="Rodrigo",
        )

        self.assertEqual(
            resposta["texto"],
            "Recomendamos levar pratos e garfos para servir o bolo. "
            "Qual horario dentro desse periodo fica melhor para voce?",
        )
        self.assertEqual(agente.obter_estado_reserva(TELEFONE)["campo_pendente"], "horario")

    @patch.object(fluxo_reservas.config_restaurante, "obter_config", return_value=_config_praia())
    def test_guardrail_final_insere_aniversario_antes_do_comprovante(self, _config) -> None:
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                contexto_aniversario=True,
                etapa="aguardando_comprovante",
                campo_pendente="comprovante",
                informacoes_pagamento_apresentadas=True,
                informacoes_aniversario_apresentadas=False,
            ),
        )
        resposta = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Quero reservar",
            conversa={"id": "conv-guard", "origem": "aniversario"},
            resposta=_resposta(texto="Envie a imagem ou o PDF do comprovante."),
        )

        self.assertLess(resposta["texto"].index("Como é aniversário"), resposta["texto"].index("comprovante"))
        self.assertTrue(agente.obter_estado_reserva(TELEFONE)["informacoes_aniversario_apresentadas"])

    def test_guardrail_substitui_handoff_mas_preserva_decoracao_especifica(self) -> None:
        generico = fluxo_reservas._substituir_handoff_generico_aniversario(
            "Confirme com a equipe se pode levar bolo.",
            categoria="bolo_aniversario",
        )
        decoracao = fluxo_reservas._substituir_handoff_generico_aniversario(
            "Sobre decoração específica, a equipe precisa confirmar com você.",
            categoria="decoracao_aniversario",
        )

        self.assertEqual(generico, config_restaurante.TEXTO_ANIVERSARIO_OBRIGATORIO)
        self.assertEqual(decoracao, "Sobre decoração específica, a equipe precisa confirmar com você.")


class ConfirmacaoHumanaTest(unittest.TestCase):
    def _contexto(self, *, status_comprovante: str = "aguardando_analise", status_reserva: str = "aguardando_analise"):
        comprovante = {
            "id": "comp-1",
            "reserva_id": "res-1",
            "conversa_id": "conv-1",
            "status_analise": status_comprovante,
            "metadata": {},
        }
        reserva = {
            "id": "res-1",
            "conversa_id": "conv-1",
            "cliente_telefone": TELEFONE,
            "data_reserva": "2030-08-03",
            "horario": "13:00:00",
            "pessoas": 14,
            "status": status_reserva,
            "status_pagamento": "aguardando_analise",
            "espaco_id": "esp-1",
            "metadata": {"preferencia_espaco_nome": "Areia"},
        }
        conversa = {
            "id": "conv-1",
            "status": "aguardando_humano",
            "metadata": {"estado_reserva": {"etapa": "aguardando_analise"}},
        }
        return {"ok": True, "comprovante": comprovante, "reserva": reserva, "conversa": conversa}

    @patch.object(fluxo_reservas, "_notificar_decisao_comprovante")
    @patch.object(fluxo_reservas, "_salvar_auditoria_comprovante", return_value=True)
    @patch.object(fluxo_reservas, "_sincronizar_conversa_aprovada", return_value=True)
    @patch.object(fluxo_reservas, "_contexto_decisao_comprovante")
    @patch.object(fluxo_reservas.supabase, "chamar_rpc")
    def test_acao_humana_autenticada_confirma_e_notifica(
        self,
        chamar_rpc,
        contexto,
        _sincronizar,
        _salvar_auditoria,
        notificar,
    ) -> None:
        dados = self._contexto()
        contexto.return_value = dados
        reserva = dados["reserva"]
        chamar_rpc.return_value = {
            "ok": True,
            "data": {**reserva, "status": "confirmada", "status_pagamento": "aprovado"},
        }
        notificar.return_value = {
            "status": "enviado",
            "mensagem_id": "msg-1",
            "provider_message_id": "wamid-ok",
        }

        resultado = fluxo_reservas.aprovar_comprovante_por_humano(
            "comp-1",
            analisado_por={"id": "user-1", "email": "operador@praia.test", "modo": "supabase_auth"},
        )

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["reserva"]["status"], "confirmada")
        chamar_rpc.assert_called_once_with(
            "confirmar_reserva_comprovante",
            {"p_reserva_id": "res-1", "p_analisado_por": "operador@praia.test"},
        )
        texto = notificar.call_args.kwargs["texto"]
        self.assertIn("Pagamento conferido!", texto)
        self.assertIn("03/08/2030 às 13h", texto)
        self.assertIn("na Areia", texto)

    @patch.object(fluxo_reservas, "_notificar_decisao_comprovante")
    @patch.object(fluxo_reservas, "_sincronizar_conversa_aprovada", return_value=True)
    @patch.object(fluxo_reservas, "_contexto_decisao_comprovante")
    @patch.object(fluxo_reservas.supabase, "chamar_rpc")
    def test_aprovacao_repetida_nao_executa_rpc_novamente(
        self,
        chamar_rpc,
        contexto,
        _sincronizar,
        notificar,
    ) -> None:
        contexto.return_value = self._contexto(status_comprovante="aprovado", status_reserva="confirmada")
        notificar.return_value = {"status": "enviado", "idempotente": True}

        resultado = fluxo_reservas.aprovar_comprovante_por_humano("comp-1")

        self.assertTrue(resultado["ok"])
        self.assertTrue(resultado["ja_decidido"])
        chamar_rpc.assert_not_called()

    @patch.object(fluxo_reservas, "_notificar_decisao_comprovante")
    @patch.object(fluxo_reservas, "_salvar_auditoria_comprovante", return_value=True)
    @patch.object(fluxo_reservas, "_sincronizar_conversa_aprovada", return_value=False)
    @patch.object(fluxo_reservas, "_contexto_decisao_comprovante")
    @patch.object(fluxo_reservas.supabase, "chamar_rpc")
    def test_aprovacao_nao_notifica_antes_de_sincronizar_conversa(
        self,
        chamar_rpc,
        contexto,
        _sincronizar,
        _salvar,
        notificar,
    ) -> None:
        dados = self._contexto()
        contexto.return_value = dados
        chamar_rpc.return_value = {
            "ok": True,
            "data": {**dados["reserva"], "status": "confirmada", "status_pagamento": "aprovado"},
        }

        resultado = fluxo_reservas.aprovar_comprovante_por_humano("comp-1")

        self.assertFalse(resultado["ok"])
        self.assertTrue(resultado["decisao_aplicada"])
        self.assertEqual(resultado["status"], 502)
        notificar.assert_not_called()

    @patch.object(fluxo_reservas, "_notificar_decisao_comprovante")
    @patch.object(fluxo_reservas, "_salvar_auditoria_comprovante", return_value=True)
    @patch.object(fluxo_reservas, "_contexto_decisao_comprovante")
    @patch.object(fluxo_reservas.supabase, "atualizar")
    def test_rejeicao_reabre_reserva_conversa_e_registra_motivo(
        self,
        atualizar,
        contexto,
        _salvar,
        notificar,
    ) -> None:
        dados = self._contexto()
        contexto.return_value = dados
        comprovante_rejeitado = {**dados["comprovante"], "status_analise": "rejeitado"}
        reserva_reaberta = {
            **dados["reserva"],
            "status": "aguardando_comprovante",
            "status_pagamento": "aguardando_comprovante",
        }
        conversa_reaberta = {
            **dados["conversa"],
            "status": "bot_ativo",
            "metadata": {
                "estado_reserva": {
                    "etapa": "aguardando_comprovante",
                    "campo_pendente": "comprovante",
                    "comprovante_status": "aguardando_comprovante",
                }
            },
        }
        atualizar.side_effect = [
            {"ok": True, "data": [comprovante_rejeitado]},
            {"ok": True, "data": [reserva_reaberta]},
            {"ok": True, "data": [conversa_reaberta]},
        ]
        notificar.return_value = {"status": "enviado", "provider_message_id": "wamid-rejeicao"}

        resultado = fluxo_reservas.rejeitar_comprovante_por_humano(
            "comp-1",
            motivo="Valor não confere",
            analisado_por={"id": "user-1", "email": "operador@praia.test"},
        )

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["reserva"]["status"], "aguardando_comprovante")
        self.assertEqual(atualizar.call_args_list[0].args[0], "comprovantes_reserva")
        self.assertEqual(atualizar.call_args_list[1].args[0], fluxo_reservas._tabela_reservas())
        conversa_payload = atualizar.call_args_list[2].args[1]
        self.assertEqual(conversa_payload["status"], "bot_ativo")
        self.assertEqual(conversa_payload["metadata"]["estado_reserva"]["campo_pendente"], "comprovante")
        texto = notificar.call_args.kwargs["texto"]
        self.assertEqual(
            texto,
            "Não conseguimos aprovar seu comprovante porque: Valor não confere. "
            "Por favor, envie um novo comprovante ou fale com a nossa equipe.",
        )

    @patch.object(fluxo_reservas, "_notificar_decisao_comprovante")
    @patch.object(fluxo_reservas, "_contexto_decisao_comprovante")
    @patch.object(fluxo_reservas.supabase, "atualizar")
    def test_falha_no_meio_da_rejeicao_desfaz_comprovante(
        self,
        atualizar,
        contexto,
        notificar,
    ) -> None:
        dados = self._contexto()
        contexto.return_value = dados
        atualizar.side_effect = [
            {"ok": True, "data": [{**dados["comprovante"], "status_analise": "rejeitado"}]},
            {"ok": True, "data": []},
            {"ok": True, "data": []},
        ]

        resultado = fluxo_reservas.rejeitar_comprovante_por_humano(
            "comp-1",
            motivo="Pagamento não identificado",
        )

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["status"], 409)
        self.assertEqual(atualizar.call_args_list[2].args[1]["status_analise"], "aguardando_analise")
        notificar.assert_not_called()

    @patch.object(fluxo_reservas.whatsapp, "enviar_com_resultado")
    @patch.object(fluxo_reservas.supabase, "atualizar", return_value={"ok": True})
    @patch.object(fluxo_reservas.supabase, "inserir")
    @patch.object(fluxo_reservas.supabase, "selecionar")
    def test_notificacao_persistida_antes_do_envio_e_idempotente(
        self,
        selecionar,
        inserir,
        _atualizar,
        enviar,
    ) -> None:
        mensagem_persistida = {
            "id": "deterministico",
            "provider_message_id": "wamid-ok",
            "metadata": {"status_entrega": "enviado"},
        }
        selecionar.side_effect = [
            {"ok": True, "data": []},
            {"ok": True, "data": [mensagem_persistida]},
        ]
        inserir.side_effect = lambda _tabela, payload: {"ok": True, "data": [payload]}
        enviar.return_value = {"ok": True, "provider": "cloud", "provider_message_id": "wamid-ok"}
        dados = self._contexto()

        primeira = fluxo_reservas._notificar_decisao_comprovante(
            comprovante=dados["comprovante"],
            reserva=dados["reserva"],
            conversa=dados["conversa"],
            acao="aprovacao",
            texto="Confirmada.",
        )
        segunda = fluxo_reservas._notificar_decisao_comprovante(
            comprovante=dados["comprovante"],
            reserva=dados["reserva"],
            conversa=dados["conversa"],
            acao="aprovacao",
            texto="Confirmada.",
        )

        self.assertEqual(primeira["status"], "enviado")
        self.assertTrue(segunda["idempotente"])
        inserir.assert_called_once()
        enviar.assert_called_once()
        self.assertEqual(inserir.call_args.args[1]["metadata"]["status_entrega"], "pendente")

    def test_rotas_seguras_de_comprovante_sao_reconhecidas(self) -> None:
        self.assertEqual(config_server._id_rota_reserva_recurso("/api/reservas/res-1/comprovantes", "comprovantes"), "res-1")
        self.assertEqual(config_server._id_rota_reserva_recurso("/api/reservas/res-1/confirmar", "confirmar"), "res-1")
        self.assertEqual(config_server._id_rota_comprovante_arquivo("/api/comprovantes/comp-1/arquivo"), "comp-1")
        self.assertEqual(config_server._id_rota_comprovante_acao("/api/comprovantes/comp-1/aprovar", "aprovar"), "comp-1")
        self.assertEqual(config_server._id_rota_comprovante_acao("/api/comprovantes/comp-1/rejeitar", "rejeitar"), "comp-1")
        self.assertEqual(config_server._id_rota_mensagem_midia("/api/mensagens/msg-1/midia"), "msg-1")

    @patch.object(config_server.comprovantes_reserva, "baixar_arquivo_mensagem")
    def test_rota_de_midia_exige_auth_e_repassa_conversa(self, baixar) -> None:
        handler = object.__new__(config_server.ConfigHandler)
        handler.path = "/api/mensagens/msg-1/midia?conversa_id=conv-1"
        with patch.object(config_server.ConfigHandler, "_exigir_config_admin", return_value=False):
            config_server.ConfigHandler._baixar_midia_mensagem(handler, "msg-1")
        baixar.assert_not_called()

        resultado = {"ok": True, "conteudo": b"pdf", "mime_type": "application/pdf", "nome_arquivo": "pix.pdf"}
        with (
            patch.object(config_server.ConfigHandler, "_exigir_config_admin", return_value=True),
            patch.object(config_server.ConfigHandler, "_responder_arquivo_privado") as responder,
        ):
            baixar.return_value = resultado
            config_server.ConfigHandler._baixar_midia_mensagem(handler, "msg-1")
        baixar.assert_called_once_with(mensagem_id="msg-1", conversa_id="conv-1")
        responder.assert_called_once_with(resultado)


if __name__ == "__main__":
    unittest.main()
