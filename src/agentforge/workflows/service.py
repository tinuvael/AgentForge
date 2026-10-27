"""Synchronous coordination at shared executor boundaries; no executor or poller."""

from datetime import UTC, datetime, timedelta
from uuid import uuid5

from agentforge.application.errors import ServiceError
from agentforge.core.policy import PolicyDenied
from agentforge.db.workflows import digest
from agentforge.projects.errors import ProjectError
from agentforge.specialists.models import SpecialistError
from agentforge.tasks.models import TaskNotFound, TaskValidationError
from agentforge.workflows.models import (
    TERMINAL,
    Attempt,
    ExistingArtifact,
    ReviewScope,
    SubmitWorkflow,
    WorkflowError,
)


def now():
    return datetime.now(UTC)


class WorkflowService:
    def __init__(self, app, repository):
        self.app, self.repository = app, repository
        # Installed by the authenticated Gateway before Application.start().
        # Resolves CURRENT client policy by identity, never retained credentials.
        self.client_access = None

    def access(self, client_id):
        if client_id is None:
            return self.app
        if self.client_access is None:
            raise WorkflowError("authorization_unavailable")
        return self.client_access(client_id)

    def authorize(self, workflow, step):
        access = self.access(workflow.client_id)
        if access is self.app:
            return access
        client = access.require("submit_workflow")
        project = workflow.definition.project_id
        if step.kind == "inference":
            access.require("delegate_task")
            access.require("get_task")
            access.require("cancel_task")
            access.binding(client, project, step.agent_id, (step.worker_id,))
            agent = self.app.agent_definition(step.agent_id)
            if (
                agent
                and agent.workspace_mode == "isolated_write"
                and not client.coding_submit
            ):
                raise WorkflowError("access_denied")
            access.capacity()
        else:
            for operation in (
                "submit_tool_job",
                "get_tool_job",
                "cancel_tool_job",
                "inspect_artifact",
            ):
                access.specialist_grant(operation, project, step.tool_id)
        return access

    def binding(self, project, step):
        with self.app.projects.open_root(project):
            pass
        if step.kind == "specialist":
            tool = self.app.specialist_service().authorize(project, step.tool_id)
            step.parameters.for_tool(tool)
            if any(
                getattr(step.limits, k) > getattr(tool.limits, k)
                for k in type(step.limits).model_fields
            ):
                raise WorkflowError("step_limits_exceed_target")
            expected = 0 if tool.adapter in {"comfyui", "fake_comfyui"} else 1
            if len(step.artifact_inputs) != expected or any(
                ref.format not in tool.input_formats for ref in step.artifact_inputs
            ):
                raise WorkflowError("incompatible_artifact")
            return tool.fingerprint()
        runtime = self.app.tasks._runtime
        runtime.validate_binding(
            project_id=project,
            agent_id=step.agent_id,
            worker_id=step.worker_id,
            task=step.request,
        )
        runtime.check_policy(project, step.worker_id, step.task_policy)
        agent = self.app.agent_definition(step.agent_id)
        if any(
            getattr(step.limits, k) > getattr(agent.limits, k)
            for k in type(step.limits).model_fields
        ):
            raise WorkflowError("step_limits_exceed_target")
        if step.required_validators and (
            agent.workspace_mode != "isolated_write"
            or not self.app.coding
            or any(
                v not in self.app.coding.config.validations
                for v in step.required_validators
            )
        ):
            raise WorkflowError("validator_not_authorized")
        return digest(
            {
                "worker": runtime.policy_binding(step.worker_id),
                "agent": agent.model_dump(mode="json"),
                "validators": {
                    v: self.app.coding.config.validations[v].model_dump(mode="json")
                    for v in step.required_validators
                },
            }
        )

    def target(self, step):
        if step.kind == "specialist":
            from agentforge.specialists.models import SpecialistDescription

            return SpecialistDescription.model_validate(
                self.app.specialists.tools[step.tool_id].model_dump()
            ).model_dump(mode="json")
        worker = self.app._workers[step.worker_id]
        agent = self.app.agent_definition(step.agent_id)
        return {
            "worker_id": worker.id,
            "provider": worker.provider,
            "model": worker.model,
            "connection": worker.provider_connection or "inline:" + worker.id,
            "trust": self.app.worker_policy.trust(worker),
            "agent_id": agent.id,
            "workspace_mode": agent.workspace_mode,
            "allowed_tools": agent.allowed_tools,
        }

    def submit(self, *, definition, idempotency_key, client_id=None):
        arguments = SubmitWorkflow(
            definition=definition, idempotency_key=idempotency_key
        )
        definition = arguments.definition
        if not self.app.tasks.available:
            raise WorkflowError("executor_unavailable")
        # Validate ALL steps, format contracts and grants before accepting any work.
        from types import SimpleNamespace

        context = SimpleNamespace(client_id=client_id, definition=definition)
        bindings = []
        steps = {s.step_id: s for s in definition.steps}
        for step in definition.steps:
            self.authorize(context, step)
            bindings.append(self.binding(definition.project_id, step))
            if step.kind == "specialist":
                for ref in step.artifact_inputs:
                    if isinstance(ref, ExistingArtifact):
                        self.artifact(
                            context, ref.artifact_id, ref.producer_job_id, ref.format
                        )
                    else:
                        producer = steps[ref.step_id]
                        tool = self.app.specialist_service().tools[producer.tool_id]
                        if (
                            ref.output >= len(tool.output_formats)
                            or tool.output_formats[ref.output] != ref.format
                        ):
                            raise WorkflowError("incompatible_artifact")
        with self.app.tasks.storage_access():
            value = self.repository.submit(
                definition,
                idempotency_key,
                client_id,
                tuple(bindings),
                tuple(self.target(s) for s in definition.steps),
            )
            self.app.tasks.work_changed()
            return value

    def artifact(self, workflow, artifact_id, job_id, format):
        project = workflow.definition.project_id
        access = self.access(workflow.client_id)
        artifact = access.inspect_artifact(project_id=project, artifact_id=artifact_id)
        job = self.app.get_tool_job(tool_job_id=job_id)
        if (
            artifact.tool_job_id != job_id
            or job.project_id != project
            or job.client_id != workflow.client_id
            or job.state != "completed"
            or artifact_id not in job.output_artifact_ids
            or artifact.status != "ready"
            or artifact.format != format
        ):
            raise WorkflowError("artifact_provenance_invalid")
        return artifact_id

    def inputs(self, workflow, step):
        project = workflow.definition.project_id
        if step.kind == "specialist":
            ids = []
            for ref in step.artifact_inputs:
                if isinstance(ref, ExistingArtifact):
                    aid, job_id = ref.artifact_id, ref.producer_job_id
                else:
                    producer = next(
                        s for s in workflow.steps if s.step_id == ref.step_id
                    )
                    if producer.state != "completed":
                        raise WorkflowError("dependency_not_ready")
                    attempt = producer.attempts[-1]
                    aid, job_id = attempt.artifact_ids[ref.output], attempt.tool_job_id
                ids.append(str(self.artifact(workflow, aid, job_id, ref.format)))
            return {"input_artifact_ids": ids}
        text = step.request
        for ref in step.text_inputs:
            producer = next(s for s in workflow.steps if s.step_id == ref.step_id)
            attempt = producer.attempts[-1]
            self.access(workflow.client_id).get_task(task_id=attempt.task_id)
            child = self.app.tasks.get_task(attempt.task_id)
            if (
                producer.state != "completed"
                or child.state != "completed"
                or child.client_id != workflow.client_id
                or child.project_id != project
            ):
                raise WorkflowError("text_provenance_invalid")
            # This is deliberate bounded final-answer transfer, never tool bodies,
            # hidden reasoning, binary retrieval or private workspace metadata.
            text += (
                f"\n\nUntrusted final text from step {ref.step_id}, "
                f"Task {attempt.task_id}:\n"
                + (child.final_answer or "")[: ref.max_chars]
            )
        return {"request": text}

    def effects(self, step):
        effects = []
        if step.approval_required:
            effects.append("explicit_review")
        if step.kind == "inference":
            if self.app._workers[step.worker_id].workflow_review_required:
                effects.append("operator_resource_review")
            if (
                self.app.agent_definition(step.agent_id).workspace_mode
                == "isolated_write"
            ):
                effects.append("repository_write")
            if (
                self.app.worker_policy.trust(self.app._workers[step.worker_id])
                == "cloud"
            ):
                effects.extend(("paid_execution", "external_data_egress"))
        else:
            tool = self.app.specialist_service().tools[step.tool_id]
            if not tool.simulated:
                effects.append("gpu_or_host_execution")
            if tool.trust == "cloud":
                effects.extend(("paid_execution", "external_data_egress"))
        # Existing Blender operation starts a private disposable scene; no
        # destructive existing-scene operation is supported by this MVP.
        return tuple(effects)

    def review_scope(self, workflow_id, step_id):
        workflow = self.repository.get(workflow_id)
        index = next(i for i, s in enumerate(workflow.steps) if s.step_id == step_id)
        return ReviewScope(
            workflow_id=workflow_id,
            project_id=workflow.definition.project_id,
            client_id=workflow.client_id,
            step=workflow.definition.steps[index],
            attempt=workflow.steps[index].attempts[-1],
            binding=workflow.bindings[index],
            target=workflow.targets[index] if workflow.targets else {},
            effects=self.effects(workflow.definition.steps[index]),
        )

    def review(self, *, workflow_id, step_id, scope_hash, approve=True):
        """Trusted operator only. Intentionally absent from the model/MCP catalog."""
        with self.app.tasks.storage_access():
            workflow = self.repository.get(workflow_id)
            index = next(
                i for i, s in enumerate(workflow.steps) if s.step_id == step_id
            )
            run = workflow.steps[index]
            attempt = run.attempts[-1]
            if (
                run.state != "approval"
                or attempt.scope_hash != scope_hash
                or workflow.state in {"cancelling", "cancelled", "failed", "completed"}
            ):
                raise WorkflowError("stale_approval")
            self.authorize(workflow, workflow.definition.steps[index])
            if (
                self.binding(
                    workflow.definition.project_id, workflow.definition.steps[index]
                )
                != workflow.bindings[index]
            ):
                raise WorkflowError("binding_changed")
            attempt = attempt.model_copy(
                update={
                    "approved_at": now() if approve else None,
                    "state": "pending" if approve else "failed",
                    "failure_category": None if approve else "approval_rejected",
                    "finished_at": None if approve else now(),
                }
            )
            run = run.model_copy(
                update={
                    "state": "pending" if approve else "failed",
                    "failure_category": None if approve else "approval_rejected",
                    "attempts": (*run.attempts[:-1], attempt),
                }
            )
            workflow = self.replace(workflow, index, run)
            value = self.repository.save(workflow)
            self.app.tasks.work_changed()
            return value

    @staticmethod
    def replace(workflow, index, run):
        return workflow.model_copy(
            update={
                "steps": (*workflow.steps[:index], run, *workflow.steps[index + 1 :])
            }
        )

    def control(self, identity, operation):
        with self.app.tasks.storage_access():
            workflow = self.repository.get(identity)
            if workflow.state in {"completed", "failed", "cancelled", "cancelling"}:
                return workflow
            if operation == "resume":
                for index, step in enumerate(workflow.definition.steps):
                    self.authorize(workflow, step)
                    if (
                        self.binding(workflow.definition.project_id, step)
                        != workflow.bindings[index]
                    ):
                        raise WorkflowError("binding_changed")
                state = "pending"
            else:
                state = "paused" if operation == "pause" else "cancelling"
            workflow = self.repository.save(
                workflow.model_copy(update={"state": state, "failure_category": None})
            )
            if state == "cancelling":
                try:
                    self.cancel_children(workflow)
                except (WorkflowError, ServiceError):
                    workflow = self.repository.save(
                        workflow.model_copy(
                            update={"failure_category": "child_cancellation_denied"}
                        )
                    )
            self.app.tasks.work_changed()
            return workflow

    def cancel_children(self, workflow):
        access = self.access(workflow.client_id)
        for step, run in zip(workflow.definition.steps, workflow.steps, strict=True):
            if not run.attempts:
                continue
            child = self.child(workflow, step, run.attempts[-1])
            if child is None or child.state not in {"queued", "running"}:
                continue
            if step.kind == "inference":
                access.cancel_task(task_id=child.task_id)
            else:
                access.cancel_tool_job(tool_job_id=child.tool_job_id)

    def bound_timeout(self, child_id, seconds):
        """Persist which budget bounds a child before I/O, including queue delay.

        asyncio timers can fire before UTC advances to their nominal deadline
        (notably with Windows' coarse clock). The recorded budget, not a later
        wall-clock comparison, identifies an exhausted workflow timeout.
        """
        for workflow in self.repository.list(active=True, limit=128):
            for index, run in enumerate(workflow.steps):
                for ordinal, attempt in enumerate(run.attempts):
                    if child_id not in {
                        attempt.dispatch_id,
                        attempt.task_id,
                        attempt.tool_job_id,
                    }:
                        continue
                    remaining = (
                        workflow.created_at
                        + timedelta(seconds=workflow.definition.timeout_seconds)
                        - now()
                    ).total_seconds()
                    if remaining < seconds and not attempt.deadline_limited:
                        attempt = attempt.model_copy(update={"deadline_limited": True})
                        run = run.model_copy(
                            update={
                                "attempts": (
                                    *run.attempts[:ordinal],
                                    attempt,
                                    *run.attempts[ordinal + 1 :],
                                )
                            }
                        )
                        self.repository.save(self.replace(workflow, index, run))
                    return min(seconds, max(0.000001, remaining))
        return seconds

    def child(self, workflow, step, attempt):
        if step.kind == "inference":
            try:
                return self.app.tasks.get_task(attempt.task_id or attempt.dispatch_id)
            except TaskNotFound:
                return None
        # The deterministic request identity survives child creation/linkage loss.
        jobs = self.app.specialist_service().repository
        from agentforge.specialists.models import SubmitToolJob

        arguments = SubmitToolJob(
            project_id=workflow.definition.project_id,
            tool_id=step.tool_id,
            input_artifact_ids=attempt.inputs["input_artifact_ids"],
            parameters=step.parameters,
            request_id=attempt.dispatch_id,
            idempotency_key="wf-" + attempt.dispatch_id.hex,
        )
        return jobs.existing(arguments, workflow.client_id)

    def dispatch(self, workflow, step, attempt):
        self.authorize(workflow, step)
        if (
            self.binding(workflow.definition.project_id, step)
            != workflow.bindings[workflow.definition.steps.index(step)]
        ):
            raise WorkflowError("binding_changed")
        # Re-resolve integrity/ownership immediately before consumption.
        if self.inputs(workflow, step) != attempt.inputs:
            raise WorkflowError("inputs_changed")
        if step.kind == "inference":
            return self.app.delegate_task(
                project_id=workflow.definition.project_id,
                agent_id=step.agent_id,
                worker_id=step.worker_id,
                task=attempt.inputs["request"],
                client_id=workflow.client_id,
                task_policy=step.task_policy,
                _dispatch_id=attempt.dispatch_id,
                _execution_limits=step.limits,
            )
        return self.app.specialist_service().submit(
            project_id=workflow.definition.project_id,
            tool_id=step.tool_id,
            input_artifact_ids=attempt.inputs["input_artifact_ids"],
            parameters=step.parameters,
            request_id=attempt.dispatch_id,
            idempotency_key="wf-" + attempt.dispatch_id.hex,
            client_id=workflow.client_id,
            _limits=step.limits,
        )

    def tick(self):
        """Called synchronously by TaskEngine; waiting uses zero execution slots."""
        deadlines = []
        for workflow in self.repository.list(active=True, limit=128):
            try:
                self.advance(workflow)
            except (
                WorkflowError,
                ServiceError,
                ProjectError,
                SpecialistError,
                ValueError,
                PolicyDenied,
                TaskValidationError,
            ) as error:
                # Fixed categories; never persist exception messages/provider data.
                current = self.repository.get(workflow.workflow_id)
                self.repository.save(
                    current.model_copy(
                        update={
                            "state": "cancelling"
                            if current.state == "cancelling"
                            else "blocked",
                            "failure_category": getattr(
                                error, "code", "authorization_or_input_invalid"
                            ),
                        }
                    )
                )

            current = self.repository.get(workflow.workflow_id)
            if current.state not in {"completed", "failed", "cancelled"}:
                deadline = current.created_at + timedelta(
                    seconds=current.definition.timeout_seconds
                )
                if deadline > now():
                    deadlines.append(deadline)
        # Only actual workflow deadlines wake an otherwise idle executor. Ordinary
        # Task-only Applications preserve their previous no-polling behavior.
        return (
            max(0.001, (min(deadlines) - now()).total_seconds()) if deadlines else None
        )

    def advance(self, workflow):
        original = workflow
        stopping = workflow.state in {
            "paused",
            "cancelling",
            "recovery_required",
            "blocked",
        }
        deadline = workflow.created_at + timedelta(
            seconds=workflow.definition.timeout_seconds
        )
        expired = now() >= deadline
        if expired and workflow.state != "cancelling":
            workflow = workflow.model_copy(
                update={"state": "cancelling", "failure_category": "workflow_timeout"}
            )
            stopping = True
        active = False
        for index, (step, run) in enumerate(
            zip(workflow.definition.steps, workflow.steps, strict=True)
        ):
            if not run.attempts or run.state in {"completed", "failed", "cancelled"}:
                continue
            attempt = run.attempts[-1]
            child = self.child(workflow, step, attempt)
            if child is None:
                if workflow.state == "cancelling":
                    workflow = self.replace(
                        workflow,
                        index,
                        run.model_copy(
                            update={
                                "state": "cancelled",
                                "attempts": (
                                    *run.attempts[:-1],
                                    attempt.model_copy(
                                        update={
                                            "state": "cancelled",
                                            "finished_at": now(),
                                        }
                                    ),
                                ),
                            }
                        ),
                    )
                continue
            child_id = child.task_id if step.kind == "inference" else child.tool_job_id
            fields = {
                "task_id" if step.kind == "inference" else "tool_job_id": child_id,
                "started_at": child.started_at,
                "finished_at": child.finished_at,
                "state": child.state,
            }
            failure = (
                child.reason if step.kind == "inference" else child.failure_category
            )
            if (
                attempt.deadline_limited
                and child.state == "failed"
                and failure in {"timeout", "specialist_timeout"}
            ):
                workflow = workflow.model_copy(
                    update={
                        "state": "cancelling",
                        "failure_category": "workflow_timeout",
                    }
                )
                stopping = True
            if child.state in {"queued", "running"}:
                active = True
                if workflow.state == "cancelling":
                    access = self.access(workflow.client_id)
                    if step.kind == "inference":
                        access.cancel_task(task_id=child_id)
                    else:
                        access.cancel_tool_job(tool_job_id=child_id)
            if child.state == "interrupted" or failure in {
                "execution_interrupted",
                "executor_cancelled",
            }:
                fields.update(
                    state="recovery_required", failure_category="recovery_required"
                )
            elif child.state == "completed":
                if step.kind == "inference":
                    fields["result_text"] = (child.final_answer or "")[:4000]
                if step.kind == "specialist":
                    fields["artifact_ids"] = child.output_artifact_ids
                elif step.required_validators:
                    result = child.coding_result
                    if result is None or any(
                        not any(
                            v.name == name
                            and v.exit_code == 0
                            and not v.timed_out
                            and not v.cancelled
                            for v in result.validation_runs
                        )
                        for name in step.required_validators
                    ):
                        fields.update(
                            state="failed", failure_category="validation_required"
                        )
            elif child.state in {"failed", "cancelled"}:
                fields["failure_category"] = failure or child.state
            attempt = attempt.model_copy(update=fields)
            run = run.model_copy(
                update={
                    "state": attempt.state,
                    "failure_category": attempt.failure_category,
                    "attempts": (*run.attempts[:-1], attempt),
                }
            )
            workflow = self.replace(workflow, index, run)
        workflow = self.repository.save(workflow) if workflow != original else workflow
        if workflow.state == "cancelling":
            if not active:
                self.repository.save(
                    workflow.model_copy(
                        update={
                            "state": "cancelled",
                            "finished_at": now(),
                            "steps": tuple(
                                s
                                if s.state in TERMINAL
                                else s.model_copy(update={"state": "cancelled"})
                                for s in workflow.steps
                            ),
                        }
                    )
                )
            return
        if stopping:
            return
        if any(s.state == "recovery_required" for s in workflow.steps):
            self.repository.save(
                workflow.model_copy(
                    update={
                        "state": "recovery_required",
                        "failure_category": "recovery_required",
                    }
                )
            )
            return
        # A workflow dispatches at most one child at a time. Different workflows
        # and ordinary Tasks/ToolJobs still share all configured execution slots.
        if active:
            self.repository.save(
                workflow.model_copy(
                    update={
                        "state": "running"
                        if any(s.state == "running" for s in workflow.steps)
                        else "queued"
                    }
                )
            )
            return
        for index, (step, run) in enumerate(
            zip(workflow.definition.steps, workflow.steps, strict=True)
        ):
            if run.state == "failed":
                safe = (
                    step.kind == "inference"
                    and self.app.agent_definition(step.agent_id).workspace_mode
                    == "project_readonly"
                ) or (
                    step.kind == "specialist"
                    and self.app.specialist_service().tools[step.tool_id].simulated
                )
                if (
                    safe
                    and len(run.attempts) < step.retry.max_attempts
                    and run.failure_category in step.retry.on
                ):
                    run = run.model_copy(
                        update={"state": "pending", "failure_category": None}
                    )
                    workflow = self.replace(workflow, index, run)
                else:
                    self.repository.save(
                        workflow.model_copy(
                            update={
                                "state": "failed",
                                "finished_at": now(),
                                "failure_category": run.failure_category,
                                "steps": tuple(
                                    s.model_copy(
                                        update={
                                            "state": "blocked",
                                            "failure_category": "dependency_failed",
                                        }
                                    )
                                    if step.step_id
                                    in workflow.definition.steps[i].depends_on
                                    and s.state == "pending"
                                    else s
                                    for i, s in enumerate(workflow.steps)
                                ),
                            }
                        )
                    )
                    return
            if run.state == "cancelled":
                self.repository.save(
                    workflow.model_copy(
                        update={
                            "state": "failed",
                            "finished_at": now(),
                            "failure_category": "child_cancelled",
                        }
                    )
                )
                return
            if run.state in {"completed", "blocked"}:
                continue
            if not all(
                next(s for s in workflow.steps if s.step_id == dep).state == "completed"
                for dep in step.depends_on
            ):
                continue
            self.authorize(workflow, step)
            if (
                self.binding(workflow.definition.project_id, step)
                != workflow.bindings[index]
            ):
                raise WorkflowError("binding_changed")
            if not run.attempts or run.attempts[-1].state == "failed":
                number = len(run.attempts) + 1
                inputs = self.inputs(workflow, step)
                dispatch_id = uuid5(workflow.workflow_id, f"{step.step_id}:{number}")
                effects = self.effects(step)
                scope_hash = digest(
                    {
                        "workflow": str(workflow.workflow_id),
                        "client": workflow.client_id,
                        "project": str(workflow.definition.project_id),
                        "step": step.model_dump(mode="json"),
                        "binding": workflow.bindings[index],
                        "inputs": inputs,
                        "attempt": number,
                        "effects": effects,
                    }
                )
                attempt = Attempt(
                    number=number,
                    dispatch_id=dispatch_id,
                    created_at=now(),
                    inputs=inputs,
                    scope_hash=scope_hash,
                    effects=effects,
                    state="approval" if effects else "pending",
                )
                run = run.model_copy(
                    update={
                        "state": attempt.state,
                        "attempts": (*run.attempts, attempt),
                    }
                )
                workflow = self.repository.save(self.replace(workflow, index, run))
            attempt = run.attempts[-1]
            if self.effects(step) and attempt.approved_at is None:
                self.repository.save(workflow.model_copy(update={"state": "approval"}))
                return
            child = self.dispatch(workflow, step, attempt)
            # A crash here is reconciled from the PRECOMMITTED dispatch identity.
            self.linked(workflow, index, run, child)
            return
        self.repository.save(
            workflow.model_copy(update={"state": "completed", "finished_at": now()})
        )

    def linked(self, workflow, index, run, child):
        attempt = run.attempts[-1].model_copy(
            update={
                "state": "queued",
                "task_id": getattr(child, "task_id", None),
                "tool_job_id": getattr(child, "tool_job_id", None),
            }
        )
        run = run.model_copy(
            update={"state": "queued", "attempts": (*run.attempts[:-1], attempt)}
        )
        self.repository.save(
            self.replace(workflow, index, run).model_copy(update={"state": "queued"})
        )
