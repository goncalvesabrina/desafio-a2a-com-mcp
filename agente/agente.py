"""Agente Central de Salas: servidor A2A v1.0 (JSON-RPC) por fora, host MCP por dentro.

    python agente/agente.py

Publica o Agent Card em /.well-known/agent-card.json e atende SendMessage e
GetTask em /a2a. Nao ha LLM: o pedido chega em formato fixo e cada decisao de
sala e do servidor MCP.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import ponte
from cliente_mcp import HostMCP, url_padrao
from tarefas import FAILED, INPUT_REQUIRED, RepositorioDeTasks

HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://{HOST}:{PORTA}")

# Codigos de erro do binding JSON-RPC do A2A v1.0
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004

TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")

log = logging.getLogger("agente")

AGENT_CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [
        {"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}

host_mcp = HostMCP(url_padrao())
tasks = RepositorioDeTasks()


class ErroA2A(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


def trace_id_de(request: Request) -> str:
    """Trace-id do header traceparent do cliente A2A, ou um novo se nao vier."""
    casamento = TRACEPARENT.match(request.headers.get("traceparent", "").strip().lower())
    return casamento.group(1) if casamento else secrets.token_hex(16)


def texto_da_mensagem(mensagem: dict) -> str:
    partes = mensagem.get("parts") or []
    return " ".join(p["text"] for p in partes if isinstance(p, dict) and isinstance(p.get("text"), str))


async def send_message(params: dict, request: Request) -> dict:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not texto_da_mensagem(mensagem):
        raise ErroA2A(INVALID_PARAMS, "message com ao menos uma part de texto e obrigatoria")
    texto = texto_da_mensagem(mensagem)
    task_id = mensagem.get("taskId")

    if not task_id:
        task = tasks.criar(trace_id_de(request))
        task.registrar_usuario(mensagem)
        async with task.trava:
            await _executar(task, ponte.iniciar(task, texto, host_mcp))
        return {"task": task.para_wire()}

    task = tasks.obter(task_id)
    if task is None:
        raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
    async with task.trava:
        if task.terminal:
            raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task_id} ja terminou em {task.estado}")
        if task.estado != INPUT_REQUIRED:
            raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task_id} nao esta aguardando input")
        task.registrar_usuario(mensagem)
        await _executar(task, ponte.continuar(task, texto, host_mcp))
    return {"task": task.para_wire()}


async def _executar(task, passo) -> None:
    """Roda um passo da ponte; uma falha inesperada termina a Task em FAILED."""
    try:
        await passo
    except Exception:
        log.exception("falha ao processar a Task %s", task.id)
        if not task.terminal:
            task.transitar(FAILED, "Falha interna ao falar com o servidor MCP")


async def get_task(params: dict, _request: Request) -> dict:
    task = tasks.obter(str(params.get("id", "")))
    if task is None:
        raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {params.get('id')}")
    return {"task": task.para_wire()}


METODOS = {"SendMessage": send_message, "GetTask": get_task}


def resposta_erro(id_rpc, codigo: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_rpc, "error": {"code": codigo, "message": mensagem}})


async def a2a(request: Request) -> JSONResponse:
    try:
        corpo = json.loads(await request.body())
    except ValueError:
        return resposta_erro(None, PARSE_ERROR, "JSON invalido")
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
        return resposta_erro(corpo.get("id") if isinstance(corpo, dict) else None, INVALID_REQUEST, "Request invalido")
    id_rpc = corpo.get("id")
    metodo = METODOS.get(corpo["method"])
    if metodo is None:
        return resposta_erro(id_rpc, METHOD_NOT_FOUND, f"Metodo nao suportado: {corpo['method']}")
    params = corpo.get("params") if isinstance(corpo.get("params"), dict) else {}
    try:
        resultado = await metodo(params, request)
    except ErroA2A as exc:
        log.info("%s recusado: %s", corpo["method"], exc.mensagem)
        return resposta_erro(id_rpc, exc.codigo, exc.mensagem)
    task = resultado["task"]
    log.info("%s task=%s estado=%s", corpo["method"], task["id"], task["status"]["state"])
    return JSONResponse({"jsonrpc": "2.0", "id": id_rpc, "result": resultado})


async def agent_card(_request: Request) -> JSONResponse:
    return JSONResponse(AGENT_CARD)


@asynccontextmanager
async def ciclo_de_vida(_app: Starlette):
    await host_mcp.abrir()
    try:
        yield
    finally:
        await host_mcp.fechar()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    log.info("agente A2A em %s (card em /.well-known/agent-card.json), MCP em %s", URL_PUBLICA, url_padrao())
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
