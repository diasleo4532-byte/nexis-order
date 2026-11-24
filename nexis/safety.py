from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class Risk(enum.Enum):
    SAFE = "safe"
    CONFIRM = "confirm"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ConfirmationRequest:
    tool: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    risk: Risk = Risk.CONFIRM


class ActionBlocked(RuntimeError):
    """Raised when policy forbids an action outright."""


class ConfirmationDenied(RuntimeError):
    """Raised when the user declines a confirmation request."""


Confirmer = Callable[[ConfirmationRequest], bool]


def default_deny(_: ConfirmationRequest) -> bool:
    return False


class SafetyPolicy:
    def __init__(self, require_confirmation: bool = True, confirmer: Confirmer | None = None):
        self.require_confirmation = require_confirmation
        self.confirmer: Confirmer = confirmer or default_deny

    def effective_risk(self, risk: Risk) -> Risk:
        if risk is Risk.CONFIRM and not self.require_confirmation:
            return Risk.SAFE
        return risk

    def check(self, request: ConfirmationRequest) -> bool:
        risk = self.effective_risk(request.risk)
        if risk is Risk.BLOCKED:
            raise ActionBlocked(f"Blocked by policy: {request.summary}")
        if risk is Risk.CONFIRM:
            return bool(self.confirmer(request))
        return True
