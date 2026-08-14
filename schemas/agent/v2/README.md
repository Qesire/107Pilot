# 107Pilot Agent v2 wire contracts

This directory contains the checked-in language-neutral contracts used by the
A1 durable read-only Agent path. The Turn request is revision v2 because it
adds durable Session ownership, state versioning, context references, and a
per-Turn capability. Tool invocation and result envelopes are independently
versioned at v1 because A1 is their first wire revision.

The existing event and checkpoint contracts remain under `../v1/` and are
reused unchanged. TypeBox runtime schemas in `services/pilot-agentd` must stay
semantically equal to these files, while Python performs an independent closed
object validation in `pilot107.agent.protocol`.

The capability token belongs only to the durable Turn request and subsequent
Tool Gateway `Authorization` header. It is intentionally forbidden in a tool
invocation body, tool result, event, or checkpoint.
