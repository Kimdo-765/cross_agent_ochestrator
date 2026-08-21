"""Handshake protocol that gates every hand-off between orchestrator and agents.

Each hand-off goes through four phases, and every phase is recorded as a
:class:`HandshakeEvent` on the iteration so the run is auditable:

    OFFER   orchestrator presents the brief (or the diff) to an agent
    ACK     preconditions hold: agent CLI available, model differs from the
            other role (reviewer), workspace ready, payload non-empty
    NACK    a precondition or postcondition failed; the orchestrator may
            re-OFFER (bounded by ``handshake_retries``) or abort
    COMMIT  postconditions hold and the result is durably recorded (worker:
            diff committed to the task branch; reviewer: score recorded)

The agents themselves are plain CLIs, so the checks are performed by the
orchestrator on verifiable facts (exit codes, git state, parseable JSON) --
never on an agent's own claim of success.
"""

from __future__ import annotations

from typing import Callable, Optional

from .models import HandshakeEvent, IterationRecord

Check = Callable[[], Optional[str]]  # returns a failure reason, or None when the check passes


class HandshakeNack(Exception):
    def __init__(self, handoff: str, phase: str, reason: str):
        super().__init__(f"{handoff} {phase} NACK: {reason}")
        self.handoff = handoff
        self.phase = phase
        self.reason = reason


class Handshake:
    """One hand-off (``worker`` or ``reviewer``) within an iteration."""

    def __init__(self, iteration: IterationRecord, handoff: str, listener: Optional[Callable[[str], None]] = None):
        self.it = iteration
        self.handoff = handoff
        self.listener = listener or (lambda m: None)

    # -- phases ---------------------------------------------------------------

    def offer(self, detail: str, **data) -> None:
        self._event("OFFER", detail, **data)

    def ack(self, detail: str = "", **data) -> None:
        self._event("ACK", detail, **data)

    def nack(self, reason: str, **data) -> HandshakeNack:
        self._event("NACK", reason, **data)
        return HandshakeNack(self.handoff, "precondition/postcondition", reason)

    def commit(self, detail: str, **data) -> None:
        self._event("COMMIT", detail, **data)

    # -- helpers ----------------------------------------------------------------

    def require(self, *checks: Check, stage: str = "precondition") -> None:
        """Run checks in order; the first failure raises a NACK."""
        for check in checks:
            reason = check()
            if reason:
                raise self.nack(f"{stage}: {reason}")

    def _event(self, phase: str, detail: str, **data) -> None:
        ev = HandshakeEvent(iteration=self.it.number, handoff=self.handoff, phase=phase, detail=detail, data=data)
        self.it.events.append(ev)
        self.listener(f"[iter {self.it.number}] {self.handoff:<8} {phase:<6} {detail}")


# --------------------------------------------------------------------------- #
# Reusable checks
# --------------------------------------------------------------------------- #


def check_available(adapter) -> Check:
    def _check() -> Optional[str]:
        ok, detail = adapter.is_available()
        return None if ok else f"agent '{adapter.spec.name}' unavailable: {detail}"

    return _check


def check_distinct_models(worker_identity: str, reviewer_identity: str) -> Check:
    def _check() -> Optional[str]:
        if worker_identity == reviewer_identity:
            return f"reviewer model {reviewer_identity} is the same as the worker's; cross-model review required"
        return None

    return _check


def check_nonempty(label: str, value: str) -> Check:
    def _check() -> Optional[str]:
        return None if value and value.strip() else f"{label} is empty"

    return _check


def check_result_ok(result) -> Check:
    def _check() -> Optional[str]:
        return None if result.ok else f"agent failed: {result.error or 'unknown error'}"

    return _check
