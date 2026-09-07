"""Session-domain models and service ports."""

from typing import TYPE_CHECKING, Any

from patchloop.session.models import Session, SessionCheckpoint, Turn, TurnRole

if TYPE_CHECKING:
    from patchloop.session.service import SessionRuntime, SessionService


def __getattr__(name: str) -> Any:
    if name in {"SessionRuntime", "SessionService"}:
        from patchloop.session.service import SessionRuntime, SessionService

        return {"SessionRuntime": SessionRuntime, "SessionService": SessionService}[name]
    raise AttributeError(name)


__all__ = [
    "Session",
    "SessionCheckpoint",
    "SessionRuntime",
    "SessionService",
    "Turn",
    "TurnRole",
]
