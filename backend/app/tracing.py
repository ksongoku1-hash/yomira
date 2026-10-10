from contextvars import ContextVar, Token
from uuid import UUID, uuid4

from starlette.requests import Request

TRACE_ID_HEADER = "X-Trace-ID"
_trace_id_context: ContextVar[str | None] = ContextVar("yomira_trace_id", default=None)


def create_trace_id() -> str:
    return str(uuid4())


def resolve_request_trace_id(request: Request) -> str:
    existing = getattr(request.state, "trace_id", None)
    if isinstance(existing, str) and existing:
        return existing

    contextual = _trace_id_context.get()
    if contextual is not None:
        request.state.trace_id = contextual
        return contextual

    supplied = request.headers.get(TRACE_ID_HEADER)
    if supplied:
        try:
            parsed = UUID(supplied)
        except ValueError:
            parsed = None
        if parsed is not None and str(parsed) == supplied.lower():
            trace_id = str(parsed)
            request.state.trace_id = trace_id
            return trace_id

    trace_id = create_trace_id()
    request.state.trace_id = trace_id
    return trace_id


def activate_trace_id(trace_id: str) -> Token[str | None]:
    return _trace_id_context.set(trace_id)


def reset_trace_id(token: Token[str | None]) -> None:
    _trace_id_context.reset(token)


def current_trace_id() -> str | None:
    return _trace_id_context.get()
