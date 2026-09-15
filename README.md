# Skill Flow

**Lightweight lifecycle orchestration for independent Claude Code runs.**

Skill Flow connects independent Claude Code sessions into a durable workflow without trying to become another AI agent runtime.

Claude Code does the work. Skill Flow manages what happens between runs.

## Installation

Requires [uv](https://docs.astral.sh/uv/) — it provisions the Python runtime
automatically, so you do not install Python separately.
This is a normal Claude Code plugin installation — you do not need to clone
the repository or install anything manually. Inside Claude Code:

```text
/plugin marketplace add https://github.com/mizanto/skill-flow
/plugin install skillflow
```

Then open any git repository and run `/skillflow:work "<task>"` — the
[quick-start](docs/quick-start.md) walks through your first Task.

The install needs the package index once; afterwards the CLI never needs
the network. If the Skills ever report the runtime is missing, ask Claude
to run `skillflow --version` via its Bash tool to tell a broken install
apart from a workflow problem.

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

`/skillflow:work "<task>"` drives the loop: SkillFlow runs each stage of the
workflow through a dedicated Skill, passing only durable artifacts forward.
You never pick a Skill or handle Task and Run IDs.

```text
/skillflow:work "<task>"
        ↓
Research → Decomposition → Implementation → Review
        ↓
   Task completed
```

The driver continues until the Task is complete, asking you only when a
human decision is required.

## Commands

```text
/skillflow:work "<task>"
/skillflow:resolve-task <task-id>
/skillflow:prepare-artifacts
/skillflow:complete-run
/skillflow:decide <decision>
```

`/skillflow:work` is the primary entry: it drives the Task loop. The other
four are manual/recovery entry points for operating a single Run by hand.
Operating rules for these commands: [`docs/operational-protocol.md`](docs/operational-protocol.md).

They ship as a Claude Code plugin, with commands and skills under [`plugins/skillflow/`](plugins/skillflow/).
Each command instructs Claude to run exactly one `skillflow` subcommand, so
the CLI must resolve from the Bash tool (it does after install, via the
shipped `bin/skillflow`). Validate and try a checkout locally:

```bash
claude plugin validate .
claude --plugin-dir .
```

The runtime also ships CLI-only operator commands with no skill:
`skillflow start` (create a Task from a bundled Workflow Definition),
`skillflow fail-run` (record the running Run as failed), and
`skillflow show-task <task-id>` (read-only lifecycle view).

New here? Start with the [quick-start](docs/quick-start.md), then the
[user guide](docs/user-guide.md) and the
[reference example](docs/reference-example.md).

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
Research
→ Decomposition
→ Implementation
→ Review
→ Done
```

It also supports review-driven rework, a return to research when a fundamental assumption is wrong, and human decisions.

The reference definition ships at [`workflows/software-change.yaml`](workflows/software-change.yaml).

## Development

Requires [uv](https://docs.astral.sh/uv/) (it provisions Python 3.14+
automatically). On a fresh checkout:

```bash
uv sync                          # create .venv with dev dependencies
uv run pytest                    # run the test suite
uv run ruff check . && uv run ruff format --check .   # lint and format check
uv run skillflow --version       # run the CLI
```

`uv sync` needs the package index once to fetch the dev tools and the single
runtime dependency (PyYAML, used to read Workflow Definition files); the
installed CLI itself never needs the network.

## Storage layout

SkillFlow keeps its durable lifecycle state in a `.skillflow/` directory at the
root of the target repository:

```text
<repo-root>/.skillflow/
├── skillflow.db      # the seven lifecycle tables: tasks, runs, results,
│                     # artifacts, workflow_definitions, human_decisions,
│                     # lifecycle_events (SQLite)
├── artifacts/        # durable artifact content
└── runs/<run-id>/    # per-run diagnostics, retained mainly on failure
```

Whether `.skillflow/` is committed or ignored is left to each target repository.
This repository ignores it while SkillFlow dogfoods itself.

## Status

✅ **MVP implemented**

The core loop (resolve → work → prepare → complete → evaluate → next
Run), human decisions, failure handling, and the reference
software-change Workflow are implemented and covered end to end. Run it
yourself with the [quick-start](docs/quick-start.md).
