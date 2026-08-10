# Pilot Agent wire protocol v1

This directory is the language-neutral contract between the Python control
plane and `pilot-agentd`. The checked-in schemas are semantically compared to
the TypeBox schemas used at runtime by `services/pilot-agentd`; neither copy is
allowed to drift independently.

## Versioning and compatibility

- Requests use `pilot107.agent-turn-request/v1`.
- Events use `pilot107.agent-turn-event/v1`.
- Checkpoints use `pilot107.agent-checkpoint/v1`.
- A consumer must reject an unknown major version. Because v1 objects are
  closed, adding a field requires a coordinated schema revision; silently
  accepting a new field is not backward compatibility.
- IDs are opaque protocol identifiers. They do not carry credentials or
  provider configuration.

The four valid task/profile/toolset combinations are fixed application
capabilities:

| task kind | prompt profile | toolset |
|---|---|---|
| `interactive` | `hpc-assistant-v1` | `a0-none` |
| `explain` | `agent-explain-v1` | `emit-explanation-v1` |
| `contract_patch` | `contract-patch-v1` | `emit-contract-patch-v1` |
| `remediation_plan` | `remediation-plan-v1` | `emit-remediation-plan-v1` |

Every protocol object and task envelope rejects unknown fields. Domain maps
whose keys are intentionally dynamic, such as a Contract, still accept JSON
values, but runtime validation recursively rejects the injection keys
`api_key`, `authorization`, `base_url`, `system_prompt`, `schema`, and `tools`
without relying on their casing. A request may name a server-side
`model_profile_id`; it cannot supply a provider URL, credential, prompt, schema,
or tool implementation.

## Event stream terminal invariant

For one Turn stream:

1. `sequence` starts at 1 and increments by exactly one.
2. Every event has the same `turn_id` and event schema version.
3. Exactly one terminal event exists: `turn_completed` or `turn_failed`.
4. The terminal event is the final event; no data follows it.
5. EOF without a terminal event, an unknown event type, a duplicate/gap, or an
   event after terminal is a protocol failure and must fail closed.

`message_delta` contains only public answer text. Provider reasoning is not a
wire event. Token counts are nullable when the provider does not report them;
absence must never be converted to a fabricated zero.

## Checkpoints

A checkpoint is normalized protocol state rather than a serialized Pi object.
It contains closed message/tool records, usage, lineage, profile bindings, and
a lowercase SHA-256 digest. Checkpoint creation and restore code must verify the
digest and sanitize credentials, URL query secrets, and reasoning before the
checkpoint crosses this boundary. A checkpoint never grants tool or execution
authority.
