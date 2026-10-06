"""Validate concrete targets without discovering or selecting workers."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentforge.core.inference import GenerationRequest, Message
from agentforge.core.worker import Worker
from agentforge.workers.config import WorkersConfig, load_workers


def worker_data(**changes):
    return {
        "id": "cpu-test",
        "provider": "ollama",
        "model": "arbitrary-model:small",
        "endpoint": "http://inference.invalid:11434",
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"id": "   "},
        {"model": ""},
        {"provider": ""},
        {"endpoint": "file:///tmp/model"},
        {"endpoint": "http://user:secret@inference.invalid"},
        {"endpoint": "http://inference.invalid?api_key=secret"},
        {"endpoint": "http://inference.invalid#secret"},
        {"context_window": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": float("inf")},
        {"options": {"temperature": float("nan")}},
        {"routing_policy": "automatic"},
    ],
)
def test_worker_rejects_invalid_configuration(changes):
    with pytest.raises(ValidationError):
        Worker(**worker_data(**changes))


def test_worker_capabilities_are_explicit():
    worker = Worker(**worker_data())
    assert worker.context_window is None
    assert worker.supports_tools is None
    assert worker.supports_streaming is False
    assert worker.deployment_label is None
    configured = Worker(
        **worker_data(
            context_window=4096,
            supports_streaming=True,
            supports_tools=False,
            deployment_label="test machine",
        )
    )
    assert configured.context_window == 4096
    assert configured.supports_tools is False
    assert configured.deployment_label == "test machine"


def test_multiple_workers_share_provider_type():
    config = WorkersConfig(
        workers=[
            Worker(**worker_data()),
            Worker(**worker_data(id="other", model="another:tag")),
        ]
    )
    assert len(config.workers) == 2
    assert {worker.provider for worker in config.workers} == {"ollama"}
    assert config.workers[0].model != config.workers[1].model


def test_duplicate_ids_and_unknown_config_fields_rejected():
    worker = Worker(**worker_data())
    with pytest.raises(ValidationError, match="unique"):
        WorkersConfig(workers=[worker, worker])
    with pytest.raises(ValidationError):
        WorkersConfig(workers=[], default_worker="automatic")


def test_load_example_configuration():
    example = Path(__file__).parents[1] / "config" / "workers.example.toml"
    worker = load_workers(example).workers[0]
    assert worker.id == "local-4080"
    assert worker.context_window == 32768
    assert worker.options == {"temperature": 0.1}
    assert worker.supports_streaming


def test_load_multiple_workers_from_explicit_file(tmp_path):
    path = tmp_path / "workers.toml"
    path.write_text(
        "\n".join(
            f'''[[workers]]
 id = "{name}"
 provider = "ollama"
 model = "{name}-model"
 endpoint = "http://{name}.invalid"
 '''
            for name in ("one", "two")
        )
    )
    assert [worker.id for worker in load_workers(path).workers] == ["one", "two"]


@pytest.mark.parametrize(
    "changes",
    [
        {"messages": []},
        {"temperature": -0.1},
        {"temperature": float("nan")},
        {"timeout_seconds": 0},
    ],
)
def test_request_validation(changes):
    data = {"messages": [Message(role="user", content="hello")], **changes}
    with pytest.raises(ValidationError):
        GenerationRequest(**data)
