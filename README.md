# CMN-C2-284 — LINE WORKS Message Agent

> **Category**: Cat 2 (domain-specific multi-step pipeline)
> **Industry**: CMN (cross-industry)

## Overview

Turns a plain-language request into a messaging action against the
[LINE WORKS](https://line.worksmobile.com/) Bot REST API: it sends a message to a talk room,
looks up the delivery status of a message it sent, or lists a room's recent activity, and returns
a confirmation naming the message or room it touched. Requests arrive as free text ("Send a note
to channel ch-101", "ms-204 の配信ステータスを確認して"), optionally with a structured target and
message body supplied alongside them, and the pipeline classifies the intent, resolves the target,
assembles the API request body, calls LINE WORKS, and formats the confirmation.

Two safety properties are built in rather than bolted on. **Nothing is invented**: the channel and
message ids come only from the caller's structured data or an explicit mention in the request, and
an unresolved id is left empty and reported rather than guessed - so the agent cannot post into the
wrong room. **A vague request never writes**: low-confidence classification falls back to the
read-only delivery lookup.

Structured request data travels in its own channel (`input_context`) rather than inside the request
text. That is not a convenience: the framework masks personal data in the text channel at every
step, so a message body written into the prose arrives at the API call already rewritten. The
structured channel is validated field by field - bounded lengths, an inert character set, and a
finite+bounded parser for every number - and only then carried through to the send.

The bundled LINE WORKS transport is a deterministic, network-free stub, so the pipeline runs and
tests end-to-end out of the box without a live LINE WORKS tenant. A real deployment injects
`get` / `post` transports at client construction; the request and response shapes already follow
the documented LINE WORKS Bot API message endpoints, so no pipeline change is needed.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Provided by the platform environment, not resolved from the default package index. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit and boundary tests
config/       agent manifest (agent.yaml) and runtime parameters (config.yaml)
docs/         design specification and test specification
```

- `docs/02_design.md` - node-by-node design, the caller-data contract and the output contract.
- `docs/03_test_spec.md` - what each test covers and how the suite is run.

## Customising

1. Point `config/config.yaml` at your own LINE WORKS tenant (`lineworks.base_url`,
   `lineworks.bot_id`) and adjust the runtime parameters (`max_retry`, `timeout_s`).
2. Inject live `get` / `post` transports into `LineWorksClient` in place of the bundled
   network-free stub, and provision the `LINEWORKS_TOKEN` secret the live path requires.
3. Review the node implementations under `src/nodes/` for domain-specific logic - in particular
   the intent keywords and the caller-data contract enforced by `src/nodes/pre_process_node.py`.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
