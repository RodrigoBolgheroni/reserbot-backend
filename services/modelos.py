from __future__ import annotations

from typing import Any, Literal, TypedDict


StatusConversa = Literal[
    "aberta",
    "aguardando_cliente",
    "em_atendimento",
    "bot_ativo",
    "humano",
    "aguardando_humano",
    "finalizada",
    "erro",
]
OrigemConversa = Literal["aniversario", "pdf", "whatsapp", "manual", "webhook"]
RemetenteMensagem = Literal["cliente", "bot", "agente", "sistema"]
StatusReserva = Literal["pendente", "identificada", "confirmada", "cancelada", "erro"]


class Cliente(TypedDict, total=False):
    id: str
    nome: str
    telefone: str
    telefone_raw: str
    telefones: list[str]
    data_nascimento: str
    data_nascimento_raw: str
    aniversario_ddmm: str
    idade: int
    info_topo_pdf: str
    periodo_aniversario: str
    tipo: str
    regiao: str
    origem: str
    perfil_id: str
    perfil_nome: str
    metadata: dict[str, Any]


class Conversa(TypedDict, total=False):
    id: str
    cliente_id: str
    cliente_telefone: str
    status: StatusConversa
    data_inicio: str
    data_fim: str
    origem: OrigemConversa
    metadata: dict[str, Any]


class MensagemConversa(TypedDict, total=False):
    id: str
    conversa_id: str
    remetente: RemetenteMensagem
    conteudo: str
    timestamp: str
    provider_message_id: str
    metadata: dict[str, Any]


class ReservaBanco(TypedDict, total=False):
    id: str
    cliente_id: str
    cliente_telefone: str
    conversa_id: str
    data_reserva: str
    horario: str
    pessoas: int
    observacoes: str
    status: StatusReserva
    metadata: dict[str, Any]


class PerfilCliente(TypedDict, total=False):
    id: str
    nome: str
    descricao: str
    ativo: bool
    criterios: dict[str, Any]
    mensagem_disparo: str
    prompt_ia: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any]
