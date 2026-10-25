"""Operator-facing layout contracts over real ASGI routes and saved sessions."""

import asyncio
from html.parser import HTMLParser
from time import time

import pytest

from agentforge.director.models import Action, TaskBinding, VisibleMessage
from tests.test_coding_workspaces import coding as coding_fixture
from tests.test_director import fixture
from tests.test_director_web import browser, new, owner
from tests.test_mcp import setup as setup_fixture
from tests.test_web import client_for, stored

setup = setup_fixture
coding = coding_fixture


class Navigation(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.primary = False
        self.links = []
        self.feed(body)

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == "nav" and values.get("aria-label") == "Primary":
            self.primary = True
        if tag == "a" and self.primary:
            self.links.append(values)

    def handle_endtag(self, tag):
        if tag == "nav":
            self.primary = False


@pytest.mark.parametrize(
    "path,active,status",
    [
        ("/", "/", 200),
        ("/projects", "/projects", 200),
        ("/workers", "/workers", 200),
        ("/tasks?state=failed", "/tasks", 200),
        ("/councils", "/councils", 200),
        ("/director", "/director", 503),
    ],
)
def test_navigation_matches_actual_routes(setup, path, active, status):
    async def scenario():
        async with client_for(setup.app) as (client, _):
            response = await client.get(path)
            assert response.status_code == status
            links = Navigation(response.text).links
            assert [link["href"] for link in links] == [
                "/",
                "/director",
                "/projects",
                "/workers",
                "/tasks",
                "/councils",
            ]
            assert [link["href"] for link in links if "aria-current" in link] == [
                active
            ]
            assert response.text.count('id="main"') == 1
            assert 'href="#main"' in response.text
            assert "https://fonts" not in response.text
            assert 'hx-history="false"' in response.text

    asyncio.run(scenario())


def test_detail_navigation_filters_and_fragments_keep_dom_identity(setup):
    async def scenario():
        async with client_for(setup.app) as (client, _):
            task_id = stored(setup, "completed")
            detail = await client.get(f"/tasks/{task_id}")
            assert [
                link["href"]
                for link in Navigation(detail.text).links
                if "aria-current" in link
            ] == ["/tasks"]
            fragment = await client.get(f"/tasks/{task_id}/fragment")
            assert fragment.text.count('id="task-detail"') == 1
            assert "<!doctype" not in fragment.text
            assert "&lt;script&gt;" in fragment.text
            assert '<pre tabindex="0">' in fragment.text
            filtered = await client.get(
                "/tasks?state=completed", headers={"HX-Request": "true"}
            )
            assert filtered.text.count('id="task-table"') == 1
            assert '<nav aria-label="Primary"' not in filtered.text
            full = await client.get("/tasks")
            assert 'hx-disabled-elt="find button"' in full.text
            assert 'aria-label="Task history table" tabindex="0"' in full.text

    asyncio.run(scenario())


def test_director_approval_preserves_exact_fields_and_escaped_arguments(
    setup, tmp_path
):
    async def scenario():
        async with fixture(setup, tmp_path) as f:
            async with browser(setup, f) as (client, _):
                path = await new(client)
                item = f.director.load(path.split("/")[-1], owner(client))
                expires = time() + 299
                item.pending = Action(
                    nonce="precise-nonce",
                    operation="delegate_task",
                    arguments={
                        "worker_id": "home-i5",
                        "agent_id": "coder",
                        "task": '<script>alert("args")</script> exact request',
                    },
                    project_id=str(setup.project.id),
                    expires=expires,
                )
                item.state = "approval"
                f.director.journal.save(item)
                response = await client.get(path)
                assert response.status_code == 200
                assert 'aria-label="Approval required"' in response.text
                current = f.director.load(item.id, owner(client))
                assert f'name="revision" value="{current.revision}"' in response.text
                assert 'name="nonce" value="precise-nonce"' in response.text
                assert 'name="csrf"' in response.text
                assert 'name="decision" value="approve"' in response.text
                assert 'name="decision" value="reject"' in response.text
                assert f'data-epoch="{expires}"' in response.text
                assert "Expires (UTC)" in response.text
                assert "&lt;script&gt;" in response.text
                assert '<script>alert("args")' not in response.text
                assert "Verification boundary" in response.text
                assert "cannot independently read or verify source" in response.text
                assert not setup.app.tasks.history()

    asyncio.run(scenario())


def test_director_long_output_restoration_and_active_task_recovery(setup, tmp_path):
    async def scenario():
        async with fixture(setup, tmp_path) as f:
            async with browser(setup, f) as (client, _):
                path = await new(client)
                item = f.director.load(path.split("/")[-1], owner(client))
                task_id = stored(setup, "running")
                item.state = "interrupted"
                item.messages = [
                    VisibleMessage(role="assistant", content="evidence <unsafe> " * 200)
                ]
                item.tasks = [
                    TaskBinding(
                        task_id=task_id,
                        project_id=setup.project.id,
                        worker_id="home-i5",
                        agent_id="repo_explorer",
                        state="running",
                    )
                ]
                f.director.journal.save(item)
                for _ in range(2):
                    response = await client.get(path)
                    assert response.status_code == 200
                    assert "Read full message" in response.text
                    assert 'aria-label="Full message" tabindex="0"' in response.text
                    assert "&lt;unsafe&gt;" in response.text
                    assert 'id="director-message"' in response.text
                    assert "Restore known Task results" in response.text
                    assert "Request Task cancellation approval" in response.text
                    assert "disabled>Send request" in response.text
                    assert f'href="/tasks/{task_id}"' in response.text
                    assert response.text.count('id="director-session"') == 1
                assert len(setup.app.tasks.history()) == 1

    asyncio.run(scenario())


def test_director_htmx_redirects_keep_origin_csrf_and_revision_guards(setup, tmp_path):
    from tests.test_director_web import csrf, idle, submission

    async def scenario():
        async with fixture(setup, tmp_path) as f:
            async with browser(setup, f) as (client, web):
                await client.get("/director")
                headers = {"HX-Request": "true", "origin": "http://127.0.0.1"}
                for origin in ("null", "https://evil.invalid"):
                    denied = await client.post(
                        "/director/new",
                        data=csrf(client),
                        headers=headers | {"origin": origin},
                    )
                    assert denied.status_code == 403
                    assert "HX-Redirect" not in denied.headers
                assert (
                    await client.post("/director/new", data={}, headers=headers)
                ).status_code == 403
                created = await client.post(
                    "/director/new", data=csrf(client), headers=headers
                )
                assert created.status_code == 200 and not created.content
                path = created.headers["HX-Redirect"]
                assert path.startswith("/director/")
                await client.get(path)
                data = submission(client, f.director, path, message="Inspect source")
                sent = await client.post(path + "/send", data=data, headers=headers)
                assert sent.status_code == 200 and sent.headers["HX-Redirect"] == path
                await idle(web)
                stale = await client.post(path + "/send", data=data, headers=headers)
                assert stale.status_code == 409 and "HX-Redirect" not in stale.headers
                assert len(setup.app.tasks.history()) == 1
                item = f.director.load(path.split("/")[-1], owner(client))
                f.director.approval(
                    item,
                    "cancel_task",
                    {"task_id": str(item.tasks[0].task_id)},
                    item.tasks[0].project_id,
                    item.tasks[0].task_id,
                )
                rejected = await client.post(
                    path + "/approval",
                    headers=headers,
                    data=submission(
                        client,
                        f.director,
                        path,
                        nonce=item.pending.nonce,
                        decision="reject",
                    ),
                )
                assert rejected.status_code == 200
                assert rejected.headers["HX-Redirect"] == path
                await idle(web)
                assert f.director.load(item.id, owner(client)).pending is None
                assert len(setup.app.tasks.history()) == 1

    asyncio.run(scenario())


def test_workspace_cleanup_htmx_redirect_preserves_confirmation(coding, database):
    from tests.test_coding_tasks import Provider, application, submit
    from tests.test_web import csrf

    async def scenario():
        app, _ = application(coding, database, Provider())
        async with client_for(app) as (client, _):
            task = submit(app, coding)
            await app.tasks.wait_task(task.task_id)
            path = f"/tasks/{task.task_id}"
            response = await client.get(path)
            remove = path + "/workspace/remove"
            assert f'hx-post="{remove}"' in response.text
            headers = {"HX-Request": "true", "origin": "http://127.0.0.1"}
            denied = await client.post(
                remove, data={"csrf": csrf(response)}, headers=headers
            )
            assert denied.status_code == 403
            assert "HX-Redirect" not in denied.headers
            removed = await client.post(
                remove,
                data={"csrf": csrf(response), "confirm": "yes"},
                headers=headers,
            )
            assert removed.status_code == 200
            assert removed.headers["HX-Redirect"] == path
            assert app.get_coding_workspace(task_id=task.task_id).state == "removed"

    asyncio.run(scenario())
