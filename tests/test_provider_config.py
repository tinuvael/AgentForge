"""Offline connection resolution, inline/named configuration and secret isolation."""

import pytest

from agentforge.providers.factory import create_providers
from agentforge.workers.config import ConfigurationError, load_workers

SECRET = "PRIVATE_TEST_BEARER_VALUE"


def config_file(tmp_path, *, provider="remote", connection=None, worker_extra=""):
    path = tmp_path / "workers.toml"
    path.write_text(
        (
            connection
            if connection is not None
            else """[[providers]]
id = "remote"
type = "openai_compatible"
base_url = "https://gateway.invalid/v1/"
"""
        )
        + f'''\n[[workers]]
id = "worker"
provider = "{provider}"
model = "configured-model"
{worker_extra}
'''
    )
    return path


def test_preferred_no_auth_and_factual_capabilities(tmp_path):
    config = load_workers(config_file(tmp_path))
    worker = config.workers[0]
    assert worker.provider == "openai_compatible"
    assert worker.provider_connection == "remote" and worker.endpoint is None
    assert worker.supports_tools is None and not worker.supports_streaming
    assert config.providers[0].bearer_token() is None
    assert "gateway.invalid" not in repr(config)


def test_auth_from_environment_not_worker_or_repr(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_TEST_TOKEN", SECRET)
    path = config_file(tmp_path)
    path.write_text(
        path.read_text().replace(
            "\n[[workers]]", '\napi_key_env = "AGENTFORGE_TEST_TOKEN"\n[[workers]]'
        )
    )
    config = load_workers(path)
    provider = create_providers(config)["remote"]
    assert SECRET not in repr(provider) + repr(config) + config.model_dump_json()
    assert "api_key" not in config.workers[0].model_dump_json()
    assert provider._http is None


@pytest.mark.parametrize(
    "value", [None, "", "  ", "Bearer value", "line\nbreak", "非ascii"]
)
def test_missing_or_invalid_required_auth_is_safe(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("AGENTFORGE_TEST_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AGENTFORGE_TEST_TOKEN", value)
    path = config_file(tmp_path)
    path.write_text(
        path.read_text().replace(
            "\n[[workers]]", '\napi_key_env = "AGENTFORGE_TEST_TOKEN"\n[[workers]]'
        )
    )
    with pytest.raises(ConfigurationError) as caught:
        load_workers(path)
    assert "AGENTFORGE_TEST_TOKEN" not in str(caught.value)
    assert (
        repr(caught.value) == "ConfigurationError("
        "'Invalid Worker/Provider configuration or authentication')"
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/backend",
        "ftp://gateway.invalid",
        "gateway.invalid",
        "http://",
        f"https://user:{SECRET}@gateway.invalid",
        f"https://gateway.invalid?key={SECRET}",
        f"https://gateway.invalid#{SECRET}",
    ],
)
def test_invalid_endpoints_do_not_echo_credentials(tmp_path, url):
    path = config_file(tmp_path)
    path.write_text(path.read_text().replace("https://gateway.invalid/v1/", url))
    with pytest.raises(ConfigurationError) as caught:
        load_workers(path)
    assert SECRET not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace('type = "openai_compatible"', 'type = "unknown"'),
        lambda text: text.replace('provider = "remote"', 'provider = "unknown"'),
        lambda text: text + text.split("[[workers]]")[0],
        lambda text: (
            text + '\n[[workers]]\nid="worker"\nprovider="remote"\nmodel="other"'
        ),
        lambda text: text + '\napi_key="PRIVATE_TEST_BEARER_VALUE"',
        lambda text: text.replace(
            'provider = "remote"',
            'provider = "remote"\nendpoint="https://inline.invalid"',
        ),
        lambda text: "broken = [",
    ],
)
def test_invalid_configuration_is_safe(tmp_path, mutation):
    path = config_file(tmp_path)
    path.write_text(mutation(path.read_text()))
    with pytest.raises(ConfigurationError) as caught:
        load_workers(path)
    assert SECRET not in str(caught.value) + repr(caught.value)


def test_inline_ollama_and_named_ollama_share_config_file(tmp_path):
    path = config_file(
        tmp_path,
        provider="local",
        connection="""[[providers]]
id = "local"
type = "ollama"
base_url = "http://lan.invalid:11434"
""",
    )
    path.write_text(
        path.read_text()
        + """\n[[workers]]
id = "inline"
provider = "ollama"
endpoint = "http://inline.invalid:11434"
model = "inline-model"
"""
    )
    config = load_workers(path)
    assert [w.provider for w in config.workers] == ["ollama", "ollama"]
    assert config.workers[0].endpoint is None
    assert str(config.workers[1].endpoint) == "http://inline.invalid:11434/"
    assert set(create_providers(config)) == {"local", "ollama"}


def test_worker_requires_one_connection_binding():
    from pydantic import ValidationError

    from agentforge.core.worker import Worker

    for binding in (
        {},
        {"endpoint": None},
        {"endpoint": "http://backend.invalid", "provider_connection": "remote"},
    ):
        with pytest.raises(ValidationError):
            Worker(id="worker", provider="ollama", model="model", **binding)


def test_example_named_connections_load_without_environment(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.delenv("AGENTFORGE_REMOTE_API_KEY", raising=False)
    path = Path(__file__).parents[1] / "config" / "workers.providers.example.toml"
    config = load_workers(path)
    assert len(config.providers) == 3 and len(config.workers) == 3
    assert [w.provider for w in config.workers] == [
        "ollama",
        "openai_compatible",
        "openai_compatible",
    ]
