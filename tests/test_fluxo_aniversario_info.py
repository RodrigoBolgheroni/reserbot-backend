"""
Testes para o fluxo de apresentação das informações obrigatórias de aniversário.

Regras:
  - Aparecem somente uma vez.
  - Não aparecem no início da coleta.
  - Aparecem somente depois que data, horário, quantidade e espaço estão resolvidos.
  - Quando há direcionamento obrigatório, somente depois do aceite explícito do espaço.
  - Apresentadas imediatamente antes / junto da mensagem de pagamento.
  - Flag persistida como informacoes_aniversario_apresentadas = True.
  - Perguntas explícitas do cliente (bolo, lista, geladeira, utensílios) respondem
    individualmente sem marcar a flag e sem impedir a apresentação futura do bloco.
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from services import agente, config_restaurante, fluxo_reservas


TELEFONE = "5511888888888"

# Texto real da constante (com acentos, como sai do módulo)
TEXTO_ANIVERSARIO = config_restaurante.TEXTO_ANIVERSARIO_OBRIGATORIO


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config_padrao() -> config_restaurante.ConfigRestaurante:
    base = config_restaurante._obter_config_env()
    return replace(
        base,
        nome="Restaurante Teste",
        estabelecimento_id="est-t",
        fonte="supabase",
        quantidade_minima_reserva=11,
        taxa_valor=50.0,
        taxa_convertida_consumacao=True,
        prazo_cancelamento_horas=24,
        pix_chave="00.000.000/0001-00",
        pix_titular="Restaurante Teste",
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
    )


def _conversa_aniversario(**extras):
    base = {"id": "conv-aniv", "origem": "aniversario"}
    base.update(extras)
    return base


def _conversa_webhook(**extras):
    base = {"id": "conv-webhook", "origem": "webhook"}
    base.update(extras)
    return base


def _estado_coleta(**extras):
    """Estado com dados incompletos (ainda em coleta)."""
    estado = {
        "origem_conversa": "aniversario",
        "contexto_aniversario": True,
    }
    estado.update(extras)
    return estado


def _estado_completo(**extras):
    """Estado com todos os campos obrigatórios preenchidos."""
    estado = {
        "data_reserva": "2030-09-14",
        "horario": "13:00",
        "pessoas": 14,
        "nome_cliente": "Maria",
        "origem_conversa": "aniversario",
        "contexto_aniversario": True,
        "preferencia_espaco_id": "salao-1",
        "preferencia_espaco_nome": "Salao",
        "etapa": "dados_completos",
        "campo_pendente": "comprovante",
    }
    estado.update(extras)
    return estado


def _resposta_confirmacao(texto="Dados completos.", dados=None):
    return {
        "texto": texto,
        "reserva_confirmada": True,
        "dados_reserva": dados or {
            "data_reserva": "2030-09-14",
            "horario": "13:00",
            "pessoas": 14,
            "nome_cliente": "Maria",
        },
        "status_reserva": "confirmada",
        "confianca": 0.9,
    }


def _resposta_coleta(texto="Me fala o dia."):
    return {
        "texto": texto,
        "reserva_confirmada": False,
        "dados_reserva": {},
        "status_reserva": "em_coleta",
        "confianca": 0.8,
    }


# ---------------------------------------------------------------------------
# 1. _deve_apresentar_informacoes_aniversario
# ---------------------------------------------------------------------------

class DeveApresentarCondicaoTest(unittest.TestCase):
    """Testa a condição centralizada isoladamente."""

    def setUp(self):
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._estados_reserva.clear()

    def test_false_quando_nao_e_aniversario(self):
        conversa = _conversa_webhook()
        estado = _estado_completo(origem_conversa="webhook", contexto_aniversario=False)
        self.assertFalse(fluxo_reservas._deve_apresentar_informacoes_aniversario(conversa, estado))

    def test_false_quando_flag_ja_setada(self):
        conversa = _conversa_aniversario()
        estado = _estado_completo(informacoes_aniversario_apresentadas=True)
        self.assertFalse(fluxo_reservas._deve_apresentar_informacoes_aniversario(conversa, estado))

    def test_false_sem_data(self):
        conversa = _conversa_aniversario()
        estado = _estado_coleta(horario="13:00", pessoas=14)
        self.assertFalse(fluxo_reservas._deve_apresentar_informacoes_aniversario(conversa, estado))

    def test_false_sem_horario(self):
        conversa = _conversa_aniversario()
        estado = _estado_coleta(data_reserva="2030-09-14", pessoas=14)
        self.assertFalse(fluxo_reservas._deve_apresentar_informacoes_aniversario(conversa, estado))

    def test_false_sem_pessoas(self):
        conversa = _conversa_aniversario()
        estado = _estado_coleta(data_reserva="2030-09-14", horario="13:00")
        self.assertFalse(fluxo_reservas._deve_apresentar_informacoes_aniversario(conversa, estado))

    def test_false_com_regra_obrigatoria_sem_aceite(self):
        conversa = _conversa_aniversario()
        estado = _estado_completo(
            regra_espaco_obrigatoria=True,
            cliente_autorizou_espaco_direcionado=False,
        )
        self.assertFalse(fluxo_reservas._deve_apresentar_informacoes_aniversario(conversa, estado))

    def test_true_com_regra_obrigatoria_e_aceite(self):
        conversa = _conversa_aniversario()
        estado = _estado_completo(
            regra_espaco_obrigatoria=True,
            cliente_autorizou_espaco_direcionado=True,
        )
        self.assertTrue(fluxo_reservas._deve_apresentar_informacoes_aniversario(conversa, estado))

    def test_true_quando_todas_condicoes_satisfeitas(self):
        conversa = _conversa_aniversario()
        estado = _estado_completo()
        self.assertTrue(fluxo_reservas._deve_apresentar_informacoes_aniversario(conversa, estado))

    def test_true_via_origem_conversa(self):
        conversa = _conversa_webhook()  # origem não é aniversário
        estado = _estado_completo(origem_conversa="aniversario")  # mas estado indica
        self.assertTrue(fluxo_reservas._deve_apresentar_informacoes_aniversario(conversa, estado))


# ---------------------------------------------------------------------------
# 2. _texto_contem_informacoes_aniversario
# ---------------------------------------------------------------------------

class TextoContemInformacoesTest(unittest.TestCase):

    def test_detecta_bloco_completo(self):
        self.assertTrue(fluxo_reservas._texto_contem_informacoes_aniversario(TEXTO_ANIVERSARIO))

    def test_nao_detecta_so_bolo(self):
        self.assertFalse(fluxo_reservas._texto_contem_informacoes_aniversario(
            "Pode trazer bolo, sem problema."
        ))

    def test_nao_detecta_resposta_individual_bolo(self):
        # Resposta específica de bolo: não contém "não trabalhamos com lista"
        self.assertFalse(fluxo_reservas._texto_contem_informacoes_aniversario(
            "Pode sim! Conseguimos guardar na geladeira até a hora do parabéns. "
            "Recomendamos trazer pratos e garfos para servir."
        ))

    def test_nao_detecta_texto_coleta(self):
        self.assertFalse(fluxo_reservas._texto_contem_informacoes_aniversario(
            "Me fala o dia que você quer reservar."
        ))

    def test_detecta_variacao_ortografica(self):
        # Sem acentos (como seria após _normalizar_texto internamente)
        texto = (
            "Como e aniversario, nao trabalhamos com lista. Bolo pode levar, "
            "a geladeira guarda e recomendamos pratos e garfos."
        )
        self.assertTrue(fluxo_reservas._texto_contem_informacoes_aniversario(texto))


# ---------------------------------------------------------------------------
# 3. _remover_informacoes_aniversario_prematuras
# ---------------------------------------------------------------------------

class RemoverInformacoesPrematuras(unittest.TestCase):

    def setUp(self):
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._estados_reserva.clear()

    def test_remove_bloco_completo_e_retorna_continuacao(self):
        estado = _estado_coleta(campo_pendente="data_reserva")
        resultado = fluxo_reservas._remover_informacoes_aniversario_prematuras(
            TEXTO_ANIVERSARIO, estado
        )
        self.assertNotIn("não trabalhamos com lista", resultado.lower())
        self.assertNotIn("bolo", resultado.lower())
        self.assertNotIn("geladeira", resultado.lower())
        self.assertGreater(len(resultado), 0)

    def test_preserva_texto_sem_bloco(self):
        texto = "Me fala o dia que você quer reservar."
        estado = _estado_coleta(campo_pendente="data_reserva")
        resultado = fluxo_reservas._remover_informacoes_aniversario_prematuras(texto, estado)
        self.assertEqual(resultado, texto)


# ---------------------------------------------------------------------------
# 4. _corrigir_handoff_aniversario_prematuro
# ---------------------------------------------------------------------------

class CorrigirHandoffPrematuro(unittest.TestCase):

    def setUp(self):
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._estados_reserva.clear()

    def test_substitui_handoff_bolo_equipe(self):
        estado = _estado_coleta(campo_pendente="data_reserva")
        texto = "Sobre o bolo, vou confirmar com a equipe e te respondo."
        resultado = fluxo_reservas._corrigir_handoff_aniversario_prematuro(texto, estado)
        self.assertNotIn("equipe", resultado.lower())
        self.assertNotIn("bolo", resultado.lower())
        self.assertGreater(len(resultado), 0)

    def test_substitui_handoff_lista_restaurante(self):
        estado = _estado_coleta(campo_pendente="data_reserva")
        texto = "A lista de convidados pode ser verificada com o restaurante."
        resultado = fluxo_reservas._corrigir_handoff_aniversario_prematuro(texto, estado)
        self.assertNotIn("lista", resultado.lower())
        self.assertGreater(len(resultado), 0)

    def test_nao_substitui_sem_mencionar_operacao(self):
        estado = _estado_coleta()
        texto = "Vou verificar com a equipe e te respondo."
        resultado = fluxo_reservas._corrigir_handoff_aniversario_prematuro(texto, estado)
        self.assertEqual(resultado, texto)

    def test_nao_substitui_sem_handoff(self):
        estado = _estado_coleta()
        texto = "Me fala o dia que você quer reservar."
        resultado = fluxo_reservas._corrigir_handoff_aniversario_prematuro(texto, estado)
        self.assertEqual(resultado, texto)

    def test_nunca_retorna_bloco_completo(self):
        """Garantir que esta função nunca insere TEXTO_ANIVERSARIO_OBRIGATORIO."""
        estado = _estado_coleta(campo_pendente="data_reserva")
        for texto in [
            "O bolo pode ser confirmado com a equipe.",
            "Sobre lista e bolo, a equipe vai verificar.",
            "Geladeira e utensílios devem ser consultados com o restaurante.",
        ]:
            with self.subTest(texto=texto):
                resultado = fluxo_reservas._corrigir_handoff_aniversario_prematuro(texto, estado)
                self.assertFalse(
                    fluxo_reservas._texto_contem_informacoes_aniversario(resultado),
                    f"Função inseriu bloco completo prematuramente: {resultado!r}",
                )


# ---------------------------------------------------------------------------
# 5. _aplicar_guardrail_aniversario_backend
# ---------------------------------------------------------------------------

class GuardrailAniversarioBackendTest(unittest.TestCase):

    def setUp(self):
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._estados_reserva.clear()

    @patch.object(agente.config_restaurante, "obter_config", return_value=None)
    def test_nao_interfere_em_conversa_nao_aniversario(self, _):
        agente.definir_estado_reserva(TELEFONE, {"origem_conversa": "webhook"})
        conversa = _conversa_webhook()
        resposta = _resposta_coleta("Me fala o dia.")
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Quero reservar uma mesa",
            conversa=conversa,
            resposta=resposta,
        )
        self.assertEqual(resultado["texto"], "Me fala o dia.")

    @patch.object(agente.config_restaurante, "obter_config", return_value=None)
    def test_nao_apresenta_no_inicio_da_coleta(self, _):
        agente.definir_estado_reserva(TELEFONE, _estado_coleta())
        conversa = _conversa_aniversario()
        resposta = _resposta_coleta("Que ótimo comemorar aqui! Me fala o dia.")
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Quero reservar meu aniversário",
            conversa=conversa,
            resposta=resposta,
        )
        self.assertFalse(
            fluxo_reservas._texto_contem_informacoes_aniversario(resultado["texto"]),
            "Guardrail não deveria inserir bloco no início da coleta",
        )
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertFalse(estado.get("informacoes_aniversario_apresentadas", False))

    @patch.object(agente.config_restaurante, "obter_config", return_value=None)
    def test_remove_bloco_prematuro_gerado_pela_ia_sem_handoff(self, _):
        """IA gera as 4 regras espontaneamente sem mencionar 'equipe'."""
        agente.definir_estado_reserva(TELEFONE, _estado_coleta())
        conversa = _conversa_aniversario()
        resposta = _resposta_coleta(TEXTO_ANIVERSARIO)
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Quero reservar meu aniversário",
            conversa=conversa,
            resposta=resposta,
        )
        self.assertFalse(
            fluxo_reservas._texto_contem_informacoes_aniversario(resultado["texto"]),
            "Guardrail deve remover bloco prematuro gerado pela IA",
        )
        # Deve retornar continuação do fluxo (não vazia)
        self.assertGreater(len(resultado["texto"].strip()), 0)
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertFalse(estado.get("informacoes_aniversario_apresentadas", False))

    @patch.object(agente.config_restaurante, "obter_config", return_value=None)
    def test_remove_handoff_prematuro_e_retorna_coleta(self, _):
        """IA diz 'confirmar bolo com a equipe' antes da data ser informada."""
        agente.definir_estado_reserva(TELEFONE, _estado_coleta())
        conversa = _conversa_aniversario()
        resposta = _resposta_coleta(
            "Claro! O bolo pode ser confirmado com a equipe. Me fala o dia."
        )
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Quero reservar meu aniversário",
            conversa=conversa,
            resposta=resposta,
        )
        # O resultado nunca deve conter o bloco completo de aniversário
        self.assertFalse(
            fluxo_reservas._texto_contem_informacoes_aniversario(resultado["texto"])
        )
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertFalse(estado.get("informacoes_aniversario_apresentadas", False))

    @patch.object(agente.config_restaurante, "obter_config")
    def test_pergunta_explicita_bolo_responde_individualmente(self, mock_config):
        """Cliente pergunta sobre bolo durante a coleta."""
        mock_config.return_value = _config_padrao()
        agente.definir_estado_reserva(TELEFONE, _estado_coleta(campo_pendente="data_reserva"))
        conversa = _conversa_aniversario()
        resposta = _resposta_coleta("Sobre o bolo, posso te ajudar.")
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Posso levar bolo?",
            conversa=conversa,
            resposta=resposta,
        )
        # A resposta individual de bolo contém informação de geladeira
        # ("Pode sim! Conseguimos guardar na geladeira até a hora do parabéns.")
        texto = resultado["texto"].lower()
        self.assertIn("geladeira", texto)
        # Resposta individual NÃO contém o bloco completo das 4 regras
        self.assertFalse(
            fluxo_reservas._texto_contem_informacoes_aniversario(resultado["texto"]),
            "Resposta individual de bolo não deve conter o bloco completo",
        )
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertFalse(
            estado.get("informacoes_aniversario_apresentadas", False),
            "Pergunta individual sobre bolo não deve marcar a flag",
        )

    @patch.object(agente.config_restaurante, "obter_config")
    def test_pergunta_explicita_lista_responde_individualmente(self, mock_config):
        """Cliente pergunta sobre lista durante a coleta."""
        mock_config.return_value = _config_padrao()
        agente.definir_estado_reserva(TELEFONE, _estado_coleta(campo_pendente="data_reserva"))
        conversa = _conversa_aniversario()
        resposta = _resposta_coleta("Texto qualquer.")
        for msg in ("Tem lista?", "Precisa de lista de convidados?", "Como funciona a lista"):
            with self.subTest(msg=msg):
                agente.definir_estado_reserva(TELEFONE, _estado_coleta(campo_pendente="data_reserva"))
                resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
                    telefone=TELEFONE,
                    mensagem_cliente=msg,
                    conversa=conversa,
                    resposta=resposta,
                )
                estado = agente.obter_estado_reserva(TELEFONE)
                self.assertFalse(
                    estado.get("informacoes_aniversario_apresentadas", False),
                    f"Mensagem '{msg}' não deve marcar a flag",
                )

    @patch.object(agente.config_restaurante, "obter_config")
    def test_flag_setada_nao_interfere_no_texto(self, mock_config):
        """Quando a flag já está setada, o texto não é alterado."""
        mock_config.return_value = _config_padrao()
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                etapa="aguardando_comprovante",
                informacoes_aniversario_apresentadas=True,
                informacoes_pagamento_apresentadas=True,
            ),
        )
        conversa = _conversa_aniversario()
        resposta = _resposta_coleta("Aguardando seu comprovante.")
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Já paguei",
            conversa=conversa,
            resposta=resposta,
        )
        self.assertEqual(resultado["texto"], "Aguardando seu comprovante.")

    @patch.object(agente.config_restaurante, "obter_config")
    def test_ia_incluiu_bloco_corretamente_no_momento_certo(self, mock_config):
        """IA espontaneamente incluiu as 4 regras no momento correto → apenas seta flag."""
        mock_config.return_value = _config_padrao()
        estado = _estado_completo()
        agente.definir_estado_reserva(TELEFONE, estado)
        conversa = _conversa_aniversario()
        # Texto da IA já contém o bloco completo (gerado corretamente)
        resposta = _resposta_coleta(TEXTO_ANIVERSARIO + " Pague via Pix.")
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Pode confirmar?",
            conversa=conversa,
            resposta=resposta,
        )
        estado_final = agente.obter_estado_reserva(TELEFONE)
        self.assertTrue(
            estado_final.get("informacoes_aniversario_apresentadas"),
            "Flag deve ser setada quando IA gerou corretamente no momento certo",
        )

    @patch.object(agente.config_restaurante, "obter_config")
    def test_nao_apresenta_antes_do_aceite_de_espaco_obrigatorio(self, mock_config):
        """Regra obrigatória de espaço sem aceite: não deve apresentar."""
        mock_config.return_value = _config_padrao()
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                regra_espaco_obrigatoria=True,
                cliente_autorizou_espaco_direcionado=False,
                espaco_direcionado_id="areia-1",
                espaco_direcionado_nome="Areia",
            ),
        )
        conversa = _conversa_aniversario()
        resposta = _resposta_coleta("Precisa aceitar a Areia.")
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Ok, continua",
            conversa=conversa,
            resposta=resposta,
        )
        self.assertFalse(
            fluxo_reservas._texto_contem_informacoes_aniversario(resultado["texto"])
        )
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertFalse(estado.get("informacoes_aniversario_apresentadas", False))


# ---------------------------------------------------------------------------
# 6. _aplicar_fluxo_comprovante — ponto único de inserção automática
# ---------------------------------------------------------------------------

class FluxoComprovantePontoUnicoTest(unittest.TestCase):

    def setUp(self):
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._estados_reserva.clear()

    @patch.object(fluxo_reservas, "registrar_solicitacao_reserva")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config")
    @patch.object(fluxo_reservas.agente, "dados_reserva_obrigatorios_ok", return_value=True)
    def test_apresenta_bloco_no_primeiro_comprovante(self, _val, mock_config, mock_registrar):
        mock_config.return_value = _config_padrao()
        mock_registrar.return_value = {"ok": True, "reserva": {"id": "res-1"}}
        agente.definir_estado_reserva(TELEFONE, _estado_completo())
        resposta = fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="Prefiro o salão",
            cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Maria"},
            conversa=_conversa_aniversario(),
            resposta={
                "texto": "Dados completos.",
                "reserva_confirmada": True,
                "dados_reserva": {
                    "data_reserva": "2030-09-14",
                    "horario": "13:00",
                    "pessoas": 14,
                    "nome_cliente": "Maria",
                },
                "status_reserva": "confirmada",
                "confianca": 0.9,
            },
        )
        self.assertEqual(resposta["status_reserva"], "aguardando_comprovante")
        self.assertTrue(fluxo_reservas._texto_contem_informacoes_aniversario(resposta["texto"]))
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertTrue(estado["informacoes_aniversario_apresentadas"])
        self.assertTrue(estado["informacoes_pagamento_apresentadas"])

    @patch.object(fluxo_reservas, "registrar_solicitacao_reserva")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config")
    @patch.object(fluxo_reservas.agente, "dados_reserva_obrigatorios_ok", return_value=True)
    def test_nao_repete_bloco_na_segunda_mensagem(self, _val, mock_config, mock_registrar):
        mock_config.return_value = _config_padrao()
        mock_registrar.return_value = {"ok": True, "reserva": {"id": "res-1"}}
        # Simular estado após primeira apresentação
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                etapa="aguardando_comprovante",
                campo_pendente="comprovante",
                informacoes_aniversario_apresentadas=True,
                informacoes_pagamento_apresentadas=True,
                informacoes_cancelamento_apresentadas=True,
                reserva_id="res-1",
            ),
        )
        segunda = fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="Já paguei",
            cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Maria"},
            conversa=_conversa_aniversario(),
            resposta={
                "texto": "Envie o comprovante.",
                "reserva_confirmada": False,
                "dados_reserva": {
                    "data_reserva": "2030-09-14",
                    "horario": "13:00",
                    "pessoas": 14,
                    "nome_cliente": "Maria",
                },
                "status_reserva": "aguardando_comprovante",
                "confianca": 0.9,
            },
        )
        self.assertFalse(
            fluxo_reservas._texto_contem_informacoes_aniversario(segunda["texto"]),
            "Bloco não deve repetir na segunda mensagem",
        )

    @patch.object(fluxo_reservas, "registrar_solicitacao_reserva")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config")
    @patch.object(fluxo_reservas.agente, "dados_reserva_obrigatorios_ok", return_value=True)
    def test_nao_apresenta_sem_aceite_espaco_obrigatorio(self, _val, mock_config, _reg):
        mock_config.return_value = _config_padrao()
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                regra_espaco_obrigatoria=True,
                cliente_autorizou_espaco_direcionado=False,
                espaco_direcionado_id="areia-1",
                espaco_direcionado_nome="Areia",
            ),
        )
        resultado = fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="Perfeito",
            cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Maria"},
            conversa=_conversa_aniversario(),
            resposta={
                "texto": "Dados completos.",
                "reserva_confirmada": True,
                "dados_reserva": {
                    "data_reserva": "2030-09-14",
                    "horario": "13:00",
                    "pessoas": 14,
                    "nome_cliente": "Maria",
                },
                "status_reserva": "confirmada",
                "confianca": 0.9,
            },
        )
        # Sem aceite de espaço obrigatório não deve entrar em aguardando_comprovante
        self.assertNotEqual(resultado["status_reserva"], "aguardando_comprovante")
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertFalse(estado.get("informacoes_aniversario_apresentadas", False))

    @patch.object(fluxo_reservas, "registrar_solicitacao_reserva")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config")
    @patch.object(fluxo_reservas.agente, "dados_reserva_obrigatorios_ok", return_value=True)
    def test_sem_direcionamento_obrigatorio_apresenta_normalmente(self, _val, mock_config, mock_reg):
        """Fluxo sem regra_espaco_obrigatoria deve apresentar no momento correto."""
        mock_config.return_value = _config_padrao()
        mock_reg.return_value = {"ok": True, "reserva": {"id": "res-2"}}
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                preferencia_espaco_id="salao-1",
                preferencia_espaco_nome="Salao",
            ),
        )
        resultado = fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="Ok",
            cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Maria"},
            conversa=_conversa_aniversario(),
            resposta={
                "texto": "Dados completos.",
                "reserva_confirmada": True,
                "dados_reserva": {
                    "data_reserva": "2030-09-14",
                    "horario": "13:00",
                    "pessoas": 14,
                    "nome_cliente": "Maria",
                },
                "status_reserva": "confirmada",
                "confianca": 0.9,
            },
        )
        self.assertEqual(resultado["status_reserva"], "aguardando_comprovante")
        self.assertTrue(fluxo_reservas._texto_contem_informacoes_aniversario(resultado["texto"]))
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertTrue(estado["informacoes_aniversario_apresentadas"])


# ---------------------------------------------------------------------------
# 7. Persistência e recarga de estado
# ---------------------------------------------------------------------------

class PersistenciaFlagTest(unittest.TestCase):

    def setUp(self):
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._estados_reserva.clear()

    @patch.object(fluxo_reservas, "registrar_solicitacao_reserva")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config")
    @patch.object(fluxo_reservas.agente, "dados_reserva_obrigatorios_ok", return_value=True)
    def test_flag_presente_no_estado_apos_apresentacao(self, _val, mock_config, mock_reg):
        mock_config.return_value = _config_padrao()
        mock_reg.return_value = {"ok": True, "reserva": {"id": "res-p"}}
        agente.definir_estado_reserva(TELEFONE, _estado_completo())
        fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="Ok",
            cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Maria"},
            conversa=_conversa_aniversario(),
            resposta={
                "texto": "Dados completos.",
                "reserva_confirmada": True,
                "dados_reserva": {
                    "data_reserva": "2030-09-14",
                    "horario": "13:00",
                    "pessoas": 14,
                    "nome_cliente": "Maria",
                },
                "status_reserva": "confirmada",
                "confianca": 0.9,
            },
        )
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertTrue(estado.get("informacoes_aniversario_apresentadas"),
                        "Flag deve estar True no estado em memória")

    def test_flag_preservada_ao_carregar_estado_do_banco(self):
        """Simula recarga do estado a partir de metadata de conversa."""
        conversa = {
            "id": "conv-db",
            "origem": "aniversario",
            "metadata": {
                "estado_reserva": {
                    **_estado_completo(),
                    "etapa": "aguardando_comprovante",
                    "informacoes_aniversario_apresentadas": True,
                    "informacoes_pagamento_apresentadas": True,
                },
                "informacoes_aniversario_apresentadas": True,
            },
        }
        fluxo_reservas._carregar_estado_reserva_conversa(conversa, TELEFONE)
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertTrue(
            estado.get("informacoes_aniversario_apresentadas"),
            "Flag deve ser True após recarga do banco",
        )

    def test_flag_false_quando_banco_nao_tinha_flag(self):
        """Estado do banco sem flag: deve ser False (não assume como True)."""
        conversa = {
            "id": "conv-db2",
            "origem": "aniversario",
            "metadata": {
                "estado_reserva": {
                    **_estado_coleta(),
                },
            },
        }
        fluxo_reservas._carregar_estado_reserva_conversa(conversa, TELEFONE)
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertFalse(estado.get("informacoes_aniversario_apresentadas", False))


# ---------------------------------------------------------------------------
# 8. Cenário completo de produção
# ---------------------------------------------------------------------------

class CenarioCompletoProducaoTest(unittest.TestCase):
    """Reproduz o cenário exato reportado no bug de produção."""

    def setUp(self):
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._estados_reserva.clear()

    @patch.object(agente.config_restaurante, "obter_config")
    def test_handoff_ia_no_inicio_nao_expoe_regras(self, mock_config):
        """
        Passo 1: 'Quero reservar meu aniversário'
        IA diz que bolo/lista devem ser confirmados com a equipe.
        Guardrail deve remover e retornar continuação do fluxo.
        """
        mock_config.return_value = _config_padrao()
        agente.definir_estado_reserva(TELEFONE, _estado_coleta())
        conversa = _conversa_aniversario()
        texto_ia = (
            "Que ótimo comemorar conosco! "
            "Sobre bolo e lista, confirmarei com a equipe. "
            "Me fala o dia que você quer reservar."
        )
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Quero reservar meu aniversário",
            conversa=conversa,
            resposta=_resposta_coleta(texto_ia),
        )
        self.assertFalse(
            fluxo_reservas._texto_contem_informacoes_aniversario(resultado["texto"]),
            "Regras não devem ser expostas no início da conversa",
        )
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertFalse(estado.get("informacoes_aniversario_apresentadas", False))

    @patch.object(agente.config_restaurante, "obter_config")
    def test_coleta_data_horario_nao_expoe_regras(self, mock_config):
        """
        Passos 2-3: cliente informa data e horário.
        Ainda sem quantidade, não deve apresentar.
        """
        mock_config.return_value = _config_padrao()
        agente.definir_estado_reserva(
            TELEFONE, _estado_coleta(data_reserva="2030-09-14", horario="13:00")
        )
        conversa = _conversa_aniversario()
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Dia 14 de setembro às 13h",
            conversa=conversa,
            resposta=_resposta_coleta("Para quantas pessoas?"),
        )
        self.assertFalse(
            fluxo_reservas._texto_contem_informacoes_aniversario(resultado["texto"])
        )
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertFalse(estado.get("informacoes_aniversario_apresentadas", False))

    @patch.object(agente.config_restaurante, "obter_config")
    def test_sem_aceite_espaco_nao_expoe_regras(self, mock_config):
        """
        Passo 4: 26 pessoas → direcionamento obrigatório para Areia.
        Antes do aceite, não deve apresentar.
        """
        mock_config.return_value = _config_padrao()
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                regra_espaco_obrigatoria=True,
                cliente_autorizou_espaco_direcionado=False,
                espaco_direcionado_id="areia-1",
                espaco_direcionado_nome="Areia",
            ),
        )
        conversa = _conversa_aniversario()
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="São 26 pessoas",
            conversa=conversa,
            resposta=_resposta_coleta("Para esse grupo usamos a Areia. Posso seguir?"),
        )
        self.assertFalse(
            fluxo_reservas._texto_contem_informacoes_aniversario(resultado["texto"])
        )
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertFalse(estado.get("informacoes_aniversario_apresentadas", False))

    @patch.object(fluxo_reservas, "registrar_solicitacao_reserva")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config")
    @patch.object(fluxo_reservas.agente, "dados_reserva_obrigatorios_ok", return_value=True)
    def test_apos_aceite_apresenta_uma_unica_vez(self, _val, mock_config, mock_reg):
        """
        Passo 5: cliente aceita Areia.
        Deve apresentar as 4 regras UMA ÚNICA VEZ junto ao pagamento.
        """
        mock_config.return_value = _config_padrao()
        mock_reg.return_value = {"ok": True, "reserva": {"id": "res-prod"}}
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                regra_espaco_obrigatoria=True,
                cliente_autorizou_espaco_direcionado=True,
                espaco_direcionado_id="areia-1",
                espaco_direcionado_nome="Areia",
                preferencia_espaco_id="areia-1",
                preferencia_espaco_nome="Areia",
            ),
        )
        resultado = fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="Pode seguir com a Areia",
            cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Maria"},
            conversa=_conversa_aniversario(),
            resposta={
                "texto": "Perfeito, seguirei com a Areia.",
                "reserva_confirmada": True,
                "dados_reserva": {
                    "data_reserva": "2030-09-14",
                    "horario": "13:00",
                    "pessoas": 26,
                    "nome_cliente": "Maria",
                },
                "status_reserva": "confirmada",
                "confianca": 0.9,
            },
        )
        self.assertEqual(resultado["status_reserva"], "aguardando_comprovante")
        self.assertTrue(fluxo_reservas._texto_contem_informacoes_aniversario(resultado["texto"]))
        self.assertEqual(
            resultado["texto"].count("não trabalhamos com lista"),
            1,
            "As regras devem aparecer exatamente uma vez",
        )
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertTrue(estado["informacoes_aniversario_apresentadas"])

    @patch.object(agente.config_restaurante, "obter_config")
    def test_nao_repete_apos_nova_mensagem_guardail(self, mock_config):
        """
        Passo 6: nova mensagem do cliente depois que bloco foi apresentado.
        Guardrail não deve repetir.
        """
        mock_config.return_value = _config_padrao()
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                etapa="aguardando_comprovante",
                informacoes_aniversario_apresentadas=True,
                informacoes_pagamento_apresentadas=True,
            ),
        )
        conversa = _conversa_aniversario()
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Entendido",
            conversa=conversa,
            resposta=_resposta_coleta("Aguardando seu comprovante."),
        )
        self.assertFalse(
            fluxo_reservas._texto_contem_informacoes_aniversario(resultado["texto"])
        )

    def test_nao_repete_apos_recarga_do_banco(self):
        """
        Passo 7: estado recarregado do banco com flag=True.
        Não deve apresentar de novo.
        """
        conversa = {
            "id": "conv-reload",
            "origem": "aniversario",
            "metadata": {
                "estado_reserva": {
                    **_estado_completo(),
                    "etapa": "aguardando_comprovante",
                    "informacoes_aniversario_apresentadas": True,
                    "informacoes_pagamento_apresentadas": True,
                },
                "informacoes_aniversario_apresentadas": True,
            },
        }
        fluxo_reservas._carregar_estado_reserva_conversa(conversa, TELEFONE)
        estado = agente.obter_estado_reserva(TELEFONE)
        # Após recarga, flag deve ser True → condição centralizada retorna False
        self.assertFalse(
            fluxo_reservas._deve_apresentar_informacoes_aniversario(conversa, estado)
        )


# ---------------------------------------------------------------------------
# 9. Pergunta explícita após apresentação não reseta a flag
# ---------------------------------------------------------------------------

class PerguntaExplicitaAposFlagTest(unittest.TestCase):

    def setUp(self):
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._estados_reserva.clear()

    @patch.object(agente.config_restaurante, "obter_config")
    def test_bolo_apos_apresentacao_nao_reseta_flag(self, mock_config):
        """
        Cliente pergunta sobre bolo DEPOIS que o bloco completo foi apresentado.
        Deve responder individualmente mas NÃO resetar a flag.
        """
        mock_config.return_value = _config_padrao()
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                etapa="aguardando_comprovante",
                informacoes_aniversario_apresentadas=True,
                informacoes_pagamento_apresentadas=True,
            ),
        )
        conversa = _conversa_aniversario()
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Posso levar bolo?",
            conversa=conversa,
            resposta=_resposta_coleta("Aguardando comprovante."),
        )
        # Resposta individual de bolo menciona geladeira
        texto = resultado["texto"].lower()
        self.assertIn("geladeira", texto)
        # Não deve conter o bloco completo (pois já foi apresentado)
        self.assertFalse(
            fluxo_reservas._texto_contem_informacoes_aniversario(resultado["texto"]),
            "Resposta pós-apresentação não deve repetir o bloco",
        )
        estado = agente.obter_estado_reserva(TELEFONE)
        self.assertTrue(
            estado.get("informacoes_aniversario_apresentadas"),
            "Pergunta pós-apresentação não deve resetar a flag",
        )

    @patch.object(fluxo_reservas, "registrar_solicitacao_reserva")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config")
    @patch.object(fluxo_reservas.agente, "dados_reserva_obrigatorios_ok", return_value=True)
    def test_pergunta_bolo_antes_coleta_nao_impede_apresentacao_posterior(
        self, _val, mock_config, mock_reg
    ):
        """
        Cliente pergunta bolo ANTES da data, bot responde individualmente.
        No momento do pagamento, o bloco completo AINDA deve ser apresentado.
        """
        mock_config.return_value = _config_padrao()
        mock_reg.return_value = {"ok": True, "reserva": {"id": "res-bolo"}}

        # Passo 1: pergunta bolo antes da data
        agente.definir_estado_reserva(TELEFONE, _estado_coleta(campo_pendente="data_reserva"))
        conversa = _conversa_aniversario()
        r1 = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Posso levar bolo?",
            conversa=conversa,
            resposta=_resposta_coleta("Texto qualquer."),
        )
        texto_r1 = r1["texto"].lower()
        # Resposta individual de bolo menciona geladeira (não a palavra bolo)
        self.assertIn("geladeira", texto_r1)
        estado_r1 = agente.obter_estado_reserva(TELEFONE)
        self.assertFalse(estado_r1.get("informacoes_aniversario_apresentadas", False))

        # Passo 2: dados completos → comprovante deve apresentar o bloco
        agente.definir_estado_reserva(
            TELEFONE,
            _estado_completo(
                preferencia_espaco_id="salao-1",
                preferencia_espaco_nome="Salao",
            ),
        )
        r2 = fluxo_reservas._aplicar_fluxo_comprovante(
            telefone=TELEFONE,
            mensagem_cliente="Ok",
            cliente={"id": "cli-1", "telefone": TELEFONE, "nome": "Maria"},
            conversa=_conversa_aniversario(),
            resposta={
                "texto": "Dados completos.",
                "reserva_confirmada": True,
                "dados_reserva": {
                    "data_reserva": "2030-09-14",
                    "horario": "13:00",
                    "pessoas": 14,
                    "nome_cliente": "Maria",
                },
                "status_reserva": "confirmada",
                "confianca": 0.9,
            },
        )
        self.assertEqual(r2["status_reserva"], "aguardando_comprovante")
        self.assertTrue(
            fluxo_reservas._texto_contem_informacoes_aniversario(r2["texto"]),
            "Bloco completo deve ser apresentado no pagamento mesmo após pergunta individual anterior",
        )
        estado_r2 = agente.obter_estado_reserva(TELEFONE)
        self.assertTrue(estado_r2["informacoes_aniversario_apresentadas"])


# ---------------------------------------------------------------------------
# 10. Verificar que todo o caminho ao pagamento passa por _aplicar_fluxo_comprovante
# ---------------------------------------------------------------------------

class PontoUnicoInsercaoTest(unittest.TestCase):
    """
    Verifica que _aplicar_guardrail_aniversario_backend, quando deve_apresentar=True,
    nunca insere o bloco — essa responsabilidade é exclusivamente de _aplicar_fluxo_comprovante.
    """

    def setUp(self):
        agente._estados_reserva.clear()

    def tearDown(self):
        agente._estados_reserva.clear()

    @patch.object(agente.config_restaurante, "obter_config")
    @patch.object(fluxo_reservas.config_restaurante, "obter_config")
    def test_guardrail_nao_insere_bloco_quando_deve_apresentar_true(self, _fc, mock_agente):
        """
        Mesmo com todas as condições satisfeitas (deve_apresentar=True),
        o guardrail NÃO insere o bloco — apenas registra a flag se a IA já incluiu.
        """
        mock_agente.return_value = _config_padrao()
        agente.definir_estado_reserva(TELEFONE, _estado_completo())
        conversa = _conversa_aniversario()
        # Texto de entrada que NÃO contém o bloco de aniversário
        resposta = _resposta_coleta("Pague via Pix para confirmar.")
        resultado = fluxo_reservas._aplicar_guardrail_aniversario_backend(
            telefone=TELEFONE,
            mensagem_cliente="Ok, confirmo",
            conversa=conversa,
            resposta=resposta,
        )
        # Guardrail não inseriu o bloco (seria duplicar com _aplicar_fluxo_comprovante)
        self.assertFalse(
            fluxo_reservas._texto_contem_informacoes_aniversario(resultado["texto"]),
            "Guardrail não deve inserir o bloco — isso é responsabilidade de _aplicar_fluxo_comprovante",
        )


if __name__ == "__main__":
    unittest.main()
