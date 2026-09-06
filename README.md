# Skill Flow

**Lightweight lifecycle orchestration for independent Claude Code runs.**

Skill Flow connects independent Claude Code sessions into a durable workflow without trying to become another AI agent runtime.

Claude Code does the work. Skill Flow manages what happens between runs.

## Why

Long-running AI coding sessions accumulate context, tool output, and intermediate reasoning. As the session grows, maintaining a useful context becomes harder and more expensive.

Skill Flow breaks work into **bounded independent Runs** and connects them through **durable Artifacts**.

```text
Task
  ↓
Run
  ↓
Result + Artifacts
  ↓
Lifecycle Evaluation
  ↓
Next Run
```

Instead of passing an entire conversation to the next session, Skill Flow selects the durable context that the next Run actually needs.

## Core concepts

- **Task** — a unit of work that can span multiple Runs.
- **Run** — one bounded execution.
- **Result** — the outcome of a Run.
- **Artifact** — durable context produced by a Run.
- **Workflow Definition** — describes the normal procedure for a type of Task.
- **Human Decision** — optional human input between Runs.

## How it works

Each Run is executed in an independent Claude Code session.

```text
/skillflow:resolve-task <task-id>
        ↓
    Claude Code
        ↓
/skillflow:prepare-artifacts
        ↓
/skillflow:complete-run
        ↓
 Lifecycle Evaluation
        ↓
   next action
```

If another Run is required, a new Claude Code session starts it:

```text
/skillflow:resolve-task <task-id>
```

Skill Flow does not automatically launch Claude Code in the MVP.

## Commands

```text
/skillflow:resolve-task <task-id>
/skillflow:prepare-artifacts
/skillflow:complete-run
/skillflow:decide <decision>
```

## Design principles

- Claude Code remains the execution layer.
- Runs are bounded and independent.
- Durable Artifacts connect Runs.
- Lifecycle evaluation is deterministic.
- Context selection is explicit and deterministic in the MVP.
- Failed Runs do not automatically fail the Task.
- Lifecycle state is separate from Claude Code transcripts.
- Skill Flow should remain a small orchestration layer.

## MVP

The initial implementation focuses on validating one core loop:

```text
Task
→ Workflow
→ Run
→ Work
→ Artifacts
→ Result
→ Lifecycle Evaluation
→ Next Run
```

The first reference workflow is a software-change lifecycle:

```text
Requirements
→ Decomposition
→ Implementation
→ Review
→ Done
```

It also supports review-driven rework, research when a fundamental assumption is wrong, and human decisions.

## Development

Requires [uv](https://docs.astral.sh/uv/). On a fresh checkout:

```bash
uv sync                          # create .venv with dev dependencies
uv run pytest                    # run the test suite
uv run ruff check . && uv run ruff format --check .   # lint and format check
uv run skillflow --version       # run the CLI
```

`uv sync` needs the package index once to fetch the dev tools; the installed CLI
itself has no runtime dependencies and never needs the network.

## Storage layout

SkillFlow keeps its durable lifecycle state in a `.skillflow/` directory at the
root of the target repository:

```text
<repo-root>/.skillflow/
├── skillflow.db      # lifecycle state, metadata, events (SQLite)
├── artifacts/        # durable artifact content
└── runs/<run-id>/    # per-run diagnostics, retained mainly on failure
```

Whether `.skillflow/` is committed or ignored is left to each target repository.
This repository ignores it while SkillFlow dogfoods itself.

## Status

🚧 **Early development**

The architecture and MVP specification are being validated before implementation.
