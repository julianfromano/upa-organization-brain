"""Structured errors designed to be actionable by an AI agent."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class UpaError(Exception):
    code: str
    message: str
    pos: tuple | None = None
    phase: str = "compile"
    extra: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"error": self.code, "phase": self.phase, "message": self.message}
        if self.pos:
            d["line"], d["column"] = self.pos
        d.update(self.extra)
        return d


class ErrorBag:
    def __init__(self) -> None:
        self.items: list[UpaError] = []

    def add(self, code: str, message: str, pos=None, phase="semantic", **extra) -> None:
        self.items.append(UpaError(code, message, pos, phase, extra))

    def __bool__(self) -> bool:
        return bool(self.items)

    def to_json(self) -> list[dict]:
        return [e.to_json() for e in self.items]
