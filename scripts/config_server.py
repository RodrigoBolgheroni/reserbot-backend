from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

ROOT_DIR: Final[Path] = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services import clientes_supabase, disparador, fluxo_reservas, pdf_clientes, perfis, whatsapp_cloud

logger = logging.getLogger(__name__)

ENV_PATH: Final[Path] = ROOT_DIR / ".env"
CONFIG_KEYS: Final[set[str]] = {
    "NOME_RESTAURANTE",
    "AGENTE_PERSONALIDADE",
    "MENSAGEM_ANIVERSARIO",
    "HORARIO_DISPARO",
}
MAX_UPLOAD_MB_PADRAO: Final[int] = 15
CLIENTES_PAGE_SIZE_PADRAO: Final[int] = 100
CLIENTES_PAGE_SIZE_MAX: Final[int] = 500
_IMPORTACOES_PENDENTES: dict[str, pdf_clientes.ResultadoExtracaoPDF] = {}


class ConfigHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        rota = urlparse(self.path).path
        if rota == "/":
            self._responder_json({"ok": True, "servico": "ReservaBot API", "health": "/api/health"})
            return
        if rota == "/api/health":
            self._responder_json({"ok": True, "servico": "ReservaBot API"})
            return
        if rota == "/api/config":
            self._responder_json(_ler_env())
            return
        if rota == "/api/clientes/aniversarios-proximos":
            self._listar_aniversarios_proximos()
            return
        if rota == "/api/clientes":
            self._listar_clientes()
            return
        if rota == "/api/reservas":
            self._listar_reservas()
            return
        if rota == "/api/perfis":
            self._listar_perfis()
            return
        if rota == "/api/whatsapp/webhook":
            self._validar_webhook_whatsapp()
            return

        self._responder_erro(HTTPStatus.NOT_FOUND, "rota nao encontrada")

    def do_POST(self) -> None:
        rota = urlparse(self.path).path
        if rota == "/api/config":
            self._salvar_config()
            return
        if rota == "/api/clientes/pdf/preview":
            self._preview_pdf_clientes()
            return
        if rota == "/api/clientes/pdf/confirmar":
            self._confirmar_importacao_clientes()
            return
        if rota == "/api/perfis":
            self._salvar_perfil()
            return
        if rota == "/api/perfis/excluir":
            self._excluir_perfil()
            return
        if rota == "/api/perfis/ativar":
            self._ativar_perfil()
            return
        if rota == "/api/disparos/aniversarios":
            self._disparar_aniversarios()
            return
        if rota == "/api/whatsapp/webhook":
            self._receber_webhook_whatsapp()
            return

        self._responder_erro(HTTPStatus.NOT_FOUND, "rota nao encontrada")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", _cors_origin(self.headers.get("Origin", "")))
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")
        super().end_headers()

    def log_message(self, formato: str, *args: Any) -> None:
        logger.info("config-server: " + formato, *args)

    def _salvar_config(self) -> None:
        try:
            payload = self._ler_json_body()
        except ValueError as erro:
            self._responder_erro(HTTPStatus.BAD_REQUEST, str(erro))
            return

        if not isinstance(payload, dict):
            self._responder_erro(HTTPStatus.BAD_REQUEST, "JSON precisa ser um objeto")
            return

        valores = {
            chave: _valor_env(valor)
            for chave, valor in payload.items()
            if chave in CONFIG_KEYS and isinstance(valor, str)
        }
        _atualizar_env(valores)
        self._responder_json({"ok": True})

    def _preview_pdf_clientes(self) -> None:
        try:
            nome_arquivo, conteudo = self._ler_pdf_upload()
            resultado = pdf_clientes.extrair_clientes_pdf(conteudo, nome_arquivo=nome_arquivo)
        except ValueError as erro:
            self._responder_erro(HTTPStatus.BAD_REQUEST, str(erro))
            return
        except RuntimeError as erro:
            self._responder_erro(HTTPStatus.INTERNAL_SERVER_ERROR, str(erro))
            return
        except Exception:
            logger.exception("Falha ao processar PDF de clientes.")
            self._responder_erro(HTTPStatus.INTERNAL_SERVER_ERROR, "Falha ao processar PDF")
            return

        import_id = uuid.uuid4().hex
        _IMPORTACOES_PENDENTES[import_id] = resultado
        self._responder_json(
            {
                "ok": True,
                "import_id": import_id,
                **pdf_clientes.resumir_extracao(resultado),
            }
        )

    def _confirmar_importacao_clientes(self) -> None:
        try:
            payload = self._ler_json_body()
        except ValueError as erro:
            self._responder_erro(HTTPStatus.BAD_REQUEST, str(erro))
            return

        if not isinstance(payload, dict) or not isinstance(payload.get("import_id"), str):
            self._responder_erro(HTTPStatus.BAD_REQUEST, "import_id obrigatorio")
            return

        import_id = payload["import_id"]
        resultado = _IMPORTACOES_PENDENTES.get(import_id)
        if resultado is None:
            self._responder_erro(HTTPStatus.NOT_FOUND, "importacao nao encontrada ou expirada")
            return

        registros = pdf_clientes.registros_importaveis(resultado)
        logger.info(
            "Confirmando importacao PDF import_id=%s com %s cliente(s) importavel(is).",
            import_id,
            len(registros),
        )
        resultado_supabase = clientes_supabase.salvar_clientes(registros)
        if not resultado_supabase.get("ok"):
            logger.warning(
                "Falha na confirmacao da importacao PDF import_id=%s: %s %s",
                import_id,
                resultado_supabase.get("erro", ""),
                resultado_supabase.get("detalhe", ""),
            )
            self._responder_json(
                {
                    "ok": False,
                    "erro": "Nao foi possivel salvar os clientes importados.",
                    "detalhes": resultado_supabase.get("detalhe") or resultado_supabase.get("erro", ""),
                    "supabase": resultado_supabase,
                    "resumo": pdf_clientes.resumir_extracao(resultado),
                },
                status=HTTPStatus.BAD_GATEWAY,
            )
            return

        _IMPORTACOES_PENDENTES.pop(import_id, None)
        self._responder_json(
            {
                "ok": True,
                "supabase": resultado_supabase,
                "resumo": pdf_clientes.resumir_extracao(resultado),
            }
        )

    def _listar_perfis(self) -> None:
        self._responder_json({"ok": True, "perfis": perfis.listar_perfis()})

    def _listar_clientes(self) -> None:
        page, page_size, offset = _paginacao_clientes(self.path)
        resultado = clientes_supabase.listar_clientes(limite=page_size, offset=offset, contar=True)
        clientes = resultado["clientes"]
        total = resultado.get("total")
        total_pages = _total_pages(total, page_size)
        has_next = page < total_pages if total_pages is not None else len(clientes) == page_size
        self._responder_json(
            {
                "ok": True,
                "clientes": clientes,
                "page": page,
                "page_size": page_size,
                "total": total if total is not None else len(clientes),
                "total_pages": total_pages if total_pages is not None else None,
                "has_next": has_next,
                "has_prev": page > 1,
            }
        )

    def _listar_aniversarios_proximos(self) -> None:
        dias = max(1, min(_query_int(self.path, "dias", 15), 60))
        limite = max(1, min(_query_int(self.path, "limit", 50), 100))
        resultado = clientes_supabase.listar_aniversarios_proximos(dias=dias, limite_clientes=limite)
        self._responder_json(
            {
                "ok": True,
                "dias": resultado["dias"],
                "total": resultado["total"],
                "clientes": resultado["clientes"],
            }
        )

    def _listar_reservas(self) -> None:
        limite = _query_int(self.path, "limit", 500)
        reservas = fluxo_reservas.listar_reservas(limite=limite)
        self._responder_json({"ok": True, "reservas": reservas, "total": len(reservas)})

    def _salvar_perfil(self) -> None:
        try:
            payload = self._ler_json_body()
        except ValueError as erro:
            self._responder_erro(HTTPStatus.BAD_REQUEST, str(erro))
            return

        if not isinstance(payload, dict):
            self._responder_erro(HTTPStatus.BAD_REQUEST, "JSON precisa ser um objeto")
            return

        resultado = perfis.salvar_perfil(payload)
        if not resultado.get("ok"):
            self._responder_json(resultado, status=HTTPStatus.BAD_REQUEST)
            return
        self._responder_json(resultado)

    def _excluir_perfil(self) -> None:
        try:
            payload = self._ler_json_body()
        except ValueError as erro:
            self._responder_erro(HTTPStatus.BAD_REQUEST, str(erro))
            return

        perfil_id = str((payload or {}).get("id") or "") if isinstance(payload, dict) else ""
        resultado = perfis.excluir_perfil(perfil_id)
        if not resultado.get("ok"):
            self._responder_json(resultado, status=HTTPStatus.BAD_REQUEST)
            return
        self._responder_json(resultado)

    def _ativar_perfil(self) -> None:
        try:
            payload = self._ler_json_body()
        except ValueError as erro:
            self._responder_erro(HTTPStatus.BAD_REQUEST, str(erro))
            return

        if not isinstance(payload, dict):
            self._responder_erro(HTTPStatus.BAD_REQUEST, "JSON precisa ser um objeto")
            return

        perfil_id = str(payload.get("id") or "")
        resultado = perfis.definir_ativo(perfil_id, bool(payload.get("ativo")))
        if not resultado.get("ok"):
            self._responder_json(resultado, status=HTTPStatus.BAD_REQUEST)
            return
        self._responder_json(resultado)

    def _disparar_aniversarios(self) -> None:
        try:
            payload = self._ler_json_body()
            if not isinstance(payload, dict):
                payload = {}
            resultados = disparador.executar_disparo_diario(
                dias=_int_payload(payload, "dias", 15),
                telefone=str(payload.get("telefone") or ""),
                somente_teste=bool(payload.get("somente_teste")),
            )
        except Exception:
            logger.exception("Falha ao executar disparo de aniversariantes pela API.")
            self._responder_erro(HTTPStatus.INTERNAL_SERVER_ERROR, "Falha ao executar disparo")
            return

        enviados = sum(1 for item in resultados if item.get("status") == "enviado")
        erros = sum(1 for item in resultados if item.get("status") == "erro")
        pulados = sum(1 for item in resultados if item.get("status") == "pulado")
        mensagem = "" if resultados else "Nenhum aniversariante encontrado para envio."
        self._responder_json(
            {
                "ok": True,
                "total_encontrados": len(resultados),
                "total_enviados": enviados,
                "falhas": erros,
                "enviados": enviados,
                "erros": erros,
                "pulados": pulados,
                "mensagem": mensagem,
                "resultados": resultados,
            }
        )

    def _validar_webhook_whatsapp(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        token = query.get("hub.verify_token", [""])[0]
        challenge = query.get("hub.challenge", [""])[0]
        modo = query.get("hub.mode", [""])[0]
        if modo == "subscribe" and whatsapp_cloud.verificar_token_webhook(token):
            corpo = challenge.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
            return
        self._responder_erro(HTTPStatus.FORBIDDEN, "token de webhook invalido")

    def _receber_webhook_whatsapp(self) -> None:
        try:
            payload = self._ler_json_body()
        except ValueError as erro:
            self._responder_erro(HTTPStatus.BAD_REQUEST, str(erro))
            return
        if not isinstance(payload, dict):
            self._responder_erro(HTTPStatus.BAD_REQUEST, "JSON precisa ser um objeto")
            return

        recebidas = whatsapp_cloud.extrair_mensagens_webhook(payload)
        resultados: list[dict[str, Any]] = []
        for mensagem in recebidas:
            try:
                resultado = fluxo_reservas.processar_mensagem_webhook(mensagem)
                resultados.append(dict(resultado))
            except Exception:
                logger.exception("Falha ao processar mensagem recebida da Cloud API.")
                resultados.append(
                    {
                        "ok": False,
                        "telefone": mensagem.get("telefone", ""),
                        "status": "erro",
                        "erro": "falha ao processar mensagem",
                    }
                )

        processadas = sum(1 for resultado in resultados if resultado.get("ok"))
        reservas = sum(1 for resultado in resultados if resultado.get("reserva_confirmada"))
        self._responder_json(
            {
                "ok": True,
                "recebidas": len(recebidas),
                "processadas": processadas,
                "reservas_confirmadas": reservas,
                "resultados": resultados,
            }
        )

    def _ler_json_body(self) -> Any:
        tamanho = self._content_length()
        try:
            return json.loads(self.rfile.read(tamanho).decode("utf-8") or "{}")
        except json.JSONDecodeError as erro:
            raise ValueError("JSON invalido") from erro

    def _ler_pdf_upload(self) -> tuple[str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("envie o PDF usando multipart/form-data")

        boundary_match = re.search(r"boundary=(?P<boundary>[^;]+)", content_type)
        if not boundary_match:
            raise ValueError("boundary ausente no upload")

        tamanho = self._content_length()
        if tamanho <= 0:
            raise ValueError("arquivo ausente")
        max_upload_bytes = _int_env("PDF_UPLOAD_MAX_MB", MAX_UPLOAD_MB_PADRAO) * 1024 * 1024
        if tamanho > max_upload_bytes:
            limite = max_upload_bytes // (1024 * 1024)
            raise ValueError(f"PDF maior que o limite de {limite} MB")

        corpo = self.rfile.read(tamanho)
        boundary = boundary_match.group("boundary").strip('"').encode("utf-8")
        for parte in corpo.split(b"--" + boundary):
            if b'name="pdf"' not in parte or b"\r\n\r\n" not in parte:
                continue

            cabecalho, _, conteudo = parte.partition(b"\r\n\r\n")
            conteudo = conteudo.removesuffix(b"\r\n")
            conteudo = conteudo.removesuffix(b"--")
            nome = _extrair_nome_arquivo(cabecalho.decode("utf-8", errors="ignore"))
            if not nome.lower().endswith(".pdf"):
                raise ValueError("arquivo precisa ter extensao .pdf")
            if not conteudo.startswith(b"%PDF"):
                raise ValueError("arquivo enviado nao parece ser um PDF")
            return nome, conteudo

        raise ValueError("campo pdf nao encontrado no upload")

    def _content_length(self) -> int:
        try:
            return int(self.headers.get("Content-Length", "0"))
        except ValueError as erro:
            raise ValueError("Content-Length invalido") from erro

    def _responder_erro(self, status: HTTPStatus, mensagem: str) -> None:
        self._responder_json({"ok": False, "erro": mensagem}, status=status)

    def _responder_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        corpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)


def _extrair_nome_arquivo(cabecalho: str) -> str:
    match = re.search(r'filename="(?P<filename>[^"]+)"', cabecalho)
    if not match:
        return "clientes.pdf"
    nome = Path(match.group("filename")).name.strip()
    return nome or "clientes.pdf"

def main() -> int:
    _carregar_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    porta = _porta_servidor()
    host = os.getenv("CONFIG_SERVER_HOST", "0.0.0.0").strip() or "0.0.0.0"
    endereco = (host, porta)

    servidor = ThreadingHTTPServer(endereco, ConfigHandler)
    logger.info("ReservaBot API escutando em %s:%s", host, porta)
    logger.info("Preferencias serao salvas em %s", ENV_PATH)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        logger.info("Encerrando servidor de configuracao.")
    finally:
        servidor.server_close()
    return 0


def _ler_env() -> dict[str, str]:
    config: dict[str, str] = {}
    if not ENV_PATH.exists():
        return config

    for linha in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if not linha or linha.lstrip().startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave = chave.strip()
        if chave in CONFIG_KEYS:
            config[chave] = valor.strip()
    return config


def _atualizar_env(valores: dict[str, str]) -> None:
    if not valores:
        return

    linhas = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    atualizadas: list[str] = []
    gravadas: set[str] = set()

    for linha in linhas:
        chave = linha.partition("=")[0].strip() if "=" in linha else ""
        if chave in valores:
            atualizadas.append(f"{chave}={valores[chave]}")
            gravadas.add(chave)
        else:
            atualizadas.append(linha)

    for chave in CONFIG_KEYS:
        if chave in valores and chave not in gravadas:
            atualizadas.append(f"{chave}={valores[chave]}")

    ENV_PATH.write_text("\n".join(atualizadas).rstrip() + "\n", encoding="utf-8")


def _valor_env(valor: str) -> str:
    return valor.replace("\r", " ").replace("\n", " ").strip()


def _int_env(nome: str, padrao: int) -> int:
    try:
        return int(os.getenv(nome, str(padrao)))
    except ValueError:
        return padrao


def _int_payload(payload: dict[str, Any], nome: str, padrao: int) -> int:
    try:
        return int(payload.get(nome, padrao))
    except (TypeError, ValueError):
        return padrao


def _porta_servidor() -> int:
    for nome in ("PORT", "CONFIG_SERVER_PORT"):
        valor = os.getenv(nome, "").strip()
        if valor:
            try:
                return int(valor)
            except ValueError:
                logger.warning("Porta invalida em %s: %s", nome, valor)
    return 8080


def _query_int(path: str, nome: str, padrao: int) -> int:
    query = parse_qs(urlparse(path).query)
    try:
        return int(query.get(nome, [str(padrao)])[0])
    except (TypeError, ValueError):
        return padrao


def _paginacao_clientes(path: str) -> tuple[int, int, int]:
    query = parse_qs(urlparse(path).query)
    page_size = _query_param_int(query, "page_size", _query_param_int(query, "limit", CLIENTES_PAGE_SIZE_PADRAO))
    page_size = max(1, min(page_size, CLIENTES_PAGE_SIZE_MAX))
    offset_recebido = _query_param_int(query, "offset", -1)
    if offset_recebido >= 0:
        offset = max(0, offset_recebido)
        page = (offset // page_size) + 1
        return page, page_size, offset

    page = max(1, _query_param_int(query, "page", 1))
    offset = (page - 1) * page_size
    return page, page_size, offset


def _query_param_int(query: dict[str, list[str]], nome: str, padrao: int) -> int:
    try:
        return int(query.get(nome, [str(padrao)])[0])
    except (TypeError, ValueError):
        return padrao


def _total_pages(total: int | None, page_size: int) -> int | None:
    if total is None:
        return None
    if total <= 0:
        return 0
    return (total + page_size - 1) // page_size


def _cors_origin(origin: str) -> str:
    configurado = os.getenv("CORS_ALLOW_ORIGIN", "*").strip() or "*"
    if configurado == "*":
        return "*"

    permitidos = {item.strip().rstrip("/") for item in configurado.split(",") if item.strip()}
    origin_limpo = origin.strip().rstrip("/")
    if origin_limpo in permitidos:
        return origin_limpo
    return next(iter(permitidos), "")


def _carregar_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return

    load_dotenv(ENV_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
