"""Servidor MCP da Central de Salas (Streamable HTTP, revisao 2026-07-28).

    REQUEST_STATE_SECRET=... python servidor-mcp/servidor.py

A reserva usa MRTR pelo caminho de primeira classe do SDK: o parametro `escolha`
de `reservar_sala` e preenchido pelo resolver `escolha_de_sala`. Quando o
intervalo esta ocupado, o resolver devolve `Elicit(...)` e o SDK encerra a
resposta com `resultType: input_required`, sem nunca chamar o cliente de volta.
O `requestState` e selado pelo `RequestStateBoundary` do SDK (AES-256-GCM, chave
derivada de REQUEST_STATE_SECRET) e amarrado a tool e ao digest dos argumentos,
entao nada fica guardado aqui entre o `input_required` e o retry.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Annotated, Any, Literal

import uvicorn
from pydantic import BaseModel, Field, create_model

import dominio
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError

NOME = "central-de-salas"
VERSAO = "1.0.0"
TTL_REQUEST_STATE = 600.0  # 10 minutos, dentro da faixa de 5 a 30 exigida
MENSAGEM_ESCOLHA = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."

log = logging.getLogger("central-de-salas")


def carregar_segredo() -> bytes:
    """Le REQUEST_STATE_SECRET (hex) e recusa subir com menos de 32 bytes."""
    bruto = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not bruto:
        sys.exit(
            "REQUEST_STATE_SECRET nao definido. Gere com:\n"
            '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
        )
    try:
        segredo = bytes.fromhex(bruto)
    except ValueError:
        segredo = bruto.encode()
    if len(segredo) < 32:
        sys.exit(f"REQUEST_STATE_SECRET precisa de ao menos 32 bytes; veio com {len(segredo)}.")
    return segredo


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ResultadoReserva(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


mcp = MCPServer(
    NOME,
    version=VERSAO,
    request_state_security=RequestStateSecurity(keys=[carregar_segredo()], ttl=TTL_REQUEST_STATE),
)


def _validar(sala: str, inicio: str, fim: str):
    try:
        return dominio.validar(sala, inicio, fim)
    except dominio.RegraVioladaError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in dominio.SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, fi = _validar(sala, inicio, fim)
    em_conflito = dominio.conflitos(sala, ini, fi)
    return Disponibilidade(
        sala=sala,
        livre=not em_conflito,
        conflitos=[ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in em_conflito],
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[Any] | None:
    """Resolver do MRTR: pergunta a alternativa so quando o intervalo esta ocupado.

    Roda de novo em cada rodada, entao as alternativas sao sempre recalculadas
    contra as reservas atuais. O SDK so aceita a resposta se a pergunta renderizada
    for identica a que foi feita, o que cobre uma alternativa tomada no meio tempo.
    """
    ini, fi = _validar(sala, inicio, fim)
    if not dominio.conflitos(sala, ini, fi):
        return None
    opcoes = dominio.alternativas(sala, ini, fi)
    if not opcoes:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    esquema = create_model(
        "EscolhaDeSala",
        sala=(Literal[tuple(opcoes)], Field(title="Sala", description="Sala alternativa escolhida")),
    )
    return Elicit(MENSAGEM_ESCOLHA, esquema)


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[BaseModel], Resolve(escolha_de_sala)],
) -> ResultadoReserva:
    if not isinstance(escolha, AcceptedElicitation):
        return ResultadoReserva(reservado=False, motivo="recusado")
    destino = sala if escolha.data is None else escolha.data.sala
    reserva = dominio.criar_reserva(destino, inicio, fim, responsavel)
    return ResultadoReserva(
        reserva=reserva["id"],
        sala=destino,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=dominio.POLITICA_VERSAO,
    )


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas; a primeira linha declara a versao.")
def politica_de_uso() -> str:
    return dominio.POLITICA_TEXTO


class LogDeRequests:
    """Middleware ASGI: registra metodo, id e traceparent de cada request no stderr."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        partes = []
        while True:
            mensagem = await receive()
            partes.append(mensagem.get("body", b""))
            if not mensagem.get("more_body"):
                break
        corpo = b"".join(partes)
        self._registrar(corpo)
        entregue = False

        async def reenviar():
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        await self.app(scope, reenviar, send)

    @staticmethod
    def _registrar(corpo: bytes) -> None:
        try:
            msg = json.loads(corpo)
        except ValueError:
            log.info("request invalido (corpo nao e JSON)")
            return
        if not isinstance(msg, dict):
            return
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        alvo = params.get("name") or params.get("uri")
        log.info(
            "method=%s id=%s%s traceparent=%s%s",
            msg.get("method"),
            json.dumps(msg.get("id")),
            f" name={alvo}" if alvo else "",
            meta.get("traceparent", "-"),
            " retry=sim" if "requestState" in params else "",
        )


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    caminho = os.environ.get("MCP_PATH", "/mcp")
    app = mcp.streamable_http_app(streamable_http_path=caminho, json_response=True, stateless_http=True, host=host)
    log.info("servidor MCP em http://%s:%d%s (politica %s)", host, porta, caminho, dominio.POLITICA_VERSAO)
    uvicorn.run(LogDeRequests(app), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
