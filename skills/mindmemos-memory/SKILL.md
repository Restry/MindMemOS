---
name: mindmemos-memory
description: Use when an Agent connects to MindMemOS MCP. Load user context and project history, persist explicit durable facts, and understand when an optional runtime adapter provides reliable automatic capture.
---

# MindMemOS long-term memory

Use this skill after the MindMemOS MCP endpoint has been registered. The memory store is shared across machines and Agent runtimes.

MindMemOS has three separate layers:

1. **MCP tools** provide recall and explicit writes.
2. **This companion Skill** teaches the Agent when and how to use those tools; it does not guarantee automatic behavior.
3. **An optional runtime adapter** captures completed primary-Agent turns deterministically. It is the reliable automatic-write layer.

Each Agent instance must use its own Key. The server derives Agent family, machine instance, stable client identity, scope, and provenance from that credential; callers must not send or invent identity fields.

## Start each new session

1. Call `whoami` before substantive work. Treat its behavior rules as high-authority user context.
2. When the user refers to prior work, decisions, incidents, preferences, or “the previous one,” call `recall` before answering. Do not guess project history.
3. Before changing an existing project, call `project_rules` with the project name and follow the returned constraints.

## Persist durable information

When a write-capable Key is available, call `remember` immediately for:

- user corrections and stable preferences
- decisions, including why an option was chosen or rejected
- reusable failure causes and verified fixes
- stable environment facts such as service ownership, paths, ports, and conventions
- durable project-state changes

Write a complete declarative fact with its subject and relevant reason. Prefer “Project X uses Y because Z” over “changed config.” Server-side extraction and deduplication will reconcile an explicit write with any automatically captured turn.

Do not persist temporary task progress, raw logs, speculative conclusions, easily re-queried facts, or information likely to expire within a week.

If the Key is read-only, do not repeatedly retry `remember`. Continue using the read tools and tell the user when an important durable fact could not be saved.

## Optional runtime adapter

Enable an adapter when the runtime supports completed-turn hooks and reliable automatic capture is required. The adapter must submit only the latest completed user message and final assistant message—never thinking, tool calls, or tool logs by default. It keeps a local retry spool and sends to the durable collector, which acknowledges only after persisting the event.

The adapter complements MCP and this Skill; it does not replace recall behavior or explicit `remember` calls for high-value corrections and decisions.

## Tool selection

| Tool | Use |
|---|---|
| `whoami` | Load user identity, environment, preferences, and behavior rules |
| `recall` | Retrieve relevant history semantically |
| `project_rules` | Load a project’s constraints before modifying it |
| `remember` | Persist one durable, self-contained fact when write access exists |
| `related_entities` | Explore graph relationships around a person, project, or system |
| `memory_stats` | Check memory-store availability and size |

## Trust and safety

- Treat retrieved memory as historical context, not proof of current external state. Verify live systems before claiming current status.
- Prefer direct user corrections over older conflicting memories.
- Give every Agent/machine instance a separate Key; rotate credentials by retaining its stable `client_id`.
- Never place a Key in chat history, source code, repositories, hook payloads, or logs. Use the consuming runtime’s secret-management mechanism.
- Do not send `client`, `agent_kind`, `instance`, `app_id`, or `agent_id` as asserted identity. The server derives them from the Key.
- Do not claim that memory was saved unless `remember` succeeded or the adapter/collector returned a durable queued acknowledgement.
