from __future__ import annotations

import logging
import json
import os
import re
import threading
import unicodedata
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final, Literal, TypedDict

from services import config_restaurante, ia_fallback


logger = logging.getLogger(__name__)

FLAG_RESERVA_CONFIRMADA: Final[str] = "RESERVA_CONFIRMADA"
MENSAGEM_RECUSA_RESERVA: Final[str] = "Tudo bem! 😊 Se quiser fazer uma reserva depois, é só chamar por aqui."
MODELO_PADRAO: Final[str] = "llama-3.3-70b-versatile"
MAX_HISTORICO_MENSAGENS: Final[int] = 12

MESES: Final[dict[str, int]] = {
    "janeiro": 1,
    "jan": 1,
    "fevereiro": 2,
    "fev": 2,
    "marco": 3,
    "março": 3,
    "mar": 3,
    "abril": 4,
    "abr": 4,
    "maio": 5,
    "mai": 5,
    "junho": 6,
    "jun": 6,
    "julho": 7,
    "jul": 7,
    "agosto": 8,
    "ago": 8,
    "setembro": 9,
    "set": 9,
    "outubro": 10,
    "out": 10,
    "novembro": 11,
    "nov": 11,
    "dezembro": 12,
    "dez": 12,
    }

NUMEROS_POR_EXTENSO: Final[dict[str, int]] = {
    "um": 1,
    "uma": 1,
    "dois": 2,
    "duas": 2,
    "tres": 3,
    "três": 3,
    "quatro": 4,
    "cinco": 5,
    "seis": 6,
    "sete": 7,
    "oito": 8,
    "nove": 9,
    "dez": 10,
}

Role = Literal["system", "user", "assistant"]


class Mensagem(TypedDict):
    role: Role
    content: str


class DadosReserva(TypedDict, total=False):
    data_reserva: str
    horario: str
    pessoas: int
    nome_cliente: str
    observacoes: str
    preferencia_espaco_id: str
    preferencia_espaco_nome: str
    espaco_confirmado: bool
    local_garantido: bool


class RespostaAgente(TypedDict):
    texto: str
    reserva_confirmada: bool
    dados_reserva: DadosReserva
    status_reserva: str
    confianca: float


class ResultadoReservaEstruturada(TypedDict):
    texto: str
    reserva_confirmada: bool
    dados_reserva: DadosReserva
    dados_confirmados: DadosReserva
    dados_mencionados: DadosReserva
    dados_incertos: dict[str, Any]
    status_reserva: str
    confianca: float
    intencao: str
    acao: str
    proximo_campo: str
    deve_avancar_estado: bool
    correcoes: dict[str, Any]
    assunto_atual: str
    pergunta_aberta: str
    tom_cliente: str
    resumo_conversa: str
    contrato_novo: bool


class EstadoReserva(TypedDict, total=False):
    data_reserva: str
    data_reserva_original: str
    data_reserva_fonte: str
    horario: str
    horario_invalido: str
    pessoas: int
    nome_cliente: str
    observacoes: str
    preferencia_espaco_id: str
    preferencia_espaco_nome: str
    preferencia_espaco_permitida: bool
    espaco_confirmado: bool
    local_garantido: bool
    motivo_local_nao_garantido: str
    disponibilidade_espaco_consultada: bool
    campo_pendente: str
    etapa: str
    pedido_imediato: bool
    tentativas_campos: dict[str, int]
    aguardando_confirmacao: bool
    confirmacao_pausada: bool
    cliente_autorizou_confirmacao: bool
    ultima_confirmacao_oferecida_em: str
    conversa_id: str
    ultimo_texto_bot: str
    ultimo_campo_perguntado: str
    ultima_intencao: str
    assunto_atual: str
    pergunta_aberta: str
    tom_cliente: str
    resumo_conversa: str
    dados_confirmados: dict[str, Any]
    dados_mencionados: dict[str, Any]
    dados_incertos: dict[str, Any]
    confirmacao_pendente: dict[str, Any]
    validacoes: list[dict[str, Any]]
    updated_at: str
    versao: int


_historicos: dict[str, list[Mensagem]] = {}
_estados_reserva: dict[str, EstadoReserva] = {}
_locks_conversa: dict[str, threading.RLock] = {}
_locks_conversa_guard = threading.Lock()
_config_restaurante_processamento: ContextVar[config_restaurante.ConfigRestaurante | None] = ContextVar(
    "config_restaurante_processamento",
    default=None,
)
_telefone_processamento: ContextVar[str] = ContextVar("telefone_processamento", default="")

ETAPA_POR_CAMPO: Final[dict[str, str]] = {
    "data_reserva": "aguardando_data",
    "horario": "aguardando_horario",
    "pessoas": "aguardando_quantidade",
    "nome_cliente": "aguardando_nome",
    "telefone": "aguardando_telefone",
    "confirmacao": "aguardando_confirmacao",
}

CAMPO_POR_ETAPA: Final[dict[str, str]] = {etapa: campo for campo, etapa in ETAPA_POR_CAMPO.items()}
INTENCOES_CONVERSACIONAIS_IA: Final[set[str]] = {
    "comentario",
    "brincadeira",
    "indecisao",
    "saudacao",
    "pergunta_bot",
    "pergunta_fonte",
    "pergunta_restaurante",
    "pergunta_disponibilidade",
    "pergunta_contextual",
    "duvida",
}
MAPA_CAMPO_IA: Final[dict[str, str]] = {
    "data": "data_reserva",
    "dia": "data_reserva",
    "data_reserva": "data_reserva",
    "horario": "horario",
    "hora": "horario",
    "quantidade": "pessoas",
    "quantidade_pessoas": "pessoas",
    "pessoas": "pessoas",
    "nome": "nome_cliente",
    "nome_cliente": "nome_cliente",
    "confirmacao": "confirmacao",
}
CHAVES_ESTADO_PERSISTIDO: Final[set[str]] = {
    "data_reserva",
    "data_reserva_original",
    "data_reserva_fonte",
    "horario",
    "horario_invalido",
    "pessoas",
    "nome_cliente",
    "observacoes",
    "preferencia_espaco_id",
    "preferencia_espaco_nome",
    "preferencia_espaco_permitida",
    "espaco_confirmado",
    "local_garantido",
    "motivo_local_nao_garantido",
    "disponibilidade_espaco_consultada",
    "campo_pendente",
    "etapa",
    "pedido_imediato",
    "tentativas_campos",
    "aguardando_confirmacao",
    "confirmacao_pausada",
    "cliente_autorizou_confirmacao",
    "ultima_confirmacao_oferecida_em",
    "conversa_id",
    "ultimo_texto_bot",
    "ultimo_campo_perguntado",
    "ultima_intencao",
    "assunto_atual",
    "pergunta_aberta",
    "tom_cliente",
    "resumo_conversa",
    "dados_confirmados",
    "dados_mencionados",
    "dados_incertos",
    "confirmacao_pendente",
    "validacoes",
    "updated_at",
    "versao",
}


def processar_mensagem(
    telefone: str,
    mensagem_cliente: str,
    nome_cliente: str = "",
    perfil_cliente: Mapping[str, Any] | None = None,
) -> RespostaAgente:
    telefone_limpo = telefone.strip()
    mensagem_limpa = mensagem_cliente.strip()
    if not telefone_limpo or not mensagem_limpa:
        return {
            "texto": "",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "sem_mensagem",
            "confianca": 0.0,
        }

    with _lock_conversa(telefone_limpo):
        config_processamento = config_restaurante.obter_config()
        _log_config_restaurante_processamento(config_processamento)
        token_config = _config_restaurante_processamento.set(config_processamento)
        token_telefone = _telefone_processamento.set(telefone_limpo)
        try:
            return _processar_mensagem_sem_lock(
                telefone=telefone_limpo,
                mensagem_cliente=mensagem_limpa,
                nome_cliente=nome_cliente,
                perfil_cliente=perfil_cliente,
            )
        finally:
            _telefone_processamento.reset(token_telefone)
            _config_restaurante_processamento.reset(token_config)


def _config_restaurante_atual() -> config_restaurante.ConfigRestaurante:
    config = _config_restaurante_processamento.get()
    return config if config is not None else config_restaurante.obter_config()


def _log_config_restaurante_processamento(config: config_restaurante.ConfigRestaurante) -> None:
    possui_configuracao_reserva = bool(
        config.quantidade_minima_reserva is not None
        or config.horarios_permitidos_reserva
        or config.taxa_valor is not None
        or config.taxa_convertida_consumacao
        or config.exige_comprovante
        or config.prazo_cancelamento_horas is not None
        or config.tolerancia_atraso_minutos is not None
    )
    logger.info(
        "Config restaurante carregada para agente: source=%s estabelecimento_id=%s "
        "configuracoes_reserva=%s taxa_valor=%s quantidade_minima=%s horarios_permitidos=%s "
        "espacos=%s faqs_ativas=%s cache_hit=%s tabelas_consultadas=%s",
        config.fonte,
        config.estabelecimento_id,
        possui_configuracao_reserva,
        _taxa_para_log(config.taxa_valor),
        config.quantidade_minima_reserva,
        list(config.horarios_permitidos_reserva),
        len([espaco for espaco in config.espacos if espaco.ativo]),
        len([faq for faq in config.faq_conteudos if faq.ativo]),
        config_restaurante.ultimo_cache_hit(),
        [
            "estabelecimentos",
            "horarios_funcionamento",
            "configuracoes_reserva",
            "espacos",
            "faq_conteudos",
        ],
    )


def _processar_mensagem_sem_lock(
    telefone: str,
    mensagem_cliente: str,
    nome_cliente: str = "",
    perfil_cliente: Mapping[str, Any] | None = None,
) -> RespostaAgente:
    telefone_limpo = telefone.strip()
    mensagem_limpa = mensagem_cliente.strip()

    if not telefone_limpo or not mensagem_limpa:
        return {
            "texto": "",
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "sem_mensagem",
            "confianca": 0.0,
        }

    estado_antes = _serializar_estado_reserva(_estados_reserva.get(telefone_limpo, {}))
    logger.info(
        "DIAG_RESERVA mensagem_recebida telefone=%s texto_bruto=%r estado_antes=%s",
        telefone_limpo,
        mensagem_limpa,
        json.dumps(estado_antes, ensure_ascii=False, sort_keys=True),
    )

    estado_atual = _estados_reserva.get(telefone_limpo, {})
    if _eh_recusa_reserva(mensagem_limpa) and not _confirmacao_ativa(estado_atual):
        logger.info("Cliente recusou convite de reserva. telefone=%s", telefone_limpo)
        limpar_historico(telefone_limpo)
        return {
            "texto": MENSAGEM_RECUSA_RESERVA,
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": "sem_interesse",
            "confianca": 1.0,
        }

    historico = _historicos.setdefault(telefone_limpo, [])
    historico.append({"role": "user", "content": mensagem_limpa})
    _limitar_historico(historico)

    if not ia_fallback.tem_provedor_configurado():
        logger.warning("Nenhum provedor de IA configurado. Usando handoff tecnico.")
        texto_modelo = _payload_fallback_tecnico()
    else:
        try:
            mensagens_groq = [
                _mensagem_sistema(
                    nome_cliente,
                    perfil_cliente=perfil_cliente,
                    telefone=telefone_limpo,
                    mensagem_cliente=mensagem_limpa,
                ),
                _mensagem_contexto_reserva(telefone_limpo),
                *historico,
            ]
            logger.info(
                "DIAG_RESERVA historico_enviado_ia telefone=%s mensagens=%s",
                telefone_limpo,
                json.dumps(mensagens_groq, ensure_ascii=False),
            )
            texto_modelo = _chamar_groq(
                mensagens=mensagens_groq,
                modelo=os.getenv("GROQ_PRIMARY_MODEL", "").strip() or os.getenv("GROQ_MODEL", MODELO_PADRAO),
                response_format_json=True,
            )
        except Exception:
            logger.exception("Falha inesperada ao processar mensagem com IA.")
            texto_modelo = _payload_fallback_tecnico()
    logger.info("DIAG_RESERVA json_bruto_ia telefone=%s resposta=%s", telefone_limpo, texto_modelo)
    json_valido = _extrair_json_resposta(texto_modelo) is not None
    logger.info("DIAG_RESERVA json_estruturado telefone=%s valido=%s tentativa_reparo=%s", telefone_limpo, json_valido, not json_valido)
    texto_para_interpretar = texto_modelo
    reparo_aplicado = False
    if not json_valido:
        texto_reparado = _reparar_interpretacao_texto_simples(
            telefone=telefone_limpo,
            mensagem_cliente=mensagem_limpa,
            resposta_natural=texto_modelo,
        )
        if texto_reparado:
            texto_para_interpretar = texto_reparado
            reparo_aplicado = True
        logger.info(
            "DIAG_RESERVA reparo_json_resultado telefone=%s sucesso=%s resposta=%s",
            telefone_limpo,
            bool(texto_reparado),
            texto_reparado or "",
        )

    interpretacao = interpretar_resposta_modelo(
        texto_modelo=texto_para_interpretar,
        mensagem_cliente=mensagem_limpa,
    )
    if reparo_aplicado:
        interpretacao["texto"] = remover_flag_reserva(texto_modelo)
    logger.info(
        "DIAG_RESERVA interpretacao_ia telefone=%s intencao=%s dados_confirmados=%s dados_mencionados=%s correcoes=%s",
        telefone_limpo,
        interpretacao.get("intencao", ""),
        json.dumps(interpretacao.get("dados_confirmados", {}), ensure_ascii=False, sort_keys=True),
        json.dumps(interpretacao.get("dados_mencionados", {}), ensure_ascii=False, sort_keys=True),
        json.dumps(interpretacao.get("correcoes", {}), ensure_ascii=False, sort_keys=True),
    )
    resposta_segura = aplicar_guardrails_reserva(
        telefone=telefone_limpo,
        mensagem_cliente=mensagem_limpa,
        interpretacao=interpretacao,
        nome_cliente=nome_cliente,
    )
    resposta_segura = _aplicar_guardrails_configuracao_resposta(
        telefone=telefone_limpo,
        mensagem_cliente=mensagem_limpa,
        texto_ia=str(interpretacao.get("texto") or ""),
        resposta=resposta_segura,
        config=_config_restaurante_atual(),
    )
    reserva_confirmada = resposta_segura["reserva_confirmada"]
    texto_cliente = resposta_segura["texto"]

    if texto_cliente:
        historico.append({"role": "assistant", "content": texto_cliente})
        _limitar_historico(historico)
        _registrar_ultima_resposta(telefone_limpo, texto_cliente)

    if reserva_confirmada:
        limpar_historico(telefone_limpo)

    estado_depois = _serializar_estado_reserva(_estados_reserva.get(telefone_limpo, {}))
    logger.info(
        "DIAG_RESERVA resposta_final telefone=%s texto=%r status=%s estado_depois=%s",
        telefone_limpo,
        texto_cliente,
        resposta_segura["status_reserva"],
        json.dumps(estado_depois, ensure_ascii=False, sort_keys=True),
    )

    return {
        "texto": texto_cliente,
        "reserva_confirmada": reserva_confirmada,
        "dados_reserva": resposta_segura["dados_reserva"],
        "status_reserva": resposta_segura["status_reserva"],
        "confianca": resposta_segura["confianca"],
    }


def _lock_conversa(telefone: str) -> threading.RLock:
    with _locks_conversa_guard:
        lock = _locks_conversa.get(telefone)
        if lock is None:
            lock = threading.RLock()
            _locks_conversa[telefone] = lock
        return lock


def limpar_historico(telefone: str) -> None:
    telefone_limpo = telefone.strip()
    _historicos.pop(telefone_limpo, None)
    _estados_reserva.pop(telefone_limpo, None)


def definir_estado_reserva(telefone: str, estado: Mapping[str, Any] | None) -> None:
    telefone_limpo = telefone.strip()
    if not telefone_limpo:
        return

    with _lock_conversa(telefone_limpo):
        estado_normalizado = _normalizar_estado_reserva_persistido(estado)
        if estado_normalizado:
            _estados_reserva[telefone_limpo] = estado_normalizado
        else:
            _estados_reserva.pop(telefone_limpo, None)


def obter_estado_reserva(telefone: str) -> dict[str, Any]:
    telefone_limpo = telefone.strip()
    if not telefone_limpo:
        return {}

    with _lock_conversa(telefone_limpo):
        estado = _estados_reserva.get(telefone_limpo)
        if not estado:
            return {}
        estado["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        estado["versao"] = 2
        return _serializar_estado_reserva(estado)


def _normalizar_estado_reserva_persistido(estado: Mapping[str, Any] | None) -> EstadoReserva:
    if not isinstance(estado, Mapping):
        return {}

    normalizado: EstadoReserva = {}
    for chave in CHAVES_ESTADO_PERSISTIDO:
        valor = estado.get(chave)
        if valor in (None, "", [], {}):
            continue
        if chave == "pessoas":
            pessoas = _pessoas_json(valor)
            if pessoas is not None:
                normalizado["pessoas"] = pessoas
            continue
        if chave == "tentativas_campos" and isinstance(valor, Mapping):
            normalizado["tentativas_campos"] = {
                str(campo): int(total)
                for campo, total in valor.items()
                if str(campo) in {"data_reserva", "horario", "pessoas", "nome_cliente", "telefone"}
                and str(total).isdigit()
            }
            continue
        if chave in {"dados_confirmados", "dados_mencionados", "dados_incertos", "confirmacao_pendente"} and isinstance(valor, Mapping):
            normalizado[chave] = dict(valor)  # type: ignore[literal-required]
            continue
        if chave == "validacoes" and isinstance(valor, list):
            normalizado["validacoes"] = [dict(item) for item in valor if isinstance(item, Mapping)][-10:]
            continue
        if chave in {"pedido_imediato", "aguardando_confirmacao", "confirmacao_pausada", "cliente_autorizou_confirmacao"}:
            normalizado[chave] = bool(valor)  # type: ignore[literal-required]
            continue
        if chave == "versao":
            try:
                normalizado["versao"] = int(valor)
            except (TypeError, ValueError):
                normalizado["versao"] = 2
            continue
        normalizado[chave] = str(valor).strip()  # type: ignore[literal-required]

    campo = _campo_aguardado(normalizado)
    if campo:
        _definir_campo_pendente(normalizado, campo)
    return normalizado


def _serializar_estado_reserva(estado: Mapping[str, Any]) -> dict[str, Any]:
    serializado: dict[str, Any] = {}
    for chave in CHAVES_ESTADO_PERSISTIDO:
        valor = estado.get(chave)
        if valor in (None, "", [], {}):
            continue
        if isinstance(valor, Mapping):
            serializado[chave] = dict(valor)
        elif isinstance(valor, list):
            serializado[chave] = [dict(item) if isinstance(item, Mapping) else item for item in valor][-10:]
        else:
            serializado[chave] = valor
    return serializado


def _mensagem_contexto_reserva(telefone: str) -> Mensagem:
    estado = _estados_reserva.get(telefone, {})
    dados = _dados_reserva_do_estado(estado)
    campo = _campo_retomada_apos_informacao(estado, telefone) if estado else "data_reserva"
    payload = {
        "dados_confirmados": estado.get("dados_confirmados") or dados,
        "dados_mencionados_nao_confirmados": estado.get("dados_mencionados", {}),
        "dados_incertos": estado.get("dados_incertos", {}),
        "dados_reserva_ja_coletados": dados,
        "campo_sugerido": campo,
        "campo_pendente_orientativo": estado.get("campo_pendente", ""),
        "etapa_interna_orientativa": estado.get("etapa", ""),
        "confirmacao_pausada": bool(estado.get("confirmacao_pausada")),
        "cliente_autorizou_confirmacao": bool(estado.get("cliente_autorizou_confirmacao")),
        "ultima_confirmacao_oferecida_em": estado.get("ultima_confirmacao_oferecida_em", ""),
        "ultima_intencao": estado.get("ultima_intencao", ""),
        "assunto_atual": estado.get("assunto_atual", ""),
        "pergunta_aberta": estado.get("pergunta_aberta", ""),
        "resumo_conversa": estado.get("resumo_conversa", ""),
        "tom_cliente": estado.get("tom_cliente", ""),
        "validacoes_backend": estado.get("validacoes", []),
    }
    return {
        "role": "system",
        "content": (
            "Contexto interno da conversa atual. Use isto para responder de forma natural, "
            "sem repetir perguntas ja feitas e sem perder dados coletados: "
            f"{json.dumps(payload, ensure_ascii=False)}. "
            "O campo pendente e apenas uma orientacao, nao uma ordem. "
            "Antes de extrair dados, entenda o significado da mensagem. "
            "Dados mencionados nao sao dados confirmados."
        ),
    }


def _registrar_ultima_resposta(telefone: str, texto: str) -> None:
    estado = _estados_reserva.get(telefone)
    if estado is None:
        return
    estado["ultimo_texto_bot"] = texto.strip()
    campo = _campo_aguardado(estado)
    if campo:
        estado["ultimo_campo_perguntado"] = campo


def _log_resposta_substituida(*, texto_ia: str, texto_final: str, funcao: str, motivo: str) -> None:
    texto_ia_limpo = remover_flag_reserva(str(texto_ia or "")).strip()
    texto_final_limpo = str(texto_final or "").strip()
    if texto_ia_limpo and texto_final_limpo and texto_ia_limpo == texto_final_limpo:
        return
    logger.info(
        "DIAG_RESERVA resposta_sobrescrita funcao=%s motivo=%s texto_ia=%r texto_final=%r",
        funcao,
        motivo,
        texto_ia_limpo,
        texto_final_limpo,
    )


def _texto_ia_ou_fallback_tecnico(interpretacao: Mapping[str, Any]) -> str:
    texto = remover_flag_reserva(str(interpretacao.get("texto") or "")).strip()
    return texto or _resposta_contingencia()


def _resposta_preservando_ia(
    *,
    estado: Mapping[str, Any],
    interpretacao: Mapping[str, Any],
    status_reserva: str = "em_coleta",
    reserva_confirmada: bool = False,
    dados_reserva: Mapping[str, Any] | None = None,
) -> RespostaAgente:
    return {
        "texto": _texto_ia_ou_fallback_tecnico(interpretacao),
        "reserva_confirmada": reserva_confirmada,
        "dados_reserva": dict(dados_reserva or _dados_reserva_do_estado(estado)),
        "status_reserva": status_reserva,
        "confianca": float(interpretacao.get("confianca") or 0.0),
    }


def _aplicar_guardrails_configuracao_resposta(
    *,
    telefone: str,
    mensagem_cliente: str,
    texto_ia: str,
    resposta: RespostaAgente,
    config: config_restaurante.ConfigRestaurante,
) -> RespostaAgente:
    texto_original = str(resposta.get("texto") or "")
    _, motivos = _corrigir_texto_por_configuracao_estruturada(
        texto=texto_original,
        mensagem_cliente=mensagem_cliente,
        config=config,
    )
    if not motivos:
        return resposta

    motivo = ",".join(motivos)
    logger.warning(
        "Resposta da IA manteve prioridade apesar de alerta de configuracao: telefone=%s motivos=%s source=%s "
        "estabelecimento_id=%s taxa_valor=%s quantidade_minima=%s horarios_permitidos=%s",
        telefone,
        motivo,
        config.fonte,
        config.estabelecimento_id,
        _taxa_para_log(config.taxa_valor),
        config.quantidade_minima_reserva,
        list(config.horarios_permitidos_reserva),
    )
    return resposta


def _corrigir_texto_por_configuracao_estruturada(
    *,
    texto: str,
    mensagem_cliente: str,
    config: config_restaurante.ConfigRestaurante,
) -> tuple[str, list[str]]:
    texto_limpo = remover_flag_reserva(str(texto or "")).strip()
    if not texto_limpo:
        return texto_limpo, []

    motivos = _motivos_contradicao_configuracao(texto_limpo, config)
    if not motivos:
        return texto_limpo, []
    return texto_limpo, motivos


def _motivos_contradicao_configuracao(
    texto: str,
    config: config_restaurante.ConfigRestaurante,
) -> list[str]:
    normalizado = _normalizar_busca(texto)
    motivos: list[str] = []

    if _taxa_configurada(config) and _texto_nega_taxa_reserva(normalizado):
        motivos.append("taxa_reserva_contradita")
    if config.exige_comprovante and _texto_nega_comprovante_pix(normalizado):
        motivos.append("comprovante_pix_contradito")
    if config.quantidade_minima_reserva is not None and _texto_nega_quantidade_minima(normalizado):
        motivos.append("quantidade_minima_contradita")
    if config.politica_cancelamento and _texto_contradiz_cancelamento(normalizado):
        motivos.append("politica_cancelamento_contradita")
    if config.horarios_permitidos_reserva and _texto_inventa_horario_reserva(texto, normalizado, config):
        motivos.append("horarios_reserva_fora_config")

    return motivos


def _taxa_configurada(config: config_restaurante.ConfigRestaurante) -> bool:
    return config.taxa_valor is not None and config.taxa_valor > 0


def _texto_nega_taxa_reserva(normalizado: str) -> bool:
    return bool(
        re.search(r"\bnao\s+(?:temos|ha|existe)\b.{0,80}\b(?:taxa|valor|custo|cobranca)\b", normalizado)
        or re.search(r"\bnao\s+cobramos\b.{0,80}\b(?:reserva|taxa|valor|custo|r\$|50)\b", normalizado)
        or re.search(r"\b(?:sem|nenhum|nenhuma)\s+(?:taxa|valor|custo|cobranca)\b", normalizado)
        or re.search(r"\breserva\b.{0,60}\bsem custo\b", normalizado)
        or re.search(r"\bsem custo adicional\b", normalizado)
        or re.search(r"\breserva gratuita\b", normalizado)
    )


def _texto_nega_comprovante_pix(normalizado: str) -> bool:
    return bool(
        re.search(r"\bnao\s+(?:precisa|precisamos|necessita|exigimos|exige)\b.{0,80}\bcomprovante\b", normalizado)
        or re.search(r"\bcomprovante\b.{0,40}\bnao\s+(?:e|eh)\s+obrigatorio\b", normalizado)
        or re.search(r"\bsem comprovante\b", normalizado)
    )


def _texto_nega_quantidade_minima(normalizado: str) -> bool:
    return bool(
        re.search(r"\bnao\s+(?:temos|ha|existe)\b.{0,80}\b(?:quantidade minima|minimo de pessoas|minimo)\b", normalizado)
        or re.search(r"\b(?:sem|nenhum|nenhuma)\s+(?:quantidade minima|minimo de pessoas|minimo)\b", normalizado)
        or re.search(r"\bqualquer quantidade\b", normalizado)
    )


def _texto_contradiz_cancelamento(normalizado: str) -> bool:
    if not re.search(r"\b(cancelamento|cancelar|estorno|desistencia)\b", normalizado):
        return False
    return bool(
        re.search(r"\bsem prazo\b", normalizado)
        or re.search(r"\ba qualquer momento\b", normalizado)
        or re.search(r"\bestorno\b.{0,40}\bsempre\b", normalizado)
        or re.search(r"\bnao\s+(?:temos|ha|existe)\b.{0,80}\b(?:politica|prazo|regra)\b", normalizado)
    )


def _texto_inventa_horario_reserva(
    texto: str,
    normalizado: str,
    config: config_restaurante.ConfigRestaurante,
) -> bool:
    analise = _analisar_guardrail_horarios_reserva(texto, config)
    logger.info(
        "Guardrail horarios reserva: horarios_detectados=%s horarios_invalidos=%s "
        "contexto_semantico=%s motivo_exato=%s guardrail_aplicado=%s",
        analise["horarios_detectados"],
        analise["horarios_invalidos"],
        analise["contexto_semantico"],
        analise["motivo_exato"],
        analise["guardrail_aplicado"],
    )
    return bool(analise["guardrail_aplicado"])


def _analisar_guardrail_horarios_reserva(
    texto: str,
    config: config_restaurante.ConfigRestaurante,
) -> dict[str, Any]:
    permitidos = set(config.horarios_permitidos_reserva)
    ocorrencias = _ocorrencias_horarios_texto(texto)
    detectados: list[str] = []
    invalidos: list[str] = []
    contextos: list[str] = []
    motivos: list[str] = []

    for ocorrencia in ocorrencias:
        horario = str(ocorrencia["horario"])
        if horario not in detectados:
            detectados.append(horario)
        contexto = _classificar_contexto_horario_resposta(
            str(ocorrencia["sentenca"]),
            contexto_local=str(ocorrencia.get("contexto_local") or ""),
        )
        contextos.append(f"{horario}:{contexto}")
        if horario in permitidos:
            continue
        if contexto == "quantidade":
            motivos.append(f"{horario}:numero_quantidade")
            continue
        if contexto == "rejeicao":
            motivos.append(f"{horario}:horario_rejeitado_pelo_bot")
            continue
        if contexto == "mencao_cliente":
            motivos.append(f"{horario}:mencao_sem_oferta")
            continue
        if contexto != "operacional":
            motivos.append(f"{horario}:contexto_nao_operacional")
            continue
        invalidos.append(horario)
        motivos.append(f"{horario}:oferta_aceite_confirmacao_fora_config")

    return {
        "horarios_detectados": detectados,
        "horarios_invalidos": invalidos,
        "contexto_semantico": contextos,
        "motivo_exato": motivos or ["sem_horario_concreto"],
        "guardrail_aplicado": bool(invalidos),
    }


def _classificar_contexto_horario_resposta(sentenca: str, *, contexto_local: str = "") -> str:
    normalizado = _normalizar_busca(sentenca)
    local = _normalizar_busca(contexto_local)
    if re.search(
        r"\b(pessoa|pessoas|convidado|convidados|grupo|grupos|quantidade|"
        r"capacidade|limite|minimo|maximo|maxima|varia|variar|serao|vao|aproximad[ao]s?)\b",
        local,
    ):
        return "quantidade"
    if re.search(
        r"\b(nao|nunca|indisponivel|fora|rejeit|nao consigo|nao temos|nao aceitamos|"
        r"nao e aceito|nao esta entre|nao faz parte|nao atendemos|fica fora)\b",
        normalizado,
    ):
        return "rejeicao"
    if re.search(
        r"\b(voce mencionou|voce falou|voce disse|cliente mencionou|cliente falou|"
        r"se chegar|sair do trabalho|sai do trabalho|chegaria|mencionou|citado|citou)\b",
        normalizado,
    ):
        return "mencao_cliente"
    if re.search(
        r"\b(horarios?\s+(?:aceitos|permitidos|disponiveis|disponivel|para reserva|de reserva|sao)|"
        r"temos\s+(?:horario|mesa|vaga|disponibilidade)|"
        r"(?:aceitamos|permitimos|trabalhamos)\b|"
        r"pode(?:mos)?\s+(?:reservar|confirmar|marcar)|"
        r"posso\s+(?:reservar|confirmar|marcar)|"
        r"vou\s+(?:confirmar|reservar|marcar)|"
        r"(?:confirmar|confirmado|confirmada|reservar|reserva|mesa|ficou|fica)\s+(?:para|as|a|no horario)|"
        r"(?:recomendo|sugiro)\b)\b",
        normalizado,
    ):
        return "operacional"
    return "mencao"


def _corrigir_horarios_reserva_config(texto: str, config: config_restaurante.ConfigRestaurante) -> str:
    correcao = _texto_horarios_reserva_config(config)
    partes = re.split(r"(?<=[.!?])\s+", texto.strip())
    if len(partes) <= 1:
        return correcao

    corrigidas: list[str] = []
    correcao_adicionada = False
    for parte in partes:
        if _analisar_guardrail_horarios_reserva(parte, config)["guardrail_aplicado"]:
            if not correcao_adicionada:
                corrigidas.append(correcao)
                correcao_adicionada = True
            continue
        corrigidas.append(parte)
    return " ".join(parte for parte in corrigidas if parte.strip()) or correcao


def _ocorrencias_horarios_texto(texto: str) -> list[dict[str, Any]]:
    normalizado = _normalizar_busca(texto)
    ocorrencias: list[dict[str, Any]] = []
    vistos: set[tuple[int, str]] = set()

    padroes = (
        (r"\b([01]?\d|2[0-3])\s*[:h]\s*([0-5]\d)\b", True),
        (r"\b([01]?\d|2[0-3])h\b", False),
        (r"\b([01]?\d|2[0-3])\s+horas?\b", False),
        (r"\b(?:as|a|das|de|entre|ate)\s+([01]?\d|2[0-3])\b", False),
    )
    for padrao, tem_minuto in padroes:
        for match in re.finditer(padrao, normalizado):
            hora = int(match.group(1))
            minuto = int(match.group(2)) if tem_minuto and match.lastindex and match.group(2) else 0
            horario = f"{hora:02d}:{minuto:02d}"
            chave = (match.start(), horario)
            if chave in vistos:
                continue
            vistos.add(chave)
            inicio = normalizado.rfind(".", 0, match.start())
            inicio = max(inicio, normalizado.rfind("!", 0, match.start()), normalizado.rfind("?", 0, match.start()))
            fim_candidatos = [
                pos
                for pos in (
                    normalizado.find(".", match.end()),
                    normalizado.find("!", match.end()),
                    normalizado.find("?", match.end()),
                )
                if pos != -1
            ]
            fim = min(fim_candidatos) if fim_candidatos else len(normalizado)
            sentenca = normalizado[inicio + 1 : fim].strip()
            contexto_local = normalizado[max(0, match.start() - 24) : min(len(normalizado), match.end() + 24)]
            ocorrencias.append(
                {
                    "horario": horario,
                    "sentenca": sentenca,
                    "contexto_local": contexto_local,
                    "inicio": match.start(),
                    "fim": match.end(),
                }
            )
    ocorrencias.sort(key=lambda item: int(item["inicio"]))
    return ocorrencias


def _horarios_citados_no_texto(texto: str) -> list[str]:
    encontrados: list[str] = []
    for ocorrencia in _ocorrencias_horarios_texto(texto):
        horario = str(ocorrencia["horario"])
        if horario not in encontrados:
            encontrados.append(horario)
    return encontrados


def _texto_taxa_reserva_config(config: config_restaurante.ConfigRestaurante) -> str:
    if not _taxa_configurada(config):
        return "Nao tenho uma taxa de reserva configurada aqui. Posso seguir com os dados da reserva para a equipe conferir."

    complementos: list[str] = []
    if config.taxa_convertida_consumacao:
        complementos.append("esse valor e convertido em consumacao")
    if config.exige_comprovante:
        complementos.append("o comprovante Pix e obrigatorio")

    texto = f"A reserva tem taxa de {_formatar_moeda_brl(config.taxa_valor)}"
    if complementos:
        texto = f"{texto}, {' e '.join(complementos)}"
    return f"{texto}."


def _texto_horarios_reserva_config(config: config_restaurante.ConfigRestaurante) -> str:
    if config.horarios_permitidos_reserva:
        horarios = _formatar_lista_texto(config.horarios_permitidos_reserva)
        return f"Os horarios aceitos para reserva sao {horarios}. Me fala qual deles voce prefere e eu verifico a solicitacao."
    return f"Para reservas, trabalhamos {config.horarios_aceitos_reserva}."


def _texto_quantidade_minima_config(config: config_restaurante.ConfigRestaurante) -> str:
    if config.quantidade_minima_reserva is None:
        return f"Consigo confirmar automaticamente reservas de ate {config.limite_pessoas_reserva} pessoas por aqui."
    return (
        f"As reservas sao feitas a partir de {config.quantidade_minima_reserva} pessoas. "
        f"Consigo confirmar automaticamente ate {config.limite_pessoas_reserva} pessoas por aqui."
    )


def _texto_cancelamento_config(config: config_restaurante.ConfigRestaurante) -> str:
    if config.politica_cancelamento:
        return config.politica_cancelamento
    if config.prazo_cancelamento_horas:
        return f"O cancelamento com estorno deve ser solicitado com pelo menos {config.prazo_cancelamento_horas} horas de antecedencia."
    return "A politica de cancelamento ainda precisa ser confirmada com a equipe."


def _texto_reserva_estruturada(config: config_restaurante.ConfigRestaurante) -> str:
    partes: list[str] = []
    if config.quantidade_minima_reserva is not None:
        partes.append(f"Reservas sao feitas a partir de {config.quantidade_minima_reserva} pessoas.")
    if config.horarios_permitidos_reserva:
        partes.append(f"Os horarios aceitos para reserva sao {_formatar_lista_texto(config.horarios_permitidos_reserva)}.")
    if _taxa_configurada(config):
        partes.append(_texto_taxa_reserva_config(config))
    if not partes:
        partes.append("Posso seguir com os dados da reserva para a equipe conferir.")
    return " ".join(partes)


def _formatar_lista_texto(valores: Sequence[str]) -> str:
    itens = [str(valor).strip() for valor in valores if str(valor).strip()]
    if not itens:
        return ""
    if len(itens) == 1:
        return itens[0]
    return ", ".join(itens[:-1]) + f" e {itens[-1]}"


def _formatar_moeda_brl(valor: float | None) -> str:
    numero = float(valor or 0)
    return f"R$ {numero:.2f}".replace(".", ",")


def _taxa_para_log(valor: float | None) -> str:
    if valor is None:
        return ""
    return f"{float(valor):.2f}"


def aplicar_guardrails_reserva(
    *,
    telefone: str,
    mensagem_cliente: str,
    interpretacao: ResultadoReservaEstruturada,
    nome_cliente: str = "",
) -> RespostaAgente:
    telefone_limpo = telefone.strip()
    estado = _estados_reserva.setdefault(telefone_limpo, {})
    aguardava_confirmacao = bool(estado.get("aguardando_confirmacao"))
    confirmacao_cliente = _eh_confirmacao_cliente(mensagem_cliente)
    status_modelo = str(interpretacao.get("status_reserva") or "em_coleta").strip().lower()
    intencao_modelo = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
    if intencao_modelo:
        estado["ultima_intencao"] = intencao_modelo

    if _nome_parece_valido(nome_cliente):
        estado["nome_cliente"] = nome_cliente.strip()

    _atualizar_contexto_conversacional_estado(estado, interpretacao)
    _atualizar_preferencia_espaco_estado(estado, mensagem_cliente=mensagem_cliente)
    data_clara_promovivel = _mensagem_tem_data_clara_promovivel(mensagem_cliente, estado, interpretacao)
    horario_claro_promovivel = _mensagem_tem_horario_claro_promovivel(mensagem_cliente, estado, interpretacao)

    if _eh_pedido_imediato(mensagem_cliente):
        estado["pedido_imediato"] = True
        estado["aguardando_confirmacao"] = False
        estado["data_reserva"] = _hoje().isoformat()
        estado["data_reserva_original"] = mensagem_cliente.strip()
        _definir_campo_pendente(estado, "horario")
        logger.info("Pedido imediato identificado. telefone=%s data_salva=%s.", telefone_limpo, estado.get("data_reserva", ""))
        return _resposta_preservando_ia(
            estado=estado,
            interpretacao=interpretacao,
            status_reserva="aguardando_humano",
        )

    intencao_conversacional = _intencao_conversacional(mensagem_cliente)
    categoria_pergunta_restaurante = _categoria_pergunta_restaurante(mensagem_cliente)
    if not categoria_pergunta_restaurante and intencao_modelo == "pergunta_restaurante":
        categoria_pergunta_restaurante = "geral"
    if (
        not intencao_conversacional
        and intencao_modelo in INTENCOES_CONVERSACIONAIS_IA
        and not _interpretacao_tem_dados_reserva(interpretacao)
        and not data_clara_promovivel
        and not horario_claro_promovivel
    ):
        intencao_conversacional = intencao_modelo

    pediu_humano = _mensagem_pede_atendimento_humano(mensagem_cliente)
    handoff_tecnico = _texto_ia_ou_fallback_tecnico(interpretacao) == _resposta_contingencia()
    if (pediu_humano or handoff_tecnico) and (
        intencao_modelo == "pedido_humano" or str(interpretacao.get("acao") or "") == "encaminhar_humano"
    ):
        estado["aguardando_confirmacao"] = False
        estado["cliente_autorizou_confirmacao"] = False
        logger.info("Atendimento humano solicitado pela IA ou fallback tecnico. telefone=%s", telefone_limpo)
        return _resposta_preservando_ia(
            estado=estado,
            interpretacao=interpretacao,
            status_reserva="aguardando_humano",
        )
    if intencao_modelo == "pedido_humano" or str(interpretacao.get("acao") or "") == "encaminhar_humano":
        logger.info(
            "Pedido de humano sugerido pela IA ignorado por falta de pedido explicito do cliente. telefone=%s intencao=%s acao=%s",
            telefone_limpo,
            intencao_modelo,
            str(interpretacao.get("acao") or ""),
        )

    resposta_confirmacao = _resolver_mensagem_confirmacao_pendente(
        telefone=telefone_limpo,
        mensagem_cliente=mensagem_cliente,
        estado=estado,
        interpretacao=interpretacao,
        nome_cliente=nome_cliente,
    )
    if resposta_confirmacao is not None:
        return resposta_confirmacao

    if _eh_recusa_reserva(mensagem_cliente) and not _confirmacao_ativa(estado):
        _estados_reserva.pop(telefone_limpo, None)
        return _resposta_preservando_ia(
            estado={},
            interpretacao=interpretacao,
            status_reserva="sem_interesse",
            dados_reserva={},
        )

    if (status_modelo == "cancelada" or _eh_cancelamento_cliente(mensagem_cliente)) and not categoria_pergunta_restaurante:
        _estados_reserva.pop(telefone_limpo, None)
        return _resposta_preservando_ia(
            estado={},
            interpretacao=interpretacao,
            status_reserva="cancelada",
            dados_reserva={},
        )

    if _pergunta_sobre_horario_indisponivel(mensagem_cliente, estado) and _mensagem_pede_explicacao_horario(mensagem_cliente):
        estado["aguardando_confirmacao"] = False
        _definir_campo_pendente(estado, "horario")
        return _resposta_preservando_ia(estado=estado, interpretacao=interpretacao, status_reserva="em_coleta")

    invalidos_pre = _atualizar_estado_e_recalcular(
        estado,
        telefone=telefone_limpo,
        mensagem_cliente=mensagem_cliente,
        interpretacao=interpretacao,
        confirmacao_cliente=confirmacao_cliente,
        aguardava_confirmacao=aguardava_confirmacao,
    )
    if invalidos_pre:
        campo = _primeiro_campo_pendente(list(invalidos_pre), estado, telefone_limpo)
        _definir_campo_pendente(estado, campo)
        dados_estado = _dados_reserva_do_estado(estado)
        texto = _mensagem_validacao_falhou(campo, estado, telefone_limpo, interpretacao, mensagem_cliente=mensagem_cliente)
        _log_resposta_substituida(
            texto_ia=interpretacao["texto"],
            texto_final=texto,
            funcao="aplicar_guardrails_reserva.validacao_pre_conversacional",
            motivo=f"validacao_invalida:{campo}",
        )
        status_reserva = "aguardando_humano" if _deve_encaminhar_humano_por_tentativas(estado, campo) else "em_coleta"
        return {
            "texto": texto,
            "reserva_confirmada": False,
            "dados_reserva": dados_estado,
            "status_reserva": status_reserva,
            "confianca": interpretacao["confianca"],
        }

    if intencao_conversacional:
        return _responder_intencao_conversacional(
            intencao=intencao_conversacional,
            telefone=telefone_limpo,
            estado=estado,
            interpretacao=interpretacao,
        )

    if categoria_pergunta_restaurante:
        return _responder_pergunta_restaurante(
            categoria=categoria_pergunta_restaurante,
            telefone=telefone_limpo,
            estado=estado,
            interpretacao=interpretacao,
            confianca=max(interpretacao["confianca"], 0.8),
        )

    if _pergunta_data_disponivel(mensagem_cliente):
        estado["aguardando_confirmacao"] = False
        _definir_campo_pendente(estado, "data_reserva")
        return _resposta_preservando_ia(estado=estado, interpretacao=interpretacao, status_reserva="em_coleta")

    if _deve_respeitar_nao_aplicavel(estado, mensagem_cliente, status_modelo):
        return {
            "texto": interpretacao["texto"],
            "reserva_confirmada": False,
            "dados_reserva": {},
            "status_reserva": status_modelo,
            "confianca": interpretacao["confianca"],
        }

    invalidos: set[str] = set()
    if _campo_aguardado(estado) == "nome_cliente" and not confirmacao_cliente:
        nome_informado = _extrair_nome_cliente(mensagem_cliente)
        if nome_informado:
            estado["nome_cliente"] = nome_informado

    faltantes = _campos_obrigatorios_faltantes(estado, telefone_limpo)
    dados_estado = _dados_reserva_do_estado(estado)

    if confirmacao_cliente and aguardava_confirmacao and not faltantes:
        return _confirmar_reserva_pendente(
            telefone=telefone_limpo,
            estado=estado,
            interpretacao=interpretacao,
            nome_cliente=nome_cliente,
            funcao="aplicar_guardrails_reserva.confirmacao_final",
        )

    if invalidos:
        campo = _primeiro_campo_pendente(list(invalidos), estado, telefone_limpo)
        _definir_campo_pendente(estado, campo)
        texto = _mensagem_validacao_falhou(campo, estado, telefone_limpo, interpretacao, mensagem_cliente=mensagem_cliente)
        _log_resposta_substituida(
            texto_ia=interpretacao["texto"],
            texto_final=texto,
            funcao="aplicar_guardrails_reserva.validacao",
            motivo=f"validacao_invalida:{campo}",
        )
        status_reserva = "aguardando_humano" if _deve_encaminhar_humano_por_tentativas(estado, campo) else "em_coleta"
        return {
            "texto": texto,
            "reserva_confirmada": False,
            "dados_reserva": dados_estado,
            "status_reserva": status_reserva,
            "confianca": interpretacao["confianca"],
        }

    if faltantes:
        campo = faltantes[0]
        _definir_campo_pendente(estado, campo)
        texto_ia = _texto_ia_ou_fallback_tecnico(interpretacao)
        if _texto_tenta_confirmar_reserva(texto_ia):
            texto = _mensagem_validacao_falhou(campo, estado, telefone_limpo, interpretacao, mensagem_cliente=mensagem_cliente)
            return {
                "texto": texto,
                "reserva_confirmada": False,
                "dados_reserva": dados_estado,
                "status_reserva": "em_coleta",
                "confianca": interpretacao["confianca"],
            }
        return _resposta_preservando_ia(
            estado=estado,
            interpretacao=interpretacao,
            status_reserva="em_coleta",
            dados_reserva=dados_estado,
        )

    if estado.get("confirmacao_pausada") and not confirmacao_cliente:
        estado["aguardando_confirmacao"] = True
        estado["cliente_autorizou_confirmacao"] = False
        _definir_campo_pendente(estado, "confirmacao")
        dados_estado = _dados_reserva_do_estado(estado)
        return _resposta_preservando_ia(
            estado=estado,
            interpretacao=interpretacao,
            status_reserva="aguardando_confirmacao",
            dados_reserva=dados_estado,
        )

    estado["aguardando_confirmacao"] = True
    estado["confirmacao_pausada"] = False
    estado["cliente_autorizou_confirmacao"] = False
    estado["ultima_confirmacao_oferecida_em"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _definir_campo_pendente(estado, "confirmacao")
    dados_estado = _dados_reserva_do_estado(estado)
    return _resposta_preservando_ia(
        estado=estado,
        interpretacao=interpretacao,
        status_reserva="aguardando_confirmacao",
        dados_reserva=dados_estado,
    )


def _atualizar_estado_e_recalcular(
    estado: EstadoReserva,
    *,
    telefone: str,
    mensagem_cliente: str,
    interpretacao: Mapping[str, Any],
    confirmacao_cliente: bool,
    aguardava_confirmacao: bool,
) -> set[str]:
    invalidos: set[str] = set()
    if not (confirmacao_cliente and aguardava_confirmacao):
        estado["aguardando_confirmacao"] = False
        _atualizar_estado_reserva(
            estado,
            mensagem_cliente=mensagem_cliente,
            dados_modelo=interpretacao["dados_reserva"],
            invalidos=invalidos,
            interpretacao=interpretacao,
        )

    _remover_campos_invalidos_estado(estado, invalidos)
    _recalcular_campo_pendente(estado, telefone)
    return invalidos


def _recalcular_campo_pendente(estado: EstadoReserva, telefone: str) -> None:
    faltantes = _campos_obrigatorios_faltantes(estado, telefone)
    if faltantes:
        _definir_campo_pendente(estado, faltantes[0])
        estado["aguardando_confirmacao"] = False
        estado["cliente_autorizou_confirmacao"] = False
        return
    _definir_campo_pendente(estado, "confirmacao")
    estado["aguardando_confirmacao"] = True


def dados_reserva_obrigatorios_ok(
    dados: Mapping[str, Any],
    *,
    nome_cliente: str = "",
    telefone: str = "",
) -> bool:
    nome = str(dados.get("nome_cliente") or nome_cliente or "").strip()
    telefone_limpo = str(telefone or "").strip()
    pessoas = _pessoas_json(dados.get("pessoas"))
    return bool(
        _data_reserva_valida(str(dados.get("data_reserva") or ""))
        and _horario_reserva_valido(
            str(dados.get("horario") or ""),
            data_reserva=str(dados.get("data_reserva") or ""),
        )
        and pessoas is not None
        and nome
        and telefone_limpo
    )


def contem_flag_reserva(texto: str) -> bool:
    return FLAG_RESERVA_CONFIRMADA in texto


def remover_flag_reserva(texto: str) -> str:
    texto_sem_flag = texto.replace(FLAG_RESERVA_CONFIRMADA, "")
    return re.sub(r"\s+", " ", texto_sem_flag).strip()


def extrair_dados_reserva(
    mensagem_cliente: str,
    resposta_agente: str,
) -> DadosReserva:
    return _extrair_dados_reserva_contextual(
        mensagem_cliente,
        resposta_agente,
        campo_aguardado="",
    )


def _extrair_dados_reserva_contextual(
    mensagem_cliente: str,
    resposta_agente: str,
    *,
    campo_aguardado: str,
) -> DadosReserva:
    texto = f"{mensagem_cliente}\n{resposta_agente}"
    dados: DadosReserva = {}

    if campo_aguardado == "data_reserva":
        data_reserva = _extrair_data(texto, permitir_dia_isolado=True)
        if data_reserva:
            dados["data_reserva"] = data_reserva
    elif campo_aguardado == "horario":
        horario = _extrair_horario(texto, permitir_numero_isolado=True)
        if horario:
            dados["horario"] = horario
    elif campo_aguardado == "pessoas":
        pessoas = _extrair_pessoas_solicitadas(texto, permitir_numero_isolado=True)
        if pessoas is not None and 1 <= pessoas <= _limite_pessoas_reserva():
            dados["pessoas"] = pessoas
    else:
        data_reserva = _extrair_data(texto)
        if data_reserva:
            dados["data_reserva"] = data_reserva

        horario = _extrair_horario(texto)
        if horario:
            dados["horario"] = horario

        pessoas = _extrair_pessoas(texto)
        if pessoas:
            dados["pessoas"] = pessoas

    observacoes = remover_flag_reserva(resposta_agente)
    if observacoes:
        dados["observacoes"] = observacoes

    return dados


def interpretar_resposta_modelo(
    *,
    texto_modelo: str,
    mensagem_cliente: str = "",
) -> ResultadoReservaEstruturada:
    payload = _extrair_json_resposta(texto_modelo)
    if payload is None:
        texto_cliente = remover_flag_reserva(texto_modelo)
        return {
            "texto": texto_cliente,
            "reserva_confirmada": contem_flag_reserva(texto_modelo),
            "dados_reserva": extrair_dados_reserva(mensagem_cliente, texto_cliente),
            "dados_confirmados": {},
            "dados_mencionados": {},
            "dados_incertos": {},
            "status_reserva": "confirmada" if contem_flag_reserva(texto_modelo) else "em_coleta",
            "confianca": 0.5 if contem_flag_reserva(texto_modelo) else 0.0,
            "intencao": "",
            "acao": "",
            "proximo_campo": "",
            "deve_avancar_estado": False,
            "correcoes": {},
            "assunto_atual": "",
            "pergunta_aberta": "",
            "tom_cliente": "",
            "resumo_conversa": "",
            "contrato_novo": False,
        }

    reserva = payload.get("reserva")
    reserva_dict = reserva if isinstance(reserva, dict) else {}
    acao = str(payload.get("acao") or "").strip().lower()
    status = str(reserva_dict.get("status") or payload.get("status_reserva") or "em_coleta").strip().lower()
    if acao == "confirmar_reserva":
        status = "confirmada"
    elif acao == "cancelar":
        status = "cancelada"
    elif acao == "encaminhar_humano":
        status = "aguardando_humano"
    if acao in {"continuar_conversa", "continuar_reserva"} and status == "nao_aplicavel":
        status = "em_coleta"
    contrato_novo = _payload_usa_contrato_novo(payload)
    dados_reserva = _dados_reserva_de_json(reserva_dict)
    dados_extraidos = payload.get("dados_extraidos")
    if isinstance(dados_extraidos, dict):
        dados_reserva.update(_dados_extraidos_de_json(dados_extraidos))
    dados_confirmados = _dados_reserva_de_json(payload.get("dados_confirmados") if isinstance(payload.get("dados_confirmados"), dict) else {})
    dados_mencionados = _dados_reserva_de_json(payload.get("dados_mencionados") if isinstance(payload.get("dados_mencionados"), dict) else {})
    if not dados_confirmados and dados_reserva and _payload_antigo_deve_confirmar_dados(payload):
        dados_confirmados = dict(dados_reserva)
    if dados_confirmados:
        dados_reserva.update(dados_confirmados)
    dados_incertos_payload = payload.get("dados_incertos")
    dados_incertos = dict(dados_incertos_payload) if isinstance(dados_incertos_payload, Mapping) else {}
    texto_cliente = str(
        payload.get("resposta_natural")
        or payload.get("resposta")
        or payload.get("resposta_cliente")
        or payload.get("texto")
        or ""
    ).strip()
    if not texto_cliente:
        texto_cliente = _resposta_contingencia()

    reserva_confirmada = status == "confirmada" and _dados_reserva_minimos(dados_reserva)
    if not reserva_confirmada and bool(reserva_dict.get("confirmada")):
        reserva_confirmada = _dados_reserva_minimos(dados_reserva)
    correcoes_payload = payload.get("correcoes")
    correcoes = dict(correcoes_payload) if isinstance(correcoes_payload, Mapping) else {}

    return {
        "texto": texto_cliente,
        "reserva_confirmada": reserva_confirmada,
        "dados_reserva": dados_reserva,
        "dados_confirmados": dados_confirmados,
        "dados_mencionados": dados_mencionados,
        "dados_incertos": dados_incertos,
        "status_reserva": status,
        "confianca": _confianca(payload.get("confianca") or reserva_dict.get("confianca")),
        "intencao": _normalizar_intencao_ia(str(payload.get("intencao") or "")),
        "acao": acao,
        "proximo_campo": _normalizar_campo_ia(str(payload.get("campo_sugerido") or payload.get("proximo_campo") or "")),
        "deve_avancar_estado": _bool_payload(payload.get("deve_avancar_estado"), padrao=_payload_antigo_deve_confirmar_dados(payload)),
        "correcoes": correcoes,
        "assunto_atual": str(payload.get("assunto_atual") or "").strip(),
        "pergunta_aberta": str(payload.get("pergunta_aberta") or "").strip(),
        "tom_cliente": str(payload.get("tom_cliente") or "").strip(),
        "resumo_conversa": str(payload.get("resumo_conversa") or "").strip(),
        "contrato_novo": contrato_novo,
    }


def _dados_extraidos_de_json(dados: Mapping[str, Any]) -> DadosReserva:
    reserva: DadosReserva = {}
    data_reserva = dados.get("data_reserva") or dados.get("data")
    if isinstance(data_reserva, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_reserva.strip()):
        reserva["data_reserva"] = data_reserva.strip()
    elif isinstance(data_reserva, str):
        data_interpretada = _extrair_data(data_reserva, permitir_dia_isolado=True)
        if data_interpretada:
            reserva["data_reserva"] = data_interpretada

    horario = dados.get("horario") or dados.get("hora")
    if isinstance(horario, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", horario.strip()):
        reserva["horario"] = horario.strip()
    elif isinstance(horario, str):
        horario_interpretado = _extrair_horario(horario, permitir_numero_isolado=True)
        if horario_interpretado:
            reserva["horario"] = horario_interpretado

    pessoas = _pessoas_json(dados.get("pessoas") or dados.get("quantidade") or dados.get("quantidade_pessoas"))
    if pessoas is None:
        quantidade_texto = dados.get("quantidade") or dados.get("quantidade_pessoas") or dados.get("pessoas")
        if isinstance(quantidade_texto, str):
            pessoas = _extrair_pessoas_solicitadas(quantidade_texto, permitir_numero_isolado=True)
    if pessoas is not None:
        reserva["pessoas"] = pessoas

    nome = dados.get("nome_cliente") or dados.get("nome")
    if isinstance(nome, str) and _nome_parece_valido(nome):
        reserva["nome_cliente"] = nome.strip()

    return reserva


def _payload_antigo_deve_confirmar_dados(payload: Mapping[str, Any]) -> bool:
    if "dados_confirmados" in payload or "dados_mencionados" in payload:
        return False
    acao = str(payload.get("acao") or "").strip().lower()
    intencao = _normalizar_intencao_ia(str(payload.get("intencao") or ""))
    return bool(
        acao in {"continuar_conversa", "continuar_reserva", "confirmar_reserva", "confirmar"}
        or intencao in {"fornecimento_dados", "correcao"}
        or not intencao
    )


def _payload_usa_contrato_novo(payload: Mapping[str, Any]) -> bool:
    return any(
        chave in payload
        for chave in (
            "dados_confirmados",
            "dados_mencionados",
            "dados_incertos",
            "deve_avancar_estado",
            "campo_sugerido",
            "assunto_atual",
            "pergunta_aberta",
            "tom_cliente",
            "resumo_conversa",
        )
    )


def _bool_payload(valor: Any, *, padrao: bool) -> bool:
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str):
        normalizado = _normalizar_busca(valor)
        if normalizado in {"true", "sim", "1", "yes"}:
            return True
        if normalizado in {"false", "nao", "0", "no"}:
            return False
    if valor is None:
        return padrao
    return bool(valor)


def _normalizar_intencao_ia(valor: str) -> str:
    normalizado = _normalizar_busca(valor).replace(" ", "_")
    mapa = {
        "correcao": "correcao",
        "corrigir_dado": "correcao",
        "fornecimento_de_dados": "fornecimento_dados",
        "informar_dados": "fornecimento_dados",
        "informar_data": "fornecimento_dados",
        "informar_horario": "fornecimento_dados",
        "informar_quantidade": "fornecimento_dados",
        "pergunta_contextual": "pergunta_contextual",
        "corrigir_horario": "correcao",
        "corrigir_data": "correcao",
        "corrigir_quantidade": "correcao",
        "pergunta_sobre_restaurante": "pergunta_restaurante",
        "pergunta_sobre_disponibilidade": "pergunta_disponibilidade",
        "sem_interesse": "recusa",
    }
    return mapa.get(normalizado, normalizado)


def _normalizar_campo_ia(valor: str) -> str:
    normalizado = _normalizar_busca(valor).replace(" ", "_")
    if normalizado in {"", "null", "none", "nenhum"}:
        return ""
    return MAPA_CAMPO_IA.get(normalizado, normalizado)


def _atualizar_estado_reserva(
    estado: EstadoReserva,
    *,
    mensagem_cliente: str,
    dados_modelo: Mapping[str, Any],
    invalidos: set[str],
    interpretacao: Mapping[str, Any],
) -> None:
    campo_pendente = _campo_aguardado(estado)
    intencao_modelo = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
    mensagem_eh_conversa = (
        intencao_modelo in INTENCOES_CONVERSACIONAIS_IA
        or bool(_intencao_conversacional(mensagem_cliente))
        or bool(_categoria_pergunta_restaurante(mensagem_cliente))
    )
    deve_avancar = bool(interpretacao.get("deve_avancar_estado")) or _mensagem_indica_correcao(mensagem_cliente, interpretacao)
    dados_confirmados = _dados_reserva_de_json(dict(interpretacao.get("dados_confirmados") or {}))
    correcoes_confirmadas = _dados_reserva_de_json(dict(interpretacao.get("correcoes") or {}))
    dados_mencionados = _dados_reserva_de_json(dict(interpretacao.get("dados_mencionados") or {}))
    dados_modelo_confirmados: DadosReserva = {}
    dados_modelo_confirmados.update(dados_confirmados)  # type: ignore[arg-type]
    dados_modelo_confirmados.update(correcoes_confirmadas)
    campos_mencionados_promovidos = _promover_dados_mencionados_confirmaveis(
        dados_modelo_confirmados,
        dados_mencionados,
        estado=estado,
        mensagem_cliente=mensagem_cliente,
        interpretacao=interpretacao,
    )
    contrato_novo = bool(interpretacao.get("contrato_novo"))
    usar_fallback_extracao = deve_avancar and not mensagem_eh_conversa and (not dados_modelo_confirmados or not contrato_novo)
    data_parser = _extrair_data(
        mensagem_cliente,
        permitir_dia_isolado=campo_pendente == "data_reserva" or not estado.get("data_reserva"),
    )
    logger.info(
        "DIAG_RESERVA parser_data texto=%r campo_pendente=%s resultado=%s deve_avancar=%s contrato_novo=%s mensagem_eh_conversa=%s",
        mensagem_cliente,
        campo_pendente,
        data_parser,
        deve_avancar,
        contrato_novo,
        mensagem_eh_conversa,
    )
    data_promovida = _deve_promover_data_clara(mensagem_cliente, data_parser, estado, interpretacao)
    if data_promovida:
        dados_modelo_confirmados["data_reserva"] = data_parser  # type: ignore[typeddict-item]
        logger.info(
            "DIAG_RESERVA dados_confirmados_promovidos campo=data_reserva valor=%s motivo=data_clara_sem_data_no_estado",
            data_parser,
        )
    horario_parser = _extrair_horario(
        mensagem_cliente,
        permitir_numero_isolado=campo_pendente == "horario",
    )
    promover_horario, motivo_horario = _avaliar_promocao_horario_claro(
        mensagem_cliente,
        horario_parser,
        estado,
        interpretacao,
    )
    logger.info(
        "DIAG_RESERVA horario_ia texto=%r campo_pendente=%s intencao=%s dados_confirmados.horario=%s dados_mencionados.horario=%s correcoes.horario=%s deve_avancar_estado=%s parser_horario=%s promover=%s motivo=%s",
        mensagem_cliente,
        campo_pendente,
        intencao_modelo,
        _valor_horario_interpretacao(interpretacao.get("dados_confirmados")),
        _valor_horario_interpretacao(interpretacao.get("dados_mencionados")),
        _valor_horario_interpretacao(interpretacao.get("correcoes")),
        bool(interpretacao.get("deve_avancar_estado")),
        horario_parser,
        promover_horario,
        motivo_horario,
    )
    if promover_horario and horario_parser:
        dados_modelo_confirmados["horario"] = horario_parser
        logger.info(
            "DIAG_RESERVA dados_confirmados_promovidos campo=horario valor=%s motivo=horario_claro_sem_horario_no_estado",
            horario_parser,
        )
    elif horario_parser:
        logger.info(
            "DIAG_RESERVA horario_parser_descartado valor=%s motivo=%s horario_estado=%s",
            horario_parser,
            motivo_horario,
            estado.get("horario", ""),
        )
    dados_usuario = (
        _extrair_dados_reserva_contextual(
            mensagem_cliente,
            "",
            campo_aguardado=campo_pendente,
        )
        if usar_fallback_extracao
        else {}
    )

    if usar_fallback_extracao:
        pessoas_solicitadas = _extrair_pessoas_solicitadas(
            mensagem_cliente,
            permitir_numero_isolado=campo_pendente == "pessoas",
        )
        if pessoas_solicitadas is not None:
            if 1 <= pessoas_solicitadas <= _limite_pessoas_reserva():
                dados_usuario["pessoas"] = pessoas_solicitadas
            else:
                invalidos.add("pessoas")

        horario_usuario = horario_parser
        if horario_usuario and not _horario_reserva_valido(
            horario_usuario,
            data_reserva=str(estado.get("data_reserva") or ""),
        ):
            invalidos.add("horario")

    tem_sinal_data = _mensagem_tem_sinal_data(mensagem_cliente) or (
        campo_pendente == "data_reserva" and _mensagem_parece_numero_isolado(mensagem_cliente)
    )
    if (
        usar_fallback_extracao
        and
        campo_pendente in {"", "data_reserva"}
        and tem_sinal_data
        and not mensagem_eh_conversa
        and not dados_usuario.get("data_reserva")
        and not dados_modelo_confirmados.get("data_reserva")
    ):
        invalidos.add("data_reserva")

    if (
        data_promovida
        or "data_reserva" in campos_mencionados_promovidos
        or _pode_aplicar_dado_confirmado("data_reserva", estado, interpretacao, mensagem_cliente)
    ):
        _definir_data_estado(
            estado,
            dados_modelo_confirmados.get("data_reserva"),
            invalidos=invalidos,
            valor_original=mensagem_cliente,
            fonte="ia_confirmado",
            permitir_sobrescrever=_mensagem_indica_correcao(mensagem_cliente, interpretacao),
        )
    if (
        promover_horario
        or "horario" in campos_mencionados_promovidos
        or _pode_aplicar_dado_confirmado("horario", estado, interpretacao, mensagem_cliente)
    ):
        _definir_horario_estado(estado, dados_modelo_confirmados.get("horario"), invalidos=invalidos, fonte="ia_confirmado")
    if "pessoas" in campos_mencionados_promovidos or _pode_aplicar_dado_confirmado("pessoas", estado, interpretacao, mensagem_cliente):
        _definir_pessoas_estado(estado, dados_modelo_confirmados.get("pessoas"), invalidos=invalidos)
    if _pode_aplicar_dado_confirmado("nome_cliente", estado, interpretacao, mensagem_cliente) and _nome_parece_valido(
        str(dados_modelo_confirmados.get("nome_cliente") or "")
    ):
        estado["nome_cliente"] = str(dados_modelo_confirmados.get("nome_cliente")).strip()

    _definir_data_estado(
        estado,
        dados_usuario.get("data_reserva"),
        invalidos=invalidos,
        valor_original=mensagem_cliente,
        fonte="cliente",
        permitir_sobrescrever=campo_pendente not in {"horario", "pessoas"} and tem_sinal_data,
    )
    _definir_horario_estado(estado, dados_usuario.get("horario"), invalidos=invalidos, fonte="cliente")
    _definir_pessoas_estado(estado, dados_usuario.get("pessoas"), invalidos=invalidos)

    observacoes = str(dados_modelo_confirmados.get("observacoes") or dados_usuario.get("observacoes") or "").strip()
    if observacoes:
        estado["observacoes"] = observacoes
    _sincronizar_dados_confirmados_estado(estado)


def _promover_dados_mencionados_confirmaveis(
    destino: DadosReserva,
    mencionados: Mapping[str, Any],
    *,
    estado: EstadoReserva,
    mensagem_cliente: str,
    interpretacao: Mapping[str, Any],
) -> set[str]:
    if not mencionados:
        return set()
    intencao = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
    if intencao in {"pergunta_contextual", "pergunta_restaurante", "pergunta_bot", "pergunta_fonte", "brincadeira"}:
        return set()

    campo_aguardado = _campo_aguardado(estado)
    correcao_ou_escolha = _mensagem_indica_correcao(mensagem_cliente, interpretacao)
    acao = str(interpretacao.get("acao") or "").strip().lower()
    mensagem_contextual = _mensagem_eh_pergunta_contextual(mensagem_cliente)
    fornecimento_direto = intencao in {"fornecimento_dados", "correcao", "informar_data", "informar_horario", "informar_quantidade"}
    pode_promover_por_acao = acao in {"pedir_confirmacao", "confirmar_reserva", "continuar_reserva"} or (
        acao == "continuar_conversa" and fornecimento_direto
    )
    promovidos: set[str] = set()

    for campo, sinal in (
        ("data_reserva", _mensagem_tem_sinal_data),
        ("horario", _mensagem_tem_sinal_horario),
        ("pessoas", _mensagem_tem_sinal_pessoas),
    ):
        valor = mencionados.get(campo)
        if valor in (None, "", []):
            continue
        if destino.get(campo):  # type: ignore[literal-required]
            continue
        sinal_do_campo = sinal(mensagem_cliente)
        direto_no_campo = campo_aguardado == campo and fornecimento_direto and sinal_do_campo and not mensagem_contextual
        if correcao_ou_escolha or direto_no_campo or (pode_promover_por_acao and sinal_do_campo and not mensagem_contextual):
            destino[campo] = valor  # type: ignore[literal-required]
            promovidos.add(campo)
            logger.info(
                "DIAG_RESERVA dados_mencionados_promovidos campo=%s valor=%s motivo=mensagem_confirmavel",
                campo,
                valor,
            )

    return promovidos


def _definir_data_estado(
    estado: EstadoReserva,
    valor: Any,
    *,
    invalidos: set[str],
    valor_original: str = "",
    fonte: str = "",
    permitir_sobrescrever: bool = False,
) -> None:
    if not isinstance(valor, str) or not valor.strip():
        return
    data_texto = valor.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_texto):
        logger.info("DIAG_RESERVA validacao_data valor=%r valido=false motivo=formato_invalido fonte=%s", valor, fonte)
        return
    if not _data_reserva_valida(data_texto):
        invalidos.add("data_reserva")
        logger.info(
            "DIAG_RESERVA validacao_data valor=%s valido=false motivo=data_passada_ou_invalida hoje=%s fonte=%s",
            data_texto,
            _hoje().isoformat(),
            fonte,
        )
        logger.info(
            "Data de reserva rejeitada: original=%r interpretada=%s salva=%s fonte=%s.",
            valor_original,
            data_texto,
            estado.get("data_reserva", ""),
            fonte,
        )
        return
    data_anterior = str(estado.get("data_reserva") or "")
    if data_anterior and data_anterior != data_texto and not permitir_sobrescrever:
        logger.info(
            "DIAG_RESERVA validacao_data valor=%s valido=true aplicada=false motivo=nao_sobrescrever data_anterior=%s fonte=%s",
            data_texto,
            data_anterior,
            fonte,
        )
        logger.info(
            "Data de reserva mantida sem alteracao: original=%r interpretada=%s salva=%s fonte=%s.",
            valor_original,
            data_texto,
            data_anterior,
            fonte,
        )
        return
    estado["data_reserva"] = data_texto
    estado["data_reserva_fonte"] = fonte
    if valor_original:
        estado["data_reserva_original"] = valor_original.strip()
    _limpar_tentativas_campo(estado, "data_reserva")
    logger.info("DIAG_RESERVA validacao_data valor=%s valido=true aplicada=true fonte=%s", data_texto, fonte)
    logger.info(
        "Data de reserva registrada: original=%r interpretada=%s salva=%s fonte=%s.",
        valor_original,
        data_texto,
        estado.get("data_reserva", ""),
        fonte,
    )


def _remover_campos_invalidos_estado(estado: EstadoReserva, invalidos: set[str]) -> None:
    data_estado = str(estado.get("data_reserva") or "")
    if data_estado and not _data_reserva_valida(data_estado):
        estado.pop("data_reserva", None)
        invalidos.add("data_reserva")
    elif data_estado and not _data_estado_confere_origem(estado):
        logger.warning(
            "Data de reserva divergente da mensagem original: original=%r salva=%s.",
            estado.get("data_reserva_original", ""),
            data_estado,
        )
        estado.pop("data_reserva", None)
        invalidos.add("data_reserva")
    horario_estado = str(estado.get("horario") or "")
    if horario_estado and not _horario_reserva_valido(
        horario_estado,
        data_reserva=str(estado.get("data_reserva") or ""),
    ):
        estado.pop("horario", None)
        invalidos.add("horario")
    if estado.get("pessoas") and _pessoas_json(estado.get("pessoas")) is None:
        estado.pop("pessoas", None)
        invalidos.add("pessoas")


def _definir_horario_estado(estado: EstadoReserva, valor: Any, *, invalidos: set[str], fonte: str = "") -> None:
    if not isinstance(valor, str) or not valor.strip():
        return
    horario = valor.strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", horario):
        logger.info("DIAG_RESERVA validacao_horario valor=%r valido=false motivo=formato_invalido fonte=%s", valor, fonte)
        return
    if not _horario_reserva_valido(horario, data_reserva=str(estado.get("data_reserva") or "")):
        estado["horario_invalido"] = horario
        invalidos.add("horario")
        logger.info(
            "DIAG_RESERVA validacao_horario valor=%s valido=false motivo=fora_funcionamento fonte=%s horario_salvo=%s",
            horario,
            fonte,
            estado.get("horario", ""),
        )
        return
    estado["horario"] = horario
    estado.pop("horario_invalido", None)
    _limpar_tentativas_campo(estado, "horario")
    logger.info(
        "DIAG_RESERVA validacao_horario valor=%s valido=true aplicada=true fonte=%s horario_salvo=%s",
        horario,
        fonte,
        estado.get("horario", ""),
    )


def _definir_pessoas_estado(estado: EstadoReserva, valor: Any, *, invalidos: set[str]) -> None:
    try:
        pessoas = int(valor)
    except (TypeError, ValueError):
        return
    if not 1 <= pessoas <= _limite_pessoas_reserva():
        invalidos.add("pessoas")
        return
    estado["pessoas"] = pessoas
    _limpar_tentativas_campo(estado, "pessoas")


def _definir_campo_pendente(estado: EstadoReserva, campo: str) -> None:
    estado["campo_pendente"] = campo
    etapa = ETAPA_POR_CAMPO.get(campo)
    if etapa:
        estado["etapa"] = etapa


def _campo_aguardado(estado: Mapping[str, Any]) -> str:
    etapa = str(estado.get("etapa") or "").strip()
    if etapa in CAMPO_POR_ETAPA:
        return CAMPO_POR_ETAPA[etapa]
    return str(estado.get("campo_pendente") or "").strip()


def _registrar_tentativa_campo(estado: EstadoReserva, campo: str) -> int:
    tentativas = estado.setdefault("tentativas_campos", {})
    tentativas[campo] = int(tentativas.get(campo, 0)) + 1
    return tentativas[campo]


def _tentativas_campo(estado: Mapping[str, Any], campo: str) -> int:
    tentativas = estado.get("tentativas_campos")
    if not isinstance(tentativas, Mapping):
        return 0
    try:
        return int(tentativas.get(campo, 0))
    except (TypeError, ValueError):
        return 0


def _limpar_tentativas_campo(estado: EstadoReserva, campo: str) -> None:
    tentativas = estado.get("tentativas_campos")
    if isinstance(tentativas, dict):
        tentativas.pop(campo, None)


def _deve_encaminhar_humano_por_tentativas(estado: Mapping[str, Any], campo: str) -> bool:
    return _tentativas_campo(estado, campo) >= 3


def _deve_contar_tentativa_campo(
    *,
    mensagem_cliente: str,
    interpretacao: Mapping[str, Any],
    campo: str,
) -> bool:
    intencao = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
    if intencao in INTENCOES_CONVERSACIONAIS_IA:
        return False
    if _intencao_conversacional(mensagem_cliente) or _categoria_pergunta_restaurante(mensagem_cliente):
        return False
    if _mensagem_eh_pergunta_contextual(mensagem_cliente):
        return False
    if _mensagem_tem_tom_de_brincadeira_ou_comentario(mensagem_cliente):
        return False
    dados_mencionados = interpretacao.get("dados_mencionados")
    dados_incertos = interpretacao.get("dados_incertos")
    if isinstance(dados_mencionados, Mapping) and any(valor not in (None, "", [], {}) for valor in dados_mencionados.values()):
        return False
    if isinstance(dados_incertos, Mapping) and any(valor not in (None, "", [], {}) for valor in dados_incertos.values()):
        return False
    if _mensagem_pode_usar_coleta_natural(mensagem_cliente):
        return False

    confianca = _confianca(interpretacao.get("confianca"))
    if confianca < 0.65:
        return True
    if _mensagem_parece_incompreensivel_ou_evasiva(mensagem_cliente):
        return True

    if campo == "data_reserva":
        return _mensagem_tem_sinal_data(mensagem_cliente)
    if campo == "horario":
        return _mensagem_tem_sinal_horario(mensagem_cliente) and not _mensagem_eh_pergunta_contextual(mensagem_cliente)
    if campo == "pessoas":
        return _mensagem_tem_sinal_pessoas(mensagem_cliente)
    return False


def _mensagem_eh_pergunta_contextual(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        "?" in texto
        or re.search(r"\b(sera que|ser[aá] que|da tempo|daria tempo|como|quando|qual|quais|onde|por que|porque|pq|voce sabe|voce responde)\b", normalizado)
    )


def _mensagem_tem_tom_de_brincadeira_ou_comentario(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\b(k+|kkk+|haha+|rsrs+|kkkk|brincadeira|zoeira)\b", normalizado)
        or re.search(r"\b(rapido|rapida|hein|nossa|legal|boa|entendi|beleza|tranquilo)\b", normalizado)
    )


def _mensagem_parece_incompreensivel_ou_evasiva(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    if not normalizado:
        return False
    if re.search(r"\b(qualquer coisa|sei la|sei nao|tanto faz)\b", normalizado):
        return True
    palavras = re.findall(r"\w+", normalizado)
    if len(palavras) == 1 and len(palavras[0]) >= 5 and not _mensagem_indica_reserva(normalizado):
        return True
    return False


def _pode_aplicar_dado_confirmado(
    campo: str,
    estado: EstadoReserva,
    interpretacao: Mapping[str, Any],
    mensagem_cliente: str,
) -> bool:
    dados_confirmados = interpretacao.get("dados_confirmados")
    correcoes = interpretacao.get("correcoes")
    tem_dado = isinstance(dados_confirmados, Mapping) and dados_confirmados.get(campo) not in (None, "", [])
    if not tem_dado and campo == "data_reserva" and isinstance(dados_confirmados, Mapping):
        tem_dado = dados_confirmados.get("data") not in (None, "", [])
    if not tem_dado and campo == "pessoas" and isinstance(dados_confirmados, Mapping):
        tem_dado = dados_confirmados.get("quantidade") not in (None, "", [])
    tem_correcao = isinstance(correcoes, Mapping) and correcoes.get(campo) not in (None, "", [])
    if not tem_correcao and campo == "horario" and isinstance(correcoes, Mapping):
        tem_correcao = correcoes.get("hora") not in (None, "", [])
    if not tem_correcao and campo == "pessoas" and isinstance(correcoes, Mapping):
        tem_correcao = correcoes.get("quantidade") not in (None, "", [])

    if not tem_dado and not tem_correcao:
        return False
    if campo == "pessoas" and tem_dado and not tem_correcao and _mensagem_quantidade_incerta(mensagem_cliente):
        logger.info(
            "DIAG_RESERVA dado_confirmado_ignorado campo=pessoas motivo=quantidade_aproximada_ou_incerta",
        )
        return False
    valor_novo = _valor_campo_interpretacao(campo, interpretacao)
    valor_atual = estado.get(campo)
    correcao = _mensagem_indica_correcao(mensagem_cliente, interpretacao)
    if valor_atual not in (None, "", []) and valor_novo not in (None, "", []) and str(valor_atual) != str(valor_novo):
        if not (correcao and _mensagem_suporta_campo_reserva(campo, mensagem_cliente, interpretacao)):
            logger.info(
                "DIAG_RESERVA dado_confirmado_ignorado campo=%s valor_estado=%r valor_modelo=%r motivo=sem_correcao_explicita",
                campo,
                valor_atual,
                valor_novo,
            )
            return False
    contrato_novo = bool(interpretacao.get("contrato_novo"))
    campo_aguardado = _campo_aguardado(estado)
    intencao = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
    if _categoria_pergunta_restaurante(mensagem_cliente) and not _mensagem_suporta_campo_reserva(campo, mensagem_cliente, interpretacao):
        logger.info(
            "DIAG_RESERVA dado_confirmado_ignorado campo=%s motivo=pergunta_restaurante_sem_sinal_do_campo",
            campo,
        )
        return False
    if tem_dado and campo == campo_aguardado and intencao in {"fornecimento_dados", "correcao"}:
        return True
    if tem_dado and campo == "horario" and campo_aguardado == "horario" and _mensagem_tem_sinal_horario(mensagem_cliente):
        return True
    if not contrato_novo:
        if _mensagem_indica_correcao(mensagem_cliente, interpretacao):
            return campo == campo_aguardado or (
                campo == "horario" and _mensagem_tem_sinal_horario(mensagem_cliente)
            )
        if campo_aguardado in {"data_reserva", "horario", "pessoas", "nome_cliente"}:
            return campo == campo_aguardado
        return not estado.get(campo)
    if (correcao or tem_correcao) and _mensagem_suporta_campo_reserva(campo, mensagem_cliente, interpretacao):
        return True
    if bool(interpretacao.get("deve_avancar_estado")):
        return True
    return campo == campo_aguardado and intencao == "fornecimento_dados"


def _deve_promover_data_clara(
    mensagem_cliente: str,
    data_parser: str | None,
    estado: Mapping[str, Any],
    interpretacao: Mapping[str, Any],
) -> bool:
    if not data_parser or estado.get("data_reserva"):
        return False
    dados_confirmados = interpretacao.get("dados_confirmados")
    if isinstance(dados_confirmados, Mapping) and any(dados_confirmados.get(chave) for chave in ("data", "data_reserva", "dia")):
        return False
    if not _data_reserva_valida(data_parser):
        logger.info("DIAG_RESERVA validacao_data_promovida valor=%s valido=false motivo=data_passada_ou_invalida", data_parser)
        return False
    normalizado = _normalizar_busca(mensagem_cliente)
    if _pergunta_data_disponivel(mensagem_cliente) or (
        "?" in mensagem_cliente and re.search(r"\b(qual|quais|tem|disponivel|disponiveis)\b", normalizado)
    ):
        return False
    if not _mensagem_tem_sinal_data(mensagem_cliente):
        return False
    campo = _campo_aguardado(estado)
    if campo and campo not in {"data_reserva", "confirmacao"}:
        return False
    return True


def _mensagem_tem_data_clara_promovivel(
    mensagem_cliente: str,
    estado: Mapping[str, Any],
    interpretacao: Mapping[str, Any],
) -> bool:
    data_parser = _extrair_data(
        mensagem_cliente,
        permitir_dia_isolado=_campo_aguardado(estado) == "data_reserva" or not estado.get("data_reserva"),
    )
    return _deve_promover_data_clara(mensagem_cliente, data_parser, estado, interpretacao)


def _valor_horario_interpretacao(valor: Any) -> str:
    if not isinstance(valor, Mapping):
        return ""
    horario = valor.get("horario") or valor.get("hora")
    return "" if horario in (None, "", []) else str(horario)


def _avaliar_promocao_horario_claro(
    mensagem_cliente: str,
    horario_parser: str | None,
    estado: Mapping[str, Any],
    interpretacao: Mapping[str, Any],
) -> tuple[bool, str]:
    if not horario_parser:
        return False, "sem_horario_extraido"
    if estado.get("horario") and not _mensagem_indica_correcao(mensagem_cliente, interpretacao):
        return False, "horario_ja_salvo"
    if _categoria_pergunta_restaurante(mensagem_cliente):
        return False, "pergunta_restaurante"
    if _mensagem_eh_pergunta_contextual(mensagem_cliente):
        return False, "pergunta_contextual"

    campo = _campo_aguardado(estado)
    if campo and campo not in {"horario", "confirmacao"}:
        return False, f"campo_aguardado_{campo}"

    intencao = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
    if intencao in {"fornecimento_dados", "correcao"}:
        return True, f"intencao_{intencao}"
    if _mensagem_indica_correcao(mensagem_cliente, interpretacao):
        return True, "correcao_detectada"
    if _mensagem_confirma_horario(mensagem_cliente, campo):
        return True, "mensagem_confirma_horario"
    return False, "horario_apenas_mencionado"


def _mensagem_tem_horario_claro_promovivel(
    mensagem_cliente: str,
    estado: Mapping[str, Any],
    interpretacao: Mapping[str, Any],
) -> bool:
    horario_parser = _extrair_horario(
        mensagem_cliente,
        permitir_numero_isolado=_campo_aguardado(estado) == "horario",
    )
    promover, _ = _avaliar_promocao_horario_claro(mensagem_cliente, horario_parser, estado, interpretacao)
    return promover


def _mensagem_confirma_horario(mensagem_cliente: str, campo_aguardado: str) -> bool:
    normalizado = _normalizar_busca(mensagem_cliente)
    if not normalizado or _mensagem_eh_pergunta_contextual(mensagem_cliente):
        return False
    if re.search(r"\b(coloca|coloque|anota|anote|marca|marque|pode ser|fechado|fecha|confirmo|prefiro|vou chegar|chego)\b", normalizado):
        return True
    if campo_aguardado == "horario":
        palavras = re.findall(r"\w+", normalizado)
        return 1 <= len(palavras) <= 4 and _extrair_horario(mensagem_cliente, permitir_numero_isolado=True) is not None
    return False


def _valor_campo_interpretacao(campo: str, interpretacao: Mapping[str, Any]) -> Any:
    dados_confirmados = interpretacao.get("dados_confirmados")
    correcoes = interpretacao.get("correcoes")
    chaves = {
        "data_reserva": ("data_reserva", "data", "dia"),
        "horario": ("horario", "hora"),
        "pessoas": ("pessoas", "quantidade", "quantidade_pessoas"),
        "nome_cliente": ("nome_cliente", "nome"),
    }.get(campo, (campo,))
    for origem in (correcoes, dados_confirmados):
        if not isinstance(origem, Mapping):
            continue
        for chave in chaves:
            valor = origem.get(chave)
            if valor not in (None, "", []):
                return valor
    return None


def _mensagem_suporta_campo_reserva(campo: str, mensagem_cliente: str, interpretacao: Mapping[str, Any]) -> bool:
    correcoes = interpretacao.get("correcoes")
    if isinstance(correcoes, Mapping) and any(valor not in (None, "", [], {}) for valor in correcoes.values()):
        if campo == "data_reserva":
            return _mensagem_tem_sinal_data(mensagem_cliente)
        if campo == "horario":
            return _mensagem_tem_sinal_horario(mensagem_cliente)
        if campo == "pessoas":
            return _mensagem_tem_sinal_pessoas(mensagem_cliente)
        return True
    if campo == "data_reserva":
        return _mensagem_tem_sinal_data(mensagem_cliente)
    if campo == "horario":
        return _mensagem_tem_sinal_horario(mensagem_cliente)
    if campo == "pessoas":
        return _mensagem_tem_sinal_pessoas(mensagem_cliente)
    return True


def _pode_aceitar_dado_modelo(
    campo: str,
    mensagem_cliente: str,
    estado: EstadoReserva,
    interpretacao: Mapping[str, Any],
) -> bool:
    campo_aguardado = _campo_aguardado(estado)
    intencao = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
    proximo_campo = _normalizar_campo_ia(str(interpretacao.get("proximo_campo") or ""))
    correcao = _mensagem_indica_correcao(mensagem_cliente, interpretacao)
    if intencao in INTENCOES_CONVERSACIONAIS_IA:
        return False
    if correcao:
        if campo == "data_reserva":
            return _mensagem_tem_sinal_data(mensagem_cliente) or bool(interpretacao.get("correcoes"))
        if campo == "horario":
            return _mensagem_tem_sinal_horario(mensagem_cliente) or bool(interpretacao.get("correcoes"))
        if campo == "pessoas":
            return _mensagem_tem_sinal_pessoas(mensagem_cliente) or bool(interpretacao.get("correcoes"))
        return True
    if campo_aguardado in {"data_reserva", "horario", "pessoas"}:
        return campo == campo_aguardado
    if campo_aguardado == "confirmacao":
        return False
    if proximo_campo and proximo_campo in {"data_reserva", "horario", "pessoas", "nome_cliente"}:
        return campo == proximo_campo and not estado.get(campo)
    if campo == "data_reserva":
        return not estado.get("data_reserva") and _mensagem_tem_sinal_data(mensagem_cliente)
    if campo == "horario":
        return not estado.get("horario") and _mensagem_tem_sinal_horario(mensagem_cliente)
    if campo == "pessoas":
        return not estado.get("pessoas") and _mensagem_tem_sinal_pessoas(mensagem_cliente)
    return False


def _atualizar_contexto_conversacional_estado(estado: EstadoReserva, interpretacao: Mapping[str, Any]) -> None:
    for chave in ("assunto_atual", "pergunta_aberta", "tom_cliente", "resumo_conversa"):
        valor = str(interpretacao.get(chave) or "").strip()
        if valor:
            estado[chave] = valor  # type: ignore[literal-required]

    intencao = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
    if intencao:
        estado["ultima_intencao"] = intencao

    dados_mencionados = interpretacao.get("dados_mencionados")
    if isinstance(dados_mencionados, Mapping) and dados_mencionados:
        estado["dados_mencionados"] = _mesclar_dict_contexto(dict(estado.get("dados_mencionados") or {}), dict(dados_mencionados))

    dados_incertos = interpretacao.get("dados_incertos")
    if isinstance(dados_incertos, Mapping) and dados_incertos:
        estado["dados_incertos"] = _mesclar_dict_contexto(dict(estado.get("dados_incertos") or {}), dict(dados_incertos))

    dados_confirmados = interpretacao.get("dados_confirmados")
    if isinstance(dados_confirmados, Mapping) and dados_confirmados:
        estado["dados_confirmados"] = _mesclar_dict_contexto(dict(estado.get("dados_confirmados") or {}), dict(dados_confirmados))


def _atualizar_preferencia_espaco_estado(estado: EstadoReserva, *, mensagem_cliente: str) -> None:
    config = _config_restaurante_atual()
    espacos = _espacos_ativos(config)
    logger.info(
        "espacos_contexto_carregado source=%s estabelecimento_id=%s",
        config.fonte,
        config.estabelecimento_id,
    )
    logger.info(
        "espacos_ativos_encontrados total=%s nomes=%s",
        len(espacos),
        [espaco.nome for espaco in espacos],
    )
    _registrar_conflitos_espaco_faq(config)

    espaco = _identificar_espaco_na_mensagem(config, mensagem_cliente)
    if espaco is None:
        return

    logger.info(
        "espaco_identificado_na_mensagem espaco_id=%s nome=%s permite_preferencia=%s",
        espaco.id,
        espaco.nome,
        espaco.permite_preferencia,
    )
    if not _mensagem_solicita_preferencia_espaco(mensagem_cliente):
        return

    logger.info("preferencia_espaco_solicitada espaco_id=%s nome=%s", espaco.id, espaco.nome)
    if not espaco.permite_preferencia:
        estado.pop("preferencia_espaco_id", None)
        estado.pop("preferencia_espaco_nome", None)
        estado["preferencia_espaco_permitida"] = False
        estado["espaco_confirmado"] = False
        estado["local_garantido"] = False
        estado["motivo_local_nao_garantido"] = "O espaco nao permite preferencia no momento."
        estado["disponibilidade_espaco_consultada"] = False
        logger.warning(
            "preferencia_espaco_nao_permitida espaco_id=%s nome=%s",
            espaco.id,
            espaco.nome,
        )
        logger.warning(
            "espaco_confirmacao_bloqueada espaco_id=%s motivo=preferencia_nao_permitida",
            espaco.id,
        )
        return

    estado["preferencia_espaco_id"] = espaco.id
    estado["preferencia_espaco_nome"] = espaco.nome
    estado["preferencia_espaco_permitida"] = True
    estado["espaco_confirmado"] = False
    estado["local_garantido"] = False
    estado["disponibilidade_espaco_consultada"] = False
    logger.info(
        "disponibilidade_espaco_consultada consultado=false espaco_id=%s motivo=sem_consulta_reservas",
        espaco.id,
    )

    motivos = []
    regra_espaco = _motivo_regra_espaco_nao_garantido(espaco, estado)
    if regra_espaco:
        motivos.append(regra_espaco)
        logger.info("regra_espaco_aplicada espaco_id=%s regra=%r", espaco.id, regra_espaco)

    regra_faq = _regra_faq_espaco_aplicavel(estado, config)
    if regra_faq:
        motivos.append(regra_faq)
        logger.info("regra_faq_aplicada espaco_id=%s regra=%r", espaco.id, regra_faq)

    if motivos:
        estado["motivo_local_nao_garantido"] = " ".join(dict.fromkeys(motivos))
    else:
        estado.pop("motivo_local_nao_garantido", None)

    logger.info("preferencia_espaco_permitida espaco_id=%s nome=%s", espaco.id, espaco.nome)
    logger.info(
        "espaco_preferencia_registrada espaco_id=%s nome=%s espaco_confirmado=%s local_garantido=%s",
        espaco.id,
        espaco.nome,
        estado.get("espaco_confirmado"),
        estado.get("local_garantido"),
    )


def _identificar_espaco_na_mensagem(
    config: config_restaurante.ConfigRestaurante,
    mensagem_cliente: str,
) -> config_restaurante.EspacoRestaurante | None:
    normalizado = _normalizar_busca(mensagem_cliente)
    if not normalizado:
        return None

    melhor: tuple[int, config_restaurante.EspacoRestaurante] | None = None
    tokens_mensagem = set(re.findall(r"\w+", normalizado))
    for espaco in _espacos_ativos(config):
        nome_normalizado = _normalizar_busca(espaco.nome)
        tokens_nome = set(re.findall(r"\w+", nome_normalizado))
        descricao_normalizada = _normalizar_busca(espaco.descricao)
        score = 0
        if nome_normalizado and re.search(rf"\b{re.escape(nome_normalizado)}\b", normalizado):
            score += 100
        score += 20 * len(tokens_mensagem & tokens_nome)
        if descricao_normalizada and any(token in descricao_normalizada for token in tokens_mensagem if len(token) > 4):
            score += 5
        if score <= 0:
            continue
        if melhor is None or score > melhor[0]:
            melhor = (score, espaco)
    return melhor[1] if melhor else None


def _mensagem_solicita_preferencia_espaco(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(
            r"\b(prefiro|preferia|preferencia|preferir|escolher|escolho|quero|queria|"
            r"pode ser|coloca|coloque|anota|anote|fica|ficar|mesa|reserva)\b",
            normalizado,
        )
    )


def _espaco_por_id(
    config: config_restaurante.ConfigRestaurante,
    espaco_id: str,
) -> config_restaurante.EspacoRestaurante | None:
    if not espaco_id:
        return None
    for espaco in _espacos_ativos(config):
        if espaco.id == espaco_id:
            return espaco
    return None


def _motivo_regra_espaco_nao_garantido(
    espaco: config_restaurante.EspacoRestaurante,
    estado: Mapping[str, Any],
) -> str:
    return _motivo_texto_regra_nao_garantido(str(espaco.regras or ""), estado)


def _regra_faq_espaco_aplicavel(
    estado: Mapping[str, Any],
    config: config_restaurante.ConfigRestaurante,
) -> str:
    motivos: list[str] = []
    for faq in config.faq_conteudos:
        if not faq.ativo or not _faq_relacionada_espaco(faq):
            continue
        motivo_nao_garantido = _motivo_texto_regra_nao_garantido(faq.conteudo, estado)
        if motivo_nao_garantido:
            motivos.append(motivo_nao_garantido)

        motivo_direcionamento = _motivo_direcionamento_operacional_faq(faq.conteudo, estado)
        if motivo_direcionamento:
            motivos.append(motivo_direcionamento)

    return " ".join(dict.fromkeys(motivos))


def _motivo_texto_regra_nao_garantido(texto: str, estado: Mapping[str, Any]) -> str:
    horario = str(estado.get("horario") or "").strip()
    if not texto or not horario:
        return ""
    horarios_regra = _horarios_citados_no_texto(texto)
    normalizado = _normalizar_busca(texto)
    if horario in horarios_regra and re.search(r"\b(nao|sem)\b.{0,80}\b(garantid\w*|garantia|preferencia)\b", normalizado):
        return "Regra cadastrada informa que a preferencia de local nesse horario nao e garantida."
    return ""


def _motivo_direcionamento_operacional_faq(texto: str, estado: Mapping[str, Any]) -> str:
    pessoas = _pessoas_json(estado.get("pessoas"))
    dia_semana = _dia_semana_texto(str(estado.get("data_reserva") or ""))
    if not texto or pessoas is None or dia_semana not in {"sabado", "domingo"}:
        return ""

    normalizado = _normalizar_busca(texto)
    if not re.search(r"\b(sabado|domingos?|domingo|finais de semana|fim de semana)\b", normalizado):
        return ""
    match = re.search(r"\bacima\s+de\s+(\d{1,4})\s+pessoas?\b", normalizado)
    if not match:
        return ""
    limite = int(match.group(1))
    if pessoas <= limite:
        return ""
    return f"FAQ ativa informa regra operacional para grupos acima de {limite} pessoas em {dia_semana}."


def _registrar_conflitos_espaco_faq(config: config_restaurante.ConfigRestaurante) -> None:
    faqs_espacos = [faq for faq in config.faq_conteudos if faq.ativo and _faq_relacionada_espaco(faq)]
    if not faqs_espacos:
        return
    for espaco in _espacos_ativos(config):
        if espaco.capacidade_maxima is None:
            continue
        for faq in faqs_espacos:
            for limite in _limites_operacionais_faq(faq.conteudo):
                if limite != espaco.capacidade_maxima:
                    logger.warning(
                        "conflito_espaco_faq_detectado espaco_id=%s capacidade_maxima=%s faq_id=%s limite_textual=%s observacao=podem_ser_conceitos_diferentes",
                        espaco.id,
                        espaco.capacidade_maxima,
                        faq.id,
                        limite,
                    )


def _limites_operacionais_faq(texto: str) -> list[int]:
    normalizado = _normalizar_busca(texto)
    return [int(match.group(1)) for match in re.finditer(r"\bacima\s+de\s+(\d{1,4})\s+pessoas?\b", normalizado)]


def _mesclar_dict_contexto(anterior: dict[str, Any], novo: dict[str, Any]) -> dict[str, Any]:
    mesclado = dict(anterior)
    for chave, valor in novo.items():
        if valor not in (None, "", [], {}):
            mesclado[str(chave)] = valor
    return mesclado


def _sincronizar_dados_confirmados_estado(estado: EstadoReserva) -> None:
    confirmados = dict(estado.get("dados_confirmados") or {})
    for campo, valor in _dados_reserva_do_estado(estado).items():
        confirmados[campo] = valor
    estado["dados_confirmados"] = confirmados


def _deve_respeitar_nao_aplicavel(estado: EstadoReserva, mensagem_cliente: str, status_modelo: str) -> bool:
    if status_modelo != "nao_aplicavel":
        return False
    if any(estado.get(campo) for campo in ("data_reserva", "horario", "pessoas")):
        return False
    return not _mensagem_indica_reserva(mensagem_cliente)


def _interpretacao_tem_dados_reserva(interpretacao: Mapping[str, Any]) -> bool:
    dados = interpretacao.get("dados_reserva")
    return isinstance(dados, Mapping) and any(dados.get(campo) not in (None, "", []) for campo in ("data_reserva", "horario", "pessoas", "nome_cliente"))


def _mensagem_indica_correcao(mensagem_cliente: str, interpretacao: Mapping[str, Any] | None = None) -> bool:
    normalizado = _normalizar_busca(mensagem_cliente)
    if re.search(r"\b(na verdade|corrige|corrigir|correcao|correção|troca|muda|mudar|melhor|queria dizer|quis dizer|ao inves|em vez)\b", normalizado):
        return True
    if isinstance(interpretacao, Mapping):
        intencao = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
        if intencao == "correcao":
            return True
        correcoes = interpretacao.get("correcoes")
        return isinstance(correcoes, Mapping) and bool(correcoes)
    return False


def _campos_obrigatorios_faltantes(estado: EstadoReserva, telefone: str) -> list[str]:
    faltantes: list[str] = []
    if not _data_reserva_valida(str(estado.get("data_reserva") or "")):
        faltantes.append("data_reserva")
    if not _horario_reserva_valido(
        str(estado.get("horario") or ""),
        data_reserva=str(estado.get("data_reserva") or ""),
    ):
        faltantes.append("horario")
    if _pessoas_json(estado.get("pessoas")) is None:
        faltantes.append("pessoas")
    if not estado.get("nome_cliente"):
        faltantes.append("nome_cliente")
    if not telefone.strip():
        faltantes.append("telefone")
    return faltantes


def _dados_reserva_do_estado(estado: EstadoReserva) -> DadosReserva:
    dados: DadosReserva = {}
    for campo in (
        "data_reserva",
        "horario",
        "pessoas",
        "nome_cliente",
        "observacoes",
        "preferencia_espaco_id",
        "preferencia_espaco_nome",
        "espaco_confirmado",
        "local_garantido",
    ):
        valor = estado.get(campo)
        if valor not in (None, "", []):
            dados[campo] = valor  # type: ignore[literal-required]
    return dados


def _primeiro_campo_pendente(invalidos: list[str], estado: EstadoReserva, telefone: str) -> str:
    faltantes = _campos_obrigatorios_faltantes(estado, telefone)
    for campo in ("data_reserva", "horario", "pessoas", "nome_cliente", "telefone"):
        if campo in invalidos:
            return campo
    for campo in ("data_reserva", "horario", "pessoas", "nome_cliente", "telefone"):
        if campo in faltantes:
            return campo
    return "data_reserva"


def _mensagem_campo_invalido(campo: str, estado: EstadoReserva, telefone: str) -> str:
    tentativa = _registrar_tentativa_campo(estado, campo)
    if campo == "data_reserva":
        if tentativa >= 3:
            return "Não consegui entender uma data válida. Me passa no formato dia/mês, como 26/07? Se preferir, posso chamar alguém da equipe."
        return "Essa data já passou. Me fala uma data a partir de hoje para eu verificar a reserva."
    if campo == "horario":
        if tentativa >= 3:
            return "Não consegui entender o horário. Você pode me passar no formato 19h ou 19:30? Se preferir, posso chamar alguém da equipe."
        return _mensagem_horario_fora_funcionamento()
    if campo == "pessoas":
        if tentativa >= 3:
            return "Não consegui entender a quantidade de pessoas. Pode me mandar só o número, como 4 ou 6? Se preferir, posso chamar alguém da equipe."
        return "Esse numero de pessoas e maior do que consigo confirmar por aqui. Para quantas pessoas sera a reserva?"
    return _mensagem_pedir_campo(campo, estado)


def _mensagem_validacao_falhou(
    campo: str,
    estado: EstadoReserva,
    telefone: str,
    interpretacao: Mapping[str, Any],
    *,
    mensagem_cliente: str,
) -> str:
    texto_modelo = _texto_modelo_para_validacao(str(interpretacao.get("texto") or ""), campo)
    if texto_modelo:
        _registrar_tentativa_campo(estado, campo)
        _registrar_validacao_estado(estado, campo=campo, resposta="modelo")
        return texto_modelo

    texto_pos_validacao = _gerar_resposta_ia_pos_validacao(
        campo=campo,
        estado=estado,
        telefone=telefone,
        interpretacao=interpretacao,
        mensagem_cliente=mensagem_cliente,
    )
    if texto_pos_validacao:
        _registrar_tentativa_campo(estado, campo)
        _registrar_validacao_estado(estado, campo=campo, resposta="ia_pos_validacao")
        return texto_pos_validacao

    _registrar_tentativa_campo(estado, campo)
    _registrar_validacao_estado(estado, campo=campo, resposta="fallback_tecnico")
    return _resposta_contingencia()


def _gerar_resposta_ia_pos_validacao(
    *,
    campo: str,
    estado: EstadoReserva,
    telefone: str,
    interpretacao: Mapping[str, Any],
    mensagem_cliente: str,
) -> str:
    if not _usa_contrato_novo(interpretacao):
        return ""
    if not ia_fallback.tem_provedor_configurado():
        return ""

    resultado_validacao = _resultado_validacao(campo, estado)
    try:
        texto_modelo = _chamar_groq(
            mensagens=[
                {
                    "role": "system",
                    "content": (
                        "Voce e a atendente virtual do restaurante. O backend validou um dado informado pelo cliente "
                        "e ele nao pode ser usado. Responda de forma natural, curta, sem parecer formulario, "
                        "explicando o motivo e retomando a conversa. Responda somente JSON valido com a chave resposta."
                    ),
                },
                _mensagem_contexto_reserva(telefone),
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "mensagem_cliente": mensagem_cliente,
                            "resposta_inicial_ia": interpretacao.get("texto", ""),
                            "campo_invalido": campo,
                            "resultado_validacao_backend": resultado_validacao,
                            "dados_confirmados_atuais": _dados_reserva_do_estado(estado),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            modelo=os.getenv("GROQ_PRIMARY_MODEL", "").strip() or os.getenv("GROQ_MODEL", MODELO_PADRAO),
        )
    except Exception:
        logger.exception("Falha ao gerar resposta natural pos-validacao.")
        return ""

    if _texto_modelo_eh_fallback_tecnico(texto_modelo):
        return ""
    payload = _extrair_json_resposta(texto_modelo)
    if isinstance(payload, Mapping):
        texto = str(payload.get("resposta") or payload.get("resposta_natural") or payload.get("texto") or "").strip()
    else:
        texto = remover_flag_reserva(texto_modelo)
    return texto if texto and not _texto_modelo_parece_rigido(texto) else ""


def _usa_contrato_novo(interpretacao: Mapping[str, Any]) -> bool:
    return bool(interpretacao.get("contrato_novo"))


def _resultado_validacao(campo: str, estado: Mapping[str, Any]) -> dict[str, Any]:
    abertura, fechamento = _horario_funcionamento_texto(str(estado.get("data_reserva") or ""))
    if campo == "horario":
        horario = str(estado.get("horario_invalido") or "")
        return {
            "valido": False,
            "campo": "horario",
            "motivo": "fora_do_funcionamento",
            "horario_informado": horario,
            "abre": abertura,
            "fecha": fechamento,
        }
    if campo == "data_reserva":
        return {
            "valido": False,
            "campo": "data",
            "motivo": "data_passada_ou_invalida",
            "hoje": _hoje().isoformat(),
        }
    if campo == "pessoas":
        return {
            "valido": False,
            "campo": "quantidade",
            "motivo": "fora_do_limite_automatico",
            "limite": _limite_pessoas_reserva(),
        }
    return {"valido": False, "campo": campo, "motivo": "campo_invalido"}


def _texto_modelo_para_validacao(texto: str, campo: str) -> str:
    texto_limpo = remover_flag_reserva(str(texto or ""))
    if not texto_limpo or _texto_modelo_parece_rigido(texto_limpo):
        return ""
    normalizado = _normalizar_busca(texto_limpo)
    if re.search(r"\b(confirmad|posso confirmar|reserva para)\b", normalizado):
        return ""
    termos_por_campo = {
        "data_reserva": r"\b(passou|a partir de hoje|data valida|data válida|nova data)\b",
        "horario": r"\b(horario|hora|funcionamento|abre|fecha|periodo)\b",
        "pessoas": r"\b(pessoas|quantidade|grupo|limite|total)\b",
    }
    padrao = termos_por_campo.get(campo)
    if padrao and re.search(padrao, normalizado):
        return texto_limpo
    return ""


def _registrar_validacao_estado(estado: EstadoReserva, *, campo: str, resposta: str) -> None:
    validacoes = estado.setdefault("validacoes", [])
    validacoes.append(
        {
            "campo": campo,
            "resposta": resposta,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    del validacoes[:-10]


def _mensagem_pedir_campo(
    campo: str,
    estado: EstadoReserva,
    *,
    registrar_tentativa: bool = True,
    tentativa: int | None = None,
) -> str:
    if registrar_tentativa:
        tentativa_atual = _registrar_tentativa_campo(estado, campo)
    elif tentativa is not None:
        tentativa_atual = tentativa
    else:
        tentativa_atual = max(_tentativas_campo(estado, campo), 1)
    if campo == "data_reserva":
        if tentativa_atual == 1:
            return "Certo, para qual data você quer fazer a reserva?"
        if tentativa_atual == 2:
            return "Você tem algum dia em mente? Pode me mandar como 26/07."
        return "Não consegui entender a data. Me passa no formato dia/mês, como 26/07? Se preferir, posso chamar alguém da equipe."
    if campo == "horario":
        if tentativa_atual >= 3:
            return "Não consegui entender o horário. Você pode me passar no formato 19h ou 19:30? Se preferir, posso chamar alguém da equipe."
        if tentativa_atual == 2:
            return "Você pode me passar um horário aproximado, como 19h ou 20h?"
        if estado.get("horario_invalido"):
            return _mensagem_horario_fora_funcionamento(estado)
        if estado.get("data_reserva") and estado.get("pessoas"):
            return "Certo, tenho a data e a quantidade de pessoas. Só falta o horário. Qual horário fica melhor para você?"
        if estado.get("data_reserva"):
            return "Certo, tenho a data. Qual horário fica melhor para você?"
        return "Certo, qual horário fica melhor para você?"
    if campo == "pessoas":
        if tentativa_atual == 1:
            return "Beleza, para quantas pessoas será a reserva?"
        if tentativa_atual == 2:
            return "Me manda só a quantidade de pessoas, pode ser um número como 4 ou 6."
        return "Não consegui entender a quantidade de pessoas. Pode me mandar só o número, como 4 ou 6? Se preferir, posso chamar alguém da equipe."
    if campo == "nome_cliente":
        if tentativa_atual >= 3:
            return "Não consegui identificar o nome para a reserva. Se preferir, posso chamar alguém da equipe para ajudar."
        return "Só falta seu nome para deixar a reserva certinha. Qual nome devo colocar?"
    return "Antes de confirmar, preciso completar os dados da reserva."


def _mensagem_horario_fora_funcionamento(estado: Mapping[str, Any] | None = None) -> str:
    data_reserva = str((estado or {}).get("data_reserva") or "")
    config = _config_restaurante_atual()
    if data_reserva and config_restaurante.fechado_na_data(data_reserva, config=config):
        return "Nesse dia estamos fechados. Me fala outra data para eu verificar a reserva."
    abertura, fechamento = _horario_funcionamento_texto(data_reserva, config=config)
    return (
        "Esse horário fica fora do nosso funcionamento, que é das "
        f"{_horario_para_cliente(abertura)} às {_horario_para_cliente(fechamento)}. "
        "Qual horário dentro desse período você prefere?"
    )


def _mensagem_confirmacao_previa(dados: Mapping[str, Any]) -> str:
    nome = str(dados.get("nome_cliente") or "").strip()
    nome_trecho = f", no nome de {nome}" if nome else ""
    return (
        "Deixa eu confirmar: ficou para "
        f"{_formatar_data_cliente(str(dados.get('data_reserva') or ''))}, "
        f"às {dados.get('horario')}, para {dados.get('pessoas')} pessoas"
        f"{nome_trecho}. Posso confirmar?"
    )


def _mensagem_confirmacao_previa_segura(dados: Mapping[str, Any]) -> str:
    texto = _mensagem_confirmacao_previa(dados)
    if not _resumo_confere_estado(texto, dados):
        logger.warning(
            "Resumo de reserva divergente do estado: data=%s horario=%s pessoas=%s texto=%r.",
            dados.get("data_reserva", ""),
            dados.get("horario", ""),
            dados.get("pessoas", ""),
            texto,
        )
    else:
        logger.info(
            "Resumo de reserva validado contra estado: data=%s horario=%s pessoas=%s.",
            dados.get("data_reserva", ""),
            dados.get("horario", ""),
            dados.get("pessoas", ""),
        )
    return texto


def _texto_modelo_confirmacao_previa(texto: str, dados: Mapping[str, Any]) -> str:
    texto_limpo = remover_flag_reserva(str(texto or ""))
    if not texto_limpo or _texto_modelo_parece_rigido(texto_limpo):
        return ""
    normalizado = _normalizar_busca(texto_limpo)
    if not re.search(r"\b(confirma|posso confirmar|esta certo|tudo certo|fechado)\b", normalizado):
        return ""
    return texto_limpo if _resumo_confere_estado(texto_limpo, dados) else ""


def _confirmacao_ativa(estado: Mapping[str, Any]) -> bool:
    return bool(estado.get("aguardando_confirmacao")) or _campo_aguardado(estado) == "confirmacao"


def _resolver_mensagem_confirmacao_pendente(
    *,
    telefone: str,
    mensagem_cliente: str,
    estado: EstadoReserva,
    interpretacao: ResultadoReservaEstruturada,
    nome_cliente: str,
) -> RespostaAgente | None:
    if not _confirmacao_ativa(estado):
        return None

    if _eh_pausa_confirmacao(mensagem_cliente):
        return _responder_pausa_confirmacao(
            telefone=telefone,
            estado=estado,
            interpretacao=interpretacao,
        )

    if _eh_confirmacao_cliente(mensagem_cliente):
        return _confirmar_reserva_pendente(
            telefone=telefone,
            estado=estado,
            interpretacao=interpretacao,
            nome_cliente=nome_cliente,
            funcao="aplicar_guardrails_reserva.confirmacao_explicita",
        )

    if _eh_pedido_resumo_confirmacao(mensagem_cliente):
        return _responder_resumo_confirmacao(
            telefone=telefone,
            estado=estado,
            interpretacao=interpretacao,
        )

    if _mensagem_confirmacao_eh_pergunta_ou_comentario(mensagem_cliente, interpretacao):
        return _responder_contexto_confirmacao(
            telefone=telefone,
            mensagem_cliente=mensagem_cliente,
            estado=estado,
            interpretacao=interpretacao,
        )

    return None


def _confirmar_reserva_pendente(
    *,
    telefone: str,
    estado: EstadoReserva,
    interpretacao: Mapping[str, Any],
    nome_cliente: str,
    funcao: str,
) -> RespostaAgente:
    invalidos: set[str] = set()
    _remover_campos_invalidos_estado(estado, invalidos)
    dados_estado = _dados_reserva_do_estado(estado)
    if not dados_reserva_obrigatorios_ok(dados_estado, nome_cliente=nome_cliente, telefone=telefone):
        logger.warning(
            "Confirmacao final bloqueada por validacao obrigatoria: data=%s horario=%s pessoas=%s nome=%s telefone=%s.",
            dados_estado.get("data_reserva", ""),
            dados_estado.get("horario", ""),
            dados_estado.get("pessoas", ""),
            bool(nome_cliente or dados_estado.get("nome_cliente")),
            bool(telefone),
        )
        estado["cliente_autorizou_confirmacao"] = False
        estado["aguardando_confirmacao"] = False
        campo = _primeiro_campo_pendente(list(invalidos), estado, telefone)
        _definir_campo_pendente(estado, campo)
        texto = _mensagem_validacao_falhou(
            campo,
            estado,
            telefone,
            interpretacao,
            mensagem_cliente=str(interpretacao.get("mensagem_cliente") or ""),
        )
        _log_resposta_substituida(
            texto_ia=str(interpretacao.get("texto") or ""),
            texto_final=texto,
            funcao=f"{funcao}.bloqueada",
            motivo=f"confirmacao_incompleta:{campo}",
        )
        return {
            "texto": texto,
            "reserva_confirmada": False,
            "dados_reserva": dados_estado,
            "status_reserva": "em_coleta",
            "confianca": float(interpretacao.get("confianca") or 0.0),
        }

    estado["confirmacao_pausada"] = False
    estado["cliente_autorizou_confirmacao"] = True
    estado["aguardando_confirmacao"] = False
    logger.info(
        "Confirmacao final validada: data=%s horario=%s pessoas=%s telefone=%s.",
        dados_estado.get("data_reserva", ""),
        dados_estado.get("horario", ""),
        dados_estado.get("pessoas", ""),
        telefone,
    )
    texto = _mensagem_reserva_confirmada(dados_estado)
    _log_resposta_substituida(
        texto_ia=str(interpretacao.get("texto") or ""),
        texto_final=texto,
        funcao=funcao,
        motivo="confirmacao_final_operacional",
    )
    return {
        "texto": texto,
        "reserva_confirmada": True,
        "dados_reserva": dados_estado,
        "status_reserva": "confirmada",
        "confianca": max(float(interpretacao.get("confianca") or 0.0), 0.9),
    }


def _responder_pausa_confirmacao(
    *,
    telefone: str,
    estado: EstadoReserva,
    interpretacao: Mapping[str, Any],
) -> RespostaAgente:
    estado["confirmacao_pausada"] = True
    estado["cliente_autorizou_confirmacao"] = False
    estado["aguardando_confirmacao"] = True
    _definir_campo_pendente(estado, "confirmacao")
    logger.info("Confirmacao pausada pelo cliente. telefone=%s", telefone)
    return _resposta_preservando_ia(
        estado=estado,
        interpretacao=interpretacao,
        status_reserva="aguardando_confirmacao",
    )


def _responder_resumo_confirmacao(
    *,
    telefone: str,
    estado: EstadoReserva,
    interpretacao: Mapping[str, Any],
) -> RespostaAgente:
    estado["aguardando_confirmacao"] = True
    estado["cliente_autorizou_confirmacao"] = False
    _definir_campo_pendente(estado, "confirmacao")
    dados_estado = _dados_reserva_do_estado(estado)
    logger.info("Pedido de resumo preservou resposta da IA. telefone=%s", telefone)
    return _resposta_preservando_ia(
        estado=estado,
        interpretacao=interpretacao,
        status_reserva="aguardando_confirmacao",
        dados_reserva=dados_estado,
    )


def _responder_contexto_confirmacao(
    *,
    telefone: str,
    mensagem_cliente: str,
    estado: EstadoReserva,
    interpretacao: Mapping[str, Any],
) -> RespostaAgente:
    estado["aguardando_confirmacao"] = True
    estado["cliente_autorizou_confirmacao"] = False
    _definir_campo_pendente(estado, "confirmacao")
    logger.info("Pergunta/comentario durante confirmacao preservou resposta da IA. telefone=%s", telefone)
    return _resposta_preservando_ia(
        estado=estado,
        interpretacao=interpretacao,
        status_reserva="aguardando_confirmacao",
    )


def _eh_pausa_confirmacao(texto: str) -> bool:
    normalizado = re.sub(r"[^\w\s]", " ", _normalizar_busca(texto))
    normalizado = re.sub(r"\s+", " ", normalizado).strip()
    if normalizado in {"n", "nao", "calma", "pera", "perai", "espera"}:
        return True
    return bool(
        re.search(r"\b(ainda\s+nao|nao\s+agora|calma|pera|perai|espera|aguenta)\b", normalizado)
        and re.search(r"\b(confirma|confirmar|fecha|fechar|confirme)\b", normalizado)
    )


def _eh_pedido_resumo_confirmacao(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\b(me\s+lembra|lembra|resume|resumo|recapitula|relembra)\b", normalizado)
        or re.search(r"\b(como|qual)\s+ficou\b", normalizado)
        or re.search(r"\bficou\s+como\b", normalizado)
    )


def _mensagem_confirmacao_eh_pergunta_ou_comentario(texto: str, interpretacao: Mapping[str, Any]) -> bool:
    intencao = _normalizar_intencao_ia(str(interpretacao.get("intencao") or ""))
    if intencao in INTENCOES_CONVERSACIONAIS_IA:
        return True
    if _intencao_conversacional(texto) or _categoria_pergunta_restaurante(texto):
        return True
    if _mensagem_eh_pergunta_contextual(texto) or _mensagem_tem_tom_de_brincadeira_ou_comentario(texto):
        return True
    return False


def _texto_modelo_sem_pedir_confirmacao(texto: str, estado: Mapping[str, Any]) -> str:
    texto_limpo = remover_flag_reserva(str(texto or ""))
    if not texto_limpo or _texto_modelo_parece_rigido(texto_limpo):
        return ""
    if _texto_modelo_inseguro_em_coleta(texto_limpo, (), estado):
        return ""
    if _texto_pede_confirmacao(texto_limpo):
        return ""
    return texto_limpo


def _texto_pede_confirmacao(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\b(posso|pode|quer|queria)\s+confirmar\b", normalizado)
        or re.search(r"\bconfirma\??\b", normalizado)
        or re.search(r"\besta\s+tudo\s+certo\??\b", normalizado)
    )


def _texto_tenta_confirmar_reserva(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\breserva\s+confirmad[ao]\b", normalizado)
        or re.search(r"\bconfirmad[ao]\s+(?:para|a reserva)\b", normalizado)
        or _texto_pede_confirmacao(texto)
    )


def _mensagem_resumo_reserva_sem_confirmacao(dados: Mapping[str, Any]) -> str:
    nome = str(dados.get("nome_cliente") or "").strip()
    nome_trecho = f", no nome de {nome}" if nome else ""
    return (
        "Ficou para "
        f"{_formatar_data_cliente(str(dados.get('data_reserva') or ''))}, "
        f"as {dados.get('horario')}, para {dados.get('pessoas')} pessoas"
        f"{nome_trecho}."
    )


def _mensagem_confirmacao_pausada(dados: Mapping[str, Any]) -> str:
    resumo = _mensagem_resumo_reserva_sem_confirmacao(dados)
    return f"Tranquilo, nao vou confirmar ainda. {resumo}"


def _fallback_contexto_confirmacao(texto: str) -> str:
    normalizado = _normalizar_busca(texto)
    if re.search(r"\b(atraso|atrasado|atrasada|atrasar|chegar tarde)\b", normalizado):
        return "Ainda nao tenho uma politica de atraso configurada por aqui. A equipe pode te orientar se precisar."
    if _categoria_pergunta_restaurante(texto):
        return "Essa informacao nao esta configurada por aqui agora."
    return "Certo, sigo com os dados da reserva anotados por aqui."


def _resumo_confere_estado(texto: str, dados: Mapping[str, Any]) -> bool:
    data_formatada = _formatar_data_cliente(str(dados.get("data_reserva") or ""))
    horario = str(dados.get("horario") or "")
    pessoas = str(dados.get("pessoas") or "")
    return bool(data_formatada and horario and pessoas and data_formatada in texto and horario in texto and pessoas in texto)


def _mensagem_reserva_confirmada(dados: Mapping[str, Any]) -> str:
    return (
        "Reserva confirmada para "
        f"{_formatar_data_cliente(str(dados.get('data_reserva') or ''))}, "
        f"às {dados.get('horario')}, para {dados.get('pessoas')} pessoas."
    )


def _formatar_data_cliente(valor: str) -> str:
    try:
        data_reserva = date.fromisoformat(valor[:10])
    except ValueError:
        return valor
    return data_reserva.strftime("%d/%m/%Y")


def _eh_confirmacao_cliente(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.fullmatch(
            r"(sim|s|confirmo|confirmado|pode confirmar|agora pode confirmar|confirma|pode fechar|pode seguir|pode continuar|pode dar andamento|segue|seguir|tudo certo|sim confirma|sim pode confirmar|isso|isso mesmo|ok|fechado|beleza|perfeito)",
            normalizado,
        )
        or re.search(
            r"\b(pode confirmar|agora pode confirmar|confirmo|confirma|pode fechar|pode seguir|pode continuar|pode dar andamento|segue com|tudo certo|esta certo|ta certo|fechado)\b",
            normalizado,
        )
    )


def _eh_cancelamento_cliente(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(re.search(r"\b(cancelar|cancela|desistir|nao quero mais)\b", normalizado))


def _eh_recusa_reserva(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    normalizado = re.sub(r"[^\w\s]", " ", normalizado)
    normalizado = re.sub(r"\s+", " ", normalizado).strip()
    if not normalizado:
        return False

    respostas_exatas = {
        "n",
        "nao",
        "nao quero",
        "agora nao",
        "depois",
        "mais tarde",
        "obrigado nao",
        "obrigada nao",
        "nao obrigado",
        "nao obrigada",
        "nao tenho interesse",
        "sem interesse",
    }
    if normalizado in respostas_exatas:
        return True

    return bool(
        re.search(r"\b(?:agora|hoje|por enquanto)\s+nao\b", normalizado)
        or re.search(r"\bnao\s+(?:quero|tenho interesse|vou querer|preciso)\b", normalizado)
        or re.search(r"\bobrigad[ao]\s+nao\b", normalizado)
    )


def _eh_pedido_imediato(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    if _eh_confirmacao_cliente(texto):
        return False
    return bool(
        re.search(r"\b(?:quero|queria|gostaria|preciso)\s+(?:reservar|fazer reserva|uma mesa|mesa|ir|chegar)(?:\s+\w+){0,4}\s+(?:agora|agr)\b", normalizado)
        or re.search(r"\btem\s+(?:mesa|reserva|lugar)(?:\s+\w+){0,4}\s+(?:agora|agr)\b", normalizado)
        or re.search(r"\b(?:hj|hoje)\s+agora\b", normalizado)
        or re.search(r"\b(to|tou|estou)\s+(indo|chegando)\b", normalizado)
        or re.search(r"\b(chegando|indo ai|indo agora|mais rapido possivel|o quanto antes)\b", normalizado)
        or re.search(r"\bquero\s+(?:pra\s+)?agora\b", normalizado)
    )


def _resposta_pedido_imediato(
    estado: EstadoReserva,
    *,
    mensagem_cliente: str,
    confianca: float,
) -> RespostaAgente:
    estado["pedido_imediato"] = True
    estado["aguardando_confirmacao"] = False
    estado["data_reserva"] = _hoje().isoformat()
    estado["data_reserva_original"] = mensagem_cliente.strip()
    _definir_campo_pendente(estado, "horario")
    logger.info(
        "Pedido imediato identificado. original=%r data_salva=%s.",
        mensagem_cliente,
        estado.get("data_reserva", ""),
    )
    return {
        "texto": (
            "Entendi, você quer para agora. Como é uma reserva em cima da hora, "
            "preciso que a equipe confirme a disponibilidade."
        ),
        "reserva_confirmada": False,
        "dados_reserva": _dados_reserva_do_estado(estado),
        "status_reserva": "aguardando_humano",
        "confianca": confianca,
    }


def _pergunta_sobre_horario_indisponivel(texto: str, estado: Mapping[str, Any]) -> bool:
    normalizado = _normalizar_busca(texto)
    horario = _extrair_horario(texto, permitir_numero_isolado=False) or str(estado.get("horario_invalido") or "")
    if horario and not _horario_reserva_valido(horario, data_reserva=str(estado.get("data_reserva") or "")):
        return True
    return bool(
        re.search(r"\b(por que|pq|porque|motivo|nao pode|nao disponivel|indisponivel)\b", normalizado)
        and re.search(r"\b(horario|hora|manha|madrugada)\b", normalizado)
    )


def _mensagem_pede_explicacao_horario(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        "?" in texto
        or re.search(r"\b(por que|pq|porque|motivo|nao pode|nao disponivel|indisponivel)\b", normalizado)
    )


def _intencao_conversacional(texto: str) -> str | None:
    normalizado = _normalizar_busca(texto)
    if not normalizado:
        return None
    if re.search(r"\b(kkkk|kkk|rsrs|haha|se nao estiver|se nao tiver|talvez|depende)\b", normalizado):
        return "comentario"
    if _mensagem_tem_sinal_data(texto) or _mensagem_tem_sinal_horario(texto) or _mensagem_tem_sinal_pessoas(texto):
        return None
    if re.search(r"\b(como|de onde|onde)\b", normalizado) and re.search(
        r"\b(sabe|sabem|saber|informacoes|dados|horarios|funcionamento|endereco)\b",
        normalizado,
    ):
        return "pergunta_fonte"
    if re.search(r"\b(voce|vc|vcs|atendente|assistente)\b", normalizado) and re.search(
        r"\b(robo|bot|virtual|automatico|ia|inteligencia artificial|humano)\b",
        normalizado,
    ):
        return "pergunta_bot"
    if re.search(r"\b(quem e voce|quem esta falando|estou falando com quem|isso e automatico)\b", normalizado):
        return "pergunta_bot"
    if re.search(
        r"\b(nao sei ainda|nao sei|estou vendo|to vendo|vou ver|preciso ver|preciso confirmar|vendo com|falar com meus amigos|falar com a galera|depois te falo|te aviso|ainda nao decidi)\b",
        normalizado,
    ):
        return "comentario_indecisao"
    if _eh_saudacao_simples(normalizado):
        return "saudacao"
    return None


def _mensagem_pede_atendimento_humano(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    if not normalizado:
        return False
    return bool(
        re.search(
            r"\b(atendente|humano|funcionario|funcionaria|equipe|pessoa|alguem)\b",
            normalizado,
        )
        and re.search(
            r"\b(falar|chamar|quero|preciso|pode|passa|transferir|atendimento|assumir|conversar)\b",
            normalizado,
        )
        or re.search(r"\b(cancelar bot|nao quero bot|quero falar com alguem|falar com alguem|falar com funcionario)\b", normalizado)
    )


def _responder_intencao_conversacional(
    *,
    intencao: str,
    telefone: str,
    estado: EstadoReserva,
    interpretacao: ResultadoReservaEstruturada,
) -> RespostaAgente:
    campo = _campo_retomada_apos_informacao(estado, telefone)
    if campo:
        _definir_campo_pendente(estado, campo)

    logger.info(
        "Intencao conversacional preservou resposta da IA: intencao=%s campo_retomada=%s.",
        intencao,
        campo,
    )
    return _resposta_preservando_ia(
        estado=estado,
        interpretacao=interpretacao,
        status_reserva="aguardando_confirmacao" if campo == "confirmacao" else "em_coleta",
    )


def _texto_modelo_para_intencao(
    texto: str,
    intencao: str,
    *,
    estado: Mapping[str, Any] | None = None,
    faltantes: Sequence[str] = (),
) -> str:
    texto_limpo = remover_flag_reserva(str(texto or ""))
    if not texto_limpo:
        return ""
    estado_seguro = estado or {}
    if _texto_modelo_parece_rigido(texto_limpo):
        return ""
    if _texto_modelo_inseguro_em_coleta(texto_limpo, faltantes, estado_seguro):
        return ""
    return texto_limpo


def _texto_fallback_intencao(intencao: str, estado: EstadoReserva) -> str:
    if intencao == "pergunta_fonte":
        return _com_continuacao_fluxo(
            "Essas informacoes foram cadastradas pela propria equipe do restaurante no nosso sistema.",
            estado,
        )
    if intencao == "pergunta_bot":
        return (
            "Sou o assistente virtual do restaurante e estou aqui para ajudar com sua reserva. "
            "Se preferir falar com alguem da equipe, tambem posso encaminhar."
        )
    if intencao == "comentario_indecisao":
        return "Sem problema. Quando decidir, me chama por aqui que eu continuo a reserva com voce."
    if intencao in {"comentario", "brincadeira", "indecisao", "duvida"}:
        return _com_continuacao_fluxo("Sem problema, sigo com voce.", estado)
    if intencao == "pergunta_disponibilidade":
        return _com_continuacao_fluxo(
            "Consigo verificar a data ou o horario que voce preferir, mas nao tenho uma agenda de disponibilidade aberta aqui.",
            estado,
        )
    if intencao == "pergunta_restaurante":
        return _com_continuacao_fluxo("Posso te ajudar com as informacoes do restaurante e com a reserva.", estado)
    if intencao == "saudacao":
        return "Oi! Estou por aqui para ajudar com sua reserva."
    return _com_continuacao_fluxo("Certo, sigo com voce.", estado)


def _texto_modelo_parece_rigido(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\bnao consegui entender\b", normalizado)
        or re.search(r"\bme passa no formato\b", normalizado)
        or re.search(r"\breserva confirmada\b", normalizado)
        or re.search(r"\bantes de confirmar preciso completar\b", normalizado)
        or re.search(r"\btive uma instabilidade\b", normalizado)
    )


def _texto_modelo_para_pedir_campo(
    texto: str,
    campo: str,
    estado: EstadoReserva,
    mensagem_cliente: str,
    *,
    faltantes: Sequence[str] = (),
) -> str:
    texto_limpo = remover_flag_reserva(str(texto or ""))
    if not texto_limpo:
        return ""
    if _texto_modelo_inseguro_em_coleta(texto_limpo, faltantes, estado):
        return ""
    ultimo_texto = str(estado.get("ultimo_texto_bot") or "")
    if ultimo_texto and _textos_muito_parecidos(ultimo_texto, texto_limpo):
        return ""
    return texto_limpo


def _texto_modelo_inseguro_em_coleta(texto: str, faltantes: Sequence[str], estado: Mapping[str, Any]) -> bool:
    normalizado = _normalizar_busca(texto)
    if re.search(r"\breserva confirmada\b", normalizado):
        return True
    if faltantes and re.search(r"\b(posso confirmar|confirma\??|reserva para|so confirmando)\b", normalizado):
        return True
    if _texto_modelo_conflita_com_estado(normalizado, estado):
        return True
    if _texto_afirma_disponibilidade_sem_agenda(normalizado):
        return True
    return False


def _texto_modelo_conflita_com_estado(normalizado: str, estado: Mapping[str, Any]) -> bool:
    if estado.get("data_reserva") and re.search(r"\b(qual data|algum dia|me passa.*dia|me passa.*data|formato dia|data ja passou)\b", normalizado):
        return True
    if estado.get("horario") and re.search(r"\b(qual horario|me passa.*horario|nao consegui entender o horario)\b", normalizado):
        return True
    if estado.get("pessoas") and re.search(r"\b(quantas pessoas|quantidade de pessoas|nao consegui entender a quantidade)\b", normalizado):
        return True
    return False


def _texto_afirma_disponibilidade_sem_agenda(normalizado: str) -> bool:
    if "disponivel" not in normalizado and "disponiveis" not in normalizado:
        return False
    if re.search(r"\b(nao|sem)\b.{0,50}\b(agenda|disponibilidade|disponivel|disponiveis)\b", normalizado):
        return False
    return bool(
        re.search(r"\b(temos|tenho|ha|existe|esta|ficou|fica|horario|horarios)\b.{0,50}\b(disponivel|disponiveis)\b", normalizado)
        or re.search(r"\b(disponivel|disponiveis)\b.{0,50}\b(para|as|no dia|nesse dia)\b", normalizado)
    )


def _mensagem_pode_usar_coleta_natural(mensagem_cliente: str) -> bool:
    normalizado = _normalizar_busca(mensagem_cliente)
    return bool(
        _eh_confirmacao_cliente(mensagem_cliente)
        or mensagem_indica_interesse_reserva(mensagem_cliente)
        or _eh_saudacao_simples(normalizado)
        or re.search(r"\b(claro|pode ser|vamos|bora|beleza|perfeito|quero sim|gostaria sim)\b", normalizado)
    )


def _textos_muito_parecidos(anterior: str, atual: str) -> bool:
    anterior_norm = _normalizar_busca(anterior)
    atual_norm = _normalizar_busca(atual)
    if not anterior_norm or not atual_norm:
        return False
    if anterior_norm == atual_norm:
        return True
    palavras_anterior = {p for p in re.findall(r"\w+", anterior_norm) if len(p) > 3}
    palavras_atual = {p for p in re.findall(r"\w+", atual_norm) if len(p) > 3}
    if not palavras_anterior or not palavras_atual:
        return False
    intersecao = len(palavras_anterior & palavras_atual)
    menor = min(len(palavras_anterior), len(palavras_atual))
    return menor > 0 and intersecao / menor >= 0.8


def _texto_so_pede_campo(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.fullmatch(r".*\b(qual|me fala|me diga|pode me passar)\b.*\b(data|dia|horario|pessoas|nome)\b.*", normalizado)
        and not re.search(r"\b(oi|ola|claro|perfeito|certo|sem problema)\b", normalizado)
    )


def _eh_saudacao_simples(normalizado: str) -> bool:
    return bool(re.fullmatch(r"\s*(oi|ola|bom dia|boa tarde|boa noite|opa|e ai|eae)\s*[!.]*\s*", normalizado))


def _categoria_pergunta_restaurante(texto: str) -> str | None:
    normalizado = _normalizar_busca(texto)
    if not normalizado:
        return None

    marcador_pergunta = bool(
        "?" in texto
        or re.search(r"\b(quando|qual|quais|onde|como|quanto|quantas|tem|aceita|aceitam|funciona|abre|fecha|fica)\b", normalizado)
    )

    if re.search(r"\b(horarios?|hora)\b", normalizado) and re.search(
        r"\b(disponivel|disponiveis|disponibilidade|livres|vagos)\b",
        normalizado,
    ):
        return "horarios_disponiveis"
    if re.search(r"\b(abre|abrir|fecha|fechar|funciona|funcionamento|que horas)\b", normalizado):
        return "funcionamento"
    if re.search(r"\b(endereco|onde fica|localizacao|localizado|rua|avenida|bairro)\b", normalizado):
        return "endereco"
    if re.search(r"\b(estacionamento|estacionar|manobrista|valet)\b", normalizado):
        return "estacionamento"
    if marcador_pergunta and re.search(
        r"\b(taxa|valor|custo|cobra|cobram|cobranca|comprovante|sinal)\b",
        normalizado,
    ) and re.search(r"\b(reserva|reservar|mesa|pix|comprovante|50)\b", normalizado):
        return "taxa_reserva"
    if re.search(r"\b(pagamento|pagar|pix|cartao|debito|credito|dinheiro|voucher|vale refeicao)\b", normalizado):
        return "pagamento"
    if re.search(r"\b(bolo|decoracao|decorar|parabens)\b", normalizado):
        return "bolo"
    if re.search(r"\b(aniversario|bolo|decoracao|decorar|parabens|comemorar)\b", normalizado):
        return "aniversario"
    if re.search(
        r"\b(salao|areia|espaco|espacos|local|preferencia|preferir|escolher|"
        r"area interna|area externa|capacidade|cabem|cabe|garantido|garantida|garantia)\b",
        normalizado,
    ):
        return "espacos"
    if re.search(r"\b(quantas pessoas|maximo|maxima|limite|grupo grande|muita gente)\b", normalizado):
        return "limite_pessoas"
    if marcador_pergunta and re.search(r"\b(cancelamento|cancelar|desmarcar|remarcar)\b", normalizado):
        return "cancelamento"
    if marcador_pergunta and re.search(
        r"\b(antecedencia|minima|politica|regra|regras|como funciona a reserva|precisa reservar|em cima da hora)\b",
        normalizado,
    ):
        return "politica_reserva"
    if marcador_pergunta and re.search(r"\b(restaurante|casa|reserva|reservas)\b", normalizado):
        return "geral"
    return None


def _responder_pergunta_restaurante(
    *,
    categoria: str,
    telefone: str,
    estado: EstadoReserva,
    interpretacao: Mapping[str, Any] | None = None,
    confianca: float,
) -> RespostaAgente:
    campo = _campo_retomada_apos_informacao(estado, telefone)
    if campo:
        _definir_campo_pendente(estado, campo)
    logger.info(
        "Pergunta sobre restaurante preservou resposta da IA: categoria=%s campo_retomada=%s.",
        categoria,
        campo,
    )
    return _resposta_preservando_ia(
        estado=estado,
        interpretacao=interpretacao or {"texto": "", "confianca": confianca},
        status_reserva="aguardando_confirmacao" if campo == "confirmacao" else "em_coleta",
    )


def _campo_retomada_apos_informacao(estado: EstadoReserva, telefone: str) -> str:
    campo_atual = _campo_aguardado(estado)
    if campo_atual in set(ETAPA_POR_CAMPO):
        return campo_atual
    if estado.get("aguardando_confirmacao"):
        return "confirmacao"
    faltantes = _campos_obrigatorios_faltantes(estado, telefone)
    if not faltantes:
        return "confirmacao"
    return faltantes[0]


def _texto_pergunta_restaurante(categoria: str, estado: EstadoReserva) -> str:
    config = _config_restaurante_atual()
    data_reserva = str(estado.get("data_reserva") or "")
    abertura, fechamento = _horario_funcionamento_texto(data_reserva, config=config)
    abertura_cliente = _horario_para_cliente(abertura)
    fechamento_cliente = _horario_para_cliente(fechamento)

    if categoria == "horarios_disponiveis":
        return (
            f"Atendemos entre {abertura_cliente} e {fechamento_cliente}. "
            "Me fala o horario que voce prefere e eu verifico a solicitacao."
        )

    if categoria == "funcionamento":
        if data_reserva and config_restaurante.fechado_na_data(data_reserva, config=config):
            base = "Nesse dia estamos fechados."
        elif data_reserva:
            base = f"Nesse dia abrimos as {abertura_cliente} e fechamos as {fechamento_cliente}."
        else:
            base = f"Funcionamos {config.dias_funcionamento}, das {abertura_cliente} as {fechamento_cliente}."
    elif categoria == "endereco":
        base = f"O endereco e {config.endereco}"
    elif categoria == "estacionamento":
        base = config.estacionamento
    elif categoria == "pagamento":
        base = f"Aceitamos {config.formas_pagamento}."
    elif categoria == "taxa_reserva":
        base = _texto_taxa_reserva_config(config)
    elif categoria == "aniversario":
        base = config.aniversario
    elif categoria == "limite_pessoas":
        base = _texto_quantidade_minima_config(config)
    elif categoria == "cancelamento":
        base = config.politica_cancelamento
        if config.telefone_atendimento:
            base = f"{base} O telefone do atendimento e {config.telefone_atendimento}."
    elif categoria == "politica_reserva":
        base = (
            f"Para reservas, trabalhamos {config.horarios_aceitos_reserva}. "
            f"Antecedencia minima: {config.antecedencia_minima}. "
            f"{config.regras_reservas_imediatas}"
        )
    else:
        base = config.informacoes_gerais or (
            f"Posso te ajudar com funcionamento, endereco, pagamentos e reservas do {config.nome}."
        )

    return _com_continuacao_fluxo(base, estado)


def _com_continuacao_fluxo(base: str, estado: EstadoReserva) -> str:
    texto = base.strip()
    if texto and texto[-1] not in ".!?":
        texto = f"{texto}."

    continuacao = _continuacao_fluxo_apos_informacao(estado)
    if not continuacao:
        return texto
    return f"{texto} {continuacao}"


def _continuacao_fluxo_apos_informacao(estado: EstadoReserva) -> str:
    campo = _campo_aguardado(estado)
    if campo == "data_reserva":
        return "Me fala o dia que voce quer reservar."
    if campo == "horario":
        return "Qual horario dentro desse periodo fica melhor para voce?"
    if campo == "pessoas":
        return "Para quantas pessoas sera a reserva?"
    if campo == "nome_cliente":
        return "Qual nome devo colocar na reserva?"
    if campo == "confirmacao":
        return "Quando estiver tudo certo, posso confirmar a reserva?"
    return ""


def mensagem_indica_interesse_reserva(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\b(quero|gostaria|queria|preciso|pode|vamos|bora)\s+(?:fazer\s+)?(?:uma\s+)?reserva(?:r)?\b", normalizado)
        or re.search(r"\b(?:reservar|fazer reserva|mesa para|quero uma mesa)\b", normalizado)
        or re.search(r"\b(mudei de ideia|pensando melhor|agora quero|reserva pra mim|reserva para mim)\b", normalizado)
    )


def _mensagem_indica_reserva(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\b(reserva|reservar|mesa|pessoas|pessoa|convidados|lugares|horario|dia|data|disponivel|disponibilidade|confirmar)\b", normalizado)
        or _eh_confirmacao_cliente(texto)
        or _mensagem_tem_sinal_data(texto)
        or _mensagem_tem_sinal_horario(texto)
        or _mensagem_tem_sinal_pessoas(texto)
    )


def _pergunta_data_disponivel(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(r"\bqual(?:\s+\w+){0,4}\s+(?:data|dia)(?:\s+\w+){0,4}\s+disponivel\b", normalizado)
        or re.search(r"\b(?:data|dia)(?:\s+\w+){0,4}\s+disponivel\b", normalizado)
        or re.search(r"\btem(?:\s+\w+){0,4}\s+(?:data|dia)(?:\s+\w+){0,4}\s+disponivel\b", normalizado)
    )


def _mensagem_tem_sinal_data(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        re.search(
            r"\b(hoje|amanha|dia\s+\d{1,2}|\d{1,2}[/-]\d{1,2}|"
            r"\d{4}-\d{2}-\d{2}|"
            r"segunda(?: feira)?|terca(?: feira)?|quarta(?: feira)?|quinta(?: feira)?|"
            r"sexta(?: feira)?|sabado|domingo|proxim[ao]\s+(?:segunda|terca|quarta|quinta|sexta|sabado|domingo))\b",
            normalizado,
        )
        or re.search(r"\b\d{1,2}\s+de\s+[a-z]{3,}\b", normalizado)
    )


def _mensagem_tem_sinal_horario(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    return bool(
        _extrair_horario(texto, permitir_numero_isolado=False)
        or re.search(r"\b([01]?\d|2[0-3])[:h]([0-5]\d)?\b", normalizado)
        or re.search(r"\b(?:as|às)\s+(\d{1,2})\b", texto, flags=re.IGNORECASE)
        or re.search(r"\b\d{1,2}\s+horas?\b", normalizado)
        or re.search(r"\b\d{1,2}\s*(?:da|de)\s*(manha|tarde|noite)\b", normalizado)
    )


def _mensagem_tem_sinal_pessoas(texto: str) -> bool:
    return _extrair_pessoas_solicitadas(texto, permitir_numero_isolado=False) is not None


def _mensagem_quantidade_incerta(texto: str) -> bool:
    normalizado = _normalizar_busca(texto)
    if not re.search(r"\b(pessoa|pessoas|convidado|convidados|grupo|grupos|serao|vao|somos|quantidade)\b", normalizado):
        return False
    if re.search(r"\b(pode\s+colocar|coloca|coloque|considera|considere|anota|anote)\b.{0,40}\b\d{1,4}\b.{0,30}\bpor enquanto\b", normalizado):
        return False
    return bool(
        re.search(r"\b(acho|umas|uns|aproximadamente|aproximado|aproximada|talvez|ainda nao sei|pode variar|variar entre)\b", normalizado)
        or re.search(r"\bentre\s+\d{1,4}\s+(?:e|ou)\s+\d{1,4}\b", normalizado)
        or re.search(r"\b\d{1,4}\s*,\s*\d{1,4}\s+ou\s+\d{1,4}\b", normalizado)
    )


def _mensagem_parece_numero_isolado(texto: str) -> bool:
    return bool(re.fullmatch(r"\D*\d{1,4}\D*", _normalizar_busca(texto)))


def _nome_parece_valido(nome: str) -> bool:
    nome_limpo = str(nome or "").strip()
    if not nome_limpo or nome_limpo.lower() in {"cliente", "contato"}:
        return False
    if re.fullmatch(r"\+?\d[\d\s().-]{6,}", nome_limpo):
        return False
    return bool(re.search(r"[A-Za-zÀ-ÿ]{2,}", nome_limpo))


def _extrair_nome_cliente(texto: str) -> str | None:
    texto_limpo = re.sub(r"\s+", " ", texto).strip()
    if not 2 <= len(texto_limpo) <= 80:
        return None
    normalizado = _normalizar_busca(texto_limpo)
    if _eh_confirmacao_cliente(texto_limpo) or _mensagem_indica_reserva(texto_limpo):
        return None
    if re.search(r"\b(cancelar|obrigado|valeu|horario|pessoas|mesa|reserva)\b", normalizado):
        return None
    if not re.fullmatch(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' .-]{1,79}", texto_limpo):
        return None
    return texto_limpo


def _normalizar_busca(texto: str) -> str:
    texto_base = str(texto or "")
    if "Ã" in texto_base or "Â" in texto_base:
        try:
            texto_base = texto_base.encode("latin1").decode("utf-8")
        except UnicodeError:
            pass
    texto_normalizado = unicodedata.normalize("NFD", texto_base.lower())
    return "".join(char for char in texto_normalizado if unicodedata.category(char) != "Mn").strip()


def _data_reserva_valida(valor: str) -> bool:
    try:
        data_reserva = date.fromisoformat(str(valor or "")[:10])
    except ValueError:
        return False
    return data_reserva >= _hoje()


def _data_estado_confere_origem(estado: Mapping[str, Any]) -> bool:
    if str(estado.get("data_reserva_fonte") or "").startswith("ia_"):
        return True
    data_estado = str(estado.get("data_reserva") or "")
    original = str(estado.get("data_reserva_original") or "")
    if not data_estado or not original:
        return True
    data_original = _extrair_data(original)
    if data_original is None:
        return True
    return data_original == data_estado


def _horario_reserva_valido(
    horario: str,
    *,
    data_reserva: str | None = None,
    config: config_restaurante.ConfigRestaurante | None = None,
) -> bool:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", horario):
        return False
    return config_restaurante.horario_valido_para_chegada(
        horario,
        data_reserva,
        config=config or _config_restaurante_atual(),
    )


def _horario_funcionamento_texto(
    data_reserva: str | None = None,
    *,
    config: config_restaurante.ConfigRestaurante | None = None,
) -> tuple[str, str]:
    abertura, fechamento = config_restaurante.horario_funcionamento(
        data_reserva,
        config=config or _config_restaurante_atual(),
    )
    if _horario_para_minutos(abertura) is None:
        abertura = "10:00"
    if _horario_para_minutos(fechamento) is None:
        fechamento = "23:59"
    return abertura, fechamento


def _horario_para_cliente(horario: str) -> str:
    if horario in {"00:00", "23:59"}:
        return "meia-noite"
    return horario


def _limite_pessoas_reserva() -> int:
    return config_restaurante.limite_pessoas_reserva(config=_config_restaurante_atual())


def _horario_para_minutos(horario: str) -> int | None:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(horario or "").strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _hoje() -> date:
    try:
        from zoneinfo import ZoneInfo

        timezone_nome = _config_restaurante_atual().timezone
        return datetime.now(ZoneInfo(timezone_nome)).date()
    except Exception:
        return date.today()


def _resposta_contingencia() -> str:
    return ia_fallback.MENSAGEM_HANDOFF_IA


def _payload_fallback_tecnico() -> str:
    return json.dumps(
        {
            "resposta": _resposta_contingencia(),
            "intencao": "pedido_humano",
            "dados_confirmados": {},
            "dados_mencionados": {},
            "dados_incertos": {},
            "correcoes": {},
            "acao": "encaminhar_humano",
            "deve_avancar_estado": False,
            "campo_sugerido": None,
            "confianca": 1.0,
        },
        ensure_ascii=False,
    )


def _reparar_interpretacao_texto_simples(
    *,
    telefone: str,
    mensagem_cliente: str,
    resposta_natural: str,
) -> str:
    if not ia_fallback.tem_provedor_configurado():
        return ""
    try:
        texto_modelo = _chamar_groq(
            mensagens=[
                {
                    "role": "system",
                    "content": (
                        "Voce converte uma resposta de atendimento em JSON estruturado para o backend. "
                        "Nao gere uma nova resposta ao cliente. Preserve exatamente a resposta_natural no campo resposta. "
                        "Classifique a intencao e separe dados_confirmados, dados_mencionados, dados_incertos e correcoes. "
                        "Se o cliente apenas mencionou um horario em uma pergunta contextual, use dados_mencionados. "
                        "Se o cliente escolheu/corrigiu um horario, use dados_confirmados ou correcoes. "
                        "Responda somente JSON valido no contrato principal."
                    ),
                },
                _mensagem_contexto_reserva(telefone),
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "mensagem_cliente": mensagem_cliente,
                            "resposta_natural": remover_flag_reserva(resposta_natural),
                            "formato": {
                                "resposta": "mesma resposta_natural, sem alterar",
                                "intencao": "pedido_humano|pergunta_restaurante|pergunta_bot|pergunta_fonte|pergunta_contextual|correcao|recusa|fornecimento_dados|comentario|brincadeira|incompreensivel",
                                "dados_confirmados": {"data": None, "horario": None, "quantidade": None, "nome": None},
                                "dados_mencionados": {"data": None, "horario": None, "quantidade": None},
                                "dados_incertos": {},
                                "correcoes": {},
                                "acao": "responder|continuar_conversa|pedir_confirmacao|confirmar_reserva|cancelar|encaminhar_humano",
                                "deve_avancar_estado": False,
                                "campo_sugerido": None,
                                "confianca": 0.0,
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            modelo=os.getenv("GROQ_PRIMARY_MODEL", "").strip() or os.getenv("GROQ_MODEL", MODELO_PADRAO),
            response_format_json=True,
        )
    except Exception:
        logger.exception("Falha ao reparar resposta em texto simples para JSON.")
        return ""

    if _texto_modelo_eh_fallback_tecnico(texto_modelo):
        return ""
    return texto_modelo if _extrair_json_resposta(texto_modelo) is not None else ""


def _chamar_groq(mensagens: Sequence[Mensagem], modelo: str, *, response_format_json: bool = False) -> str:
    telefone = _telefone_processamento.get()
    estado = _estados_reserva.get(telefone, {}) if telefone else {}
    resultado = ia_fallback.executar_ia_com_fallback(
        mensagens,
        telefone=telefone,
        conversa_id=str(estado.get("conversa_id") or ""),
        modelo_preferido=modelo,
        response_format_json=response_format_json,
    )
    if resultado.get("ok") and resultado.get("conteudo"):
        return str(resultado["conteudo"]).strip()
    return _payload_fallback_tecnico()


def _texto_modelo_eh_fallback_tecnico(texto_modelo: str) -> bool:
    payload = _extrair_json_resposta(texto_modelo)
    return bool(
        isinstance(payload, Mapping)
        and payload.get("acao") == "encaminhar_humano"
        and str(payload.get("resposta") or "") == _resposta_contingencia()
    )


def _json_contexto(valor: Mapping[str, Any] | Sequence[Any]) -> str:
    return json.dumps(valor, ensure_ascii=False, separators=(",", ":"))


def _espacos_ativos(config: config_restaurante.ConfigRestaurante) -> list[config_restaurante.EspacoRestaurante]:
    return [espaco for espaco in config.espacos if espaco.ativo]


def _contexto_espacos_restaurante(config: config_restaurante.ConfigRestaurante) -> str:
    espacos = [
        {
            "id": espaco.id,
            "nome": espaco.nome,
            "descricao": espaco.descricao,
            "capacidade_maxima": espaco.capacidade_maxima,
            "permite_preferencia": espaco.permite_preferencia,
            "ativo": espaco.ativo,
        }
        for espaco in _espacos_ativos(config)
    ]
    return _json_contexto({"espacos_ativos": espacos})


def _contexto_regras_espacos(config: config_restaurante.ConfigRestaurante) -> str:
    regras = [
        {
            "origem": "espacos.regras",
            "espaco_id": espaco.id,
            "espaco_nome": espaco.nome,
            "texto": espaco.regras,
        }
        for espaco in _espacos_ativos(config)
        if espaco.regras
    ]
    return _json_contexto({"regras_espaco": regras})


def _contexto_faqs_espacos(faqs: Sequence[config_restaurante.FaqConteudo]) -> str:
    regras = [
        {
            "origem": "faq_conteudos",
            "faq_id": faq.id,
            "categoria": faq.categoria,
            "titulo": faq.titulo,
            "conteudo": faq.conteudo,
            "tags": list(faq.tags),
        }
        for faq in faqs
        if _faq_relacionada_espaco(faq)
    ]
    return _json_contexto({"faqs_espacos_relevantes": regras})


def _contexto_reserva_espacos(estado: Mapping[str, Any], config: config_restaurante.ConfigRestaurante) -> str:
    data_reserva = str(estado.get("data_reserva") or "")
    horario = str(estado.get("horario") or "")
    pessoas = _pessoas_json(estado.get("pessoas"))
    espaco = _espaco_por_id(config, str(estado.get("preferencia_espaco_id") or ""))
    capacidade_maxima = espaco.capacidade_maxima if espaco is not None else None
    capacidade_comporta = None
    if capacidade_maxima is not None and pessoas is not None:
        capacidade_comporta = pessoas <= capacidade_maxima
        logger.info(
            "capacidade_espaco_consultada espaco_id=%s capacidade_maxima=%s quantidade=%s comporta=%s",
            espaco.id,
            capacidade_maxima,
            pessoas,
            capacidade_comporta,
        )
    logger.info(
        "disponibilidade_espaco_consultada consultado=%s espaco_id=%s",
        bool(estado.get("disponibilidade_espaco_consultada")),
        str(estado.get("preferencia_espaco_id") or ""),
    )

    contexto = {
        "contexto_reserva": {
            "data": data_reserva or None,
            "dia_semana": _dia_semana_texto(data_reserva),
            "horario": horario or None,
            "quantidade": pessoas,
            "preferencia_espaco_id": str(estado.get("preferencia_espaco_id") or "") or None,
            "preferencia_espaco_nome": str(estado.get("preferencia_espaco_nome") or "") or None,
            "espaco_confirmado": bool(estado.get("espaco_confirmado")),
            "local_garantido": bool(estado.get("local_garantido")),
        },
        "resultado_disponibilidade": {
            "consultado": bool(estado.get("disponibilidade_espaco_consultada")),
            "espaco_id": str(estado.get("preferencia_espaco_id") or "") or None,
            "ocupacao_atual": None,
            "ocupacao_resultante": None,
            "capacidade_maxima": capacidade_maxima,
            "quantidade_solicitada": pessoas,
            "disponivel_por_capacidade": capacidade_comporta,
            "preferencia_registrada": bool(estado.get("preferencia_espaco_id")),
            "local_garantido": bool(estado.get("local_garantido")),
            "motivo_local_nao_garantido": str(estado.get("motivo_local_nao_garantido") or ""),
        },
    }
    return _json_contexto(contexto)


def _dia_semana_texto(data_reserva: str) -> str | None:
    try:
        data = date.fromisoformat(data_reserva[:10])
    except ValueError:
        return None
    nomes = {1: "segunda", 2: "terca", 3: "quarta", 4: "quinta", 5: "sexta", 6: "sabado", 7: "domingo"}
    return nomes.get(data.isoweekday())


def _selecionar_faqs_relevantes(
    *,
    config: config_restaurante.ConfigRestaurante,
    mensagem_cliente: str,
    estado: Mapping[str, Any],
    historico: Sequence[Mapping[str, str]],
    limite: int = 5,
) -> list[config_restaurante.FaqConteudo]:
    faqs_ativas = [faq for faq in config.faq_conteudos if faq.ativo]
    if not faqs_ativas:
        logger.info("FAQs relevantes selecionadas: total_ativas=0 selecionadas=0 metodo=busca_lexica")
        return []

    consulta = _texto_consulta_faq(mensagem_cliente, estado, historico)
    tokens_consulta = _tokens_busca_faq(consulta)
    if not tokens_consulta:
        logger.info(
            "FAQs relevantes selecionadas: total_ativas=%s selecionadas=0 metodo=busca_lexica motivo=consulta_vazia",
            len(faqs_ativas),
        )
        return []

    pontuadas: list[tuple[int, int, config_restaurante.FaqConteudo]] = []
    for indice, faq in enumerate(faqs_ativas):
        score = _pontuar_faq(faq, tokens_consulta)
        if score > 0:
            pontuadas.append((score, -indice, faq))

    pontuadas.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selecionadas = [faq for _, _, faq in pontuadas[: max(1, min(limite, 5))]]
    faqs_espacos = [faq for faq in selecionadas if _faq_relacionada_espaco(faq)]
    logger.info(
        "FAQs relevantes selecionadas: total_ativas=%s selecionadas=%s titulos=%s metodo=busca_lexica",
        len(faqs_ativas),
        len(selecionadas),
        [faq.titulo for faq in selecionadas],
    )
    if faqs_espacos:
        logger.info(
            "faqs_espacos_selecionadas total=%s titulos=%s",
            len(faqs_espacos),
            [faq.titulo for faq in faqs_espacos],
        )
    return selecionadas


def _texto_consulta_faq(
    mensagem_cliente: str,
    estado: Mapping[str, Any],
    historico: Sequence[Mapping[str, str]],
) -> str:
    partes = [mensagem_cliente]
    for chave in ("assunto_atual", "pergunta_aberta", "resumo_conversa", "ultima_intencao"):
        valor = str(estado.get(chave) or "").strip()
        if valor:
            partes.append(valor)
    for mensagem in list(historico)[-4:]:
        if mensagem.get("role") == "user":
            partes.append(str(mensagem.get("content") or ""))
    return " ".join(partes)


def _tokens_busca_faq(texto: str, *, expandir: bool = True) -> set[str]:
    stopwords = {
        "a",
        "as",
        "ao",
        "aos",
        "da",
        "das",
        "de",
        "do",
        "dos",
        "e",
        "em",
        "na",
        "nas",
        "no",
        "nos",
        "o",
        "os",
        "ou",
        "para",
        "por",
        "qual",
        "quais",
        "que",
        "quanto",
        "se",
        "tem",
        "ter",
        "um",
        "uma",
        "uns",
        "umas",
        "voce",
        "voces",
    }
    normalizado = _normalizar_busca(texto)
    brutos = re.findall(r"[a-z0-9]{2,}", normalizado)
    tokens: set[str] = set()
    for token in brutos:
        singular = _singular_simples(token)
        if token not in stopwords:
            tokens.add(token)
        if singular not in stopwords:
            tokens.add(singular)
    if expandir:
        tokens.update(_sinonimos_faq(tokens))
    return {token for token in tokens if len(token) >= 2}


def _singular_simples(token: str) -> str:
    if len(token) > 4 and token.endswith("oes"):
        return token[:-3] + "ao"
    if len(token) > 4 and token.endswith("ais"):
        return token[:-3] + "al"
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _sinonimos_faq(tokens: set[str]) -> set[str]:
    grupos = [
        {"quadra", "quadras", "locacao", "locar", "alugar", "aluguel", "esporte", "esportes", "volei", "futebol", "beach", "day", "use"},
        {"bolo", "decoracao", "decorar", "utensilios", "garfos", "pratos", "geladeira", "parabens"},
        {"aniversario", "comemorar"},
        {"lista"},
        {"entrada", "entrar", "valor", "valores", "preco", "precos", "custa", "crianca", "criancas"},
        {"gympass"},
        {
            "salao",
            "areia",
            "espaco",
            "espacos",
            "local",
            "preferencia",
            "garantia",
            "garantido",
            "garantida",
            "capacidade",
            "cabem",
            "cabe",
            "interno",
            "interna",
            "externo",
            "externa",
            "area",
            "lugar",
            "direcionado",
            "direcionada",
        },
        {"estacionamento", "estacionar", "carro", "vaga"},
    ]
    expandidos: set[str] = set()
    for grupo in grupos:
        if tokens & grupo:
            expandidos.update(grupo)
    return expandidos


def _pontuar_faq(faq: config_restaurante.FaqConteudo, tokens_consulta: set[str]) -> int:
    tokens_categoria = _tokens_busca_faq(faq.categoria, expandir=False)
    tokens_titulo = _tokens_busca_faq(faq.titulo, expandir=False)
    tokens_tags = _tokens_busca_faq(" ".join(faq.tags), expandir=False)
    tokens_conteudo = _tokens_busca_faq(faq.conteudo, expandir=False)
    score = 0
    score += 5 * len(tokens_consulta & tokens_titulo)
    score += 5 * len(tokens_consulta & tokens_tags)
    score += 4 * len(tokens_consulta & tokens_categoria)
    score += len(tokens_consulta & tokens_conteudo)
    tokens_bolo = {"bolo", "decoracao", "decorar", "utensilios", "garfos", "pratos", "geladeira", "parabens"}
    if tokens_consulta & tokens_bolo:
        tokens_faq_resumo = tokens_titulo | tokens_tags | tokens_categoria
        if tokens_faq_resumo & tokens_bolo:
            score += 20
        if "lista" in tokens_faq_resumo and "lista" not in tokens_consulta:
            score -= 12
    tokens_espaco = _tokens_espaco()
    if tokens_consulta & tokens_espaco:
        tokens_faq_resumo = tokens_titulo | tokens_tags | tokens_categoria
        if tokens_faq_resumo & tokens_espaco:
            score += 20
        if "preferencia" in tokens_faq_resumo and tokens_consulta & {"garantia", "garantido", "garantida", "local", "salao", "areia", "espaco"}:
            score += 12
    return score


def _tokens_espaco() -> set[str]:
    return {
        "salao",
        "areia",
        "espaco",
        "espacos",
        "local",
        "preferencia",
        "garantia",
        "garantido",
        "garantida",
        "capacidade",
        "cabem",
        "cabe",
        "interno",
        "interna",
        "externo",
        "externa",
        "area",
        "lugar",
        "direcionado",
        "direcionada",
    }


def _faq_relacionada_espaco(faq: config_restaurante.FaqConteudo) -> bool:
    texto = " ".join([faq.categoria, faq.titulo, faq.conteudo, " ".join(faq.tags)])
    return bool(_tokens_busca_faq(texto, expandir=True) & _tokens_espaco())


def _contexto_faqs_relevantes(faqs: Sequence[config_restaurante.FaqConteudo]) -> str:
    partes: list[str] = []
    for faq in faqs:
        tags = f" tags: {', '.join(faq.tags)}" if faq.tags else ""
        partes.append(f"{faq.categoria} - {faq.titulo}: {faq.conteudo}{tags}")
    return " | ".join(partes)


def _mensagem_sistema(
    nome_cliente: str,
    *,
    perfil_cliente: Mapping[str, Any] | None = None,
    telefone: str = "",
    mensagem_cliente: str = "",
) -> Mensagem:
    config = _config_restaurante_atual()
    restaurante = config.nome
    hoje = _hoje().strftime("%d/%m/%Y")
    nome = nome_cliente.strip() or "cliente"
    telefone_limpo = telefone.strip()
    estado = _estados_reserva.get(telefone_limpo, {}) if telefone_limpo else {}
    historico = _historicos.get(telefone_limpo, []) if telefone_limpo else []
    espacos_contexto = _contexto_espacos_restaurante(config)
    regras_espacos_contexto = _contexto_regras_espacos(config)
    contexto_reserva_espacos = _contexto_reserva_espacos(estado, config)
    espacos_ativos = _espacos_ativos(config)
    logger.info(
        "espacos_contexto_carregado source=%s estabelecimento_id=%s",
        config.fonte,
        config.estabelecimento_id,
    )
    logger.info(
        "espacos_ativos_encontrados total=%s nomes=%s",
        len(espacos_ativos),
        [espaco.nome for espaco in espacos_ativos],
    )
    _registrar_conflitos_espaco_faq(config)
    faqs_selecionadas = _selecionar_faqs_relevantes(
        config=config,
        mensagem_cliente=mensagem_cliente,
        estado=estado,
        historico=historico,
        limite=5,
    )
    faqs_contexto = _contexto_faqs_relevantes(faqs_selecionadas)
    faqs_espacos_contexto = _contexto_faqs_espacos(faqs_selecionadas)
    perfil_nome = str((perfil_cliente or {}).get("nome") or "").strip()
    perfil_prompt = str((perfil_cliente or {}).get("prompt_ia") or "").strip()
    instrucao_perfil = ""
    if perfil_nome or perfil_prompt:
        instrucao_perfil = (
            "\n\n"
            f"Perfil do cliente: {perfil_nome or 'perfil sem nome'}.\n"
            "Adapte o atendimento a esse perfil sem alongar as respostas.\n"
        )
        if perfil_prompt:
            instrucao_perfil += f"Orientacao especifica do perfil: {perfil_prompt}\n"

    conteudo = (
            f"Você é a atendente virtual do {restaurante}. "
            f"Está conversando com {nome}. "
            "Responda sempre em português do Brasil. "
            f"{instrucao_perfil}"
            "\n\n"
            "Seu jeito de falar:\n"
            "- Direta e simpática, como uma atendente real no WhatsApp\n"
            "- Sem 'Olá', 'Oi' ou saudações — a abertura já foi feita\n"
            "- Respostas curtas, no máximo 1-2 linhas\n"
            "- Sem formalidade excessiva, sem 'prezado', sem 'atenciosamente'\n"
            "- Use linguagem natural\n"
            "- Não use emojis\n"
            "- Tente ao maximo parecer um humano fazendo o atendimento\n"
            "\n"
            "Exemplos de tom:\n"
            "Cliente: 'Quero reservar'\n"
            "Você: 'Ótimo! Qual data e horário você prefere?'\n"
            "\n"
            "Cliente: 'Sábado às 20h'\n"
            "Você: 'Perfeito! Quantas pessoas no total?'\n"
            "\n"
            "Cliente: '8 pessoas'\n"
            "Você: 'Anotado! Sábado, 20h, 8 pessoas — confirma?'\n"
            "\n"
            "Seu objetivo:\n"
            "Coletar dia, horário e número de pessoas. "
            "Converse primeiro como uma atendente humana inteligente; voce e a orquestradora principal da conversa. "
            "Entenda o significado completo da mensagem antes de extrair qualquer dado. "
            "Antes de tratar a mensagem como preenchimento de campo, classifique a intencao: "
            "pedido de humano, pergunta sobre restaurante, pergunta sobre o bot/conversa, correcao, recusa/cancelamento, "
            "pergunta contextual, fornecimento de data/horario/quantidade ou mensagem incompreensivel. "
            "Perguntas, comentarios e duvidas nao sao erro e nao devem soar como formulario. "
            "Quando fizer sentido, responda ao que o cliente disse e retome naturalmente o proximo dado da reserva. "
            "Interprete dados como uma pessoa interpretaria: '11 horas' e '11 da manha' significam horario 11:00; "
            "'20 sem contar comigo' significa 21 pessoas; 'eu e mais 3' significa 4 pessoas; "
            "comentarios como 'se nao estiver chovendo eu vou no dia 28 kkk' sao comentarios/condicionais, nao erro de horario. "
            "Nunca considere um numero como dado confirmado apenas porque apareceu na frase. "
            "Exemplo: 'eu saio do trabalho as 20h, sera que da tempo?' menciona 20h, mas nao escolhe horario. "
            "Nesse caso use dados_mencionados, nao dados_confirmados, e deve_avancar_estado=false. "
            "Ja 'entao coloca 20h30' confirma ou corrige horario. "
            "Separe sempre dados_confirmados, dados_mencionados e dados_incertos. "
            "Use a etapa atual para interpretar numeros: se o sistema aguarda quantidade, um numero solto e quantidade; "
            "se aguarda horario, um numero solto pode ser horario; se aguarda data, pode ser dia. "
            "Pergunte data e horário juntos na primeira pergunta quando a conversa ainda estiver iniciando. "
            "Depois pergunte o total de pessoas. "
            "Nunca confirme uma reserva sem data, horario, quantidade de pessoas, nome e telefone. "
            "Se o cliente disser sim mas faltar algum campo, pergunte o campo faltante. "
            "Se o horario ou a quantidade forem invalidos, nao use esse valor. "
            "Antes da confirmacao final, envie um resumo com data, horario e pessoas e pergunte se pode confirmar. "
            "So use status confirmada depois que o cliente confirmar esse resumo. "
            "\n\n"
            "Informacoes reais do restaurante para responder perguntas durante a reserva:\n"
            f"- Endereco: {config.endereco}\n"
            f"- Funcionamento: {config.dias_funcionamento}, das {config.horario_abertura} as {config.horario_fechamento}\n"
            f"- Horarios aceitos para reserva: {config.horarios_aceitos_reserva}\n"
            f"- Quantidade minima para reserva: {_texto_quantidade_minima_config(config)}\n"
            f"- Limite de pessoas por reserva automatica: {config.limite_pessoas_reserva}\n"
            f"- Taxa de reserva: {_texto_taxa_reserva_config(config)}\n"
            f"- Comprovante de Pix: {'obrigatorio' if config.exige_comprovante else 'nao configurado como obrigatorio'}\n"
            f"- Formas de pagamento: {config.formas_pagamento}\n"
            f"- Cancelamento: {config.politica_cancelamento}\n"
            f"- Instrucoes de reserva: {config.regras_reservas_imediatas}\n"
            f"- Estacionamento: {config.estacionamento}\n"
            f"- Aniversarios: {config.aniversario}\n"
            f"- Espacos ativos estruturados: {espacos_contexto}\n"
            f"- Regras cadastradas em espacos.regras: {regras_espacos_contexto}\n"
            f"- FAQs de espacos/preferencia selecionadas: {faqs_espacos_contexto}\n"
            f"- Contexto da reserva para espacos: {contexto_reserva_espacos}\n"
            f"- FAQs relevantes para a mensagem atual: {faqs_contexto or 'Nenhuma FAQ especifica selecionada para esta mensagem.'}\n"
            "Sobre espacos: use a tabela de espacos para fatos estruturados como nome, descricao, ativo, capacidade_maxima e permite_preferencia. "
            "Use espacos.regras e FAQs de espacos como regras operacionais complementares. "
            "Preferencia de local nao significa local confirmado. Nunca garanta espaco sem consulta real de disponibilidade. "
            "Se o cliente fizer uma pergunta normal sobre o restaurante, responda e retome o campo da reserva que faltava. "
            "Se o cliente perguntar como voce sabe uma informacao, explique que os dados foram cadastrados pela equipe no sistema. "
            "Nunca diga que um horario esta disponivel sem uma agenda real integrada; diga que atende dentro do funcionamento e vai verificar a solicitacao. "
            "Responda sempre e somente em JSON válido, sem markdown. Use este formato principal: "
            '{"resposta":"texto natural para enviar ao cliente",'
            '"intencao":"pedido_humano|pergunta_restaurante|pergunta_bot|pergunta_fonte|pergunta_contextual|correcao|recusa|fornecimento_dados|comentario|brincadeira|incompreensivel",'
            '"dados_confirmados":{"data":"YYYY-MM-DD ou null","horario":"HH:MM ou null","quantidade":numero ou null,"nome":"texto ou null"},'
            '"dados_mencionados":{"data":"YYYY-MM-DD ou null","horario":"HH:MM ou null","quantidade":numero ou null},'
            '"dados_incertos":{"campo":"valor incerto"} ou {},'
            '"correcoes":{"campo":"valor corrigido"} ou {},'
            '"acao":"responder|continuar_conversa|pedir_confirmacao|confirmar_reserva|cancelar|encaminhar_humano",'
            '"deve_avancar_estado":true ou false,'
            '"campo_sugerido":"data|horario|quantidade|nome|confirmacao|null",'
            '"assunto_atual":"texto curto","pergunta_aberta":"texto curto","tom_cliente":"texto curto","resumo_conversa":"texto curto",'
            '"confianca":0.0}. '
            "O formato antigo tambem e aceito internamente: "
            '{"resposta_cliente":"texto para enviar ao cliente",'
            '"reserva":{"status":"em_coleta|confirmada|cancelada|nao_aplicavel",'
            '"data_reserva":"YYYY-MM-DD ou null","horario":"HH:MM ou null",'
            '"pessoas":numero ou null,"observacoes":"texto curto ou null"},'
            '"confianca":0.0}. '
            "Use status confirmada apenas quando data, horário, pessoas e confirmação do cliente estiverem claros. "
            f"Data de hoje: {hoje}."
    )
    logger.info(
        "Contexto Groq montado: telefone=%s source=%s estabelecimento_id=%s espacos=%s faqs_total_ativas=%s "
        "faqs_selecionadas=%s faq_titulos=%s metodo=busca_lexica titulo_tags_categoria_conteudo tamanho_aproximado=%s",
        telefone_limpo,
        config.fonte,
        config.estabelecimento_id,
        len([espaco for espaco in config.espacos if espaco.ativo]),
        len([faq for faq in config.faq_conteudos if faq.ativo]),
        len(faqs_selecionadas),
        [faq.titulo for faq in faqs_selecionadas],
        len(conteudo),
    )
    return {
        "role": "system",
        "content": conteudo,
    }


def _extrair_json_resposta(texto: str) -> dict[str, Any] | None:
    texto_limpo = texto.strip()
    if not texto_limpo:
        return None

    candidatos = [texto_limpo]
    match = re.search(r"\{.*\}", texto_limpo, flags=re.DOTALL)
    if match:
        candidatos.append(match.group(0))

    for candidato in candidatos:
        try:
            dados = json.loads(candidato)
        except json.JSONDecodeError:
            continue
        if isinstance(dados, dict):
            return dados
    return None


def _dados_reserva_de_json(reserva: dict[str, Any]) -> DadosReserva:
    dados: DadosReserva = {}
    data_reserva = reserva.get("data_reserva") or reserva.get("data") or reserva.get("dia")
    if isinstance(data_reserva, str) and _texto_iso_data(data_reserva):
        dados["data_reserva"] = data_reserva.strip()[:10]
    elif isinstance(data_reserva, str):
        data_interpretada = _extrair_data(data_reserva, permitir_dia_isolado=True)
        if data_interpretada:
            dados["data_reserva"] = data_interpretada

    horario = reserva.get("horario") or reserva.get("hora")
    if isinstance(horario, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", horario):
        dados["horario"] = horario
    elif isinstance(horario, str):
        horario_interpretado = _extrair_horario(horario, permitir_numero_isolado=True)
        if horario_interpretado:
            dados["horario"] = horario_interpretado

    pessoas_valor = reserva.get("pessoas") or reserva.get("quantidade") or reserva.get("quantidade_pessoas")
    pessoas = _pessoas_json(pessoas_valor)
    if pessoas is None and isinstance(pessoas_valor, str):
        pessoas = _extrair_pessoas_solicitadas(str(pessoas_valor), permitir_numero_isolado=True)
    if pessoas is not None:
        dados["pessoas"] = pessoas

    observacoes = reserva.get("observacoes")
    if isinstance(observacoes, str) and observacoes.strip():
        dados["observacoes"] = observacoes.strip()

    return dados


def _pessoas_json(valor: Any) -> int | None:
    try:
        pessoas = int(valor)
    except (TypeError, ValueError):
        return None
    return pessoas if 1 <= pessoas <= _limite_pessoas_reserva() else None


def _dados_reserva_minimos(dados: DadosReserva) -> bool:
    return dados_reserva_obrigatorios_ok(
        dados,
        nome_cliente=str(dados.get("nome_cliente") or "cliente"),
        telefone="telefone",
    )


def _confianca(valor: Any) -> float:
    try:
        confianca = float(valor)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(confianca, 1.0))

def _limitar_historico(historico: list[Mensagem]) -> None:
    excesso = len(historico) - MAX_HISTORICO_MENSAGENS
    if excesso > 0:
        del historico[:excesso]


def _extrair_data(texto: str, *, permitir_dia_isolado: bool = False) -> str | None:
    data_iso = _extrair_data_iso(texto)
    if data_iso:
        return data_iso

    data_mes_extenso = _extrair_data_mes_extenso(texto)
    if data_mes_extenso:
        return data_mes_extenso

    match = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", texto)
    if match:
        dia = int(match.group(1))
        mes = int(match.group(2))
        ano_raw = match.group(3)
        ano = _hoje().year
        if ano_raw:
            ano = int(ano_raw)
            if ano < 100:
                ano += 2000

        return _formatar_data_futura(dia, mes, ano)

    data_relativa = _extrair_data_relativa(texto)
    if data_relativa:
        return data_relativa

    match_dia = re.search(r"\bdia\s+(\d{1,2})\b", texto, flags=re.IGNORECASE)
    if match_dia:
        hoje = _hoje()
        dia = int(match_dia.group(1))
        return _formatar_data_futura(dia, hoje.month, hoje.year)

    if permitir_dia_isolado:
        match_isolado = re.fullmatch(r"\D*(\d{1,2})\D*", _normalizar_busca(texto))
        if match_isolado:
            hoje = _hoje()
            dia = int(match_isolado.group(1))
            return _formatar_data_futura(dia, hoje.month, hoje.year)

    return None


def _texto_iso_data(valor: str) -> bool:
    texto = str(valor or "").strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto):
        return False
    try:
        date.fromisoformat(texto)
    except ValueError:
        return False
    return True


def _extrair_data_iso(texto: str) -> str | None:
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", str(texto or ""))
    if not match:
        return None
    try:
        data_reserva = date.fromisoformat(match.group(1))
    except ValueError:
        return None
    if data_reserva < _hoje():
        return None
    return data_reserva.isoformat()


def _extrair_data_relativa(texto: str) -> str | None:
    texto_normalizado = _normalizar_busca(texto)
    hoje = _hoje()

    if re.search(r"\bhoje\b", texto_normalizado):
        return hoje.isoformat()

    if re.search(r"\bamanha\b", texto_normalizado):
        return (hoje + timedelta(days=1)).isoformat()

    dias_semana = {
        "segunda": 0,
        "terca": 1,
        "quarta": 2,
        "quinta": 3,
        "sexta": 4,
        "sabado": 5,
        "domingo": 6,
    }
    match_dia_semana = re.search(
        r"\b(?P<proxima>proxim[ao]\s+)?(?P<dia>segunda|terca|quarta|quinta|sexta|sabado|domingo)(?:\s+feira)?\b",
        texto_normalizado,
    )
    if match_dia_semana:
        dia_semana = dias_semana[match_dia_semana.group("dia")]
        dias_ate = (dia_semana - hoje.weekday()) % 7
        if match_dia_semana.group("proxima") and dias_ate == 0:
            dias_ate = 7
        return (hoje + timedelta(days=dias_ate)).isoformat()

    return None


def _extrair_data_mes_extenso(texto: str) -> str | None:
    texto_normalizado = _normalizar_busca(texto)
    match = re.search(
        r"\b(?:(?:segunda|terca|quarta|quinta|sexta|sabado|domingo)(?:\s+feira)?\s*,?\s*)?"
        r"(\d{1,2})\s+de\s+([a-z]{3,})(?:\s+de\s+(\d{4}))?\b",
        texto_normalizado,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    dia = int(match.group(1))
    mes = MESES.get(match.group(2))
    if mes is None:
        return None
    ano = int(match.group(3)) if match.group(3) else _hoje().year

    return _formatar_data_futura(dia, mes, ano)


def _formatar_data_futura(dia: int, mes: int, ano: int) -> str | None:
    try:
        data_reserva = date(ano, mes, dia)
    except ValueError:
        return None

    if data_reserva < _hoje():
        return None

    return data_reserva.isoformat()


def _extrair_horario(texto: str, *, permitir_numero_isolado: bool = False) -> str | None:
    texto_normalizado = _normalizar_busca(texto)
    if re.search(r"\bmeia[-\s]?noite\b", texto_normalizado):
        return "00:00"
    if re.search(r"\bmeio[-\s]?dia\b", texto_normalizado):
        return "12:00"

    match = re.search(r"\b([01]?\d|2[0-3])[:h]([0-5]\d)?\b", texto_normalizado)
    if match:
        hora = int(match.group(1))
        minuto = int(match.group(2) or 0)
        return f"{hora:02d}:{minuto:02d}"

    palavras_numero = "|".join(sorted((re.escape(palavra) for palavra in NUMEROS_POR_EXTENSO), key=len, reverse=True))
    match_texto_meia = re.search(rf"\b({palavras_numero})\s+e\s+meia\b", texto_normalizado)
    if match_texto_meia:
        hora = _numero_texto_para_int(match_texto_meia.group(1))
        if hora is not None and 0 <= hora <= 23:
            return f"{hora:02d}:30"

    match_texto_periodo = re.search(
        rf"\b({palavras_numero})\s*(?:horas?\s*)?(?:da|de)\s*(manha|tarde|noite)\b",
        texto_normalizado,
    )
    if match_texto_periodo:
        hora = _numero_texto_para_int(match_texto_periodo.group(1))
        periodo = match_texto_periodo.group(2)
        if hora is not None:
            if periodo in {"tarde", "noite"} and 1 <= hora <= 11:
                hora += 12
            if 0 <= hora <= 23:
                return f"{hora:02d}:00"

    match_texto_horas = re.search(rf"\b({palavras_numero})\s+horas?\b", texto_normalizado)
    if match_texto_horas:
        hora = _numero_texto_para_int(match_texto_horas.group(1))
        if hora is not None and 0 <= hora <= 23:
            return f"{hora:02d}:00"

    match_periodo_normalizado = re.search(
        r"\b(\d{1,2})\s*(?:horas?\s*)?(?:da|de)\s*(manha|tarde|noite)\b",
        texto_normalizado,
    )
    if match_periodo_normalizado:
        hora = int(match_periodo_normalizado.group(1))
        periodo = match_periodo_normalizado.group(2)
        if periodo in {"tarde", "noite"} and 1 <= hora <= 11:
            hora += 12
        if 0 <= hora <= 23:
            return f"{hora:02d}:00"

    match_as = re.search(r"\bas\s+(\d{1,2})(?:\s*horas?)?\b", texto_normalizado)
    if match_as:
        hora = int(match_as.group(1))
        if 0 <= hora <= 23:
            return f"{hora:02d}:00"

    match_horas = re.search(r"\b(\d{1,2})\s+horas?\b", texto_normalizado)
    if match_horas:
        hora = int(match_horas.group(1))
        if 0 <= hora <= 23:
            return f"{hora:02d}:00"

    if permitir_numero_isolado:
        match_isolado = re.fullmatch(r"\s*(\d{1,2})\s*[.!?]?\s*", texto_normalizado)
        if match_isolado:
            hora = int(match_isolado.group(1))
            if 0 <= hora <= 23:
                return f"{hora:02d}:00"

    match_periodo = re.search(
        r"\b(\d{1,2})\s*(?:horas?\s*)?(?:da|de)\s*(manha|manhã|tarde|noite)\b",
        texto,
        flags=re.IGNORECASE,
    )
    if not match_periodo:
        return None

    hora = int(match_periodo.group(1))
    periodo = match_periodo.group(2).lower()

    if periodo in {"tarde", "noite"} and 1 <= hora <= 11:
        hora += 12

    if 0 <= hora <= 23:
        return f"{hora:02d}:00"

    return None


def _extrair_pessoas(texto: str) -> int | None:
    pessoas_solicitadas = _extrair_pessoas_solicitadas(texto, permitir_numero_isolado=False)
    if pessoas_solicitadas is not None and 1 <= pessoas_solicitadas <= _limite_pessoas_reserva():
        return pessoas_solicitadas
    return None


def _extrair_pessoas_solicitadas(texto: str, *, permitir_numero_isolado: bool) -> int | None:
    texto_normalizado = _normalizar_busca(texto)

    match_sem_contar = re.search(
        r"\b(\d{1,4}|um|uma|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez)\s+sem\s+contar\s+(?:comigo|eu)\b",
        texto_normalizado,
    )
    if match_sem_contar:
        numero = _numero_texto_para_int(match_sem_contar.group(1))
        if numero is not None:
            return numero + 1

    match_e_mais = re.search(
        r"\b(?:eu|vou|vai\s+eu|vamos\s+eu)\s+e\s+mais\s+(\d{1,4}|um|uma|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez)\b",
        texto_normalizado,
    )
    if match_e_mais:
        numero = _numero_texto_para_int(match_e_mais.group(1))
        if numero is not None:
            return numero + 1

    pessoas_descritas = _contar_pessoas_descricao_simples(texto_normalizado)
    if pessoas_descritas is not None:
        return pessoas_descritas

    padroes = (
        r"\b(\d{1,4})\s*(?:pessoas|pessoa|convidados|lugares)\b",
        r"\bmesa\s+para\s+(\d{1,4})\b",
        r"\bpara\s+(\d{1,4})\b",
        r"\b(?:vao|iremos|somos)\s+(\d{1,4})\b",
    )

    for padrao in padroes:
        match = re.search(padrao, texto_normalizado)
        if match:
            return int(match.group(1))

    if permitir_numero_isolado:
        match_isolado = re.fullmatch(r"\s*(\d{1,4})\s*[.!?]?\s*", texto_normalizado)
        if match_isolado:
            return int(match_isolado.group(1))

    for palavra, numero in NUMEROS_POR_EXTENSO.items():
        padrao = rf"\b{re.escape(palavra)}\s*(?:pessoas|pessoa|convidados|lugares)\b"
        if re.search(padrao, texto_normalizado):
            return numero

    return None


def _numero_texto_para_int(valor: str) -> int | None:
    valor_limpo = _normalizar_busca(valor)
    if valor_limpo.isdigit():
        return int(valor_limpo)
    return NUMEROS_POR_EXTENSO.get(valor_limpo)


def _contar_pessoas_descricao_simples(texto_normalizado: str) -> int | None:
    if not re.search(r"\b(vai|vou|vamos|iremos|somos)\s+eu\b", texto_normalizado):
        return None

    total = 1
    if re.search(r"\b(minha\s+esposa|meu\s+esposo|minha\s+mulher|meu\s+marido|namorada|namorado)\b", texto_normalizado):
        total += 1

    match_criancas = re.search(
        r"\b(\d{1,2}|um|uma|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez)\s+(?:criancas|crianca|filhos|filhas)\b",
        texto_normalizado,
    )
    if match_criancas:
        numero = _numero_texto_para_int(match_criancas.group(1))
        if numero is not None:
            total += numero

    return total if total > 1 else None
