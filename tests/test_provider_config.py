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
    assert str(caught.value) == "Required Provider authentication is unavailable"


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


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.invalid/v1",
        "http://192.168.1.2/v1",
        "http://10.0.0.2/v1",
        "http://[fd00::1]/v1",
        "http://localhost.remote.invalid/v1",
        "http://localhost./v1",
    ],
)
def test_authenticated_non_loopback_requires_https(tmp_path, monkeypatch, url):
    monkeypatch.setenv("AGENTFORGE_TEST_TOKEN", SECRET)
    path = config_file(tmp_path)
    path.write_text(
        path.read_text()
        .replace("https://gateway.invalid/v1/", url)
        .replace("\n[[workers]]", '\napi_key_env="AGENTFORGE_TEST_TOKEN"\n[[workers]]')
    )
    with pytest.raises(ConfigurationError, match="HTTPS") as caught:
        load_workers(path)
    assert SECRET not in str(caught.value) and url not in str(caught.value)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "[::1]", "localhost"])
def test_authenticated_loopback_development_allowed(tmp_path, monkeypatch, host):
    monkeypatch.setenv("AGENTFORGE_TEST_TOKEN", SECRET)
    path = config_file(tmp_path)
    path.write_text(
        path.read_text()
        .replace("https://gateway.invalid/v1/", f"http://{host}:8000/v1")
        .replace("\n[[workers]]", '\napi_key_env="AGENTFORGE_TEST_TOKEN"\n[[workers]]')
    )
    assert load_workers(path).providers[0].bearer_token() == SECRET


def test_universal_example_needs_independent_secrets(monkeypatch):
    from pathlib import Path

    path = Path(__file__).parents[1] / "config/workers.universal.example.toml"
    monkeypatch.setenv("AGENTFORGE_SERVICE_A_KEY", "SYNTHETIC_A")
    monkeypatch.delenv("AGENTFORGE_SERVICE_B_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="authentication is unavailable"):
        load_workers(path)
    monkeypatch.setenv("AGENTFORGE_SERVICE_B_KEY", "SYNTHETIC_B")
    config = load_workers(path)
    assert len(config.providers) == 3 and len(config.workers) == 4
    assert [w.provider_connection for w in config.workers] == [
        "service-a",
        "service-a",
        "service-b",
        "local",
    ]
    assert [p.bearer_token() for p in config.providers] == [
        "SYNTHETIC_A",
        "SYNTHETIC_B",
        None,
    ]
    assert "SYNTHETIC" not in repr(config) + config.model_dump_json()


def test_cli_connection_errors_are_actionable_and_secret_safe(tmp_path, capsys):
    from agentforge.cli import main

    path = config_file(tmp_path)
    path.write_text(
        path.read_text().replace(
            "https://gateway.invalid/v1/", f"https://user:{SECRET}@gateway.invalid"
        )
    )
    assert main(["worker", "config-check", "--workers", str(path)]) == 2
    output = capsys.readouterr().err
    assert "HTTPS" in output and "secret environment variables" in output
    assert SECRET not in output and "gateway.invalid" not in output
