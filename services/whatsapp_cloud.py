from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from services.comunicacao import MensagemRecebida, ResultadoEnvioCanal, normalizar_telefone


logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"


class WhatsAppCloudChannel:
    provider = "cloud"

    def iniciar(self) -> bool:
        ok = configurado()
        if not ok:
            logger.warning("WhatsApp Cloud API nao configurada.")
        return ok

    def enviar_mensagem(self, telefone: str, texto: str) -> ResultadoEnvioCanal:
        telefone_limpo = normalizar_telefone(telefone)
        if not telefone_limpo:
            return {"ok": False, "telefone": telefone, "provider": self.provider, "erro": "telefone invalido"}
        if not texto.strip():
            return {"ok": True, "telefone": telefone_limpo, "provider": self.provider}

        token = _access_token()
        phone_id = _phone_number_id()
        if not token or not phone_id:
            return {
                "ok": False,
                "telefone": telefone_limpo,
                "provider": self.provider,
                "erro": "WhatsApp Cloud API nao configurada",
            }

        payload = {
            "messaging_product": "whatsapp",
            "to": telefone_limpo,
            "type": "text",
            "text": {"preview_url": False, "body": texto},
        }
        request = Request(
            f"https://graph.facebook.com/{_api_version()}/{phone_id}/messages",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with urlopen(request, timeout=_timeout()) as response:
                data = _ler_json(response.read().decode("utf-8"))
                message_id = _extrair_message_id(data)
                logger.info(
                    "Mensagem enviada pela WhatsApp Cloud API para %s. message_id=%s",
                    telefone_limpo,
                    message_id or "indisponivel",
                )
                return {
                    "ok": True,
                    "telefone": telefone_limpo,
                    "provider": self.provider,
                    "provider_message_id": message_id,
                }
        except HTTPError as erro:
            detalhe = _ler_erro_http(erro)
            logger.warning("WhatsApp Cloud API retornou HTTP %s: %s", erro.code, detalhe)
            return {
                "ok": False,
                "telefone": telefone_limpo,
                "provider": self.provider,
                "erro": f"HTTP {erro.code}",
                "detalhe": detalhe,
            }
        except (OSError, URLError) as erro:
            logger.warning("Falha ao enviar pela WhatsApp Cloud API: %s", erro)
            return {
                "ok": False,
                "telefone": telefone_limpo,
                "provider": self.provider,
                "erro": "falha de conexao",
                "detalhe": str(erro),
            }

    def ler_ultima_mensagem(
        self,
        remetente_conhecido: str | None = None,
        nome_bot: str | None = None,
    ) -> tuple[str | None, str | None]:
        logger.debug("WhatsApp Cloud API usa webhooks; leitura ativa nao se aplica.")
        return None, None


def extrair_mensagens_webhook(payload: dict[str, Any]) -> list[MensagemRecebida]:
    mensagens: list[MensagemRecebida] = []
    for entry in _lista(payload.get("entry")):
        for change in _lista(entry.get("changes")):
            value = change.get("value") if isinstance(change, dict) else {}
            if not isinstance(value, dict):
                continue
            contatos = _contatos_por_wa_id(value)
            for item in _lista(value.get("messages")):
                mensagem = _parse_mensagem(item, contatos)
                if mensagem is not None:
                    mensagens.append(mensagem)
    return mensagens


def verificar_token_webhook(token_recebido: str) -> bool:
    esperado = _verify_token()
    return bool(esperado and token_recebido == esperado)


def configurado() -> bool:
    return bool(_access_token() and _phone_number_id())


def _parse_mensagem(item: Any, contatos: dict[str, str]) -> MensagemRecebida | None:
    if not isinstance(item, dict):
        return None
    telefone = normalizar_telefone(str(item.get("from", "")))
    if not telefone:
        return None
    texto = _texto_mensagem(item)
    if not texto:
        return None
    timestamp = _timestamp(item.get("timestamp"))
    return {
        "telefone": telefone,
        "texto": texto,
        "remetente": contatos.get(telefone, telefone),
        "timestamp": timestamp,
        "provider_message_id": str(item.get("id") or ""),
        "raw": item,
    }


def _texto_mensagem(item: dict[str, Any]) -> str:
    tipo = item.get("type")
    if tipo == "text":
        text = item.get("text")
        if isinstance(text, dict):
            return str(text.get("body") or "").strip()
    if tipo == "button":
        button = item.get("button")
        if isinstance(button, dict):
            return str(button.get("text") or button.get("payload") or "").strip()
    if tipo == "interactive":
        interactive = item.get("interactive")
        if isinstance(interactive, dict):
            for chave in ("button_reply", "list_reply"):
                reply = interactive.get(chave)
                if isinstance(reply, dict):
                    return str(reply.get("title") or reply.get("id") or "").strip()
    return ""


def _contatos_por_wa_id(value: dict[str, Any]) -> dict[str, str]:
    contatos: dict[str, str] = {}
    for contato in _lista(value.get("contacts")):
        if not isinstance(contato, dict):
            continue
        telefone = normalizar_telefone(str(contato.get("wa_id", "")))
        profile = contato.get("profile")
        nome = ""
        if isinstance(profile, dict):
            nome = str(profile.get("name") or "").strip()
        if telefone and nome:
            contatos[telefone] = nome
    return contatos


def _timestamp(valor: Any) -> str:
    try:
        return datetime.fromtimestamp(int(valor), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(tz=timezone.utc).isoformat()


def _lista(valor: Any) -> list[Any]:
    return valor if isinstance(valor, list) else []


def _access_token() -> str:
    return _env("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_CLOUD_ACCESS_TOKEN")


def _phone_number_id() -> str:
    return _env("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_CLOUD_PHONE_NUMBER_ID")


def _verify_token() -> str:
    return _env("WHATSAPP_VERIFY_TOKEN", "WHATSAPP_CLOUD_VERIFY_TOKEN")


def _api_version() -> str:
    return _env("WHATSAPP_API_VERSION", "WHATSAPP_CLOUD_API_VERSION") or "v20.0"


def _timeout() -> int:
    try:
        return int(_env("WHATSAPP_TIMEOUT_SEGUNDOS", "WHATSAPP_CLOUD_TIMEOUT_SEGUNDOS") or "20")
    except ValueError:
        return 20


def _env(*nomes: str) -> str:
    env_local = _ler_env_local()
    for nome in nomes:
        valor = os.getenv(nome, "").strip() or env_local.get(nome, "").strip()
        if valor:
            return _limpar_valor_env(valor)
    return ""


def _ler_env_local(path: Path = ENV_PATH) -> dict[str, str]:
    if not path.exists():
        return {}

    try:
        linhas = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    valores: dict[str, str] = {}
    for linha in linhas:
        texto = linha.strip()
        if not texto or texto.startswith("#") or "=" not in texto:
            continue
        chave, valor = texto.split("=", 1)
        valores[chave.strip()] = _limpar_valor_env(valor)
    return valores


def _limpar_valor_env(valor: str) -> str:
    texto = valor.strip()
    if len(texto) >= 2 and texto[0] == texto[-1] and texto[0] in {"'", '"'}:
        return texto[1:-1].strip()
    return texto


def _ler_json(corpo: str) -> dict[str, Any]:
    try:
        data = json.loads(corpo or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _extrair_message_id(data: dict[str, Any]) -> str:
    messages = data.get("messages")
    if isinstance(messages, list) and messages:
        item = messages[0]
        if isinstance(item, dict):
            return str(item.get("id") or "")
    return ""


def _ler_erro_http(erro: HTTPError) -> str:
    try:
        corpo = erro.read().decode("utf-8")
    except Exception:
        return str(erro)
    return corpo[:1200] if corpo else str(erro)
