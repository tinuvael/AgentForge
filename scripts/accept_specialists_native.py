"""Opt-in real-application acceptance through an existing authenticated Gateway.

This makes actual configured application calls. Never run automatically in CI.
"""

import argparse
import asyncio
import base64
import hashlib
import ipaddress
import json
import os
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agentforge.specialists.models import TERMINAL


async def acceptance(args):
    token = os.environ[args.token_env]
    url = urlsplit(args.gateway)
    try:
        loopback = ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        loopback = False
    if (
        url.username
        or url.password
        or url.query
        or url.fragment
        or url.scheme not in {"https", "http"}
        or (url.scheme == "http" and not loopback)
    ):
        raise ValueError("Gateway credentials require HTTPS or literal loopback HTTP")
    if (
        not token
        or not token.isascii()
        or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in token)
    ):
        raise ValueError("Gateway credential unavailable")
    async with (
        httpx.AsyncClient(
            headers={"Authorization": "Bearer " + token},
            follow_redirects=False,
            trust_env=False,
            timeout=30,
        ) as http,
        streamable_http_client(args.gateway, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        async def call(name, **fields):
            result = await session.call_tool(name, fields)
            if result.isError:
                raise RuntimeError("Authorized MCP operation failed: " + name)
            return result.structuredContent

        async def submit(tool, inputs, parameters):
            fields = dict(
                project_id=str(args.project),
                tool_id=tool,
                input_artifact_ids=inputs,
                parameters=parameters,
                request_id=str(uuid4()),
                idempotency_key=uuid4().hex,
            )
            initial = await call("submit_tool_job", **fields)
            print(json.dumps(initial), flush=True)
            duplicate = await call("submit_tool_job", **fields)
            assert initial["tool_job_id"] == duplicate["tool_job_id"]
            identity = initial["tool_job_id"]
            while True:
                job = await call("get_tool_job", tool_job_id=identity)
                if job["state"] in TERMINAL:
                    break
                await asyncio.sleep(1)
            print(json.dumps(job, indent=2))
            if job["state"] != "completed" or job["simulated"]:
                raise RuntimeError(
                    "Real application did not complete; retain job ID for recovery"
                )
            artifact_id = job["output_artifact_ids"][0]
            descriptor = await call(
                "inspect_artifact",
                project_id=str(args.project),
                artifact_id=artifact_id,
            )
            assert descriptor["status"] == "ready" and not descriptor["simulated"]
            digest, offset = hashlib.sha256(), 0
            while True:
                chunk = await call(
                    "retrieve_artifact",
                    project_id=str(args.project),
                    artifact_id=artifact_id,
                    offset=offset,
                    max_bytes=49152,
                )
                digest.update(base64.b64decode(chunk["data_base64"], validate=True))
                if chunk["next_offset"] is None:
                    break
                offset = chunk["next_offset"]
            assert digest.hexdigest() == descriptor["sha256"]
            print(json.dumps(descriptor, indent=2))
            return artifact_id

        image = str(args.input_artifact) if args.input_artifact else None
        async with asyncio.timeout(args.timeout):
            if args.image_tool:
                image = await submit(
                    args.image_tool, [], {"seed": args.seed, "text": args.text}
                )
            if args.blender_tool:
                if not image:
                    raise ValueError("Blender needs --input-artifact or --image-tool")
                await submit(
                    args.blender_tool,
                    [image],
                    {"operation": "reference_blockout", "depth": 0.25},
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway", required=True, help="Existing protected Gateway /mcp URL"
    )
    parser.add_argument("--project", required=True, type=UUID)
    parser.add_argument("--token-env", default="AGENTFORGE_ACCEPTANCE_TOKEN")
    parser.add_argument("--image-tool")
    parser.add_argument("--blender-tool")
    parser.add_argument("--input-artifact", type=UUID)
    parser.add_argument("--seed", default=0, type=int)
    parser.add_argument("--text", default="a simple reference object")
    parser.add_argument("--timeout", default=600, type=int)
    args = parser.parse_args()
    if not args.image_tool and not args.blender_tool:
        parser.error("Select an explicit real tool")
    asyncio.run(acceptance(args))


if __name__ == "__main__":
    main()
