"""Dominio das salas: dados do starter, regras da politica e reservas em memoria.

Funcoes puras sobre a lista de reservas carregada na subida. Nada aqui conhece
MCP: o servidor traduz excecoes `RegraVioladaError` em erro de execucao da tool.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(__file__).resolve().parent.parent / "dados"
FUSO = timezone(timedelta(hours=-3))
ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class RegraVioladaError(Exception):
    """Pedido que viola uma regra de sala ou da politica; a mensagem e a do enunciado."""


SALAS: list[dict] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
RESERVAS: list[dict] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
POLITICA_TEXTO: str = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO: str = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()

_proximo_id = len(RESERVAS) + 1


def sala_por_id(sala_id: str) -> dict | None:
    return next((s for s in SALAS if s["id"] == sala_id), None)


def _instante(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except ValueError as exc:
        raise RegraVioladaError(f"Data invalida: {valor}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO)
    return dt


def validar(sala_id: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica as regras na ordem: sala, intervalo, janela, duracao."""
    if sala_por_id(sala_id) is None:
        raise RegraVioladaError(f"Sala inexistente: {sala_id}")
    ini, fi = _instante(inicio), _instante(fim)
    if fi <= ini:
        raise RegraVioladaError(ERRO_INTERVALO)
    ini_local, fim_local = ini.astimezone(FUSO), fi.astimezone(FUSO)
    abre = datetime.combine(ini_local.date(), ABERTURA, FUSO)
    fecha = datetime.combine(ini_local.date(), FECHAMENTO, FUSO)
    if ini_local < abre or fim_local > fecha:
        raise RegraVioladaError(ERRO_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise RegraVioladaError(ERRO_DURACAO)
    return ini, fi


def conflitos(sala_id: str, ini: datetime, fi: datetime) -> list[dict]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala_id and ini < _instante(r["fim"]) and _instante(r["inicio"]) < fi
    ]


def alternativas(sala_id: str, ini: datetime, fi: datetime) -> list[str]:
    """Salas livres com capacidade >= a pedida, por (capacidade, id), no maximo tres."""
    capacidade = sala_por_id(sala_id)["capacidade"]
    candidatas = [
        s
        for s in SALAS
        if s["id"] != sala_id and s["capacidade"] >= capacidade and not conflitos(s["id"], ini, fi)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:MAX_ALTERNATIVAS]]


def criar_reserva(sala_id: str, inicio: str, fim: str, responsavel: str) -> dict:
    global _proximo_id
    reserva = {
        "id": f"res-{_proximo_id:04d}",
        "sala": sala_id,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
    }
    _proximo_id += 1
    RESERVAS.append(reserva)
    return reserva
