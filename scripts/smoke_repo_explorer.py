"""Manual opt-in Ollama smoke test. Never imported or run by automated tests."""

import argparse
import asyncio

from agentforge.agents import REPO_EXPLORER, AgentRuntime, repository_toolset
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.index import IndexRepository
from agentforge.db.projects import ProjectRepository
from agentforge.index.service import ProjectIndex
from agentforge.projects.service import ProjectRegistry
from agentforge.providers.ollama import OllamaProvider
from agentforge.tools.service import RepositoryTools
from agentforge.workers.config import load_workers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--workers", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument(
        "--task",
        default=(
            "Explain how ProjectRegistry.open_root protects repository access. "
            "Inspect the implementation and relevant tests before answering."
        ),
    )
    arguments = parser.parse_args()
    engine = create_database_engine(arguments.database_url)
    try:
        sessions = create_session_factory(engine)
        projects = ProjectRegistry(ProjectRepository(sessions))
        index = ProjectIndex(projects, IndexRepository(sessions))
        runtime = AgentRuntime(
            projects=projects,
            workers=load_workers(arguments.workers),
            agents=[REPO_EXPLORER],
            providers={"ollama": OllamaProvider()},
            tools=repository_toolset(index, RepositoryTools(projects)),
        )
        result = asyncio.run(
            runtime.run(
                project_id=arguments.project_id,
                worker_id=arguments.worker_id,
                agent_id="repo_explorer",
                task=arguments.task,
            )
        )
        print(result.model_dump_json(indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
