# Test Specification - CMN-C2-284 LINE WORKS Message Agent

## Test Strategy
- Test types: Unit (per node + service + config + inner graph + the caller-data
  contract) / Proof-of-Boundary (full outer-graph invoke, the real ASGI
  `/invoke` entry point, output containment, import isolation, state safety,
  server boot, HITL stub).
- Location: `tests/unit/`, `tests/proof_of_boundary/` (`tests/integration/` is an
  empty package; end-to-end coverage lives in the proof-of-boundary suite, which
  drives the real compiled graph and the real HTTP adapter).
- The LINE WORKS call is exercised through the deterministic, network-free stub
  transport (default) and through injected fake clients; no live LINE WORKS call.
- **Trust-gate routing canon**: every per-node unit test invokes the node as
  `node(state)` - `BaseNode.__call__` routes the full security pipeline (trust
  gate -> input mask -> `execute()` -> credential scan) - never bare
  `node.execute(state)`. State builders set `caller_trust_level =
  TrustLevel.VERIFIED_EXTERNAL.value` for PreProcessNode (the single external
  gate) and `TrustLevel.ANONYMOUS.value` for every other node. The documented
  exceptions, each with the reason stated at the test: the
  `CallLineWorksApiNode.execute(state, config=...)` config-override test (a 2nd
  argument `__call__` cannot forward); the PostProcessNode error-path tests
  (`__call__` short-circuits on an already-errored state, so the branch under
  test only runs on a direct call); and the caller-data contract module, which
  calls `execute()` directly on purpose - the guarantee under test is the
  TEMPLATE's own, and an end-to-end refusal cannot tell you which layer refused.
  The trust rejection test asserts on the RETURNED error dict (`status ==
  AgentStatus.ERROR.value`, "trust gate denied" in `error_log`, execute-only keys
  absent) - `__call__` never raises for a trust denial.
- Assertion contract: the invoke surface is `result["output"]` / `status` /
  `trace_id` / `correlation_id` / `node_history` (never `formatted_output` at the
  invoke surface); status is compared to `AgentStatus.SUCCESS`/`.value`
  (lowercase `success`/`error`); the outer graph is called as
  `invoke(user_input=..., ctx=..., input_context=...)`; identifiers may be masked
  (`[MASKED]`) so record evidence is asserted by presence, not raw repr; audit
  spies assert on `call.args[1]` (the event payload), never the whole-call repr.
- Framework pipeline behaviours encoded by the suite: `__call__` short-circuits
  on an incoming errored state (execute() is skipped; error status/error_log pass
  through); the framework's input mask rewrites Title-Case bigrams (across
  newlines), emails, and digit groups in `user_input`/`validated_input` to
  `[MASKED]` before `execute()` sees the text - positive payloads are PII-free
  (LINE WORKS short ids like ch-101 / ms-204, no `@`), intentional-PII tests
  assert the `[MASKED]` path.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).
- Security assertions are BEHAVIOURAL: a refusal is asserted as error status with
  nothing carried forward, never as a particular gate's wording.

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations | denial RETURNS an error dict (`status == AgentStatus.ERROR.value`, "trust gate denied" in error_log, execute-only keys absent); VERIFIED_EXTERNAL passes; every inner node declares ANONYMOUS |
| U-02 | test_pre_process_node.py | serialize request + target hint into `validated_input` (JSON); HTML strip; hint priority channel_id > target_hint > message_id | hint resolved by priority; `<script>` stripped; empty/missing -> `status=error` |
| U-03 | test_caller_data_contract.py | the caller-data contract: injection screen (control tokens, override phrasing, obfuscated variants, both raw and post-strip, keys included), target-id shape + numeric bounds + non-finite spellings, message body charset, page-size finite/bounded matrix, absent-data degrade | attack forms refused and the value never echoed; ordinary messaging text and ordinary ids accepted; every malformed value fails closed with a field-naming error; no context -> baseline still shaped |
| U-04 | test_validate_input_node.py | empty/short guard; JSON-shaped input; framework `[MASKED]` path for emails; node-level token flag-and-redact (`secret_*`) | email -> `[MASKED]` before execute; token -> `[REDACTED]` + `redaction_flags=["token"]` (JSON string); empty/short -> error; audit payload carries flags only |
| U-05 | test_classify_intent_node.py | intent = send_message / check_delivery / list_activity (keyword, send-first priority, read-only default) | correct intent per keyword; no-signal defaults to check_delivery with a non-fatal note; empty -> error; audit emits the intent label only |
| U-06 | test_infer_lineworks_fields_node.py | channel/message-id resolution (explicit text mention > id-shaped hint; `ms-*` hints never used as a channel; never invented); message body (caller contract > keyword-quoted > any-quoted, else empty); caller page size; request body per intent (JSON string) | check_delivery `{message_id}`; send `{channel_id, content{type,text}}`; list_activity `{channel_id, limit}`; caller body beats the prose; caller limit reaches the body, else the default; unresolved ids left `""`; empty input -> error |
| U-07 | test_call_lineworks_api_node.py | send/status/activity via the network-free stub; `lineworks_config` state field + `execute(state, config=...)` override; API error / unresolved id / empty send text / unknown intent / missing payload; secret posture (live transport refuses to run unauthenticated; token via `ctx.secrets`, never env/state); the declared call deadline | record_id/record_ref (+ delivery_status for lookups) on success; 403 surfaces in error_log; live+no-secret -> error "unauthenticated"; live+bound secret -> token passed to client; a late result is discarded, not used; a non-finite/out-of-range deadline degrades to the default; audit emits presence signals with `stub_transport=True` |
| U-08 | test_confirm_node.py | human-readable confirmation per intent verb; status/ref/id parts; empty parts omitted | "Sent/Checked/Listed ... - status=... - ref=... - id=..."; missing evidence -> error |
| U-09 | test_post_process_node.py | `formatted_output` shaping (payload round-trip; channel/message/delivery fields); errored state passes through `__call__` un-masked (short-circuit); the output gate (full-node path + direct helper tests): record evidence, undeclared keys, nested credential walk with a top-level control, containment; **the ERROR envelope as a closed set**, parameterised over all six non-success inputs (inner error; inner error with the answer still in `result`; inner error with a credential in `error_log`; missing record evidence; credential nested in the payload; credential-shaped mapping key) | success shape with parsed `lineworks_payload`; error status/error_log preserved, no success shape fabricated; SUCCESS without record evidence blocked; a nested credential blocked; a violation clears every output-bearing field; on every non-success path the envelope's key set is exactly `{"reason"}` with every value in `ERROR_REASONS`, it stays truthy, and a sentinel seeded in `error_log` (a name + a runtime-assembled credential-shaped token in an echoed body) appears in no key and no value of the returned mapping at any depth; gate violations travel in `error_log` only; a credential-shaped mapping key is withheld from the violation label with the clearing intact; ordinary domain output untouched; clean-path control |
| U-10 | test_lineworks_client.py | LINE WORKS Bot REST API client: send / message status / room activity; `Authorization: Bearer` header; `LineWorksApiError` on non-2xx; stub shapes (status record / send receipt `ms-*` echo / activity list, `_stub` marker); `uses_stub_transport` | correct URLs/headers/bodies; 400 raises with `description`; deterministic stub shapes |
| U-11 | test_config.py | `config/agent.yaml` manifest + `config/config.yaml` runtime sanity | id CMN-C2-284, Cat 2, CMN, namespace cmn, deterministic, ToolCallingAgent; manifest is FLAT (no `agent:` block, single dotted `class:`); VERIFIED_EXTERNAL; `requires.secrets`/`extras` both `[]`; runtime max_retry/timeout_s ints; `lineworks.base_url` + `bot_id` |
| U-12 | test_domain_workflow_graph.py | inner `LineWorksWorkflowGraph`: identity, `lineworks_config` JSON injection, the call deadline carried, the caller-context bridge seeding, `route()` error short-circuit and its State annotation, `get_output` contract, compile, direct inner invoke on the stub | name/state_schema correct; config forwarded as a JSON string; bridge seeds target_hint/caller_message; route annotated with this graph's own State; error -> END; inner invoke runs validate -> classify -> infer -> call -> confirm to SUCCESS with record evidence |
| U-13 | test_framework_compliance_tc06_tc07.py | TC-06/TC-07: the framework's default input/output gates are `@final` | overriding either raises at class definition |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: no direct platform-SDK import |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + external trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json` "input" (asserted); VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, LineWorksWorkflowGraphNode, PostProcessNode, FinalizeNode]` and record evidence + confirmation in `result["output"]`; ANONYMOUS caller denied at pre_process (error, no post_process, no output); blank input -> error, not crash |
| PB-6b | Real ASGI `/invoke` end to end | test_pb_invoke_endpoint.py | runtime config reaches the compiled graph (max_retry, lineworks section, timeout_s); authenticated request returns real record evidence; caller message data reaches the send INTACT while the same body inside the request text arrives `[MASKED]`; a caller page size visibly changes the request body; every intent path reachable; missing/wrong/non-ASCII Bearer token -> 401 with a generic body; malformed caller data and injection content fail closed with nothing sent and nothing echoed; a bare `NaN` literal in the body refused; oversized `input_context` -> 413; no credential-shaped string anywhere in the (nested) response, with a control proving the scanner works; a credential-shaped caller body passes the charset and is contained at the OUTPUT gate; structural tokens (`90d`, ticket numbers, `STAR 2026`, decimal ratios) survive byte-identical; an email address cannot enter the structured channel at all |
| PB-6c | Output containment | test_pb_output_containment.py | a credential nested in the inner result, driven through the REAL compiled graph, produces an ERROR whose envelope carries neither the credential nor the answer it rode in on (no `ms-777`, no released confirmation), no traceback and no source paths, and is exactly `{"reason": "output_withheld_by_gate"}` — the gate's own finding stays on `error_log`; the clean path still ships; a control proves the scanner recognises the planted value |
| PB-6d | The ERROR envelope | test_error_envelope_no_record_evidence.py | the error envelope is present AND truthy (a falsy one re-opens the `formatted_output or result` shaping), names no `record_id`/`record_ref`/`channel_id`, the delta CLEARS every output-bearing state field, and the error status is still reported — plus a clean-path control so the containment assertions cannot pass vacuously. Error reasons carry closed-set labels only: the HTTP status not the upstream body, the exception TYPE not the transport string. **Reachability:** this branch is not reachable through the compiled graph (an ERROR status routes to `finalize`, bypassing `post_process`, and the framework short-circuits an already-errored state before `execute()` runs), so the tests call `execute()` directly — source-level defence in depth |
| PB-6e | The ERROR envelope over the wire | test_error_envelope_closed_set.py | the same closed-set property read off the REAL ASGI `/invoke` response body, with the sentinel seeded by patching one inner node's `execute()` on its CLASS (GraphNode rebuilds the inner graph per invoke, so the line rides the state reducer into the outer state as a real node's would): an inner-workflow error yields `output` null with no `error_log` key and no `Subgraph`/`Traceback`/source path; the LIVE gate-refusal path yields exactly `{"reason": "output_withheld_by_gate"}` with neither the gate's finding nor the confirmation that was merged into `result`; a LINE WORKS 403 whose body names the channel and quotes the message reaches nothing the caller sees; the sentinel's three fragments are absent from every key AND value at any depth; a control proves an unpatched request still ships the answer |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived - non-HITL** (the runtime config declares no `hitl.enabled: true`): module-level skipif; stub bodies are real AssertionErrors so enabling HITL without implementing PB-7 fails loudly |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); agent constructs + compiles via the supported path; `/invoke` + `/health` routes exposed |

> PB-1 (audit emission) is covered inside the unit suite via the emit-spy tests
> (validate / classify / call nodes assert on the event payload,
> `call.args[1]`). PB-3 (live external service) is exercised at deployment
> first-invoke, not in this suite - the bundled transport is the documented
> network-free stub.

## Test Execution Summary
- Runner: `python -m pytest tests/ -v` against the installed
  `agenticstar-agentcore` wheel (the version CI pins).
- Total tests: 240
- Pass: 238 / Fail: 0 / Skip: 2 (PB-7 A/B - auto-waived, non-HITL)
