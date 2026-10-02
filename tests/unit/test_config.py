# CMN-C2-284 - Unit tests: config/agent.yaml manifest + config/config.yaml runtime sanity.
#
# The manifest is FLAT: the registry reads every key at root level, so an
# `agent:` block would make the agent unloadable on the platform even though a
# directly-constructed graph still works locally. Runtime parameters live in
# config/config.yaml and are read at run time, not from the manifest.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_CONFIG_DIR = pathlib.Path(__file__).parents[2] / "config"
_MANIFEST_PATH = _CONFIG_DIR / "agent.yaml"
_RUNTIME_PATH = _CONFIG_DIR / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _manifest():
    return yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _runtime():
    return yaml.safe_load(_RUNTIME_PATH.read_text(encoding="utf-8"))


def test_manifest_identity():
    data = _manifest()
    assert data["id"] == "CMN-C2-284"
    assert data["category"] == "Cat 2"
    assert data["industry"] == "CMN"
    assert data["namespace"] == "cmn"
    assert data["base_type"] == "ToolCallingAgent"
    assert data["generation_mode"] == "deterministic"


def test_manifest_is_flat():
    """No `agent:` block, and the entry point is a single dotted path."""
    data = _manifest()
    assert "agent" not in data
    assert data["class"] == "src.graph.graph.LineWorksMessageAgent"
    assert "module" not in data


def test_manifest_security():
    data = _manifest()
    # Agent-level entry trust, enforced by the outer backbone pre_process gate
    # (VERIFIED_EXTERNAL); inner domain nodes stay ANONYMOUS.
    assert data["required_trust_level"] == "VERIFIED_EXTERNAL"


def test_manifest_declares_no_compile_time_requirements():
    """`requires` declares only what compile time must provision.

    The integration token is read with ctx.secrets.get() and the pipeline runs
    without it on the bundled network-free transport, so it is NOT a
    compile-time requirement. Declaring an unprovisioned secret here would make
    the platform refuse to load the agent instead of running it.
    """
    requires = _manifest()["requires"]
    assert requires["secrets"] == []
    assert requires["extras"] == []


def test_runtime_config_parameters():
    runtime = _runtime()
    assert isinstance(runtime["max_retry"], int)
    assert isinstance(runtime["timeout_s"], int)


def test_runtime_config_lineworks_section():
    """Read at run time by the graph, not by the manifest loader."""
    runtime = _runtime()
    assert runtime["lineworks"]["base_url"] == "https://www.worksapis.com/v1.0"
    assert runtime["lineworks"]["bot_id"] == "stub-bot"
