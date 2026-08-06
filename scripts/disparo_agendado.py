from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, time
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services import clientes_supabase, disparador, supabase  # noqa: E402
from services.comunicacao import normalizar_telefone  # noqa: E402


LOGGER = logging.getLogger("reservabot.disparo_agendado")
DATA_ESPERADA = date(2026, 8, 6)
HORARIO_ESPERADO = time(19, 0)
TIMEZONE_ESPERADO = "America/Sao_Paulo"
CHAVE_ESPERADA = "disparo-2026-08-06-19h-3-clientes"


def main() -> int:
    _configurar_logs()
    try:
        dry_run = _ler_bool_obrigatorio("DRY_RUN", default=True)
        data_referencia = _ler_data("DISPARO_DATA_REFERENCIA", DATA_ESPERADA)
        horario_local = _ler_horario("DISPARO_HORARIO_LOCAL", HORARIO_ESPERADO)
        timezone_nome = os.getenv("DISPARO_TIMEZONE", TIMEZONE_ESPERADO).strip() or TIMEZONE_ESPERADO
        chave = os.getenv("DISPARO_CHAVE_IDEMPOTENCIA", CHAVE_ESPERADA).strip() or CHAVE_ESPERADA
        zona = ZoneInfo(timezone_nome)
        horario_programado = datetime.combine(data_referencia, horario_local, tzinfo=zona)
        _validar_agendamento_fixo(
            data_referencia=data_referencia,
            horario_local=horario_local,
            timezone_nome=timezone_nome,
            chave=chave,
        )

        if not dry_run:
            agora = datetime.now(zona)
            if agora.date() != data_referencia or agora < horario_programado:
                raise RuntimeError(
                    "Envio real bloqueado: o job so pode executar em 06/08/2026 a partir de 19:00 America/Sao_Paulo."
                )

        if not supabase.configurado():
            raise RuntimeError("Supabase nao configurado; informe SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY.")

        clientes = _resolver_destinatarios()
        resultado = disparador.executar_disparo_agendado(
            clientes,
            data_referencia=data_referencia,
            chave_idempotencia=chave,
            horario_programado=horario_programado.isoformat(),
            dry_run=dry_run,
            bloco_campanha=os.getenv("DISPARO_BLOCO_CAMPANHA", "").strip() or None,
        )
        print(json.dumps(resultado, ensure_ascii=False, indent=2, sort_keys=True))
        if resultado.get("ok"):
            return 0
        return 1 if not dry_run else 2
    except (ValueError, RuntimeError, ZoneInfoNotFoundError) as erro:
        LOGGER.error("Disparo agendado abortado antes do envio: %s", erro)
        print(json.dumps({"ok": False, "aborted": True, "erro": str(erro)}, ensure_ascii=False, indent=2))
        return 2


def _resolver_destinatarios() -> list[dict[str, object]]:
    ids = _lista_env("DISPARO_CLIENTE_IDS")
    telefones = _lista_env("DISPARO_TELEFONES")
    if ids and telefones:
        raise ValueError("Informe apenas DISPARO_CLIENTE_IDS ou DISPARO_TELEFONES, nunca os dois.")
    if not ids and not telefones:
        raise ValueError("Informe exatamente 3 IDs em DISPARO_CLIENTE_IDS ou 3 telefones em DISPARO_TELEFONES.")

    if ids:
        _validar_quantidade_exata(ids, "DISPARO_CLIENTE_IDS")
        ids_normalizados = []
        for valor in ids:
            try:
                ids_normalizados.append(str(UUID(valor)))
            except ValueError as erro:
                raise ValueError("DISPARO_CLIENTE_IDS contem um UUID invalido.") from erro
        if len(set(ids_normalizados)) != 3:
            raise ValueError("DISPARO_CLIENTE_IDS precisa conter 3 IDs distintos.")

        clientes: list[dict[str, object]] = []
        for cliente_id in ids_normalizados:
            cliente = clientes_supabase.buscar_cliente_por_id(cliente_id)
            if not cliente:
                raise ValueError("Um dos IDs configurados nao foi encontrado em clientes.")
            clientes.append(cliente)
        _validar_clientes_resolvidos(clientes)
        return clientes

    _validar_quantidade_exata(telefones, "DISPARO_TELEFONES")
    telefones_normalizados = [normalizar_telefone(valor) for valor in telefones]
    if any(not valor for valor in telefones_normalizados):
        raise ValueError("DISPARO_TELEFONES contem telefone invalido.")
    if len(set(telefones_normalizados)) != 3:
        raise ValueError("DISPARO_TELEFONES precisa conter 3 telefones distintos.")

    clientes = []
    for telefone in telefones_normalizados:
        cliente = clientes_supabase.buscar_cliente_por_telefone(telefone)
        if not cliente:
            raise ValueError("Um dos telefones configurados nao foi encontrado em clientes.")
        clientes.append(cliente)
    _validar_clientes_resolvidos(clientes)
    return clientes


def _validar_clientes_resolvidos(clientes: list[dict[str, object]]) -> None:
    if len(clientes) != 3:
        raise ValueError(f"A resolucao retornou {len(clientes)} destinatarios; esperado exatamente 3.")
    ids = [str(cliente.get("id") or "") for cliente in clientes]
    telefones = [normalizar_telefone(str(cliente.get("telefone") or "")) for cliente in clientes]
    if any(not valor for valor in telefones):
        raise ValueError("A lista resolvida contem telefone invalido.")
    if len(set(ids)) != 3 or len(set(telefones)) != 3:
        raise ValueError("Os tres destinatarios resolvidos precisam ter IDs e telefones distintos.")


def _validar_agendamento_fixo(
    *,
    data_referencia: date,
    horario_local: time,
    timezone_nome: str,
    chave: str,
) -> None:
    if data_referencia != DATA_ESPERADA:
        raise ValueError("Este job e de uso unico para 06/08/2026.")
    if horario_local != HORARIO_ESPERADO:
        raise ValueError("Este job e de uso unico para 19:00.")
    if timezone_nome != TIMEZONE_ESPERADO:
        raise ValueError("Este job e de uso unico para America/Sao_Paulo.")
    if chave != CHAVE_ESPERADA:
        raise ValueError("A chave idempotente nao corresponde ao agendamento aprovado.")


def _validar_quantidade_exata(valores: list[str], nome: str) -> None:
    if len(valores) != 3:
        raise ValueError(f"{nome} precisa conter exatamente 3 itens; recebido={len(valores)}.")


def _lista_env(nome: str) -> list[str]:
    bruto = os.getenv(nome, "")
    return [item.strip() for item in bruto.replace("\n", ",").split(",") if item.strip()]


def _ler_bool_obrigatorio(nome: str, *, default: bool) -> bool:
    bruto = os.getenv(nome)
    if bruto is None or not bruto.strip():
        return default
    valor = bruto.strip().lower()
    if valor in {"true", "1", "yes", "sim"}:
        return True
    if valor in {"false", "0", "no", "nao", "não"}:
        return False
    raise ValueError(f"{nome} deve ser true ou false.")


def _ler_data(nome: str, padrao: date) -> date:
    bruto = os.getenv(nome, "").strip()
    if not bruto:
        return padrao
    return date.fromisoformat(bruto[:10])


def _ler_horario(nome: str, padrao: time) -> time:
    bruto = os.getenv(nome, "").strip()
    if not bruto:
        return padrao
    return time.fromisoformat(bruto[:5])


def _configurar_logs() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")


if __name__ == "__main__":
    raise SystemExit(main())
