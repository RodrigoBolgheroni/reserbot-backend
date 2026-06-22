from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)

ROOT_DIR: Final[Path] = Path(__file__).resolve().parents[1]
ENV_PATH: Final[Path] = ROOT_DIR / ".env"
TIMEOUT_SEGUNDOS: Final[int] = 30


class SupabaseConfig(TypedDict):
    url: str
    chave: str


class SupabaseResultado(TypedDict, total=False):
    ok: bool
    status: int
    tabela: str
    data: Any
    total: int
    content_range: str
    erro: str
    detalhe: str


def configurado() -> bool:
    return _carregar_config() is not None


def inserir(
    tabela: str,
    payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    retornar: bool = True,
) -> SupabaseResultado:
    prefer = "return=representation" if retornar else "return=minimal"
    return requisitar("POST", tabela, payload=payload, prefer=prefer)


def upsert(
    tabela: str,
    payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    on_conflict: str,
    retornar: bool = True,
) -> SupabaseResultado:
    prefer = "resolution=merge-duplicates,return=representation" if retornar else "resolution=merge-duplicates,return=minimal"
    query = f"?on_conflict={quote(on_conflict)}"
    return requisitar("POST", tabela, payload=payload, query=query, prefer=prefer)


def selecionar(
    tabela: str,
    *,
    filtros: Mapping[str, str] | None = None,
    colunas: str = "*",
    limite: int | None = None,
    offset: int | None = None,
    contar: bool = False,
) -> SupabaseResultado:
    parametros: dict[str, str] = {"select": colunas}
    for campo, valor in (filtros or {}).items():
        parametros[campo] = valor
    if limite is not None:
        parametros["limit"] = str(limite)
    if offset is not None:
        parametros["offset"] = str(offset)
    prefer = "count=exact" if contar else None
    return requisitar("GET", tabela, query=f"?{urlencode(parametros)}", prefer=prefer)


def atualizar(
    tabela: str,
    payload: Mapping[str, Any],
    *,
    filtros: Mapping[str, str],
    retornar: bool = True,
) -> SupabaseResultado:
    parametros = urlencode(dict(filtros))
    prefer = "return=representation" if retornar else "return=minimal"
    return requisitar("PATCH", tabela, payload=payload, query=f"?{parametros}", prefer=prefer)


def deletar(
    tabela: str,
    *,
    filtros: Mapping[str, str],
    retornar: bool = False,
) -> SupabaseResultado:
    parametros = urlencode(dict(filtros))
    prefer = "return=representation" if retornar else "return=minimal"
    return requisitar("DELETE", tabela, query=f"?{parametros}", prefer=prefer)


def requisitar(
    metodo: str,
    tabela: str,
    *,
    payload: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    query: str = "",
    prefer: str | None = None,
) -> SupabaseResultado:
    config = _carregar_config()
    if config is None:
        return {
            "ok": False,
            "tabela": tabela,
            "erro": "Supabase nao configurado",
            "detalhe": "Preencha SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY ou SUPABASE_ANON_KEY no .env.",
        }

    url = f"{config['url']}/rest/v1/{quote(tabela)}{query}"
    dados = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "apikey": config["chave"],
        "Authorization": f"Bearer {config['chave']}",
        "Accept": "application/json",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if prefer:
        headers["Prefer"] = prefer

    request = Request(url, data=dados, method=metodo, headers=headers)
    try:
        with urlopen(request, timeout=TIMEOUT_SEGUNDOS) as response:
            corpo = response.read().decode("utf-8")
            resultado: SupabaseResultado = {
                "ok": True,
                "status": getattr(response, "status", 200),
                "tabela": tabela,
                "data": _parse_json(corpo),
            }
            content_range = response.headers.get("Content-Range", "")
            if content_range:
                resultado["content_range"] = content_range
                total = _parse_total_content_range(content_range)
                if total is not None:
                    resultado["total"] = total
            return resultado
    except HTTPError as erro:
        detalhe = _ler_erro_http(erro)
        logger.warning("Supabase HTTP %s em %s: %s", erro.code, tabela, detalhe)
        return {
            "ok": False,
            "status": erro.code,
            "tabela": tabela,
            "erro": f"Supabase retornou HTTP {erro.code}",
            "detalhe": detalhe,
        }
    except URLError as erro:
        logger.warning("Falha de conexao com Supabase em %s: %s", tabela, erro.reason)
        return {
            "ok": False,
            "tabela": tabela,
            "erro": "Falha de conexao com Supabase",
            "detalhe": str(erro.reason),
        }
    except OSError as erro:
        logger.warning("Falha ao chamar Supabase em %s: %s", tabela, erro)
        return {
            "ok": False,
            "tabela": tabela,
            "erro": "Falha ao chamar Supabase",
            "detalhe": str(erro),
        }


def tabela_env(nome_env: str, padrao: str) -> str:
    env_local = _ler_env_local()
    return _env(nome_env, env_local) or padrao


def _carregar_config() -> SupabaseConfig | None:
    env_local = _ler_env_local()
    url = _env("SUPABASE_URL", env_local).rstrip("/")
    chave = _env("SUPABASE_SERVICE_ROLE_KEY", env_local) or _env("SUPABASE_ANON_KEY", env_local)
    if not url or not chave:
        return None
    return {"url": url, "chave": chave}


def _env(nome: str, env_local: Mapping[str, str]) -> str:
    return os.getenv(nome, "").strip() or env_local.get(nome, "").strip()


def _ler_env_local(path: Path = ENV_PATH) -> dict[str, str]:
    if not path.exists():
        return {}

    try:
        linhas = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    valores: dict[str, str] = {}
    for linha in linhas:
        if not linha or linha.lstrip().startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        valores[chave.strip()] = valor.strip()
    return valores


def _parse_json(corpo: str) -> Any:
    if not corpo:
        return None
    try:
        return json.loads(corpo)
    except json.JSONDecodeError:
        return corpo


def _parse_total_content_range(valor: str) -> int | None:
    if "/" not in valor:
        return None
    total = valor.rsplit("/", 1)[-1].strip()
    if not total or total == "*":
        return None
    try:
        return int(total)
    except ValueError:
        return None


def _ler_erro_http(erro: HTTPError) -> str:
    try:
        corpo = erro.read().decode("utf-8")
    except Exception:
        return str(erro)
    return corpo[:1200] if corpo else str(erro)
