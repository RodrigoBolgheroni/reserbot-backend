from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
from typing import Any, Final

from services.comunicacao import ResultadoEnvioCanal


logger = logging.getLogger(__name__)

__all__ = ["iniciar", "enviar", "enviar_com_resultado", "ler_ultima_mensagem"]

ROOT_DIR: Final[Path] = Path(__file__).resolve().parents[1]
WHATSAPP_URL: Final[str] = "https://web.whatsapp.com"
_driver: Any | None = None


def iniciar() -> bool:
    if _usar_cloud_api():
        return _canal_cloud().iniciar()

    global _driver

    if _driver is not None:
        return True

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        profile_dir = Path(
            os.getenv("WHATSAPP_PROFILE_DIR", "data/chrome_profile")
        )
        if not profile_dir.is_absolute():
            profile_dir = ROOT_DIR / profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)

        options = webdriver.ChromeOptions()
        options.add_argument(f"--user-data-dir={profile_dir}")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--disable-notifications")
        options.add_argument("--start-maximized")

        service = Service(ChromeDriverManager().install())
        _driver = webdriver.Chrome(service=service, options=options)
        _driver.get(WHATSAPP_URL)

        logger.info("WhatsApp Web iniciado. Faca login pelo QR Code se necessario.")
        return True
    except Exception:
        logger.exception("Falha ao iniciar WhatsApp Web.")
        _driver = None
        return False


def enviar(telefone: str, texto: str) -> bool:
    resultado = enviar_com_resultado(telefone, texto)
    return bool(resultado.get("ok"))


def enviar_com_resultado(telefone: str, texto: str) -> ResultadoEnvioCanal:
    if _usar_cloud_api():
        return _canal_cloud().enviar_mensagem(telefone, texto)

    telefone_limpo = _normalizar_telefone(telefone)
    if not telefone_limpo:
        logger.warning("Telefone invalido para envio: %s", telefone)
        return {
            "ok": False,
            "telefone": telefone,
            "provider": "selenium",
            "erro": "telefone invalido",
        }

    if not iniciar():
        return {
            "ok": False,
            "telefone": telefone_limpo,
            "provider": "selenium",
            "erro": "falha ao iniciar WhatsApp Web",
        }

    try:
        _abrir_conversa(telefone_limpo)

        if not texto.strip():
            return {
                "ok": True,
                "telefone": telefone_limpo,
                "provider": "selenium",
            }

        caixa = _aguardar_caixa_mensagem()
        _colar_e_enviar(caixa, texto)
        logger.info("Mensagem enviada para %s.", telefone_limpo)
        return {
            "ok": True,
            "telefone": telefone_limpo,
            "provider": "selenium",
        }
    except Exception:
        logger.exception("Falha ao enviar mensagem para %s.", telefone_limpo)
        return {
            "ok": False,
            "telefone": telefone_limpo,
            "provider": "selenium",
            "erro": "falha ao enviar mensagem",
        }


def _obter_nome_conta_logada() -> str | None:
    """Pega o nome da conta WhatsApp logada no momento."""
    if _driver is None:
        return None
    try:
        resultado = _driver.execute_script("""
            // O nome da conta logada aparece no header do perfil
            const el = document.querySelector('[data-testid="contact-info-subtitle"]')
                    || document.querySelector('header span[dir="auto"]');
            return el ? el.innerText.trim() : null;
        """)
        return resultado
    except Exception:
        return None


def ler_ultima_mensagem(
    remetente_conhecido: str | None = None,
    nome_bot: str | None = None,
) -> tuple[str | None, str | None]:
    """Retorna (texto, remetente) da última mensagem recebida."""
    if _usar_cloud_api():
        return _canal_cloud().ler_ultima_mensagem(remetente_conhecido, nome_bot)

    if _driver is None:
        return None, None

    try:
        import time
        time.sleep(1.5)

        resultado = _driver.execute_script("""
            const containers = document.querySelectorAll('[data-testid="msg-container"]');
            const msgs = [];
            for (const c of containers) {
                const copyable = c.querySelector('.copyable-text');
                if (!copyable) continue;
                const prePlain = copyable.getAttribute('data-pre-plain-text') || '';
                const wrapper = c.closest('.message-in, .message-out') || c.querySelector('.message-in, .message-out');
                const fromMe = wrapper ? wrapper.classList.contains('message-out') : false;

                // Pega todos os spans de texto, excluindo os dentro de quoted-message
                const quoted = copyable.querySelector('[data-testid="quoted-message"]');
                const spans = copyable.querySelectorAll('[data-testid="selectable-text"]');
                let text = '';
                for (const span of spans) {
                    if (quoted && quoted.contains(span)) continue;
                    text = span.innerText.trim();
                }

                if (text) msgs.push({ text, prePlain, fromMe });
            }
            return msgs;
        """)
        if not resultado:
            logger.debug("ler_ultima_mensagem: nenhum msg-container encontrado.")
            return None, None

        def extrair_remetente(pre: str) -> str | None:
            try:
                return pre.split("] ", 1)[1].rstrip(": ").strip()
            except (IndexError, AttributeError):
                return None

        # Se já sabemos quem é o cliente, filtra direto
        if remetente_conhecido:
            recebidas = [
                m for m in resultado
                if extrair_remetente(m["prePlain"]) == remetente_conhecido
            ]
            if recebidas:
                texto = recebidas[-1]["text"]
                logger.debug("ler_ultima_mensagem: %r (remetente conhecido)", texto)
                return texto, remetente_conhecido

        # Se sabemos o nome do bot, exclui as mensagens dele
        if nome_bot:
            recebidas = [
                m for m in resultado
                if extrair_remetente(m["prePlain"]) != nome_bot
            ]
            if recebidas:
                rem = extrair_remetente(recebidas[-1]["prePlain"])
                texto = recebidas[-1]["text"]
                logger.debug("ler_ultima_mensagem: %r remetente=%r", texto, rem)
                return texto, rem

        recebidas = [m for m in resultado if not m.get("fromMe")]
        if recebidas:
            rem = extrair_remetente(recebidas[-1]["prePlain"])
            texto = recebidas[-1]["text"]
            logger.debug("ler_ultima_mensagem: %r remetente=%r (incoming)", texto, rem)
            return texto, rem

        logger.debug("ler_ultima_mensagem: sem critério suficiente para filtrar.")
        return None, None

    except Exception:
        logger.exception("Falha ao ler ultima mensagem.")
        return None, None
def _abrir_conversa(telefone: str) -> None:
    assert _driver is not None
    _driver.get(f"{WHATSAPP_URL}/send?phone={telefone}")
    _aguardar_caixa_mensagem()


def _aguardar_caixa_mensagem() -> Any:
    assert _driver is not None

    from selenium.webdriver.support.ui import WebDriverWait

    timeout = _int_env("WHATSAPP_WAIT_TIMEOUT_SEGUNDOS", 60)
    wait = WebDriverWait(_driver, timeout)
    return wait.until(_localizar_caixa_mensagem)


def _localizar_caixa_mensagem(driver: Any) -> Any | None:
    seletores = (
        "footer div[contenteditable='true'][role='textbox']",
        "footer div[contenteditable='true'][data-tab]",
        "div[contenteditable='true'][role='textbox']",
    )

    for seletor in seletores:
        elementos = driver.find_elements("css selector", seletor)
        for elemento in elementos:
            if elemento.is_displayed() and elemento.is_enabled():
                return elemento

    return None


def _colar_e_enviar(caixa: Any, texto: str) -> None:
    import pyperclip
    from selenium.webdriver.common.keys import Keys

    pyperclip.copy(texto)
    caixa.click()
    caixa.send_keys(_tecla_colar(Keys), "v")
    caixa.send_keys(Keys.ENTER)


def _tecla_colar(keys: Any) -> Any:
    if platform.system() == "Darwin":
        return keys.COMMAND
    return keys.CONTROL


def _normalizar_telefone(telefone: str) -> str:
    from services.comunicacao import normalizar_telefone

    return normalizar_telefone(telefone)


def _usar_cloud_api() -> bool:
    return _provider_whatsapp() in {"cloud", "cloud_api", "meta"}


def _provider_whatsapp() -> str:
    return os.getenv("WHATSAPP_PROVIDER", "cloud").strip().lower() or "cloud"


def _canal_cloud() -> Any:
    from services.whatsapp_cloud import WhatsAppCloudChannel

    return WhatsAppCloudChannel()


def _int_env(nome: str, padrao: int) -> int:
    try:
        return int(os.getenv(nome, str(padrao)))
    except ValueError:
        return padrao
