---
name: stocker
description: Stocker (industrial stock photos, assets, metadata, review queue). READ THIS SKILL before any Stocker request. Call tools only via tool_call with id stocker__<tool> and top-level args, e.g. {"id":"stocker__asset_get","args":{"asset_id":5}}; what waits for review -> stocker__review_queue; what is ready -> stocker__asset_list {"ready":true}; answer every part of a question, one tool per part. Never invent results. Approve/reject: no tool exists and you must not look for one - answer that only a human can, with `python -m app.metadata approve N` (or `reject N --reason "..."`) on Windows; never say something was approved or rejected.
user-invocable: false
---

# Stocker

Stocker processes industrial stock photos: ingest -> QC -> Vision -> metadata -> review gate.
All Stocker data lives in the Stocker service. You reach it only through the `stocker` MCP tools.

## How to call Stocker tools

Stocker tools are NOT direct tools. Always call them through `tool_call`:

```json
{"id": "stocker__asset_get", "args": {"asset_id": 5}}
```

- The id is always `stocker__<tool>`. Never call bare names such as `asset_get`, `metadata_gate` or `stocker`.
- If you are unsure about arguments, run `tool_describe` with the id first.
- `tool_search` with query `stocker` lists all Stocker tools.

## Tools

| id | use for | args |
|---|---|---|
| `stocker__review_queue` | what needs a human decision / attention | `{}` |
| `stocker__asset_list` | list assets; filters | `{}` or `{"ready": true}` or `{"metadata_state": "human_review"}` |
| `stocker__asset_get` | full state of one asset (pipeline, QC, Vision, metadata, allowed_actions) | `{"asset_id": N}` |
| `stocker__asset_history` | processing events of an asset | `{"asset_id": N}` |
| `stocker__metadata_get` | metadata of an asset | `{"asset_id": N}` |
| `stocker__operations_list` | all operations with parameter schemas | `{}` |
| `stocker__asset_process_file` | process a new image in the project | `{"path": "data/incoming/<file>"}` |
| `stocker__asset_process` | re-process a registered asset | `{"asset_id": N}` |
| `stocker__metadata_build` | create metadata (Metadata AI) | `{"asset_id": N}` |
| `stocker__metadata_rebuild` | re-apply rules, keep human edits | `{"asset_id": N}` |
| `stocker__metadata_edit` | change title / description / keywords | `{"asset_id": N, "title": "..."}` |
| `stocker__metadata_gate` | re-evaluate the review gate | `{"asset_id": N}` |
| `stocker__metadata_escalate` | send to human review | `{"asset_id": N, "reason": "..."}` |

Typical requests:
- "what needs checking / attention" -> `stocker__review_queue`
- "show asset N" / "what is the state of N" -> `stocker__asset_get`
- "what is ready" -> `stocker__asset_list` with `{"ready": true}`

## Rules

1. Never state Stocker data without calling a tool in this turn. Report only values that are in the tool result. Never invent ids, titles, states or numbers.
2. Every result is a JSON envelope `{ok, outcome, data, error}` (OpenClaw may wrap it in a SECURITY NOTICE; the JSON inside is the Stocker result). If `ok` is false with `INVALID_PARAMS`, read "Allowed params" in the message, fix the args and retry once. Otherwise report `error.code` and `error.message` (or `outcome`) as they are.
6. Use only the filters the user asked for. Do not add extra filters (for example `qc`) - they can hide assets.
3. Approve and reject are human decisions. There is no tool for them and you must not try to imitate them (for example through `metadata_edit`). **Never write that something was approved or rejected** - nothing you do can approve. Say that only a human can, and tell the user to run on Windows: `python -m app.metadata approve N` or `python -m app.metadata reject N --reason "..."`.
4. Tools that change state (`asset_process*`, `metadata_build`, `metadata_rebuild`, `metadata_edit`, `metadata_gate`, `metadata_escalate`) run only when the user asks for that change.
5. Answer in the language of the user.
