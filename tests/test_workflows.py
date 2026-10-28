"""Public bounded workflow contracts and durable dispatch/security failure seams."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import ValidationError

from agentforge.application.access import ClientApplication, ClientPolicy, ProjectGrant
from agentforge.application.errors import ServiceError
from agentforge.core.inference import GenerationResult
from agentforge.core.policy import PolicyDenied
from agentforge.core.provider_errors import BackendUnavailable
from agentforge.gateway.server import GatewayConfig
from agentforge.mcp.server import create_server
from agentforge.specialists.models import SpecialistError
from agentforge.workflows.models import (
    Definition,
    InferenceStep,
    Retry,
    SpecialistStep,
    StepArtifact,
    TextReference,
    WorkflowError,
)
from tests.test_mcp import ScriptedProvider
from tests.test_specialists import run
from tests.test_specialists import setup as specialist_setup

setup = specialist_setup


def provider_for(app, provider=None):
    provider = provider or ScriptedProvider()
    provider.name = "ollama"
    worker = app._workers["text"].model_copy(update={"supports_tools": True})
    app._workers["text"] = worker
    app.tasks._runtime._workers["text"] = worker
    app.tasks._runtime._providers["ollama"] = provider
    return provider


def image_step(**fields):
    return SpecialistStep(
        step_id="image",
        kind="specialist",
        tool_id="image",
        intended_effect="Generate a synthetic PNG",
        **fields,
    )


def chain(project, *, approval=False):
    return Definition(
        project_id=project,
        steps=(
            image_step(),
            SpecialistStep(
                step_id="mesh",
                kind="specialist",
                tool_id="blender",
                intended_effect="Synthetic mesh from PNG reference",
                depends_on=("image",),
                artifact_inputs=(StepArtifact(step_id="image", format="png"),),
                approval_required=approval,
            ),
        ),
    )


async def until(
    app,
    identity,
    states=(
        "completed",
        "failed",
        "blocked",
        "recovery_required",
        "approval",
        "cancelled",
    ),
):
    async with asyncio.timeout(8):
        while True:
            value = app.get_workflow(workflow_id=identity)
            if value.state in states:
                return value
            if not app.tasks.available:
                for loop in app.tasks._loops:
                    if loop.done() and loop.exception():
                        raise loop.exception()
            assert app.tasks.available
            await asyncio.sleep(0.005)


def scoped(app, project, **updates):
    from agentforge.application.tool_contracts import TOOL_CONTRACTS

    policy = ClientPolicy(
        client_id="alpha",
        token_sha256="a" * 64,
        projects=(
            ProjectGrant(
                project_id=project, workers=("text",), specialists=("image", "blender")
            ),
        ),
        agents=("general_agent",),
        tools=tuple(t.name for t in TOOL_CONTRACTS),
        **updates,
    )
    holder = [policy]
    app.workflows.client_access = lambda identity: ClientApplication(
        app,
        lambda: (
            holder[0]
            if holder[0].client_id == identity and holder[0].enabled
            else (_ for _ in ()).throw(ServiceError("access_denied"))
        ),
        GatewayConfig(),
    )
    return app.workflows.client_access("alpha"), holder


def test_public_mcp_chain_concurrency_one_disconnect_restart(setup):
    async def scenario():
        app = setup.make(concurrency=1)
        access, _ = scoped(app, setup.project)
        await app.start()
        try:
            server = create_server(
                lambda: app, borrowed=True, client_access=lambda _: access
            )
            async with create_connected_server_and_client_session(server) as sdk:
                args = dict(
                    definition=chain(setup.project).model_dump(mode="json"),
                    idempotency_key="chain",
                )
                result = await sdk.call_tool("submit_workflow", args)
                assert not result.isError, result
                identity = UUID(result.structuredContent["workflow_id"])
                duplicate = await sdk.call_tool("submit_workflow", args)
                assert duplicate.structuredContent["workflow_id"] == str(identity)
            # Disconnect does not own the workflow/executor lifetime.
            value = await until(app, identity)
            assert value.state == "completed", value
            first, last = (s.attempts[0] for s in value.steps)
            artifact = access.inspect_artifact(
                project_id=setup.project, artifact_id=last.artifact_ids[0]
            )
            assert artifact.input_artifact_ids == first.artifact_ids
            assert artifact.tool_job_id == last.tool_job_id
            assert len(app.specialists.repository.jobs()) == 2
        finally:
            await app.close()
        app = setup.make(concurrency=1)
        scoped(app, setup.project)
        await app.start()
        try:
            assert app.get_workflow(workflow_id=identity) == value
            assert (
                app.inspect_artifact(
                    project_id=setup.project, artifact_id=artifact.artifact_id
                ).status
                == "ready"
            )
            assert len(app.specialists.repository.jobs()) == 2
        finally:
            await app.close()

    run(scenario())


@pytest.mark.parametrize(
    "mutation",
    [
        "cycle",
        "unknown",
        "duplicate",
        "type",
        "oversize",
        "reference",
        "wrong_producer",
        "budget",
    ],
)
def test_invalid_definitions_before_dispatch(mutation):
    value = chain(uuid4()).model_dump(mode="json")
    if mutation == "cycle":
        value["steps"][0]["depends_on"] = ["mesh"]
    elif mutation == "unknown":
        value["steps"][1]["depends_on"] = ["missing"]
    elif mutation == "duplicate":
        value["steps"][1]["step_id"] = "image"
    elif mutation == "type":
        value["steps"][0]["kind"] = "shell"
    elif mutation == "oversize":
        value["steps"] *= 9
    elif mutation == "reference":
        value["steps"][1]["artifact_inputs"][0]["step_id"] = "missing"
    elif mutation == "wrong_producer":
        value["steps"][0] = InferenceStep(
            step_id="image",
            kind="inference",
            agent_id="general_agent",
            worker_id="text",
            request="Read",
            intended_effect="Inspect",
        ).model_dump(mode="json")
    else:
        value["max_attempts"] = 1
    with pytest.raises(ValidationError):
        Definition.model_validate(value)


@pytest.mark.parametrize("revocation", ["specialist", "client", "cloud", "target"])
def test_resume_current_authority_and_target_rechecked(setup, revocation):
    async def scenario():
        app = setup.make(concurrency=1)
        access, holder = scoped(app, setup.project)
        await app.start()
        try:
            value = access.submit_workflow(
                definition=chain(setup.project, approval=True), idempotency_key="review"
            )
            value = await until(app, value.workflow_id)
            assert value.state == "approval"
            step = value.steps[-1]
            app.pause_workflow(workflow_id=value.workflow_id)
            if revocation == "client":
                holder[0] = holder[0].model_copy(update={"enabled": False})
            elif revocation == "specialist":
                holder[0] = holder[0].model_copy(
                    update={
                        "projects": (
                            ProjectGrant(
                                project_id=setup.project, specialists=("image",)
                            ),
                        )
                    }
                )
            elif revocation == "cloud":
                tool = app.specialists.tools["blender"]
                app.specialists.tools["blender"] = tool.model_copy(
                    update={"trust": "cloud"}
                )
            else:
                tool = app.specialists.tools["blender"]
                app.specialists.tools["blender"] = tool.model_copy(
                    update={"connection_id": "changed"}
                )
            with pytest.raises(
                (WorkflowError, ServiceError, SpecialistError, PolicyDenied)
            ):
                app.workflows.review(
                    workflow_id=value.workflow_id,
                    step_id="mesh",
                    scope_hash=step.attempts[-1].scope_hash,
                )
            assert len(app.specialists.repository.jobs()) == 1
        finally:
            await app.close()

    run(scenario())


def test_pause_accepted_child_completion_review_and_cancel(setup):
    from agentforge.specialists.adapters import ADAPTERS, FakeComfyUI

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Gate(FakeComfyUI):
            async def run(self, *args):
                entered.set()
                await release.wait()
                return await super().run(*args)

        app = setup.make(concurrency=1)
        await app.start()
        saved = ADAPTERS["fake_comfyui"]
        ADAPTERS["fake_comfyui"] = Gate
        try:
            value = app.submit_workflow(
                definition=chain(setup.project, approval=True), idempotency_key="pause"
            )
            await entered.wait()
            app.pause_workflow(workflow_id=value.workflow_id)
            release.set()
            await asyncio.sleep(0.03)
            app.workflows.tick()
            paused = app.get_workflow(workflow_id=value.workflow_id)
            assert paused.state == "paused" and paused.steps[0].state == "completed"
            assert not paused.steps[1].attempts
            app.resume_workflow(workflow_id=value.workflow_id)
            review = await until(app, value.workflow_id)
            assert (
                review.state == "approval"
                and len(app.specialists.repository.jobs()) == 1
            )
            app.cancel_workflow(workflow_id=value.workflow_id)
            cancelled = await until(app, value.workflow_id, ("cancelled",))
            assert cancelled.steps[0].state == "completed"
            assert cancelled.steps[1].state == "cancelled"
            app.resume_workflow(workflow_id=value.workflow_id)
            assert app.get_workflow(workflow_id=value.workflow_id).state == "cancelled"
            assert len(app.specialists.repository.jobs()) == 1
        finally:
            ADAPTERS["fake_comfyui"] = saved
            await app.close()

    run(scenario())


@pytest.mark.parametrize("kind", ["specialist", "inference"])
def test_child_creation_linkage_crash_has_durable_identity(setup, kind):
    class Crash(BaseException):
        pass

    async def scenario():
        app = setup.make(concurrency=1)
        provider_for(app)
        await app.start()
        definition = (
            Definition(project_id=setup.project, steps=(image_step(),))
            if kind == "specialist"
            else Definition(
                project_id=setup.project,
                steps=(
                    InferenceStep(
                        step_id="read",
                        kind="inference",
                        agent_id="general_agent",
                        worker_id="text",
                        request="Inspect",
                        intended_effect="Read",
                    ),
                ),
            )
        )
        value = app.submit_workflow(definition=definition, idempotency_key="crash")
        original = app.workflows.linked

        def crash(*args):
            raise Crash

        app.workflows.linked = crash
        with pytest.raises(Crash):
            app.workflows.tick()
        app.workflows.linked = original
        # Let the accepted child finish through its public API while simulating
        # loss of the coordinator after creation but before linkage.
        app.workflows.tick = lambda: None
        recorded = app.get_workflow(workflow_id=value.workflow_id).steps[0].attempts[-1]
        if kind == "inference":
            assert (
                await app.tasks.wait_task(recorded.task_id or recorded.dispatch_id)
            ).state == "completed"
        else:
            jobs = app.specialists.repository.jobs()
            from tests.test_specialists import wait

            assert (await wait(app, jobs[0])).state == "completed"
        # Restart observes the existing child without replay.
        child = app.get_workflow(workflow_id=value.workflow_id).steps[0].attempts[-1]
        assert child.dispatch_id
        app.pause_workflow(workflow_id=value.workflow_id)
        await app.close()
        app = setup.make(concurrency=1)
        provider_for(app)
        await app.start()
        try:
            app.resume_workflow(workflow_id=value.workflow_id)
            completed = await until(app, value.workflow_id)
            assert completed.state == "completed", completed
            assert len(completed.steps[0].attempts) == 1
            assert (
                len(app.specialists.repository.jobs()) + len(app.tasks.history()) == 1
            )
        finally:
            await app.close()

    run(scenario())


def test_approval_scope_one_use_and_success(setup):
    async def scenario():
        app = setup.make(concurrency=1)
        await app.start()
        try:
            value = app.submit_workflow(
                definition=chain(setup.project, approval=True),
                idempotency_key="approve",
            )
            value = await until(app, value.workflow_id)
            fields = dict(
                workflow_id=value.workflow_id,
                step_id="mesh",
                scope_hash=value.steps[-1].attempts[-1].scope_hash,
            )
            with pytest.raises(WorkflowError, match="stale_approval"):
                app.workflows.review(**(fields | {"scope_hash": "changed"}))
            app.workflows.review(**fields)
            with pytest.raises(WorkflowError, match="stale_approval"):
                app.workflows.review(**fields)
            value = await until(
                app, value.workflow_id, ("completed", "failed", "blocked")
            )
            assert value.state == "completed"
        finally:
            await app.close()

    run(scenario())


@pytest.mark.parametrize("damage", ["expired", "corrupt"])
def test_artifact_integrity_before_successor(setup, damage):
    async def scenario():
        app = setup.make(concurrency=1)
        await app.start()
        try:
            value = app.submit_workflow(
                definition=chain(setup.project, approval=True),
                idempotency_key="artifact",
            )
            value = await until(app, value.workflow_id)
            artifact = value.steps[0].attempts[-1].artifact_ids[0]
            if damage == "expired":
                app.specialists.repository.expire(artifact)
            else:
                # Use the store's actual naming contract, not a guessed path.
                app.specialists.files.remove(artifact)
                app.specialists.files.write(artifact, b"corrupt")
            app.workflows.review(
                workflow_id=value.workflow_id,
                step_id="mesh",
                scope_hash=value.steps[-1].attempts[-1].scope_hash,
            )
            value = await until(app, value.workflow_id, ("blocked",))
            assert len(app.specialists.repository.jobs()) == 1
        finally:
            await app.close()

    run(scenario())


def test_failed_dependency_bounded_retry_and_text_transfer(setup):
    async def scenario():
        app = setup.make(concurrency=1)
        provider = ScriptedProvider()
        provider.turns = [
            BackendUnavailable("private"),
            GenerationResult(content="EVIDENCE" * 1000, model="fake"),
            GenerationResult(content="Observed", model="fake"),
        ]
        provider_for(app, provider)
        await app.start()
        try:
            first = InferenceStep(
                step_id="read",
                kind="inference",
                agent_id="general_agent",
                worker_id="text",
                request="Read only",
                intended_effect="Read",
                retry=Retry(max_attempts=2, on=("provider_error",)),
            )
            second = InferenceStep(
                step_id="check",
                kind="inference",
                agent_id="general_agent",
                worker_id="text",
                request="Check",
                intended_effect="Read evidence",
                depends_on=("read",),
                text_inputs=(TextReference(step_id="read", max_chars=17),),
            )
            value = app.submit_workflow(
                definition=Definition(project_id=setup.project, steps=(first, second)),
                idempotency_key="retry",
            )
            value = await until(app, value.workflow_id)
            assert value.state == "completed", value
            assert len(value.steps[0].attempts) == 2
            assert value.steps[0].attempts[0].failure_category == "provider_error"
            assert (
                provider.requests[-1]
                .messages[-1]
                .content.endswith(("EVIDENCE" * 3)[:17])
            )
            assert value.steps[-1].attempts[-1].result_text == "Observed"
            assert len(app.tasks.history()) == 3
        finally:
            await app.close()

    run(scenario())


def test_unauthorized_bindings_cross_client_and_duplicate_conflict(setup):
    async def scenario():
        app = setup.make(concurrency=1)
        access, holder = scoped(app, setup.project)
        await app.start()
        try:
            with pytest.raises((ServiceError, WorkflowError)):
                access.submit_workflow(
                    definition=chain(setup.second), idempotency_key="bad"
                )
            value = access.submit_workflow(
                definition=chain(setup.project), idempotency_key="good"
            )
            with pytest.raises(WorkflowError, match="idempotency_conflict"):
                access.submit_workflow(
                    definition=chain(setup.project, approval=True),
                    idempotency_key="good",
                )
            holder[0] = holder[0].model_copy(
                update={"client_id": "beta", "read_shared": True}
            )
            other = ClientApplication(app, lambda: holder[0], GatewayConfig())
            with pytest.raises(ServiceError):
                other.get_workflow(workflow_id=value.workflow_id)
            app.cancel_workflow(workflow_id=value.workflow_id)
        finally:
            await app.close()

    run(scenario())


def test_failed_dependency_is_blocked_after_bounded_exhaustion(setup):
    async def scenario():
        app = setup.make(concurrency=1)
        provider = provider_for(app)
        provider.turns = [BackendUnavailable("private")] * 3
        await app.start()
        try:
            first = InferenceStep(
                step_id="read",
                kind="inference",
                agent_id="general_agent",
                worker_id="text",
                request="Read",
                intended_effect="Read",
                retry=Retry(max_attempts=2, on=("provider_error",)),
            )
            second = InferenceStep(
                step_id="next",
                kind="inference",
                agent_id="general_agent",
                worker_id="text",
                request="Consume",
                intended_effect="Read",
                depends_on=("read",),
            )
            value = app.submit_workflow(
                definition=Definition(project_id=setup.project, steps=(first, second)),
                idempotency_key="exhaust",
            )
            value = await until(app, value.workflow_id)
            assert value.state == "failed"
            assert len(value.steps[0].attempts) == 2
            assert value.steps[1].state == "blocked" and not value.steps[1].attempts
            assert len(app.tasks.history()) == len(provider.requests) == 2
        finally:
            await app.close()

    run(scenario())


def test_running_cancellation_shared_capacity_and_no_unapproved_successor(setup):
    async def scenario():
        app = setup.make(concurrency=1)
        provider = provider_for(app)
        provider.release.clear()
        await app.start()
        try:
            step = InferenceStep(
                step_id="read",
                kind="inference",
                agent_id="general_agent",
                worker_id="text",
                request="Wait",
                intended_effect="Read",
            )
            value = app.submit_workflow(
                definition=Definition(
                    project_id=setup.project,
                    steps=(
                        step,
                        image_step(depends_on=("read",), approval_required=True),
                    ),
                ),
                idempotency_key="cancel",
            )
            await provider.entered.wait()
            ordinary = app.submit_tool_job(
                project_id=setup.project,
                tool_id="image",
                request_id=uuid4(),
                idempotency_key="ordinary",
            )
            assert app.get_tool_job(tool_job_id=ordinary.tool_job_id).state == "queued"
            assert len(app.tasks._tokens) == 1
            app.cancel_workflow(workflow_id=value.workflow_id)
            child = app.tasks.get_task(
                app.get_workflow(workflow_id=value.workflow_id)
                .steps[0]
                .attempts[0]
                .task_id
            )
            assert child.cancellation_requested_at is not None
            provider.release.set()
            value = await until(app, value.workflow_id, ("cancelled",))
            assert not value.steps[1].attempts
            from tests.test_specialists import wait

            assert (await wait(app, ordinary)).state == "completed"
            assert len(app.specialists.repository.jobs()) == 1
        finally:
            provider.release.set()
            await app.close()

    run(scenario())


def test_restart_approval_and_ambiguous_remote_effect_never_replayed(
    setup, monkeypatch
):
    from agentforge.specialists.models import Parameters
    from tests.test_specialists_real import Server, comfy, transport

    server = Server(mode="ambiguous")
    transport(monkeypatch, server)

    async def scenario():
        config = comfy(setup)
        app = setup.make(config=config, concurrency=1)
        await app.start()
        definition = Definition(
            project_id=setup.project,
            steps=(
                image_step(
                    parameters=Parameters(text="approved"),
                    retry=Retry(max_attempts=3, on=("specialist_adapter_failed",)),
                ),
            ),
        )
        value = app.submit_workflow(definition=definition, idempotency_key="remote")
        value = await until(app, value.workflow_id)
        assert value.state == "approval" and not server.calls
        await app.close()
        app = setup.make(config=config, concurrency=1)
        await app.start()
        try:
            value = app.get_workflow(workflow_id=value.workflow_id)
            assert value.state == "approval" and not server.calls
            app.workflows.review(
                workflow_id=value.workflow_id,
                step_id="image",
                scope_hash=value.steps[0].attempts[0].scope_hash,
            )
            value = await until(app, value.workflow_id, ("recovery_required",))
            assert len(value.steps[0].attempts) == 1
            job_id = value.steps[0].attempts[0].tool_job_id
        finally:
            await app.close()
        app = setup.make(config=config, concurrency=1)
        await app.start()
        try:
            app.resume_workflow(workflow_id=value.workflow_id)
            result = await until(app, value.workflow_id, ("recovery_required",))
            assert result.steps[0].attempts[0].tool_job_id == job_id
            assert len(app.specialists.repository.jobs()) == 1
            assert sum(r.url.path == "/prompt" for r in server.calls) == 1
            assert len(result.steps[0].attempts) == 1
        finally:
            await app.close()

    run(scenario())


def test_workflow_deadline_and_dispatch_failure_preserve_attempt(setup, monkeypatch):
    async def scenario():
        app = setup.make(concurrency=1)
        await app.start()
        try:
            value = app.submit_workflow(
                definition=chain(setup.project), idempotency_key="intent"
            )

            def deny(*args):
                raise WorkflowError("dispatch_blocked")

            monkeypatch.setattr(app.workflows, "dispatch", deny)
            app.workflows.tick()
            value = app.get_workflow(workflow_id=value.workflow_id)
            assert value.state == "blocked" and len(value.steps[0].attempts) == 1
            identity = value.steps[0].attempts[0].dispatch_id
            value = value.model_copy(
                update={"created_at": datetime.now(UTC) - timedelta(days=2)}
            )
            app.workflows.repository.save(value)
            app.workflows.tick()
            value = app.get_workflow(workflow_id=value.workflow_id)
            assert (
                value.state == "cancelled"
                and value.failure_category == "workflow_timeout"
            )
            assert value.steps[0].attempts[0].dispatch_id == identity
            assert not app.specialists.repository.jobs()
        finally:
            await app.close()

    run(scenario())


@pytest.mark.parametrize("htmx", [False, True])
def test_dashboard_scope_review_csrf_and_lifecycle(setup, htmx):
    from tests.test_web import client_for

    async def scenario():
        app = setup.make(concurrency=1)
        async with client_for(app) as (browser, _):
            value = app.submit_workflow(
                definition=chain(setup.project, approval=True), idempotency_key="web"
            )
            value = await until(app, value.workflow_id)
            page = await browser.get(f"/workflows/{value.workflow_id}")
            assert page.status_code == 200
            assert (
                "Exact resolved inputs" in page.text
                and "Recorded execution target" in page.text
            )
            assert str(value.steps[0].attempts[0].tool_job_id) in page.text
            assert (await browser.get("/workflows")).status_code == 200
            scope = value.steps[-1].attempts[-1].scope_hash
            path = f"/workflows/{value.workflow_id}/review/mesh/{scope}/approve"
            assert f'hx-post="{path}"' in page.text
            for operation in ("pause", "resume", "cancel"):
                control = f"/workflows/{value.workflow_id}/{operation}"
                assert f'hx-post="{control}"' in page.text
            assert (
                await browser.post(path, data={"confirm": "yes"})
            ).status_code == 403
            csrf = browser.cookies.get("agentforge_csrf")
            assert (
                await browser.post(
                    path,
                    headers={"origin": "https://evil.invalid"},
                    data={"confirm": "yes", "csrf": csrf},
                )
            ).status_code == 403
            assert (
                await browser.post(
                    path,
                    headers={"origin": "null"},
                    data={"confirm": "yes", "csrf": csrf},
                )
            ).status_code == 403
            response = await browser.post(
                path,
                headers={"HX-Request": "true", "Origin": "http://127.0.0.1"}
                if htmx
                else {},
                data={"confirm": "yes", "csrf": csrf},
            )
            assert response.status_code == (200 if htmx else 303)
            assert response.headers["HX-Redirect" if htmx else "location"] == (
                f"/workflows/{value.workflow_id}"
            )
            assert (
                await browser.post(path, data={"confirm": "yes", "csrf": csrf})
            ).status_code == 409
            value = await until(app, value.workflow_id, ("completed",))
            assert (
                "completed"
                in (await browser.get(f"/workflows/{value.workflow_id}")).text
            )

    run(scenario())


def test_existing_artifact_cross_client_and_producer_checks(setup):
    from agentforge.workflows.models import ExistingArtifact
    from tests.test_specialists import image

    async def scenario():
        app = setup.make(concurrency=1)
        access, _ = scoped(app, setup.project)
        await app.start()
        try:
            job, artifact = await image(app, setup)
            ref = ExistingArtifact(
                source="artifact",
                artifact_id=artifact.artifact_id,
                producer_job_id=job.tool_job_id,
                format="png",
            )
            step = SpecialistStep(
                step_id="mesh",
                kind="specialist",
                tool_id="blender",
                intended_effect="Process reference",
                artifact_inputs=(ref,),
            )
            # Local artifact has no alpha owner; even read_shared cannot confer
            # workflow producer ownership for new effects.
            with pytest.raises((WorkflowError, ServiceError)):
                access.submit_workflow(
                    definition=Definition(project_id=setup.project, steps=(step,)),
                    idempotency_key="foreign",
                )
            wrong = step.model_copy(
                update={
                    "artifact_inputs": (
                        ref.model_copy(update={"producer_job_id": uuid4()}),
                    )
                }
            )
            from agentforge.specialists.models import SpecialistError

            with pytest.raises(SpecialistError, match="tool_job_not_found"):
                app.submit_workflow(
                    definition=Definition(project_id=setup.project, steps=(wrong,)),
                    idempotency_key="producer",
                )
        finally:
            await app.close()

    run(scenario())


def test_offline_installed_contract_acceptance():
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts/verify_workflows.py"),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "native HTTP Local Director" in result.stdout


def test_known_receipt_reconciles_same_child_while_paused(setup, monkeypatch):
    from tests.test_specialists_real import Server, comfy, transport

    server = Server(mode="running")
    transport(monkeypatch, server)

    async def scenario():
        config = comfy(setup)
        app = setup.make(config=config, concurrency=1)
        await app.start()
        value = app.submit_workflow(
            definition=chain(setup.project, approval=True), idempotency_key="receipt"
        )
        value = await until(app, value.workflow_id)
        app.workflows.review(
            workflow_id=value.workflow_id,
            step_id="image",
            scope_hash=value.steps[0].attempts[0].scope_hash,
        )
        while server.polls == 0:
            await asyncio.sleep(0.01)
        app.pause_workflow(workflow_id=value.workflow_id)
        app.workflows.tick()
        value = app.get_workflow(workflow_id=value.workflow_id)
        job_id = value.steps[0].attempts[0].tool_job_id
        await app.close()
        server.mode = "success"
        app = setup.make(config=config, concurrency=1)
        await app.start()
        try:
            async with asyncio.timeout(5):
                while (
                    app.get_workflow(workflow_id=value.workflow_id).steps[0].state
                    != "completed"
                ):
                    await asyncio.sleep(0.01)
            paused = app.get_workflow(workflow_id=value.workflow_id)
            assert paused.state == "paused" and not paused.steps[1].attempts
            assert paused.steps[0].attempts[0].tool_job_id == job_id
            assert len(paused.steps[0].attempts) == 1
            assert sum(r.url.path == "/prompt" for r in server.calls) == 1
            app.resume_workflow(workflow_id=value.workflow_id)
            review = await until(app, value.workflow_id)
            assert review.state == "approval" and review.steps[1].state == "approval"
            app.cancel_workflow(workflow_id=value.workflow_id)
            assert (await until(app, value.workflow_id, ("cancelled",))).steps[
                1
            ].state == "cancelled"
            assert len(app.specialists.repository.jobs()) == 1
        finally:
            await app.close()

    run(scenario())


@pytest.mark.parametrize("kind", ["specialist", "inference"])
@pytest.mark.parametrize("window", ["before_creation", "after_creation"])
def test_process_death_dispatch_intent_and_child_linkage(setup, tmp_path, kind, window):
    import subprocess
    import sys

    settings = tmp_path / "specialists.json"
    settings.write_text(setup.config.model_dump_json())
    definition = (
        Definition(project_id=setup.project, steps=(image_step(),))
        if kind == "specialist"
        else Definition(
            project_id=setup.project,
            steps=(
                InferenceStep(
                    step_id="read",
                    kind="inference",
                    agent_id="general_agent",
                    worker_id="text",
                    intended_effect="Read",
                    request="Read only",
                ),
            ),
        )
    )
    plan = tmp_path / "plan.json"
    plan.write_text(definition.model_dump_json())
    code = """
import asyncio, os, sys
from pathlib import Path
from agentforge.application.service import Application
from agentforge.db.database import create_database_engine
from agentforge.core.worker import Worker
from agentforge.core.inference import GenerationResult
from agentforge.specialists.models import SpecialistConfig
from agentforge.workers.config import WorkersConfig
from agentforge.workflows.models import Definition
class Provider:
    name = "ollama"
    async def generate(self, worker, request):
        return GenerationResult(content="Read result", model=worker.model)
async def main():
    worker = Worker(id="text", provider="ollama", endpoint="http://localhost:1",
                    model="fake", trust="local", supports_tools=True)
    app = Application(
        create_database_engine(sys.argv[1]), WorkersConfig(workers=[worker]),
        specialist_config=SpecialistConfig.model_validate_json(
            Path(sys.argv[2]).read_text()),
        providers={"ollama": Provider()}, concurrency=1)

    await app.start()
    value = app.submit_workflow(
        definition=Definition.model_validate_json(Path(sys.argv[3]).read_text()),
        idempotency_key="process")
    print("WORKFLOW=" + str(value.workflow_id), flush=True)
    def die(*args):
        os._exit(23)
    if sys.argv[4] == "before_creation":
        app.workflows.dispatch = die
    else:
        app.workflows.linked = die
    app.workflows.tick()
    raise AssertionError("crash hook not reached")
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(setup.url), str(settings), str(plan), window],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 23, result.stdout + result.stderr
    identity = UUID(
        next(
            line.split("=", 1)[1]
            for line in result.stdout.splitlines()
            if line.startswith("WORKFLOW=")
        )
    )

    async def recover():
        app = setup.make(concurrency=1)
        provider_for(app)
        await app.start()
        try:
            value = await until(app, identity)
            assert value.state == "completed", value
            assert len(value.steps[0].attempts) == 1
            assert (
                len(app.tasks.history()) + len(app.specialists.repository.jobs()) == 1
            )
            attempt = value.steps[0].attempts[0]
            if kind == "inference":
                assert attempt.task_id == attempt.dispatch_id
            else:
                assert (
                    app.get_tool_job(tool_job_id=attempt.tool_job_id).request_id
                    == attempt.dispatch_id
                )
        finally:
            await app.close()

    run(recover())


@pytest.mark.parametrize("early_timer", [False, True])
def test_overall_deadline_bounds_running_child_with_one_slot(
    setup, monkeypatch, early_timer
):
    async def scenario():
        app = setup.make(concurrency=1)
        provider = provider_for(app)
        provider.release.clear()
        await app.start()
        try:
            step = InferenceStep(
                step_id="read",
                kind="inference",
                agent_id="general_agent",
                worker_id="text",
                request="Read",
                intended_effect="Read",
            )
            value = app.submit_workflow(
                definition=Definition(
                    project_id=setup.project, steps=(step,), timeout_seconds=1
                ),
                idempotency_key="deadline",
            )
            if early_timer:
                # Reproduce a monotonic budget expiring while UTC is still before
                # the nominal deadline, as with coarse Windows asyncio timers.
                import agentforge.workflows.service as coordination

                monkeypatch.setattr(
                    coordination,
                    "now",
                    lambda: value.created_at + timedelta(seconds=0.5),
                )
            await provider.entered.wait()
            value = await until(app, value.workflow_id, ("cancelled",))
            assert value.failure_category == "workflow_timeout"
            child = app.tasks.get_task(value.steps[0].attempts[0].task_id)
            assert child.reason in {"timeout", "provider_timeout"}
            assert (child.finished_at - child.started_at).total_seconds() < 3
        finally:
            await app.close()

    run(scenario())


def test_cancel_intent_cannot_be_resumed_before_child_finishes(setup):
    async def scenario():
        app = setup.make(concurrency=1)
        await app.start()
        try:
            value = app.submit_workflow(
                definition=chain(setup.project, approval=True),
                idempotency_key="cancel-intent",
            )
            value = await until(app, value.workflow_id)
            app.cancel_workflow(workflow_id=value.workflow_id)
            assert (
                app.resume_workflow(workflow_id=value.workflow_id).state == "cancelling"
            )
            with pytest.raises(WorkflowError, match="stale_approval"):
                app.workflows.review(
                    workflow_id=value.workflow_id,
                    step_id="mesh",
                    scope_hash=value.steps[1].attempts[0].scope_hash,
                )
            assert (await until(app, value.workflow_id, ("cancelled",))).steps[
                1
            ].state == "cancelled"
            assert len(app.specialists.repository.jobs()) == 1
        finally:
            await app.close()

    run(scenario())


def test_operator_resource_review_and_revoked_read_scope(setup):
    async def scenario():
        app = setup.make(concurrency=1)
        provider_for(app)
        access, holder = scoped(app, setup.project)
        app._workers["text"] = app._workers["text"].model_copy(
            update={"workflow_review_required": True}
        )
        app.tasks._runtime._workers["text"] = app._workers["text"]
        await app.start()
        try:
            step = InferenceStep(
                step_id="read",
                kind="inference",
                agent_id="general_agent",
                worker_id="text",
                intended_effect="GPU-heavy configured work",
                request="Inspect",
            )
            value = access.submit_workflow(
                definition=Definition(project_id=setup.project, steps=(step,)),
                idempotency_key="resource",
            )
            value = await until(app, value.workflow_id)
            assert value.state == "approval"
            assert value.steps[0].attempts[0].effects == ("operator_resource_review",)
            assert not app.tasks.history()
            holder[0] = holder[0].model_copy(
                update={"tools": tuple(t for t in holder[0].tools if t != "get_task")}
            )
            with pytest.raises(ServiceError, match="access_denied"):
                access.get_workflow(workflow_id=value.workflow_id)
            with pytest.raises(ServiceError, match="access_denied"):
                app.workflows.review(
                    workflow_id=value.workflow_id,
                    step_id="read",
                    scope_hash=value.steps[0].attempts[0].scope_hash,
                )
        finally:
            await app.close()

    run(scenario())


def test_default_review_flag_preserves_existing_worker_fingerprints(setup):
    import hashlib
    import json

    app = setup.make(concurrency=1)
    try:
        worker = app._workers["text"]
        legacy = {
            "worker": worker.model_dump(
                mode="json", exclude={"workflow_review_required"}
            ),
            "connection": None,
        }
        expected = hashlib.sha256(
            json.dumps(legacy, sort_keys=True).encode()
        ).hexdigest()
        assert app.worker_policy.binding(worker)["target_fingerprint"] == expected
        changed = worker.model_copy(update={"workflow_review_required": True})
        assert app.worker_policy.binding(changed)["target_fingerprint"] != expected
    finally:
        app.database.dispose()
