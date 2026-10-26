"""Classic self-hosted ComfyUI API. No interrupts, redirects or submission retries."""

import asyncio
import copy
import hashlib
import json
import os
import re
from uuid import UUID

import httpx

from agentforge.specialists.adapters import Output
from agentforge.specialists.models import SpecialistError


class ComfyUI:
    def __init__(self, tool, context, *, transport=None):
        self.tool, self.context, self.transport = tool, context, transport
        self.config = tool.comfyui

    async def request(self, client, method, path, *, cap=None, **kwargs):
        cap = cap or self.config.max_response_bytes
        async with client.stream(method, path, **kwargs) as response:
            # Reject every redirect, including same-origin redirects. No body or
            # Location enters errors; credentials never leave the configured origin.
            if (
                response.status_code != 200
                or response.headers.get("content-encoding", "identity") != "identity"
            ):
                raise SpecialistError("comfyui_http_failed")
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                if len(data) + len(chunk) > cap:
                    raise SpecialistError("comfyui_response_limit")
                data.extend(chunk)
            return bytes(data)

    async def document(self, *args, **kwargs):
        try:
            value = json.loads(await self.request(*args, **kwargs))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, RecursionError):
            raise SpecialistError("comfyui_response_invalid") from None

    async def verify_receipt(self, receipt):
        """Operator-only recovery of the acceptance/receipt crash window."""
        headers = self.headers()
        async with httpx.AsyncClient(
            base_url=str(self.config.base_url),
            headers=headers,
            follow_redirects=False,
            trust_env=False,
            timeout=self.config.request_timeout_seconds,
            transport=self.transport,
        ) as client:
            history = await self.document(client, "GET", "/history/" + str(receipt))
            queue = await self.document(client, "GET", "/queue")
            candidates = [
                entry.get("prompt")
                for identity, entry in history.items()
                if identity == str(receipt) and isinstance(entry, dict)
            ]
            candidates += queue.get("queue_running", []) + queue.get(
                "queue_pending", []
            )
            prefix = "agentforge_" + self.context.job.tool_job_id.hex
            for item in candidates:
                try:
                    if (
                        item[1] == str(receipt)
                        and item[3]["client_id"] == str(self.context.job.tool_job_id)
                        and item[2][self.config.output_node]["inputs"][
                            "filename_prefix"
                        ]
                        == prefix
                    ):
                        return
                except (TypeError, IndexError, KeyError):
                    continue
        raise SpecialistError("recovery_receipt_unverified")

    def headers(self):
        headers = {"Accept-Encoding": "identity"}
        if self.config.api_key_env:
            token = os.environ.get(self.config.api_key_env, "")
            if (
                not token
                or not token.isascii()
                or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in token)
            ):
                raise SpecialistError("comfyui_auth_unavailable")
            headers["Authorization"] = "Bearer " + token
        return headers

    async def run(self, parameters, inputs):
        config, context = self.config, self.context
        headers = self.headers()
        prefix = "agentforge_" + context.job.tool_job_id.hex
        workflow = copy.deepcopy(json.loads(config.workflow_json))
        for node in workflow.values():
            node.pop("_meta", None)
        for binding, value in (
            (config.seed_field, parameters.seed),
            (config.text_field, parameters.text),
        ):
            if binding:
                workflow[binding.node]["inputs"][binding.field] = value
        workflow[config.output_node]["inputs"][config.output_prefix_field] = prefix
        async with httpx.AsyncClient(
            base_url=str(config.base_url),
            headers=headers,
            follow_redirects=False,
            trust_env=False,
            timeout=config.request_timeout_seconds,
            transport=self.transport,
        ) as client:
            receipt = context.job.execution.remote_job_id
            try:
                if receipt is None:
                    if context.job.execution.phase != "not_submitted":
                        raise SpecialistError("recovery_required")
                    body = {
                        "prompt": workflow,
                        "client_id": str(context.job.tool_job_id),
                    }
                    digest = hashlib.sha256(
                        json.dumps(body, sort_keys=True).encode()
                    ).hexdigest()
                    # A crash after this commit is ambiguous, even if no bytes left
                    # this process. Classic /prompt offers no idempotency contract.
                    context.record(
                        phase="submission_unknown",
                        intent_sha256=digest,
                        remote_may_continue=True,
                    )
                    result = await self.document(client, "POST", "/prompt", json=body)
                    try:
                        receipt = UUID(result["prompt_id"])
                    except (ValueError, TypeError, KeyError, AttributeError):
                        raise SpecialistError("recovery_required") from None
                    context.record(phase="accepted", remote_job_id=receipt)
                while True:
                    history = await self.document(
                        client, "GET", "/history/" + str(receipt)
                    )
                    if str(receipt) in history:
                        entry = history[str(receipt)]
                        status = entry.get("status", {})
                        if status.get("status_str") == "error":
                            context.record(
                                remote_state="failed", remote_may_continue=False
                            )
                            raise SpecialistError("comfyui_execution_failed")
                        if (
                            status.get("completed") is True
                            and status.get("status_str") == "success"
                        ):
                            context.record(
                                remote_state="completed", remote_may_continue=False
                            )
                            images = (
                                entry.get("outputs", {})
                                .get(config.output_node, {})
                                .get("images", [])
                            )
                            if not isinstance(images, list) or len(images) != 1:
                                raise SpecialistError("comfyui_output_invalid")
                            ref = images[0]
                            filename = ref.get("filename", "")
                            if (
                                ref.get("type") != "output"
                                or ref.get("subfolder") != ""
                                or not isinstance(filename, str)
                                or not re.fullmatch(
                                    re.escape(prefix) + r"_[0-9]{5,}_[.]png", filename
                                )
                            ):
                                raise SpecialistError("comfyui_output_invalid")
                            data = await self.request(
                                client,
                                "GET",
                                "/view",
                                cap=self.tool.limits.max_output_bytes,
                                params={
                                    "filename": filename,
                                    "subfolder": "",
                                    "type": "output",
                                },
                            )
                            return (
                                Output("generated-image.png", "png", "image/png", data),
                            )
                    queue = await self.document(client, "GET", "/queue")
                    state = "unknown"
                    for key, label in (
                        ("queue_pending", "queued"),
                        ("queue_running", "running"),
                    ):
                        items = queue.get(key, [])
                        if not isinstance(items, list):
                            raise SpecialistError("comfyui_response_invalid")
                        if any(
                            isinstance(item, list)
                            and len(item) > 1
                            and item[1] == str(receipt)
                            for item in items
                        ):
                            state = label
                    context.record(remote_state=state)
                    # Missing receipts/history are not proof of non-execution.
                    await asyncio.sleep(config.poll_seconds)
            except httpx.HTTPError:
                raise SpecialistError("recovery_required") from None
            except (TypeError, KeyError, AttributeError):
                raise SpecialistError("comfyui_response_invalid") from None
