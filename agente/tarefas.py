"""Tasks A2A em memoria: identidade, estado, historico e artifacts.

A `Pendencia` guarda o que a ponte precisa para retomar uma Task pausada,
incluindo o `requestState` opaco. Ela fica fora de `para_wire()` e por isso
nunca sai em resposta A2A.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Any

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
FAILED = "TASK_STATE_FAILED"
CANCELED = "TASK_STATE_CANCELED"
TERMINAIS = {COMPLETED, FAILED, CANCELED}


def _id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


@dataclass
class Pendencia:
    """Pausa de uma Task: a pergunta traduzida e o estado MCP para o retry."""

    tool: str
    argumentos: dict[str, Any]
    chave: str
    opcoes: list[str]
    request_state: str


@dataclass
class Task:
    trace_id: str
    id: str = field(default_factory=lambda: _id("task"))
    context_id: str = field(default_factory=lambda: _id("ctx"))
    estado: str = SUBMITTED
    mensagem: dict | None = None
    historico: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    pendencia: Pendencia | None = None
    trava: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def registrar_usuario(self, mensagem: dict) -> None:
        self.historico.append(mensagem)

    def transitar(self, estado: str, texto: str | None = None) -> None:
        if self.terminal:
            raise RuntimeError(f"Task {self.id} ja terminou em {self.estado}")
        self.estado = estado
        if texto is not None:
            self.mensagem = {
                "messageId": _id("msg"),
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": self.id,
                "contextId": self.context_id,
            }
            self.historico.append(self.mensagem)
        if self.terminal:
            self.pendencia = None

    def anexar_artifact(self, nome: str, texto: str) -> None:
        self.artifacts.append({"artifactId": _id("art"), "name": nome, "parts": [{"text": texto}]})

    def para_wire(self) -> dict:
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem is not None:
            status["message"] = self.mensagem
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.historico,
            "artifacts": self.artifacts,
        }


class RepositorioDeTasks:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def criar(self, trace_id: str) -> Task:
        task = Task(trace_id=trace_id)
        self._tasks[task.id] = task
        return task

    def obter(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)
