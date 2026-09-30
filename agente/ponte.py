"""A ponte: onde o `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED` e volta.

Aqui nao ha regra de sala. O agente so interpreta o texto em formato fixo,
traduz o resultado MCP em transicao de Task e, na continuacao, traduz
`escolha=<valor>` em `inputResponses`. O `requestState` e guardado e ecoado
sem nunca ser aberto.
"""

from __future__ import annotations

import json
import logging
import re

import mcp_types as t
from mcp.shared.exceptions import MCPError

from cliente_mcp import HostMCP
from tarefas import CANCELED, COMPLETED, FAILED, INPUT_REQUIRED, WORKING, Pendencia, Task

TOOL_RESERVA = "reservar_sala"
RECUSAR = "recusar"
PEDIDO = re.compile(r"^\s*reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(.+?)\s*$")
ESCOLHA = re.compile(r"^\s*escolha=(\S+)\s*$")
FORMATO = (
    "Pedido fora do formato: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
)

log = logging.getLogger("agente.ponte")


def linha_de_alternativas(opcoes: list[str]) -> str:
    return "alternativas: " + ", ".join(opcoes)


async def iniciar(task: Task, texto: str, host: HostMCP) -> None:
    """Primeira mensagem da Task: descobre, le a politica e faz o tools/call original."""
    task.transitar(WORKING)
    casamento = PEDIDO.match(texto)
    if casamento is None:
        task.transitar(FAILED, FORMATO)
        return
    sala, inicio, fim, responsavel = casamento.groups()
    argumentos = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    try:
        if await host.tool(TOOL_RESERVA, task.trace_id) is None:
            task.transitar(FAILED, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
            return
        await host.versao_politica(task.trace_id)
        resultado = await host.chamar(TOOL_RESERVA, argumentos, task.trace_id)
    except MCPError as exc:
        task.transitar(FAILED, exc.error.message)
        return
    await _aplicar(task, TOOL_RESERVA, argumentos, resultado, host)


async def continuar(task: Task, texto: str, host: HostMCP) -> None:
    """Continuacao de uma Task pausada: `escolha=<id>` ou `escolha=recusar`."""
    pendencia = task.pendencia
    casamento = ESCOLHA.match(texto)
    valor = casamento.group(1) if casamento else None
    if valor == RECUSAR:
        resposta = t.ElicitResult(action="decline")
    elif valor in pendencia.opcoes:
        resposta = t.ElicitResult(action="accept", content={"sala": valor})
    else:
        # Escolha fora do enum: a Task continua pausada e repete a pergunta.
        task.transitar(INPUT_REQUIRED, linha_de_alternativas(pendencia.opcoes))
        return

    task.transitar(WORKING)
    try:
        # O retry: mesmo tools/call, id novo, a mesma chave do inputRequests e o
        # requestState ecoado exatamente como o servidor o entregou.
        resultado = await host.chamar(
            pendencia.tool,
            pendencia.argumentos,
            task.trace_id,
            input_responses={pendencia.chave: resposta},
            request_state=pendencia.request_state,
        )
    except MCPError as exc:
        task.transitar(FAILED, exc.error.message)
        return
    await _aplicar(task, pendencia.tool, pendencia.argumentos, resultado, host)


async def _aplicar(
    task: Task,
    tool: str,
    argumentos: dict,
    resultado: t.CallToolResult | t.InputRequiredResult,
    host: HostMCP,
) -> None:
    if isinstance(resultado, t.InputRequiredResult):
        _pausar(task, tool, argumentos, resultado)
        return

    if resultado.is_error:
        texto = " ".join(p.text for p in resultado.content if isinstance(p, t.TextContent))
        task.transitar(FAILED, texto or "A tool devolveu erro sem mensagem")
        return

    dados = resultado.structured_content or {}
    if not dados.get("reservado"):
        task.transitar(CANCELED, f"Reserva nao realizada: {dados.get('motivo') or 'recusada'}.")
        return

    reserva = {campo: dados.get(campo) for campo in ("reserva", "sala", "inicio", "fim", "responsavel")}
    reserva["politica"] = await host.versao_politica(task.trace_id)
    task.anexar_artifact("reserva", json.dumps(reserva))
    task.transitar(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")


def _pausar(task: Task, tool: str, argumentos: dict, resultado: t.InputRequiredResult) -> None:
    """input_required -> TASK_STATE_INPUT_REQUIRED, guardando o requestState na Task."""
    pedidos = resultado.input_requests or {}
    opcoes: list[str] = []
    if len(pedidos) == 1 and resultado.request_state:
        chave, pedido = next(iter(pedidos.items()))
        if isinstance(pedido, t.ElicitRequest) and isinstance(pedido.params, t.ElicitRequestFormParams):
            campo = pedido.params.requested_schema.get("properties", {}).get("sala", {})
            opcoes = list(campo.get("enum") or ([campo["const"]] if "const" in campo else []))
    if not opcoes:
        task.transitar(FAILED, "O servidor MCP pediu uma informacao que este agente nao sabe perguntar")
        return
    task.pendencia = Pendencia(
        tool=tool,
        argumentos=argumentos,
        chave=chave,
        opcoes=opcoes,
        request_state=resultado.request_state,
    )
    task.transitar(INPUT_REQUIRED, linha_de_alternativas(opcoes))
