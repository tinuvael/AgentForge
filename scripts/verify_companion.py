"""Offline real stdio + loopback HTTP smoke (usable against an installed wheel)."""

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


def verify_companion(database_url, workers_path):
    # Reserve a free loopback port; startup failure is explicit if raced.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with TemporaryDirectory() as directory:
        with (Path(directory) / "stderr.log").open("w+") as diagnostics:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "agentforge",
                    "mcp",
                    "--database-url",
                    str(database_url),
                    "--workers",
                    str(workers_path),
                    "--companion",
                    "--companion-port",
                    str(port),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=diagnostics,
                text=True,
            )
            try:

                def request(identity, method, params=None):
                    payload = {"jsonrpc": "2.0", "id": identity, "method": method}
                    if params is not None:
                        payload["params"] = params
                    process.stdin.write(json.dumps(payload) + "\n")
                    process.stdin.flush()
                    # Every line must be MCP JSON-RPC; a banner/log breaks this test.
                    response = json.loads(process.stdout.readline())
                    assert response["jsonrpc"] == "2.0" and response["id"] == identity
                    assert "error" not in response, response
                    return response["result"]

                request(
                    1,
                    "initialize",
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "companion-smoke", "version": "1"},
                    },
                )
                process.stdin.write(
                    '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                )
                process.stdin.flush()
                opener = build_opener(ProxyHandler({}))  # Local HTTP, never a Provider.
                for path in (
                    "/",
                    "/workers",
                    "/projects",
                    "/tasks",
                    "/councils",
                    "/companion",
                    "/companion/fragment",
                    "/static/companion.css",
                    "/static/companion.js",
                ):
                    with opener.open(
                        f"http://127.0.0.1:{port}{path}", timeout=5
                    ) as response:
                        assert response.status == 200
                        assert response.read()
                    status = request(
                        2, "tools/call", {"name": "agentforge_status", "arguments": {}}
                    )
                    assert status["structuredContent"]["available"]
                tools = request(3, "tools/list")["tools"]
                assert "watch_task" in {tool["name"] for tool in tools}
                from agentforge.mcp.server import TOOL_CONTRACTS

                assert {tool["name"] for tool in tools} == {
                    t.name for t in TOOL_CONTRACTS
                }
                try:
                    opener.open(
                        Request(
                            f"http://127.0.0.1:{port}/companion",
                            headers={"Host": "evil.invalid"},
                        ),
                        timeout=5,
                    )
                except HTTPError as error:
                    assert error.code == 400
                else:
                    raise AssertionError("Host protection missing")
                try:
                    opener.open(
                        f"http://127.0.0.1:{port}/companion/tasks/invalid-PRIVATE",
                        timeout=5,
                    )
                except HTTPError as error:
                    assert error.code == 422
                    assert b"invalid-PRIVATE" not in error.read()
                else:
                    raise AssertionError("UUID validation missing")
                request(4, "tools/call", {"name": "agentforge_status", "arguments": {}})
                process.stdin.close()
                assert process.wait(timeout=8) == 0
                assert process.stdout.read() == ""
                diagnostics.seek(0)
                log = diagnostics.read()
                assert "Traceback" not in log and "http://" not in log
                assert "invalid-PRIVATE" not in log
                # A TIME_WAIT socket may prevent rebinding; test the listener itself.
                with socket.socket() as listener:
                    listener.settimeout(2)
                    assert listener.connect_ex(("127.0.0.1", port)) != 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
    print(
        "Combined MCP/Companion: valid stdio, concurrent HTTP, "
        "Host and EOF shutdown: OK"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--workers", required=True)
    args = parser.parse_args()
    verify_companion(args.database_url, args.workers)
