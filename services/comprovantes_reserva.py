from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Final
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

from services import supabase, whatsapp_cloud


logger = logging.getLogger(__name__)

TABELA_COMPROVANTES: Final[str] = "comprovantes_reserva"
BUCKET_PADRAO: Final[str] = "reserva-comprovantes"
MIME_TYPES_ACEITOS: Final[set[str]] = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
}
MAX_ARQUIVO_BYTES: Final[int] = 15 * 1024 * 1024
TIMEOUT_SEGUNDOS: Final[int] = 30


def receber_comprovante(
    *,
    media: Mapping[str, Any],
    provider_message_id: str,
    conversa_id: str,
    reserva_id: str,
) -> dict[str, Any]:
    provider_id = str(provider_message_id or "").strip()
    media_id = str(media.get("media_id") or "").strip()
    tipo = str(media.get("tipo") or "").strip().lower()
    mime_webhook = _normalizar_mime(media.get("mime_type"))
    if not provider_id:
        return {"ok": False, "erro": "provider_message_id ausente"}
    if not str(conversa_id or "").strip():
        return {"ok": False, "erro": "conversa_id ausente"}
    if not str(reserva_id or "").strip():
        return {"ok": False, "erro": "reserva_id ausente"}
    if not media_id:
        return {"ok": False, "erro": "media_id ausente"}
    if tipo not in {"image", "document"}:
        return {"ok": False, "erro": "tipo de midia nao aceito", "tipo": tipo}
    if mime_webhook not in MIME_TYPES_ACEITOS:
        return {"ok": False, "erro": "tipo de arquivo nao aceito", "mime_type": mime_webhook}

    existente = _buscar_por_provider_message_id(provider_id)
    if existente:
        return {"ok": True, "comprovante": existente, "duplicado": True}

    download = whatsapp_cloud.baixar_midia(media_id, limite_bytes=MAX_ARQUIVO_BYTES)
    if not download.get("ok"):
        return download
    conteudo = download.get("conteudo")
    if not isinstance(conteudo, bytes):
        return {"ok": False, "erro": "conteudo da midia ausente"}

    mime_type = _normalizar_mime(download.get("mime_type") or mime_webhook)
    if mime_type not in MIME_TYPES_ACEITOS:
        return {"ok": False, "erro": "tipo de arquivo nao aceito", "mime_type": mime_type}

    bucket = os.getenv("SUPABASE_COMPROVANTES_BUCKET", BUCKET_PADRAO).strip() or BUCKET_PADRAO
    nome_original = _nome_arquivo_seguro(str(media.get("nome_arquivo") or ""), mime_type)
    caminho = _caminho_storage(conversa_id=conversa_id, reserva_id=reserva_id, nome_original=nome_original)
    upload = _upload_privado(bucket=bucket, caminho=caminho, conteudo=conteudo, mime_type=mime_type)
    if not upload.get("ok"):
        return upload

    payload = {
        "reserva_id": reserva_id or None,
        "conversa_id": conversa_id,
        "provider_message_id": provider_id,
        "media_id": media_id,
        "tipo_midia": "pdf" if mime_type == "application/pdf" else "imagem",
        "mime_type": mime_type,
        "nome_original": nome_original,
        "tamanho_bytes": len(conteudo),
        "sha256": str(download.get("sha256") or media.get("sha256") or "").strip() or None,
        "bucket": bucket,
        "storage_path": caminho,
        "recebido_em": _agora(),
        "status_analise": "aguardando_analise",
        "metadata": {"provider": "whatsapp_cloud"},
    }
    resultado = supabase.inserir(TABELA_COMPROVANTES, payload)
    if not resultado.get("ok"):
        _remover_objeto_privado(bucket=bucket, caminho=caminho)
        return {
            "ok": False,
            "erro": "nao foi possivel registrar o comprovante",
            "detalhe": resultado.get("detalhe") or resultado.get("erro"),
        }
    comprovante = _primeiro(resultado.get("data")) or payload
    logger.info(
        "Comprovante privado registrado: comprovante_id=%s conversa_id=%s reserva_id=%s mime_type=%s tamanho=%s status=aguardando_analise.",
        comprovante.get("id", ""),
        conversa_id,
        reserva_id,
        mime_type,
        len(conteudo),
    )
    return {"ok": True, "comprovante": comprovante, "duplicado": False}


def listar_por_reserva(reserva_id: str) -> list[dict[str, Any]]:
    resultado = supabase.selecionar(
        TABELA_COMPROVANTES,
        filtros={"reserva_id": f"eq.{reserva_id}"},
        colunas=(
            "id,reserva_id,conversa_id,provider_message_id,tipo_midia,mime_type,nome_original,"
            "tamanho_bytes,recebido_em,status_analise,analisado_em,analisado_por,created_at"
        ),
        order="recebido_em.desc",
    )
    if not resultado.get("ok"):
        logger.warning("Comprovantes nao listados para reserva=%s: %s", reserva_id, resultado.get("erro"))
        return []
    return [dict(item) for item in (resultado.get("data") or []) if isinstance(item, Mapping)]


def listar_por_conversa(conversa_id: str) -> list[dict[str, Any]]:
    conversa_id_limpo = str(conversa_id or "").strip()
    if not conversa_id_limpo:
        return []
    resultado = supabase.selecionar(
        TABELA_COMPROVANTES,
        filtros={"conversa_id": f"eq.{conversa_id_limpo}"},
        colunas=(
            "id,reserva_id,conversa_id,provider_message_id,tipo_midia,mime_type,nome_original,"
            "tamanho_bytes,recebido_em,status_analise,bucket,storage_path"
        ),
        order="recebido_em.asc",
    )
    if not resultado.get("ok"):
        logger.warning("Comprovantes nao listados para conversa=%s: %s", conversa_id_limpo, resultado.get("erro"))
        return []
    comprovantes: list[dict[str, Any]] = []
    for item in resultado.get("data") or []:
        if not isinstance(item, Mapping):
            continue
        comprovantes.append(
            {
                "id": str(item.get("id") or ""),
                "reserva_id": str(item.get("reserva_id") or ""),
                "conversa_id": str(item.get("conversa_id") or ""),
                "provider_message_id": str(item.get("provider_message_id") or ""),
                "tipo_midia": str(item.get("tipo_midia") or ""),
                "mime_type": str(item.get("mime_type") or ""),
                "nome_original": str(item.get("nome_original") or ""),
                "tamanho_bytes": _inteiro_seguro(item.get("tamanho_bytes")),
                "recebido_em": str(item.get("recebido_em") or ""),
                "status_analise": str(item.get("status_analise") or ""),
                "disponivel": bool(item.get("bucket") and item.get("storage_path")),
            }
        )
    return comprovantes


def obter_por_provider_message_id(provider_message_id: str) -> dict[str, Any] | None:
    return _buscar_por_provider_message_id(provider_message_id)


def baixar_arquivo(comprovante_id: str) -> dict[str, Any]:
    resultado = supabase.selecionar(
        TABELA_COMPROVANTES,
        filtros={"id": f"eq.{comprovante_id}"},
        colunas="id,mime_type,nome_original,tamanho_bytes,bucket,storage_path",
        limite=1,
    )
    comprovante = _primeiro(resultado.get("data")) if resultado.get("ok") else None
    if not comprovante:
        return {"ok": False, "status": 404, "erro": "comprovante nao encontrado"}
    download = _download_privado(
        bucket=str(comprovante.get("bucket") or ""),
        caminho=str(comprovante.get("storage_path") or ""),
    )
    if not download.get("ok"):
        return download
    return {
        "ok": True,
        "conteudo": download["conteudo"],
        "mime_type": str(comprovante.get("mime_type") or "application/octet-stream"),
        "nome_arquivo": str(comprovante.get("nome_original") or "comprovante"),
        "tamanho": len(download["conteudo"]),
    }


def baixar_arquivo_mensagem(*, mensagem_id: str, conversa_id: str) -> dict[str, Any]:
    mensagem_id_limpo = str(mensagem_id or "").strip()
    conversa_id_limpo = str(conversa_id or "").strip()
    if not mensagem_id_limpo or not conversa_id_limpo:
        return {"ok": False, "status": 400, "erro": "mensagem_id e conversa_id sao obrigatorios"}

    resultado_mensagem = supabase.selecionar(
        os.getenv("SUPABASE_MENSAGENS_TABLE", "mensagens").strip() or "mensagens",
        filtros={"id": f"eq.{mensagem_id_limpo}", "conversa_id": f"eq.{conversa_id_limpo}"},
        colunas="id,conversa_id,provider_message_id,metadata",
        limite=1,
    )
    mensagem = _primeiro(resultado_mensagem.get("data")) if resultado_mensagem.get("ok") else None
    if not mensagem:
        return {"ok": False, "status": 404, "erro": "midia da mensagem nao encontrada"}

    metadata = mensagem.get("metadata") if isinstance(mensagem.get("metadata"), Mapping) else {}
    comprovante_id = str(metadata.get("comprovante_id") or "").strip()
    filtros = {"conversa_id": f"eq.{conversa_id_limpo}"}
    if comprovante_id:
        filtros["id"] = f"eq.{comprovante_id}"
    else:
        provider_message_id = str(mensagem.get("provider_message_id") or "").strip()
        if not provider_message_id:
            return {"ok": False, "status": 404, "erro": "comprovante da mensagem nao encontrado"}
        filtros["provider_message_id"] = f"eq.{provider_message_id}"

    resultado_comprovante = supabase.selecionar(
        TABELA_COMPROVANTES,
        filtros=filtros,
        colunas="id,conversa_id,mime_type,nome_original,tamanho_bytes,bucket,storage_path",
        limite=1,
    )
    comprovante = _primeiro(resultado_comprovante.get("data")) if resultado_comprovante.get("ok") else None
    if not comprovante:
        return {"ok": False, "status": 404, "erro": "comprovante da mensagem nao encontrado"}

    bucket = str(comprovante.get("bucket") or "").strip()
    storage_path = str(comprovante.get("storage_path") or "").strip()
    if not bucket or not storage_path:
        return {"ok": False, "status": 409, "erro": "arquivo do comprovante ainda nao esta disponivel"}
    download = _download_privado(bucket=bucket, caminho=storage_path)
    if not download.get("ok"):
        return download
    return {
        "ok": True,
        "conteudo": download["conteudo"],
        "mime_type": str(comprovante.get("mime_type") or "application/octet-stream"),
        "nome_arquivo": str(comprovante.get("nome_original") or "comprovante"),
        "tamanho": len(download["conteudo"]),
    }


def marcar_analisado(*, reserva_id: str, aprovado: bool, analisado_por: str = "painel") -> bool:
    status = "aprovado" if aprovado else "rejeitado"
    resultado = supabase.atualizar(
        TABELA_COMPROVANTES,
        {"status_analise": status, "analisado_em": _agora(), "analisado_por": analisado_por},
        filtros={"reserva_id": f"eq.{reserva_id}", "status_analise": "eq.aguardando_analise"},
        retornar=False,
    )
    return bool(resultado.get("ok"))


def _buscar_por_provider_message_id(provider_message_id: str) -> dict[str, Any] | None:
    provider_id = str(provider_message_id or "").strip()
    if not provider_id:
        return None
    resultado = supabase.selecionar(
        TABELA_COMPROVANTES,
        filtros={"provider_message_id": f"eq.{provider_id}"},
        limite=1,
    )
    if not resultado.get("ok"):
        return None
    return _primeiro(resultado.get("data"))


def _inteiro_seguro(valor: Any) -> int:
    try:
        return int(valor or 0)
    except (TypeError, ValueError):
        return 0


def _upload_privado(*, bucket: str, caminho: str, conteudo: bytes, mime_type: str) -> dict[str, Any]:
    config = supabase.config_service_role()
    if config is None:
        return {"ok": False, "erro": "Supabase service role nao configurado"}
    url = f"{config['url']}/storage/v1/object/{quote(bucket, safe='')}/{quote(caminho, safe='/')}"
    request = Request(
        url,
        data=conteudo,
        method="POST",
        headers={
            "apikey": config["chave"],
            "Authorization": f"Bearer {config['chave']}",
            "Content-Type": mime_type,
            "x-upsert": "false",
        },
    )
    return _executar_storage(request, operacao="upload")


def _download_privado(*, bucket: str, caminho: str) -> dict[str, Any]:
    config = supabase.config_service_role()
    if config is None:
        return {"ok": False, "status": 503, "erro": "Supabase service role nao configurado"}
    url = f"{config['url']}/storage/v1/object/authenticated/{quote(bucket, safe='')}/{quote(caminho, safe='/')}"
    request = Request(
        url,
        method="GET",
        headers={"apikey": config["chave"], "Authorization": f"Bearer {config['chave']}"},
    )
    return _executar_storage(request, operacao="download", binario=True)


def _remover_objeto_privado(*, bucket: str, caminho: str) -> None:
    config = supabase.config_service_role()
    if config is None:
        return
    payload = json.dumps({"prefixes": [caminho]}).encode("utf-8")
    request = Request(
        f"{config['url']}/storage/v1/object/{quote(bucket, safe='')}",
        data=payload,
        method="DELETE",
        headers={
            "apikey": config["chave"],
            "Authorization": f"Bearer {config['chave']}",
            "Content-Type": "application/json",
        },
    )
    resultado = _executar_storage(request, operacao="rollback")
    if not resultado.get("ok"):
        logger.warning("Rollback de comprovante no Storage falhou para path=%s.", caminho)


def _executar_storage(request: Request, *, operacao: str, binario: bool = False) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=TIMEOUT_SEGUNDOS) as response:
            conteudo = response.read()
            return {"ok": True, "conteudo": conteudo} if binario else {"ok": True}
    except HTTPError as erro:
        try:
            detalhe = erro.read().decode("utf-8")[:800]
        except Exception:
            detalhe = str(erro)
        logger.warning("Supabase Storage %s retornou HTTP %s: %s", operacao, erro.code, detalhe)
        return {"ok": False, "status": erro.code, "erro": f"Storage retornou HTTP {erro.code}", "detalhe": detalhe}
    except (OSError, URLError) as erro:
        logger.warning("Falha de conexao no Supabase Storage durante %s: %s", operacao, erro)
        return {"ok": False, "status": 503, "erro": "falha de conexao com Storage", "detalhe": str(erro)}


def _caminho_storage(*, conversa_id: str, reserva_id: str, nome_original: str) -> str:
    conversa = _segmento_seguro(conversa_id) or "sem-conversa"
    reserva = _segmento_seguro(reserva_id) or "sem-reserva"
    return f"{reserva}/{conversa}/{uuid4().hex}-{nome_original}"


def _nome_arquivo_seguro(nome: str, mime_type: str) -> str:
    extensao = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}[mime_type]
    base = str(nome or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip(".-")
    if not base:
        return f"comprovante{extensao}"
    if not base.lower().endswith(extensao):
        base = f"{base}{extensao}"
    return base[:180]


def _segmento_seguro(valor: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(valor or "").strip()).strip("-")


def _normalizar_mime(valor: Any) -> str:
    return str(valor or "").split(";", 1)[0].strip().lower()


def _primeiro(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list) and data and isinstance(data[0], Mapping):
        return dict(data[0])
    if isinstance(data, Mapping):
        return dict(data)
    return None


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
