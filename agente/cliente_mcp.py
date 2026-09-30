"""O agente como host MCP: um cliente vivo, descoberta em runtime e chamadas cruas.

Toda chamada passa pela `ClientSession` do SDK com `allow_input_required=True`:
o `input_required` volta cru para a ponte, em vez de ser respondido aqui. O SDK
estampa em cada request o `_meta` obrigatorio (versao, clientInfo e
clientCapabilities) e os headers espelhados (MCP-Protocol-Version, Mcp-Method,
Mcp-Name), e numera cada request com um id JSON-RPC novo, retry incluido.
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import AsyncExitStack
from typing import Any

import mcp_types as t
from mcp import Client

PROTOCOLO = "2026-07-28"
URI_POLITICA = "politica://uso"

log = logging.getLogger("agente.mcp")


def novo_traceparent(trace_id: str) -> str:
    """Mesmo trace-id da Task, span-id novo a cada request MCP."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


async def _elicitation_nunca(_ctx: Any, _params: Any) -> t.ElicitResult:
    # Registrado so para o SDK declarar {"elicitation": {"form": {}}} nas
    # capabilities. Quem responde a elicitation e o cliente A2A, pela ponte.
    raise RuntimeError("a elicitation e respondida pelo cliente A2A, nunca pelo agente")


class HostMCP:
    def __init__(self, url: str) -> None:
        self._url = url
        self._pilha = AsyncExitStack()
        self._cliente: Client | None = None
        self._tools: dict[str, t.Tool] | None = None
        self._versao_politica: str | None = None

    async def abrir(self) -> None:
        self._cliente = await self._pilha.enter_async_context(
            Client(
                self._url,
                mode=PROTOCOLO,
                elicitation_callback=_elicitation_nunca,
                client_info=t.Implementation(name="agente-central-de-salas", version="1.0.0"),
                cache=None,
            )
        )

    async def fechar(self) -> None:
        await self._pilha.aclose()

    async def tool(self, nome: str, trace_id: str) -> t.Tool | None:
        """Descobre as tools por tools/list na primeira necessidade e guarda o resultado."""
        if self._tools is None:
            resultado = await self._cliente.list_tools(meta={"traceparent": novo_traceparent(trace_id)})
            self._tools = {tool.name: tool for tool in resultado.tools}
            log.info("tools descobertas: %s", ", ".join(self._tools))
        return self._tools.get(nome)

    async def versao_politica(self, trace_id: str) -> str:
        """Le o resource da politica e extrai a versao da primeira linha (`versao: X`)."""
        if self._versao_politica is None:
            resultado = await self._cliente.read_resource(
                URI_POLITICA, meta={"traceparent": novo_traceparent(trace_id)}
            )
            primeira = resultado.contents[0].text.splitlines()[0]
            self._versao_politica = primeira.split(":", 1)[1].strip()
        return self._versao_politica

    async def chamar(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace_id: str,
        input_responses: dict[str, t.ElicitResult] | None = None,
        request_state: str | None = None,
    ) -> t.CallToolResult | t.InputRequiredResult:
        """Um tools/call, com id novo. No retry, `request_state` segue exatamente como veio."""
        return await self._cliente.session.call_tool(
            nome,
            argumentos,
            input_responses=input_responses,
            request_state=request_state,
            meta={"traceparent": novo_traceparent(trace_id)},
            allow_input_required=True,
        )


def url_padrao() -> str:
    return os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
