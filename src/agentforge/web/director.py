"""Loopback operator chat adapter. All planning/approvals live in Director core."""

import asyncio
import hashlib
import hmac
import ipaddress
import re
import secrets
from urllib.parse import parse_qs
from uuid import UUID

import anyio
from fastapi import Request
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, RedirectResponse

from agentforge.director.client import DirectorError
from agentforge.director.core import TERMINAL

COOKIE = "agentforge_director"


class DirectorWeb:
    def __init__(self, director):
        self.director = director
        self.jobs = {}
        self.catalogs = {}

    async def close(self):
        with anyio.CancelScope(shield=True):
            for job in self.jobs.values():
                job.cancel()
            await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        self.jobs.clear()

    def start(self, item, owner, **kwargs):
        if item.id in self.jobs or len(self.jobs) >= self.director.config.limits.active:
            raise HTTPException(429)

        async def execute():
            try:
                await self.director.run(item.id, owner, **kwargs)
            except DirectorError as error:
                current = self.director.load(item.id, owner)
                current.error = error.code
                self.director.journal.save(current)
            except Exception:
                current = self.director.load(item.id, owner)
                current.error = "director_unavailable"
                self.director.journal.save(current)
            finally:
                self.jobs.pop(item.id, None)

        self.jobs[item.id] = asyncio.create_task(execute())


def add_director_routes(web, render, valid_csrf):
    @web.middleware("http")
    async def browser_identity(request, call_next):
        if not request.url.path.startswith("/director"):
            return await call_next(request)
        token = request.cookies.get(COOKIE, "")
        new = re.fullmatch(r"[a-f0-9]{64}", token) is None
        if new:
            token = secrets.token_hex(32)
        request.state.director_owner = hashlib.sha256(token.encode()).hexdigest()
        response = await call_next(request)
        if new:
            response.set_cookie(
                COOKIE,
                token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                max_age=30 * 24 * 3600,
                path="/director",
            )
        return response

    def service(request):
        # Chat is installed only in the borrowed loopback Companion. Check actual
        # peer/authority too; proxy headers are disabled by the listener.
        try:
            local = ipaddress.ip_address(request.client.host).is_loopback
        except (ValueError, AttributeError):
            local = False
        host = request.url.hostname
        try:
            local_host = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local_host = host == "localhost"
        if (
            not local
            or not local_host
            or any(len(request.headers.getlist(h)) > 1 for h in ("host", "origin"))
            or request.headers.get("sec-fetch-site") == "cross-site"
            or request.headers.get("origin") is not None
            and request.headers["origin"]
            != f"{request.url.scheme}://{request.url.netloc}"
        ):
            raise HTTPException(403)
        result = getattr(web.state, "director", None)
        if result is None:
            raise HTTPException(503)
        return result

    def owned(request, identity):
        svc = service(request)
        try:
            return svc, svc.director.load(identity, request.state.director_owner)
        except ValueError:
            raise HTTPException(404) from None

    async def fields(request):
        service(request)
        if (
            request.headers.get("content-type", "").split(";", 1)[0]
            != "application/x-www-form-urlencoded"
        ):
            raise HTTPException(403)
        body = bytearray()
        with anyio.fail_after(5):
            async for chunk in request.stream():
                if len(body) + len(chunk) > 65_536:
                    raise HTTPException(413)
                body.extend(chunk)
        try:
            data = parse_qs(body.decode("ascii"), strict_parsing=True, max_num_fields=8)
        except (ValueError, UnicodeError):
            raise HTTPException(422) from None
        if any(len(v) != 1 for v in data.values()):
            raise HTTPException(422)
        data = {k: v[0] for k, v in data.items()}
        csrf = data.pop("csrf", "")
        cookie = request.cookies.get("agentforge_csrf", "")
        if not valid_csrf(cookie) or not hmac.compare_digest(csrf, cookie):
            raise HTTPException(403)
        return data

    def redirect(request, item=None):
        target = "/director" + (f"/{item.id}" if item else "")
        if request.headers.get("HX-Request") == "true":
            # XHR keeps the real Origin under no-referrer. A full GET navigation
            # restores fresh revisions without caching or nesting the page in a form.
            return HTMLResponse(headers={"HX-Redirect": target})
        return RedirectResponse(target, 303)

    def page(request, svc, item=None, *, fragment=False):
        return render(
            request,
            "director_fragment.html" if fragment else "director.html",
            conversation=item,
            conversations=svc.director.journal.list(
                request.state.director_owner, svc.director.identity
            ),
            model=svc.director.config.model,
            catalog=svc.catalogs.get(item.id, {}) if item else {},
            working=item.id in svc.jobs if item else False,
            input_limit=svc.director.config.limits.input_chars,
        )

    @web.get("/director")
    async def home(request: Request):
        try:
            svc = service(request)
        except HTTPException as error:
            if error.status_code != 503:
                raise
            response = render(request, "director_disabled.html")
            response.status_code = 503
            return response
        return page(request, svc)

    @web.post("/director/new")
    async def new(request: Request):
        if await fields(request):
            raise HTTPException(422)
        svc = service(request)
        try:
            item = svc.director.create(request.state.director_owner)
        except ValueError:
            raise HTTPException(429) from None
        return redirect(request, item)

    @web.get("/director/{identity}")
    async def conversation(request: Request, identity: UUID):
        svc, item = owned(request, identity)
        if item.id not in svc.jobs:
            item.calls = 0
            try:
                async with asyncio.timeout(15):
                    svc.catalogs[item.id] = await svc.director.discover(item)
            except Exception:
                item.error = "gateway_disconnected"
                svc.director.journal.save(item)
            finally:
                svc.director.catalogs.pop(item.id, None)
        return page(request, svc, item)

    @web.get("/director/{identity}/fragment")
    async def fragment(request: Request, identity: UUID):
        svc, item = owned(request, identity)
        return page(request, svc, item, fragment=True)

    async def mutation(request, identity, allowed):
        svc, item = owned(request, identity)
        data = await fields(request)
        if set(data) - (allowed | {"revision"}):
            raise HTTPException(422)
        if data.pop("revision", "") != str(item.revision) or item.id in svc.jobs:
            raise HTTPException(409)
        return svc, item, data

    @web.post("/director/{identity}/send")
    async def send(request: Request, identity: UUID):
        svc, item, data = await mutation(
            request, identity, {"message", "project", "worker"}
        )
        text = data.get("message", "")
        if not text.strip() or len(text) > svc.director.config.limits.input_chars:
            raise HTTPException(413)
        if (
            item.pending
            or item.submission
            or any(t.state not in TERMINAL for t in item.tasks)
        ):
            raise HTTPException(409)
        catalog = svc.catalogs.get(item.id, {})
        project, worker = data.get("project"), data.get("worker")
        if project and project not in {
            p["project_id"] for p in catalog.get("projects", [])
        }:
            raise HTTPException(403)
        if worker and worker not in {
            w["worker_id"] for w in catalog.get("workers", [])
        }:
            raise HTTPException(403)
        item.project_id = UUID(project) if project else None
        item.worker_id = worker or None
        svc.director.journal.save(item)
        svc.start(item, request.state.director_owner, text=text)
        await asyncio.sleep(0)
        return redirect(request, item)

    @web.post("/director/{identity}/approval")
    async def approval(request: Request, identity: UUID):
        svc, item, data = await mutation(request, identity, {"nonce", "decision"})
        if data.get("decision") not in {"approve", "reject"}:
            raise HTTPException(422)
        from time import time

        if (
            item.pending is None
            or not hmac.compare_digest(data.get("nonce", ""), item.pending.nonce)
            or item.pending.expires <= time()
        ):
            raise HTTPException(409)
        svc.start(
            item,
            request.state.director_owner,
            nonce=data["nonce"],
            approve=data["decision"] == "approve",
        )
        await asyncio.sleep(0)
        return redirect(request, item)

    @web.post("/director/{identity}/resume")
    async def resume(request: Request, identity: UUID):
        svc, item, data = await mutation(request, identity, set())
        if data or item.pending or (item.submission and not item.pending_workflow):
            raise HTTPException(409)
        svc.start(item, request.state.director_owner, resume=True)
        await asyncio.sleep(0)
        return redirect(request, item)

    @web.post("/director/{identity}/stop")
    async def stop(request: Request, identity: UUID):
        svc, item = owned(request, identity)
        data = await fields(request)
        if data != {"operation": str(item.request_id)}:
            raise HTTPException(409)
        job = svc.jobs.get(item.id)
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        return redirect(request, item)

    @web.post("/director/{identity}/cancel-task")
    async def cancel_task(request: Request, identity: UUID):
        svc, item, data = await mutation(request, identity, {"task"})
        task = next((t for t in item.tasks if str(t.task_id) == data.get("task")), None)
        if task is None or item.pending or item.submission:
            raise HTTPException(403)
        svc.director.approval(
            item,
            "cancel_task",
            {"task_id": str(task.task_id)},
            task.project_id,
            task.task_id,
        )
        return redirect(request, item)

    @web.post("/director/{identity}/clear")
    async def clear(request: Request, identity: UUID):
        svc, item, data = await mutation(request, identity, {"confirm"})
        if (
            data.get("confirm") != "clear"
            or item.submission
            or any(t.state not in TERMINAL for t in item.tasks)
        ):
            raise HTTPException(409)
        svc.director.journal.path(item.id).unlink()
        svc.catalogs.pop(item.id, None)
        return redirect(request)
