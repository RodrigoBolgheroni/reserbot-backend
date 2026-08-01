"""
Módulo de seleção automática de templates de aniversário vinculados aos Tipos de Clientes (perfis_clientes).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Final, TypedDict

from services import perfis
from services.comunicacao import normalizar_telefone

logger = logging.getLogger(__name__)

TEMPLATE_REFORCO_ANIVERSARIO: Final[str] = "aniversario_reforco_sem_resposta"

# Status configurado dos templates aprovados pela Meta
STATUS_TEMPLATES_CONFIGURADOS: dict[str, str] = {
    "aniversario_f_18_24_qua_qui": "APPROVED",
    "aniversario_f_18_24_fds": "APPROVED",
    "aniversario_f_25_35_qua_qui": "APPROVED",
    "aniversario_f_25_35_fds": "APPROVED",
    "aniversario_f_36_mais_qua_qui": "APPROVED",
    "aniversario_f_36_mais_fds": "APPROVED",
    "aniversario_m_18_24_qua_qui": "APPROVED",
    "aniversario_m_18_24_fds": "APPROVED",
    "aniversario_m_25_35_qua_qui": "APPROVED",
    "aniversario_m_25_35_fds": "APPROVED",
    "aniversario_m_36_mais_qua_qui": "APPROVED",
    "aniversario_m_36_mais_fds": "APPROVED",
    "aniversario_reforco_sem_resposta": "APPROVED",
}


def obter_status_template(template_name: str) -> str:
    if not template_name:
        return "unknown"
    return STATUS_TEMPLATES_CONFIGURADOS.get(template_name.strip(), "unknown")


def atualizar_status_template_configurado(template_name: str, status: str) -> None:
    if template_name and status:
        STATUS_TEMPLATES_CONFIGURADOS[template_name.strip()] = status.strip().upper()


class ResultadoSelecaoTemplate(TypedDict):
    elegivel: bool
    cliente_id: str | None
    primeiro_nome: str | None
    telefone_normalizado: str | None
    idade: int | None
    faixa: str | None
    categoria: str | None
    bloco: str | None
    perfil_id: str | None
    perfil_nome: str | None
    template_name: str | None
    language: str
    variaveis: dict[str, str]
    tom_assistente: str | None
    motivo_bloqueio: str | None


PERFIS_ANIVERSARIO_PADRAO: Final[list[dict[str, Any]]] = [
    {
        "id": "perf-f-18-24",
        "nome": "Mulheres de 18 a 24 anos",
        "descricao": "Mulheres jovens entre 18 e 24 anos",
        "ativo": True,
        "criterios": {
            "sexo": "f",
            "categoria_template": "f",
            "idade_minima": 18,
            "idade_maxima": 24,
            "configuracao_aniversario": {
                "ativo": True,
                "template_qua_qui": "aniversario_f_18_24_qua_qui",
                "template_fds": "aniversario_f_18_24_fds",
                "template_reforco": "aniversario_reforco_sem_resposta",
            },
            "tom_assistente": "Comunicação jovem, descontraída e acolhedora para comemorações de 18 a 24 anos.",
        },
        "prompt_ia": "Use um tom jovem, descontraído e entusiasta. Destaque drinks e comemorações em grupo.",
    },
    {
        "id": "perf-f-25-35",
        "nome": "Mulheres de 25 a 35 anos",
        "descricao": "Mulheres entre 25 e 35 anos",
        "ativo": True,
        "criterios": {
            "sexo": "f",
            "categoria_template": "f",
            "idade_minima": 25,
            "idade_maxima": 35,
            "configuracao_aniversario": {
                "ativo": True,
                "template_qua_qui": "aniversario_f_25_35_qua_qui",
                "template_fds": "aniversario_f_25_35_fds",
                "template_reforco": "aniversario_reforco_sem_resposta",
            },
            "tom_assistente": "Tom elegante, atencioso e sofisticado para celebrações de 25 a 35 anos.",
        },
        "prompt_ia": "Use um tom sofisticado, alegre e atencioso. Destaque reservadinhos, ambiente agradável e carta de vinhos/drinks.",
    },
    {
        "id": "perf-f-36-mais",
        "nome": "Mulheres de 36 anos ou mais",
        "descricao": "Mulheres com 36 anos ou mais",
        "ativo": True,
        "criterios": {
            "sexo": "f",
            "categoria_template": "f",
            "idade_minima": 36,
            "idade_maxima": None,
            "configuracao_aniversario": {
                "ativo": True,
                "template_qua_qui": "aniversario_f_36_mais_qua_qui",
                "template_fds": "aniversario_f_36_mais_fds",
                "template_reforco": "aniversario_reforco_sem_resposta",
            },
            "tom_assistente": "Atendimento cortês, refinado e exclusivo para celebrações de 36+ anos.",
        },
        "prompt_ia": "Use um tom respeitoso, refinado e acolhedor. Priorize conforto e atendimento personalizado.",
    },
    {
        "id": "perf-m-18-24",
        "nome": "Homens de 18 a 24 anos",
        "descricao": "Homens jovens entre 18 e 24 anos",
        "ativo": True,
        "criterios": {
            "sexo": "m",
            "categoria_template": "m",
            "idade_minima": 18,
            "idade_maxima": 24,
            "configuracao_aniversario": {
                "ativo": True,
                "template_qua_qui": "aniversario_m_18_24_qua_qui",
                "template_fds": "aniversario_m_18_24_fds",
                "template_reforco": "aniversario_reforco_sem_resposta",
            },
            "tom_assistente": "Comunicação direta, descontraída e amigável para público masculino de 18 a 24 anos.",
        },
        "prompt_ia": "Use um tom prático, descontraído e direto. Destaque facilidade de reserva e grupos de amigos.",
    },
    {
        "id": "perf-m-25-35",
        "nome": "Homens de 25 a 35 anos",
        "descricao": "Homens entre 25 e 35 anos",
        "ativo": True,
        "criterios": {
            "sexo": "m",
            "categoria_template": "m",
            "idade_minima": 25,
            "idade_maxima": 35,
            "configuracao_aniversario": {
                "ativo": True,
                "template_qua_qui": "aniversario_m_25_35_qua_qui",
                "template_fds": "aniversario_m_25_35_fds",
                "template_reforco": "aniversario_reforco_sem_resposta",
            },
            "tom_assistente": "Tom prático, acolhedor e atencioso para público masculino de 25 a 35 anos.",
        },
        "prompt_ia": "Use um tom prático, objetivo e cortês. Destaque boas opções de mesas, horários e gastronomia.",
    },
    {
        "id": "perf-m-36-mais",
        "nome": "Homens de 36 anos ou mais",
        "descricao": "Homens com 36 anos ou mais",
        "ativo": True,
        "criterios": {
            "sexo": "m",
            "categoria_template": "m",
            "idade_minima": 36,
            "idade_maxima": None,
            "configuracao_aniversario": {
                "ativo": True,
                "template_qua_qui": "aniversario_m_36_mais_qua_qui",
                "template_fds": "aniversario_m_36_mais_fds",
                "template_reforco": "aniversario_reforco_sem_resposta",
            },
            "tom_assistente": "Comunicação formal, respeitosa e eficiente para público masculino de 36+ anos.",
        },
        "prompt_ia": "Use um tom respeitoso, formal e eficiente. Destaque excelência no serviço e reservas exclusivas.",
    },
]


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


def extrair_categoria_perfil_ou_cliente(dados: Mapping[str, Any]) -> str | None:
    for campo in ("categoria", "sexo", "genero", "categoria_template"):
        val = normalizar_categoria(dados.get(campo))
        if val:
            return val

    meta = dados.get("metadata")
    if isinstance(meta, Mapping):
        for campo in ("categoria", "sexo", "genero", "categoria_template"):
            val = normalizar_categoria(meta.get(campo))
            if val:
                return val

    perfil = dados.get("perfil")
    if isinstance(perfil, Mapping):
        criterios = perfil.get("criterios")
        if isinstance(criterios, Mapping):
            for campo in ("categoria", "sexo", "genero", "categoria_template"):
                val = normalizar_categoria(criterios.get(campo))
                if val:
                    return val
        for campo in ("categoria", "sexo", "genero", "categoria_template"):
            val = normalizar_categoria(perfil.get(campo))
            if val:
                return val

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
        dob_ref = date(ref.year, dob.month, 28 if dob.month == 2 and dob.day == 29 else dob.day)
        idade = ref.year - dob.year - (1 if (ref.month, ref.day) < (dob_ref.month, dob_ref.day) else 0)

    if 0 <= idade <= 130:
        return idade
    return None


def obter_faixa_idade(idade: int | None) -> str | None:
    if idade is None or idade < 0:
        return None
    if idade < 18:
        return "menor_18"
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


def _int_opcional(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def selecionar_template_aniversario(
    perfil_ou_cliente: Mapping[str, Any],
    data_referencia: date | str | None = None,
    bloco_campanha: str | None = None,
) -> ResultadoSelecaoTemplate:
    dados = dict(perfil_ou_cliente or {})
    cliente_id = str(dados.get("id") or dados.get("cliente_id") or "") or None

    # Data de referência
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

    # 2. Telefone
    tel_bruto = str(dados.get("telefone") or dados.get("telefone_raw") or "")
    tel_norm = normalizar_telefone(tel_bruto)

    # 3. Bloco da Campanha (qua_qui / fds) - resolvido antecipadamente
    bloco = resolver_bloco_campanha(bloco_campanha or ref_date)

    # 4. Data de Nascimento e Idade
    meta = dados.get("metadata") if isinstance(dados.get("metadata"), Mapping) else {}
    data_nasc = dados.get("data_nascimento") or dados.get("data_nascimento_raw") or meta.get("data_nascimento")
    idade = calcular_idade_campanha(data_nasc, ref_date) if data_nasc else None
    faixa = obter_faixa_idade(idade) if idade is not None else None

    # Verificação de idade < 18
    if idade is not None and idade < 18:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="menor_de_18",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            bloco=bloco,
        )

    cat_direct = dados.get("categoria") if "categoria" in dados else None
    if cat_direct is not None:
        if str(cat_direct).strip() == "":
            return _montar_resultado(
                elegivel=False,
                motivo_bloqueio="categoria_ausente",
                cliente_id=cliente_id,
                primeiro_nome=primeiro_nome,
                telefone_norm=tel_norm,
                idade=idade,
                faixa=faixa,
                bloco=bloco,
            )
        if not normalizar_categoria(cat_direct):
            return _montar_resultado(
                elegivel=False,
                motivo_bloqueio="categoria_invalida",
                cliente_id=cliente_id,
                primeiro_nome=primeiro_nome,
                telefone_norm=tel_norm,
                idade=idade,
                faixa=faixa,
                bloco=bloco,
            )

    # 5. Localizar o Perfil Vinculado
    perfil = perfis.resolver_perfil_cliente(dados)

    # Se cliente não tem perfil explícito, tentar classificar deterministicamente contra perfis cadastrados
    if not perfil:
        perfil = perfis.classificar_cliente(dados)

    if not perfil:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="perfil_ausente",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            bloco=bloco,
        )

    perfil_id = str(perfil.get("id") or "")
    perfil_nome = str(perfil.get("nome") or "")
    criterios = perfil.get("criterios") if isinstance(perfil.get("criterios"), Mapping) else {}
    tom_assistente = str(perfil.get("prompt_ia") or criterios.get("tom_assistente") or perfil.get("tom_assistente") or "")
    cat_perfil = normalizar_categoria(criterios.get("categoria_template") or criterios.get("sexo") or criterios.get("categoria"))

    # 6. Validar se Perfil está Ativo
    if perfil.get("ativo") is False:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="perfil_inativo",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    # 7. Validar Configuração de Aniversário no Perfil
    cfg_aniv = (
        criterios.get("configuracao_aniversario")
        if isinstance(criterios.get("configuracao_aniversario"), Mapping)
        else (perfil.get("configuracao_aniversario") if isinstance(perfil.get("configuracao_aniversario"), Mapping) else None)
    )

    if not cfg_aniv or not isinstance(cfg_aniv, dict):
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="perfil_sem_configuracao_aniversario",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    if cfg_aniv.get("ativo") is False:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="campanha_inativa",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    # 8. Validações de Cliente (Nome, Telefone, Ativo, Marketing, Opt-out, Bloqueado)
    if not primeiro_nome:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="nome_ausente",
            cliente_id=cliente_id,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    if not tel_norm:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="telefone_invalido",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    ativo = dados.get("ativo", meta.get("ativo", True))
    if str(ativo).lower() in {"false", "0", "nao", "não"}:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="cliente_inativo",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    autoriza_mkt = dados.get("autoriza_marketing", dados.get("marketing_autorizado", meta.get("autoriza_marketing", True)))
    if str(autoriza_mkt).lower() in {"false", "0", "nao", "não"}:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="marketing_nao_autorizado",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    opt_out = dados.get("opt_out", meta.get("opt_out", False))
    if str(opt_out).lower() in {"true", "1", "sim"}:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="opt_out",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    bloqueado = dados.get("bloqueado", meta.get("bloqueado", False))
    if str(bloqueado).lower() in {"true", "1", "sim"}:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="contato_bloqueado",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    if not data_nasc:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="data_nascimento_ausente",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            bloco=bloco,
            categoria=cat_perfil,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    if idade is None:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="data_nascimento_invalida",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            bloco=bloco,
            categoria=cat_perfil,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    if idade < 18:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="menor_de_18",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    # 9. Validar compatibilidade da idade com a faixa configurada no perfil
    idade_min = _int_opcional(criterios.get("idade_minima") or criterios.get("idade_min"))
    idade_max = _int_opcional(criterios.get("idade_maxima") or criterios.get("idade_max"))
    if idade_min is not None and idade < idade_min:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="idade_incompativel_perfil",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )
    if idade_max is not None and idade > idade_max:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="idade_incompativel_perfil",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    if not bloco:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="bloco_nao_definido",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    # 10. Ler o Template DIRETAMENTE do Perfil
    if bloco == "qua_qui":
        template_name = str(cfg_aniv.get("template_qua_qui") or "").strip()
    elif bloco == "fds":
        template_name = str(cfg_aniv.get("template_fds") or "").strip()
    else:
        template_name = ""

    if not template_name:
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="template_nao_configurado_no_perfil",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            tom_assistente=tom_assistente,
        )

    # 11. Validar Status APPROVED
    status_template = obter_status_template(template_name)
    if status_template != "APPROVED":
        return _montar_resultado(
            elegivel=False,
            motivo_bloqueio="template_nao_aprovado",
            cliente_id=cliente_id,
            primeiro_nome=primeiro_nome,
            telefone_norm=tel_norm,
            idade=idade,
            faixa=faixa,
            categoria=cat_perfil,
            bloco=bloco,
            perfil_id=perfil_id,
            perfil_nome=perfil_nome,
            template_name=template_name,
            tom_assistente=tom_assistente,
        )

    # Tudo VÁLIDO!
    return _montar_resultado(
        elegivel=True,
        motivo_bloqueio=None,
        cliente_id=cliente_id,
        primeiro_nome=primeiro_nome,
        telefone_norm=tel_norm,
        idade=idade,
        faixa=faixa,
        categoria=cat_perfil,
        bloco=bloco,
        perfil_id=perfil_id,
        perfil_nome=perfil_nome,
        template_name=template_name,
        language="pt_BR",
        variaveis={"1": primeiro_nome},
        tom_assistente=tom_assistente,
    )


def _montar_resultado(
    *,
    elegivel: bool,
    motivo_bloqueio: str | None,
    cliente_id: str | None = None,
    primeiro_nome: str | None = None,
    telefone_norm: str | None = None,
    idade: int | None = None,
    faixa: str | None = None,
    categoria: str | None = None,
    bloco: str | None = None,
    perfil_id: str | None = None,
    perfil_nome: str | None = None,
    template_name: str | None = None,
    language: str = "pt_BR",
    variaveis: dict[str, str] | None = None,
    tom_assistente: str | None = None,
) -> ResultadoSelecaoTemplate:
    return {
        "elegivel": elegivel,
        "cliente_id": cliente_id,
        "primeiro_nome": primeiro_nome,
        "telefone_normalizado": telefone_norm,
        "idade": idade,
        "faixa": faixa,
        "categoria": categoria,
        "bloco": bloco,
        "perfil_id": perfil_id,
        "perfil_nome": perfil_nome,
        "template_name": template_name,
        "language": language,
        "variaveis": variaveis or ({"1": primeiro_nome} if primeiro_nome else {}),
        "tom_assistente": tom_assistente,
        "motivo_bloqueio": motivo_bloqueio,
    }
