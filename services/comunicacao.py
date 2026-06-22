from __future__ import annotations

from typing import Any, Protocol, TypedDict


class MensagemRecebida(TypedDict, total=False):
    telefone: str
    texto: str
    remetente: str
    timestamp: str
    provider_message_id: str
    raw: dict[str, Any]


class ResultadoEnvioCanal(TypedDict, total=False):
    ok: bool
    telefone: str
    provider: str
    provider_message_id: str
    erro: str
    detalhe: str


class CanalMensagens(Protocol):
    provider: str

    def iniciar(self) -> bool:
        ...

    def enviar_mensagem(self, telefone: str, texto: str) -> ResultadoEnvioCanal:
        ...

    def ler_ultima_mensagem(
        self,
        remetente_conhecido: str | None = None,
        nome_bot: str | None = None,
    ) -> tuple[str | None, str | None]:
        ...


def normalizar_telefone(telefone: str) -> str:
    digitos = "".join(caractere for caractere in telefone if caractere.isdigit())
    if len(digitos) in (10, 11):
        digitos = f"55{digitos}"
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        return digitos
    return ""
