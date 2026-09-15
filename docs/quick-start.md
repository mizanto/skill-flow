# SkillFlow quick-start

Run a Task through SkillFlow with one command. SkillFlow drives the Run loop
for you; each Run's work still happens in an isolated context with only the
durable context it needs. For the concepts underneath, see
[`user-guide.md`](user-guide.md); for the manual CLI path, see
[`operational-protocol.md`](operational-protocol.md) and
[`reference-example.md`](reference-example.md).

## 1. Install

You need [Claude Code](https://code.claude.com/docs/en/home) and
[uv](https://docs.astral.sh/uv/) — `uv` provisions Python automatically.
No checkout, no `PATH` edits, no venv.

Inside Claude Code, add the marketplace and install the plugin:

```text
/plugin marketplace add https://github.com/mizanto/skill-flow
/plugin install skillflow
```

`uv` needs the package index once; afterwards the CLI never needs the
network.

## 2. Run your first Task

From inside any git repository, describe the work:

```text
/skillflow:work "Create hello.txt containing hello"
```

That one command creates the Task and starts the loop: SkillFlow runs each
stage through a dedicated Skill (research → decomposition → implementation
→ review for the bundled `software-change` Workflow) and continues until
the Task is `completed`. You watch; you do not start Runs yourself, pick
Skills, or handle Task and Run IDs.

## 3. First-run approvals

The first run may ask you to approve tools the Skills use — typically
reading and writing files and running shell commands for the step work.
The SkillFlow Skills come with their `skillflow` CLI permissions
pre-approved; approve the remaining file and shell prompts and later runs
ask less.

## 4. Where artifacts live

Durable outputs land under `.skillflow/` at the repository root:

```text
.skillflow/
├── skillflow.db      # lifecycle state (never hand-edit this)
├── artifacts/        # durable artifact content, versioned per Task
└── runs/<run-id>/    # per-run files (diagnostics land here on failure)
```

Runs are connected by these artifacts, not by conversation history. Each Run
receives only the declared context its step needs.

## 5. Stop and resume

Stopping is always safe: lifecycle state is durable. To continue, run with
no arguments from inside the same repository:

```text
/skillflow:work
```

The driver picks up the running Run (re-dispatching its skill when the Run
was interrupted) or resolves the next one. If several Tasks are open, the
driver asks which one to continue. A Run is never resumed — the next
Run is always new.

## 6. Human decisions

When a review needs a human call, the driver shows you the review artifact
path(s) and asks. Pick one of the offered decisions — approve, request
changes, or cancel — and add an optional comment; the loop continues from
there. The decision and its comment are recorded durably with the Task.

## 7. Committing `.skillflow/`

Each repository decides for itself whether `.skillflow/` is committed or
ignored. To ignore it:

```text
echo '.skillflow/' >> .gitignore
```

## 8. Debugging

You only need this section when something goes wrong — the normal flow
never requires these commands.

`skillflow show-task <task-id>` prints a read-only view of the Task:
status, ordered Runs with provenance, per-Run Results, artifact references,
recorded decisions, and lifecycle events. It changes nothing.

When a Run fails, the Task stays `active` and the next Run retries the same
step (or skill). Ask Claude to inspect the failure first:

```text
skillflow show-task <task-id>
```

To check the install itself rather than a Task, ask Claude to run
`skillflow --version`: if that fails, the install did not complete —
re-run the two commands in §1.

For the manual CLI path around failures and decisions, see
[`user-guide.md`](user-guide.md) and
[`reference-example.md`](reference-example.md).

## 9. The manual path

`/skillflow:work` is the normal path. The four lifecycle commands below are
manual/recovery entry points for operating a single Run by hand — most
users never need them:

```text
/skillflow:resolve-task <task-id>
/skillflow:prepare-artifacts
/skillflow:complete-run
/skillflow:decide <decision>
```

Their operating rules are in [`operational-protocol.md`](operational-protocol.md).
