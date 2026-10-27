"""Operator-only receipt recovery. Stop the other database owner before running."""

import argparse
import asyncio
from uuid import UUID

from agentforge.application.service import Application


async def recover(args):
    app = Application.from_config(
        database_url=args.database_url,
        workers_path=args.workers,
        specialists_path=args.specialists,
    )
    await app.start()
    try:
        job = await app.specialist_service().reconcile_remote(
            tool_job_id=args.job, remote_job_id=args.receipt
        )
        print(job.model_dump_json())
        while job.state in {"queued", "running"}:
            await asyncio.sleep(0.2)
            job = app.get_tool_job(tool_job_id=args.job)
        print(job.model_dump_json())
    finally:
        await app.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--workers", required=True)
    parser.add_argument("--specialists", required=True)
    parser.add_argument("--job", type=UUID, required=True)
    parser.add_argument("--receipt", type=UUID, required=True)
    asyncio.run(recover(parser.parse_args()))


if __name__ == "__main__":
    main()
