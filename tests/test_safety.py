from __future__ import annotations

import pytest

from nexis.safety import (
    ActionBlocked,
    ConfirmationRequest,
    Risk,
    SafetyPolicy,
)


def request(risk):
    return ConfirmationRequest(tool="t", summary="s", risk=risk)


def test_safe_actions_do_not_prompt():
    policy = SafetyPolicy(require_confirmation=True, confirmer=lambda r: False)
    assert policy.check(request(Risk.SAFE)) is True


def test_confirm_actions_prompt_and_respect_denial():
    policy = SafetyPolicy(require_confirmation=True, confirmer=lambda r: False)
    assert policy.check(request(Risk.CONFIRM)) is False


def test_confirm_actions_prompt_and_respect_approval():
    policy = SafetyPolicy(require_confirmation=True, confirmer=lambda r: True)
    assert policy.check(request(Risk.CONFIRM)) is True


def test_confirm_actions_auto_approve_when_disabled():
    policy = SafetyPolicy(require_confirmation=False)
    assert policy.check(request(Risk.CONFIRM)) is True


def test_blocked_is_always_blocked():
    policy = SafetyPolicy(require_confirmation=False)
    with pytest.raises(ActionBlocked):
        policy.check(request(Risk.BLOCKED))


def test_default_confirmer_denies():
    policy = SafetyPolicy(require_confirmation=True)
    assert policy.check(request(Risk.CONFIRM)) is False
