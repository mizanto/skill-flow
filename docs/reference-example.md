# Reference example: driving `software-change` to Done

A copy-paste-runnable transcript of the reference Workflow
(`workflows/software-change.yaml`): setup, the four-run happy path, and
three branches — rework, human decision, and failure with retry. Every
command below was executed verbatim to produce this document; ids and
timestamps differ on your machine. Long outputs are trimmed with `[...]`
— only to shorten, never to reshape.

Conventions: `$TASK` (and `$TASK2`…) hold Task ids; all commands run
with the scratch repo as the working directory; each `resolve-task`
starts a new Run.

## 0. Setup

Install once per machine (see the [quick-start](quick-start.md)), then
work in a scratch repo:

```bash
mkdir -p /tmp/sf-example && cd /tmp/sf-example
git init -q .
skillflow --version
```

```text
skillflow 0.1.0
```

Create the Task (`start` initializes the workspace, stages the bundled
Workflow Definition, and registers it):

```bash
TASK=$(skillflow start --title "Ship it")
echo "$TASK"
skillflow show-task "$TASK"
```

```text
task-bc527dba84c3428eb6345f0e08fcf937
Task task-bc527dba84c3428eb6345f0e08fcf937: Ship it (active)
Workflow: software-change
Created: 2026-09-15T07:50:42.419314+00:00 Updated: 2026-09-15T07:50:42.419314+00:00

Runs: none

Events (1):
  2026-09-15T07:50:42.419314+00:00 task.created status=active, workflow_definition_id=software-change
```

## 1. Happy path: research → decomposition → implementation → review

**Run 1 — research.** Resolve, write the research file, check the gap,
complete with the `ready` outcome:

```bash
skillflow resolve-task "$TASK"
cat > research.md <<'EOF'
# Research
- The change ships behind review.
EOF
skillflow prepare-artifacts
skillflow complete-run --outcome ready --artifact research.md:research:research.md
```

```text
Task task-bc527dba84c3428eb6345f0e08fcf937: Ship it
Run run-c5e5f17729d8486490d58a6867cc4124 (running) -- step 'research' via skill 'skillflow:research'
Workflow: software-change
Execution: model: opus, effort: high
Context: none selected
Unresolved context types: review, plan, research
Expected outputs:
  - research (required)
Next: do the bounded work for this step, then run `/skillflow:prepare-artifacts`.
[...]
✗ research (required)
[...]
Run run-c5e5f17729d8486490d58a6867cc4124 completed.

Next action:
Run decomposition.

Next: /skillflow:work
```

Note `✗ research (required)`: `prepare-artifacts` compares expected
outputs against *registered* artifacts, and nothing is registered until
completion — everything missing on a fresh run is normal. Its value is
telling you exactly what to produce. The unresolved context types
(`review`, `plan`, `research`) resolve only when a review sends the Task
back to research.

**Run 2 — decomposition.** The previous run's artifact arrives as
context; `review` stays unresolved (it only resolves after a review
sends the Task back, which this path never does):

```bash
skillflow resolve-task "$TASK"
cat > plan.md <<'EOF'
# Plan
- Implement behind review.
EOF
skillflow complete-run --outcome ready --artifact plan.md:plan:plan.md
```

```text
[...]
Run run-c5cfc01e819c40779ff6c9dda44223d1 (running) -- step 'decomposition' via skill 'skillflow:decomposition'
Workflow: software-change
Execution: model: opus, effort: high
Context:
  - research.md (research v1): .skillflow/artifacts/task-bc527dba84c3428eb6345f0e08fcf937/research-v1.md
Unresolved context types: review
Expected outputs:
  - plan (required)
[...]
Next action:
Run implementation.
[...]
```

**Run 3 — implementation.** This step declares no outputs: the durable
outputs of a change are the repository's own files. Complete with the
outcome alone:

```bash
skillflow resolve-task "$TASK"
echo "// implementation happens in the repo" > impl-note.txt
skillflow complete-run --outcome ready
```

```text
[...]
Run run-6f9846f3f9eb44a7b8be66aa68559166 (running) -- step 'implementation' via skill 'skillflow:implementation'
Workflow: software-change
Execution: model: sonnet, effort: high
Context:
  - plan.md (plan v1): .skillflow/artifacts/task-bc527dba84c3428eb6345f0e08fcf937/plan-v1.md
Unresolved context types: review
Expected outputs: none declared
[...]
Next action:
Run review.
[...]
```

**Run 4 — review.** Approve to finish the Task:

```bash
skillflow resolve-task "$TASK"
cat > review.md <<'EOF'
# Review
Approved.
EOF
skillflow complete-run --outcome approved --artifact review.md:review:review.md
skillflow show-task "$TASK"
```

```text
[...]
Task status:
completed
Task task-bc527dba84c3428eb6345f0e08fcf937: Ship it (completed)
[...]
Runs (4):

  [1] run-c5e5f17729d8486490d58a6867cc4124 (completed) -- step 'research', workflow 'software-change'
      trigger: initial
      started: 2026-09-15T07:50:46.908109+00:00  completed: 2026-09-15T07:50:47.117312+00:00
      Result result-0b036fefbdaf48e7ac9f3e332ac03bd1 (completed)
        outcome: research/ready
      Artifacts:
        - research.md (research v1) id artifact-37932153acd44d779f41e17a1b83136c path task-bc527dba84c3428eb6345f0e08fcf937/research-v1.md
      Decisions: none

  [2..4] ... (decomposition, implementation, review — same shape)

Events (17):
  2026-09-15T07:50:42.419314+00:00 task.created status=active, workflow_definition_id=software-change
  [...]
  2026-09-15T07:50:53.333517+00:00 task.status_changed run run-b3ef50b32a4941b39fd6bb1244bd0bd6 action=complete, from=active, reason=approved, to=completed
  [...]
```

The lifecycle is over: resolving a completed Task fails with
`TaskAlreadyCompleted` and creates no Run.

## 2. Branch: review-driven rework

Each branch below uses its own Task in the same scratch repo (all
previous branches are Done, so no `--task` disambiguation is needed)
and reuses the `research.md` / `plan.md` files. Drive to review,
then answer `changes_requested` instead of `approved`:

```bash
TASK2=$(skillflow start --title "Rework demo")
skillflow resolve-task "$TASK2" >/dev/null
skillflow complete-run --outcome ready --artifact research.md:research:research.md >/dev/null
skillflow resolve-task "$TASK2" >/dev/null
skillflow complete-run --outcome ready --artifact plan.md:plan:plan.md >/dev/null
skillflow resolve-task "$TASK2" >/dev/null
skillflow complete-run --outcome ready >/dev/null
skillflow resolve-task "$TASK2" >/dev/null
cat > review.md <<'EOF'
# Review
Changes requested: rework the implementation.
EOF
skillflow complete-run --outcome changes_requested --artifact review.md:review:review.md
```

```text
[...]
Next action:
Run implementation.
[...]
```

Rework is an ordinary rule targeting an earlier step — no special
entity. The next resolve starts an implementation Run, and this time
the review arrives as context:

```bash
skillflow resolve-task "$TASK2"
```

```text
[...]
Run run-ba9ce92808e24701b4bc760f82035230 (running) -- step 'implementation' via skill 'skillflow:implementation'
Workflow: software-change
Execution: model: sonnet, effort: high
Context:
  - plan.md (plan v1): .skillflow/artifacts/$TASK2/plan-v1.md
  - review.md (review v1): .skillflow/artifacts/$TASK2/review-v1.md
Expected outputs: none declared
[...]
```

Finish the loop — complete the rework, resolve the second review,
approve it (the fresh `review.md` registers as `review v2`):

```bash
skillflow complete-run --outcome ready
skillflow resolve-task "$TASK2" >/dev/null
cat > review.md <<'EOF'
# Review
Approved after rework.
EOF
skillflow complete-run --outcome approved --artifact review.md:review:review.md
```

```text
[...]
Next action:
Run review.
[...]
Task status:
completed
```

The Task completes with 6 Runs.

## 3. Branch: human decision

Drive to review and answer `human_required`:

```bash
TASK3=$(skillflow start --title "Decision demo")
skillflow resolve-task "$TASK3" >/dev/null
skillflow complete-run --outcome ready --artifact research.md:research:research.md >/dev/null
skillflow resolve-task "$TASK3" >/dev/null
skillflow complete-run --outcome ready --artifact plan.md:plan:plan.md >/dev/null
skillflow resolve-task "$TASK3" >/dev/null
skillflow complete-run --outcome ready >/dev/null
skillflow resolve-task "$TASK3" >/dev/null
cat > review.md <<'EOF'
# Review
Needs a human call.
EOF
skillflow complete-run --outcome human_required --artifact review.md:review:review.md
skillflow resolve-task "$TASK3"; echo "exit: $?"
```

```text
[...]
Next action:
Human decision required.

Use:

/skillflow:decide <decision>
skillflow resolve-task: HumanDecisionRequired: task 'task-a41c2f277e0041168bdac19c84fd7c50' is 'waiting_for_human' (step 'review' of run 'run-41a119f5de7549709898849fa79c2f82'); allowed decisions: approve, request_changes, cancel; artifacts: review.md (review v1): .skillflow/artifacts/task-a41c2f277e0041168bdac19c84fd7c50/review-v1.md; record a decision with `skillflow decide <decision> --task task-a41c2f277e0041168bdac19c84fd7c50`, then run `skillflow resolve-task task-a41c2f277e0041168bdac19c84fd7c50`
No Run was created.
exit: 1
```

No Run can resolve while the Task waits. Record the decision with a
comment, then resume:

```bash
skillflow decide request_changes --comment "Rework the plan first"
skillflow resolve-task "$TASK3"
```

```text
Human decision recorded: request_changes.

Next action:
Run implementation.
[...]
```

The decision is durable: `show-task` lists it under the review Run with
its comment, and the resumed implementation Run carries
`trigger: 'request_changes' from run '<review-run>'`. Finish to Done:

```bash
skillflow complete-run --outcome ready
skillflow resolve-task "$TASK3" >/dev/null
cat > review.md <<'EOF'
# Review
Approved.
EOF
skillflow complete-run --outcome approved --artifact review.md:review:review.md
```

```text
[...]
Next action:
Run review.
[...]
Task status:
completed
```

## 4. Branch: failure and retry

Resolve, fail the Run with diagnostics, watch the Task stay active,
and retry by resolving again — a new Run, same step:

```bash
TASK4=$(skillflow start --title "Failure demo")
skillflow resolve-task "$TASK4" >/dev/null
skillflow fail-run --message "Test harness hung; see output.log"
skillflow resolve-task "$TASK4"
```

```text
Run run-d89189862f874171a65509651660cd3f failed.

Task status:
active

Diagnostics: runs/run-d89189862f874171a65509651660cd3f/output.log

Next action:
Run research.
[...]
Task task-393e86200e624455bcf1d9f67f7ac017: Failure demo
Run run-0b77302bea244b31b24240318a8dff11 (running) -- step 'research' via skill 'skillflow:research'
Workflow: software-change
```

The failed Run is never resumed; the retry is a new Run, and the
diagnostics file it references is real:

```bash
cat .skillflow/runs/*/output.log
```

Complete the retried Run normally (`--outcome ready` plus the
research artifact routes to decomposition) and continue exactly
like the happy path from there.

## 5. Workflow selection

A Task created without a Workflow refuses to resolve until one is
assigned. SkillFlow never selects one — it lists what is available:

```bash
# The selection demo needs a Python that can `import skillflow` — the
# interpreter behind your `skillflow`. With a checkout that is the venv
# script's shebang python; with a plugin install, the venv beside the shim:
SF_BIN="$(command -v skillflow)"
if head -n 1 "$SF_BIN" | grep -q '^#!.*python'; then
  SKILLFLOW_PY="$(head -n 1 "$SF_BIN" | cut -c3- | cut -d' ' -f1)"
else
  SKILLFLOW_PY="$(dirname "$(dirname "$SF_BIN")")/.venv/bin/python"
fi
TASK5=$("$SKILLFLOW_PY" -c "
import contextlib
from skillflow import store, workspace
from skillflow.service import create_task
ws = workspace.Workspace(root=workspace.find_repo_root())
with contextlib.closing(store.open_store(ws)) as conn:
    print(create_task(conn, title='Unassigned').id)
")
skillflow resolve-task "$TASK5"; echo "exit: $?"
skillflow resolve-task "$TASK5" --workflow software-change
```

```text
skillflow resolve-task: WorkflowSelectionRequired: task 'task-873b32777e404a83a55a1c13565773db' has no Workflow Definition and none was given; SkillFlow never selects one. available Workflow Definitions: 'software-change'; re-run as `skillflow resolve-task task-873b32777e404a83a55a1c13565773db --workflow NAME`
No Run was created.
exit: 1
Task task-873b32777e404a83a55a1c13565773db: Unassigned
Run run-fe88160ff0a0413a997e321dd78ca795 (running) -- step 'research' via skill 'skillflow:research'
Workflow: software-change
```

Assignment is permanent; from here the Task behaves exactly like §1.
