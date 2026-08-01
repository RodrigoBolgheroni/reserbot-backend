"""
Módulo de seleção automática de templates de aniversário aprovados pela Meta.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Final, TypedDict

from services.comunicacao import normalizar_telefone

logger = logging.getLogger(__name__)

TEMPLATE_REFORCO_ANIVERSARIO: Final[str] = "aniversario_reforco_sem_resposta"

# Mapeamento centralizado de templates aprovados pela Meta: (categoria, faixa, bloco) -> template_name
MAPEAMENTO_TEMPLATES: Final[dict[tuple[str, str, str], str]] = {
    # Feminino
    ("f", "18_24", "qua_qui"): "aniversario_f_18_24_qua_qui",
    ("f", "18_24", "fds"): "aniversario_f_18_24_fds",
    ("f", "25_35", "qua_qui"): "aniversario_f_25_35_qua_qui",
    ("f", "25_35", "fds"): "aniversario_f_25_35_fds",
    ("f", "36_mais", "qua_qui"): "aniversario_f_36_mais_qua_qui",
    ("f", "36_mais", "fds"): "aniversario_f_36_mais_fds",
    # Masculino
    ("m", "18_24", "qua_qui"): "aniversario_m_18_24_qua_qui",
    ("m", "18_24", "fds"): "aniversario_m_18_24_fds",
    ("m", "25_35", "qua_qui"): "aniversario_m_25_35_qua_qui",
    ("m", "25_35", "fds"): "aniversario_m_25_35_fds",
    ("m", "36_mais", "qua_qui"): "aniversario_m_36_mais_qua_qui",
    ("m", "36_mais", "fds"): "aniversario_m_36_mais_fds",
}


class ResultadoSelecaoTemplate(TypedDict):
    elegivel: bool
    cliente_id: str | None
    primeiro_nome: str | None
    telefone_normalizado: str | None
    idade: int | None
    faixa: str | None
    categoria: str | None
    bloco: str | None
    template_name: str | None
    language: str
    variaveis: dict[str, str]
    motivo_bloqueio: str | None


def obter_primeiro_nome(nome: str | None) -> str | None:
    if not nome:
        return None
    limpo = " ".join(str(nome).strip().split())
    if not limpo:
        return None
    partes = limpo.split()
    return partes[0] if partes else None


def normalizar_categoria(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip().lower()
    if not texto:
        return None

    if texto in {"f", "feminino", "mulher", "feminina"}:
        return "f"
    if texto in {"m", "masculino", "homem", "masculina"}:
        return "m"

    return None


def calcular_idade_campanha(data_nascimento: Any, data_referencia: date | None = None) -> int | None:
    if not data_nascimento:
        return None

    ref = data_referencia or date.today()
    dob: date | None = None

    if isinstance(data_nascimento, datetime):
        dob = data_nascimento.date()
    elif isinstance(data_nascimento, date):
        dob = data_nascimento
    elif isinstance(data_nascimento, str):
        texto = data_nascimento.strip()
        if not texto:
            return None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                dob = datetime.strptime(texto, fmt).date()
                break
            except ValueError:
                continue

    if dob is None:
        return None

    try:
        idade = ref.year - dob.year - (1 if (ref.month, ref.day) < (dob.month, dob.day) else 0)
    except Exception:
        # Tratamento para 29 de fevereiro em anos não bissextos
        dob_ref = date(ref.year, dob.month, 28 if dob.month == 2 and dob.day == 29 else dob.day)
        idade = ref.year - dob.year - (1 if (ref.month, ref.day) < (dob_ref.month, dob_ref.day) else 0)

    if 0 <= idade <= 130:
        return idade
    return None


def obter_faixa_idade(idade: int | None) -> str | None:
    if idade is None or idade < 18:
        return None
    if 18 <= idade <= 24:
        return "18_24"
    if 25 <= idade <= 35:
        return "25_35"
    return "36_mais"


def resolver_bloco_campanha(data_referencia_ou_bloco: date | str | None) -> str | None:
    if isinstance(data_referencia_ou_bloco, str):
        val = data_referencia_ou_bloco.strip().lower()
        if val in {"qua_qui", "fds"}:
            return val

    ref: date | None = None
    if isinstance(data_referencia_ou_bloco, date):
        ref = data_referencia_ou_bloco
    elif isinstance(data_referencia_ou_bloco, str):
        try:
            ref = date.fromisoformat(data_referencia_ou_bloco.strip()[:10])
        except ValueError:
            ref = None

    if ref is None:
        return None

    # weekday(): 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
    w = ref.weekday()
    if w in (2, 3):  # Quarta, Quinta
        return "qua_qui"
    if w in (4, 5, 6):  # Sexta, Sábado, Domingo
        return "fds"

    return None


def extrair_categoria_perfil_ou_cliente(dados: Mapping[str, Any]) -> str | None:
    # 1. Procura em campos diretos
    for campo in ("categoria", "sexo", "genero", "perfil_demografico"):
        val = normalizar_categoria(dados.get(campo))
        if val:
            return val

    # 2. Procura em metadata do cliente
    meta = dados.get("metadata")
    if isinstance(meta, Mapping):
        for campo in ("categoria", "sexo", "genero", "perfil_demografico"):
            val = normalizar_categoria(meta.get(campo))
            if val:
                return val

    # 3. Procura no perfil associado ou seus criterios
    perfil = dados.get("perfil")
    if isinstance(perfil, Mapping):
        for campo in ("categoria", "sexo", "genero"):
            val = normalizar_categoria(perfil.get(campo))
            if val:
                return val
        criterios = perfil.get("criterios")
        if isinstance(criterios, Mapping):
            for campo in ("categoria", "sexo", "genero"):
                val = normalizar_categoria(criterios.get(campo))
                if val:
                    return val

    return None


def selecionar_template_aniversario(
    perfil_ou_cliente: Mapping[str, Any],
    data_referencia: date | str | None = None,
    bloco_campanha: str | None = None,
) -> ResultadoSelecaoTemplate:
    dados = dict(perfil_ou_cliente or {})
    cliente_id = str(dados.get("id") or dados.get("cliente_id") or "") or None

    # Data de referência
    ref_date: date | None = None
    if isinstance(data_referencia, date):
        ref_date = data_referencia
    elif isinstance(data_referencia, str) and data_referencia.strip():
        try:
            ref_date = date.fromisoformat(data_referencia.strip()[:10])
        except ValueError:
            ref_date = date.today()
    else:
        ref_date = date.today()

    # 1. Nome / Primeiro nome
    nome_bruto = dados.get("nome") or dados.get("nome_completo")
    primeiro_nome = obter_primeiro_nome(nome_bruto)
    if not primeiro_nome:
        return _resultado_bloqueado(cliente_id, "nome_ausente")

    # 2. Telefone
    tel_bruto = str(dados.get("telefone") or dados.get("telefone_raw") or "")
    tel_norm = normalizar_telefone(tel_bruto)
    if not tel_norm:
        return _resultado_bloqueado(cliente_id, "telefone_invalido", primeiro_nome=primeiro_nome)

    # 3. Cliente Ativo
    meta = dados.get("metadata") if isinstance(dados.get("metadata"), Mapping) else {}
    ativo = dados.get("ativo", meta.get("ativo", True))
    if str(ativo).lower() in {"false", "0", "nao", "não"}:
        return _resultado_bloqueado(cliente_id, "cliente_inativo", primeiro_nome=primeiro_nome, telefone_norm=tel_norm)

    # 4. Autorização de Marketing
    autoriza_mkt = dados.get(
        "autoriza_marketing",
        dados.get("marketing_autorizado", meta.get("autoriza_marketing", meta.get("marketing_autorizado", True))),
    )
    if str(autoriza_mkt).lower() in {"false", "0", "nao", "não"}:
        return _resultado_bloqueado(cliente_id, "marketing_nao_autorizado", primeiro_nome=primeiro_nome, telefone_norm=tel_norm)

    # 5. Opt-out
    opt_out = dados.get("opt_out", meta.get("opt_out", False))
    if str(opt_out).lower() in {"true", "1", "sim"}:
        return _resultado_bloqueado(cliente_id, "opt_out", primeiro_nome=primeiro_nome, telefone_norm=tel_norm)

    # 6. Bloqueio de contato
    bloqueado = dados.get("bloqueado", meta.get("bloqueado", False))
    if str(bloqueado).lower() in {"true", "1", "sim"}:
        return _resultado_bloqueado(cliente_id, "contato_bloqueado", primeiro_nome=primeiro_nome, telefone_norm=tel_norm)

    # 7. Data de nascimento e cálculo de idade
    data_nasc = dados.get("data_nascimento") or dados.get("data_nascimento_raw") or meta.get("data_nascimento")
    if not data_nasc:
        return _resultado_bloqueado(cliente_id, "data_nascimento_ausente", primeiro_nome=primeiro_nome, telefone_norm=tel_norm)

    idade = calcular_idade_campanha(data_nasc, ref_date)
    if idade is None:
        return _resultado_bloqueado(cliente_id, "data_nascimento_invalida", primeiro_nome=primeiro_nome, telefone_norm=tel_norm)

    if idade < 18:
        return _resultado_bloqueado(cliente_id, "menor_de_18", primeiro_nome=primeiro_nome, telefone_norm=tel_norm, idade=idade)

    faixa = obter_faixa_idade(idade)

    # 8. Categoria (feminino/masculino)
    cat_bruta = dados.get("categoria") or dados.get("sexo") or dados.get("genero") or meta.get("categoria") or meta.get("sexo") or meta.get("genero")
    if not cat_bruta:
        # tentar extrator recursivo
        cat_bruta = extrair_categoria_perfil_ou_cliente(dados)

    categoria = normalizar_categoria(cat_bruta)
    if not cat_bruta:
        return _resultado_bloqueado(cliente_id, "categoria_ausente", primeiro_nome=primeiro_nome, telefone_norm=tel_norm, idade=idade, faixa=faixa)
    if not categoria:
        return _resultado_bloqueado(cliente_id, "categoria_invalida", primeiro_nome=primeiro_nome, telefone_norm=tel_norm, idade=idade, faixa=faixa)

    # 9. Bloco da Campanha (qua_qui / fds)
    bloco = resolver_bloco_campanha(bloco_campanha or ref_date)
    if not bloco:
        return _resultado_bloqueado(
            cliente_id,
            "bloco_nao_definido",
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=categoria,
        )

    # 10. Seleção do Template
    chave = (categoria, faixa, bloco)
    template_name = MAPEAMENTO_TEMPLATES.get(chave)
    if not template_name:
        return _resultado_bloqueado(
            cliente_id,
            "template_nao_mapeado",
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=categoria,
            bloco=bloco,
        )

    return {
        "elegivel": True,
        "cliente_id": cliente_id,
        "primeiro_nome": primeiro_nome,
        "telefone_normalizado": tel_norm,
        "idade": idade,
        "faixa": faixa,
        "categoria": categoria,
        "bloco": bloco,
        "template_name": template_name,
        "language": "pt_BR",
        "variaveis": {
            "1": primeiro_nome,
        },
        "motivo_bloqueio": None,
    }


def _resultado_bloqueado(
    cliente_id: str | None,
    motivo: str,
    *,
    primeiro_nome: str | None = None,
    telefone_norm: str | None = None,
    idade: int | None = None,
    faixa: str | None = None,
    categoria: str | None = None,
    bloco: str | None = None,
) -> ResultadoSelecaoTemplate:
    return {
        "elegivel": False,
        "cliente_id": cliente_id,
        "primeiro_nome": primeiro_nome,
        "telefone_normalizado": telefone_norm,
        "idade": idade,
        "faixa": faixa,
        "categoria": categoria,
        "bloco": bloco,
        "template_name": None,
        "language": "pt_BR",
        "variaveis": {},
        "motivo_bloqueio": motivo,
    }
