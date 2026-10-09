"""HTML-only Companion routes, sharing the dashboard security boundary."""

import asyncio
from uuid import UUID

import anyio
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from agentforge.application.companion import progress_label
from agentforge.web.app import pagination, query


class WatchedResponse(StreamingResponse):
    """Release watch_task even if HTTP disconnects before body iteration."""

    def __init__(self, content, subscription):
        super().__init__(
            content, media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )
        self.subscription = subscription

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.subscription.__aexit__(None, None, None)


def add_companion_routes(web, core, render, check_cancellation, event_response):
    @web.get("/companion", response_class=HTMLResponse)
    @web.get("/companion/fragment", response_class=HTMLResponse)
    async def home(request: Request):
        values = query(request)
        app = core()
        active = app.companion.active(**values.model_dump())
        status = app.status()
        return render(
            request,
            "companion_home_fragment.html"
            if request.url.path.endswith("fragment")
            else "companion_home.html",
            active=active,
            status=status,
            workers=app.list_workers().workers,
            recent=app.tasks.history(limit=5),
            councils=app.councils.list(limit=5).councils,
            **pagination(
                request,
                values,
                max(status.running_tasks, status.queued_tasks)
                > values.offset + values.limit,
            ),
            fragment_url="/companion/fragment?" + str(request.query_params),
        )

    def task_response(request, task_id, fragment=False):
        return render(
            request,
            "companion_task_fragment.html" if fragment else "companion_task.html",
            item=core().companion.detail(task_id),
            progress_label=progress_label,
        )

    @web.get("/companion/tasks/{task_id}", response_class=HTMLResponse)
    async def task(request: Request, task_id: UUID):
        return task_response(request, task_id)

    @web.get("/companion/tasks/{task_id}/fragment", response_class=HTMLResponse)
    async def task_fragment(request: Request, task_id: UUID):
        return task_response(request, task_id, True)

    @web.post("/companion/tasks/{task_id}/cancel")
    async def cancel(request: Request, task_id: UUID):
        await check_cancellation(request)
        core().cancel_task(task_id=task_id)
        if request.headers.get("HX-Request") == "true":
            return HTMLResponse(headers={"HX-Redirect": f"/companion/tasks/{task_id}"})
        return RedirectResponse(f"/companion/tasks/{task_id}", status_code=303)

    @web.get("/companion/tasks/{task_id}/diff", response_class=HTMLResponse)
    async def diff(request: Request, task_id: UUID):
        return render(
            request, "companion_diff.html", diff=core().get_coding_diff(task_id=task_id)
        )

    @web.get("/companion/tasks/{task_id}/events")
    async def events(request: Request, task_id: UUID):
        # Reserve before response iteration. WatchedResponse releases even when a
        # disconnect happens before iteration; watch_task owns subscriber cleanup.
        subscription = core().watch_task(task_id=task_id)
        updates = await subscription.__aenter__()

        async def stream():
            pending = None
            try:
                while True:
                    if pending is None:
                        pending = asyncio.create_task(anext(updates))
                    done, _ = await asyncio.wait({pending}, timeout=15)
                    if not done:
                        yield ": keepalive\n\n"
                        continue
                    try:
                        progress = pending.result()
                    except StopAsyncIteration:
                        return
                    pending = None
                    yield f"event: {progress.observation}\ndata: {{}}\n\n"
                    if progress.terminal or progress.observation == "shutdown":
                        return
            finally:
                if pending is not None:
                    pending.cancel()
                    with anyio.CancelScope(shield=True):
                        await asyncio.gather(pending, return_exceptions=True)

        return WatchedResponse(stream(), subscription)

    @web.get("/companion/councils/{council_id}", response_class=HTMLResponse)
    @web.get("/companion/councils/{council_id}/fragment", response_class=HTMLResponse)
    async def council(request: Request, council_id: UUID):
        app = core()
        council = app.get_council(council_id=council_id)
        return render(
            request,
            "companion_council_fragment.html"
            if request.url.path.endswith("fragment")
            else "companion_council.html",
            council=council,
            project_name=app.companion.project_name(council.project_id),
        )

    @web.get("/companion/councils/{council_id}/events")
    async def council_events(request: Request, council_id: UUID):
        app = core()
        council = app.get_council(council_id=council_id)
        return event_response(
            app.tasks.observer.subscribe_many(
                tuple(p.task_id for p in council.participants)
            ),
            lambda: app.get_council(council_id=council_id).terminal,
        )
