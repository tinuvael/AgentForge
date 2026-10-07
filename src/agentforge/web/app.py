"""Server-rendered HTTP adapter. All core calls stay on the owning event loop."""

import asyncio
import hashlib
import hmac
import logging
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from uuid import UUID

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agentforge.application.service import Application, ServiceError
from agentforge.projects.errors import ProjectStorageError
from agentforge.tasks.models import (
    TERMINAL_STATES,
    TaskNotFound,
    TaskState,
    TaskStorageError,
)
from agentforge.tasks.observation import ObservationUnavailable
from agentforge.telemetry.models import TelemetryUnavailable

_ASSETS = Path(__file__).parent
_COOKIE = "agentforge_csrf"
_LOG = logging.getLogger(__name__)


class EventResponse(StreamingResponse):
    """Release reservations even if a disconnect occurs before iteration starts."""

    def __init__(self, content, subscription, **kwargs):
        super().__init__(content, **kwargs)
        self.subscription = subscription

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.subscription.__exit__(None, None, None)


class PageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=1_000_000)


class TaskQuery(PageQuery):
    state: TaskState | None = None
    project_id: UUID | None = None
    agent_id: str | None = Field(default=None, max_length=100)
    worker_id: str | None = Field(default=None, max_length=100)

    @field_validator("state", "project_id", "agent_id", "worker_id", mode="before")
    @classmethod
    def empty_filter(cls, value):
        return None if value == "" else value


def query(request: Request, kind=PageQuery):
    if len(request.query_params) != len(request.query_params.multi_items()):
        raise HTTPException(422)
    try:
        return kind.model_validate(dict(request.query_params))
    except ValidationError:
        raise HTTPException(422) from None


def pagination(request: Request, values: PageQuery, has_next: bool):
    def link(offset):
        params = values.model_dump(exclude_none=True)
        params["offset"] = offset
        return request.url.path + "?" + urlencode(params)

    return {
        "previous": link(max(0, values.offset - values.limit))
        if values.offset
        else None,
        "next": link(values.offset + values.limit)
        if has_next and values.offset + values.limit <= 1_000_000
        else None,
        "offset": values.offset,
    }


def create_app(
    application_factory: Callable[[], Application],
    *,
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "[::1]"),
) -> FastAPI:
    templates = Jinja2Templates(directory=_ASSETS / "templates")
    templates.env.filters["known"] = lambda value: "—" if value is None else value
    templates.env.filters["seconds"] = lambda value: (
        "—" if value is None else f"{value:.3f} s"
    )
    templates.env.filters["capability"] = lambda value: (
        "unknown" if value is None else "yes" if value else "no"
    )
    csrf_key = secrets.token_bytes(32)
    application: Application | None = None

    @asynccontextmanager
    async def lifespan(_):
        nonlocal application
        if application is not None:
            raise ServiceError("service_unavailable")
        application = application_factory()  # Construct on executor's owning thread.
        try:
            await application.start()
            yield
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    await application.close()
            finally:
                application = None

    web = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    web.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    web.mount("/static", StaticFiles(directory=_ASSETS / "static"), name="static")

    def core() -> Application:
        if application is None:
            raise ServiceError("service_unavailable")
        return application

    def valid_token(token: str) -> bool:
        if len(token) != 129 or token[64] != ".":
            return False
        nonce, signature = token.split(".", 1)
        if any(char not in "0123456789abcdef" for char in nonce + signature):
            return False
        expected = hmac.new(csrf_key, nonce.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def render(request, name, **context):
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={"csrf": getattr(request.state, "csrf", ""), **context},
        )

    def failure(request, code, status):
        _LOG.warning("Dashboard operation failed: %s", code)
        response = render(
            request,
            "error.html",
            code=code,
            fragment=request.headers.get("HX-Request") == "true",
        )
        response.status_code = status
        if request.headers.get("HX-Request") == "true":
            response.headers["HX-Retarget"] = "#notice"
            response.headers["HX-Reswap"] = "innerHTML"
        return response

    @web.middleware("http")
    async def boundary(request: Request, call_next):
        token = request.cookies.get(_COOKIE, "")
        new_cookie = not valid_token(token)
        if new_cookie:
            nonce = secrets.token_hex(32)
            token = (
                nonce
                + "."
                + hmac.new(csrf_key, nonce.encode(), hashlib.sha256).hexdigest()
            )
        request.state.csrf = token
        try:
            response = await call_next(request)
        except TaskNotFound:
            response = failure(request, "task_not_found", 404)
        except (TaskStorageError, ProjectStorageError, TelemetryUnavailable):
            response = failure(request, "storage_unavailable", 503)
        except (ServiceError, ObservationUnavailable):
            response = failure(request, "service_unavailable", 503)
        except Exception:
            # Fixed diagnostics; no traceback, exception repr, SQL or user payload.
            response = failure(request, "internal_error", 500)
        if new_cookie:
            response.set_cookie(
                _COOKIE,
                token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                path="/",
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @web.exception_handler(HTTPException)
    async def http_error(request, error):
        codes = {
            403: "csrf_or_confirmation_required",
            404: "not_found",
            405: "method_not_allowed",
            422: "invalid_arguments",
        }
        return failure(
            request, codes.get(error.status_code, "request_rejected"), error.status_code
        )

    @web.exception_handler(RequestValidationError)
    async def invalid_request(request, _):
        return failure(request, "invalid_arguments", 422)

    @web.get("/", response_class=HTMLResponse)
    async def overview(request: Request):
        app = core()
        return render(
            request,
            "overview.html",
            status=app.status(),
            counts=app.tasks.state_counts(),
            tasks=app.tasks.history(limit=10),
            comparisons=app.telemetry.compare(group_by="worker_id", limit=20),
        )

    @web.get("/workers", response_class=HTMLResponse)
    async def workers(request: Request):
        values = query(request)
        page = core().list_workers(limit=values.limit, offset=values.offset)
        return render(
            request,
            "workers.html",
            workers=page.workers,
            **pagination(request, values, page.next_offset is not None),
        )

    @web.get("/projects", response_class=HTMLResponse)
    async def projects(request: Request):
        values = query(request)
        items = core().projects.list_summaries(
            limit=values.limit + 1, offset=values.offset
        )
        return render(
            request,
            "projects.html",
            projects=items[: values.limit],
            **pagination(request, values, len(items) > values.limit),
        )

    @web.get("/tasks", response_class=HTMLResponse)
    async def tasks(request: Request):
        values = query(request, TaskQuery)
        items = core().tasks.history(
            **values.model_dump() | {"limit": values.limit + 1}
        )
        return render(
            request,
            "task_table.html"
            if request.headers.get("HX-Request") == "true"
            and request.headers.get("HX-History-Restore-Request") != "true"
            else "tasks.html",
            tasks=items[: values.limit],
            filters=values,
            **pagination(request, values, len(items) > values.limit),
        )

    def detail_response(request, task_id, fragment=False):
        return render(
            request,
            "task_fragment.html" if fragment else "task.html",
            detail=core().dashboard.detail(task_id),
        )

    @web.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task(request: Request, task_id: UUID):
        return detail_response(request, task_id)

    @web.get("/tasks/{task_id}/fragment", response_class=HTMLResponse)
    async def task_fragment(request: Request, task_id: UUID):
        return detail_response(request, task_id, True)

    @web.post("/tasks/{task_id}/cancel", response_class=HTMLResponse)
    async def cancel(request: Request, task_id: UUID):
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403)
        if (
            request.headers.get("content-type", "").split(";", 1)[0]
            != "application/x-www-form-urlencoded"
        ):
            raise HTTPException(403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(403)
        try:
            fields = parse_qs(
                body.decode("ascii"), max_num_fields=4, strict_parsing=True
            )
        except (ValueError, UnicodeError):
            raise HTTPException(403) from None
        supplied = fields.get("csrf", [])
        cookie = request.cookies.get(_COOKIE, "")
        if (
            len(supplied) != 1
            or not valid_token(cookie)
            or not valid_token(supplied[0])
            or not hmac.compare_digest(supplied[0], cookie)
            or fields.get("confirm") != ["yes"]
        ):
            raise HTTPException(403)
        core().cancel_task(task_id=task_id)
        if request.headers.get("HX-Request") == "true":
            return detail_response(request, task_id, True)
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    @web.get("/tasks/{task_id}/events")
    async def events(request: Request, task_id: UUID):
        app = core()
        app.tasks.get_task(task_id)  # Safe 404 before opening a response.
        # Reserve before sending headers; context exits even on disconnect/shutdown.
        subscription = app.tasks.observer.subscribe(task_id)
        queue = subscription.__enter__()

        async def stream():
            while True:
                try:
                    notice = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if notice not in {"refresh", "resync", "terminal", "shutdown"}:
                    notice = "resync"
                if notice != "shutdown":
                    try:
                        if app.tasks.get_task(task_id).state in TERMINAL_STATES:
                            notice = "terminal"
                    except Exception:
                        notice = "shutdown"
                # Fixed safe contract: refresh/resync/terminal/shutdown, no payload.
                yield f"event: {notice}\ndata: {{}}\n\n"
                if notice in {"terminal", "shutdown"}:
                    break

        return EventResponse(
            stream(),
            subscription,
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )

    return web
