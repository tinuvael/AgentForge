"""Actual process-loss checkpoint; the parent verifies recovery without replay."""

import asyncio
import json
import os
import sys
from uuid import UUID, uuid4

from agentforge.application.service import Application


async def main():
    database, workers, specialists, project, stage = sys.argv[1:]
    app = Application.from_config(
        database_url=database, workers_path=workers, specialists_path=specialists
    )
    await app.start()
    files = app.specialists.files
    original = files.write
    job = app.submit_tool_job(
        project_id=UUID(project),
        tool_id="image",
        request_id=uuid4(),
        idempotency_key="crash",
    )

    def crash(identity, data):
        if stage == "published":
            original(identity, data)
        else:
            path = files.path / files.name(identity, pending=True)
            with path.open("xb") as stream:
                stream.write(data[:16])
                stream.flush()
                os.fsync(stream.fileno())
        print(
            json.dumps({"job": str(job.tool_job_id), "artifact": str(identity)}),
            flush=True,
        )
        os._exit(73)

    files.write = crash
    await asyncio.Event().wait()


asyncio.run(main())
