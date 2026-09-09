# Reference example: driving `software-change` to Done

A copy-paste-runnable transcript of the reference Workflow
(`workflows/software-change.yaml`): setup, the four-run happy path, and
three branches — rework, human decision, and failure with retry. Every
command below was executed verbatim to produce this document; ids and
timestamps differ on your machine. Long outputs are trimmed with `[...]`
— only to shorten, never to reshape.

Conventions: `$TASK` (and `$TASK2`…) hold Task ids; all commands run
with the scratch repo as the working directory; each `resolve-task`
starts a new Run as a new session would.

## 0. Setup

Install once per machine (see the [user guide](user-guide.md) §2), then
work in a scratch repo:

```bash
SF_CHECKOUT=~/skillflow  # ← your SkillFlow checkout
export PATH="$SF_CHECKOUT/.venv/bin:$PATH"
export SKILLFLOW_PY="$SF_CHECKOUT/.venv/bin/python"
mkdir -p /tmp/sf-example/workflows && cd /tmp/sf-example
git init -q .
cp "$SF_CHECKOUT/workflows/software-change.yaml" workflows/
skillflow --version
```

```text
skillflow 0.1.0
```

Initialize the workspace, register the definition, and create the Task:

```bash
"$SKILLFLOW_PY" -c "from skillflow.workspace import init_workspace; init_workspace()"
TASK=$("$SKILLFLOW_PY" - <<'EOF'
import contextlib
from skillflow import store, workspace
from skillflow.service import create_task, register_workflow
from skillflow.workflow_loader import load_workflow

ws = workspace.Workspace(root=workspace.find_repo_root())
with contextlib.closing(store.open_store(ws)) as conn:
    register_workflow(conn, load_workflow(ws.workflows_dir / "software-change.yaml"))
    task = create_task(conn, title="Ship it", workflow_definition_id="software-change")
    print(task.id)
EOF
)
echo "$TASK"
skillflow show-task "$TASK"
```

```text
task-cde9c20e4bce442990af7c2f9f69a4d5
Task task-cde9c20e4bce442990af7c2f9f69a4d5: Ship it (active)
Workflow: software-change
Created: 2026-09-09T20:54:38.887270+00:00 Updated: 2026-09-09T20:54:38.887270+00:00

Runs: none

Events (1):
  2026-09-09T20:54:38.887270+00:00 task.created status=active, workflow_definition_id=software-change
```

## 1. Happy path: requirements → decomposition → implementation → review

**Run 1 — requirements.** Resolve, write the requirements file, check
the gap, complete with the `ready` outcome:

```bash
skillflow resolve-task "$TASK"
cat > requirements.md <<'EOF'
# Requirements
- The change ships behind review.
EOF
skillflow prepare-artifacts
skillflow complete-run --outcome ready --artifact requirements.md:requirements:requirements.md
```

```text
Task task-cde9c20e4bce442990af7c2f9f69a4d5: Ship it
Run run-72803a4aca04495a997273dfa29561d8 (running) -- step 'requirements' via skill 'requirements-analysis'
Execution: model: opus, effort: high
Context: none selected
Expected outputs:
  - requirements (required)
Next: do the bounded work for this step, then run `/skillflow:prepare-artifacts`.
[...]
✗ requirements (required)
[...]
Run run-72803a4aca04495a997273dfa29561d8 completed.

Next action:
Run decomposition.

Start the next Run in a new Claude Code session:

/skillflow:resolve-task task-cde9c20e4bce442990af7c2f9f69a4d5
```

Note `✗ requirements (required)`: `prepare-artifacts` compares expected
outputs against *registered* artifacts, and nothing is registered until
completion — everything missing on a fresh run is normal. Its value is
telling you exactly what to produce.

**Run 2 — decomposition.** The previous run's artifact arrives as
context; `research` stays unresolved (it only resolves after a
research Run, which this path never takes):

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
Run run-c934e7f88a11411bb76537bc113c231d (running) -- step 'decomposition' via skill 'decomposition'
Execution: model: opus, effort: high
Context:
  - requirements.md (requirements v1)
Unresolved context types: research
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
Run run-87a024c0afa54f07a990d13ee2c6eb55 (running) -- step 'implementation' via skill 'implementation'
Execution: model: sonnet, effort: high
Context:
  - requirements.md (requirements v1)
  - plan.md (plan v1)
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
Task task-cde9c20e4bce442990af7c2f9f69a4d5: Ship it (completed)
[...]
Runs (4):

  [1] run-72803a4aca04495a997273dfa29561d8 (completed) -- step 'requirements', workflow 'software-change'
      trigger: initial
      started: 2026-09-09T20:54:43.496857+00:00  completed: 2026-09-09T20:54:43.639306+00:00
      Result result-c4bb7f4dc2cb47229e251302d788d780 (completed)
        outcome: requirements/ready
      Artifacts:
        - requirements.md (requirements v1) id artifact-01fd7b56fcc24cd985a9151450531dfc path task-cde9c20e4bce442990af7c2f9f69a4d5/requirements-v1.md
      Decisions: none

  [2..4] ... (decomposition, implementation, review — same shape)

Events (17):
  2026-09-09T20:54:38.887270+00:00 task.created status=active, workflow_definition_id=software-change
  [...]
  2026-09-09T20:55:05.617310+00:00 task.status_changed run run-833bfba5967343aeb7cd94f8f2be7f20 action=complete, from=active, reason=approved, to=completed
  [...]
```

The lifecycle is over: resolving a completed Task fails with
`TaskAlreadyCompleted` and creates no Run.

## 2. Branch: review-driven rework

Each branch below uses its own Task in the same scratch repo (all
previous branches are Done, so no `--task` disambiguation is needed)
and reuses the `requirements.md` / `plan.md` files. Drive to review,
then answer `changes_requested` instead of `approved`:

```bash
TASK2=$("$SKILLFLOW_PY" -c "
import contextlib
from skillflow import store, workspace
from skillflow.service import create_task
ws = workspace.Workspace(root=workspace.find_repo_root())
with contextlib.closing(store.open_store(ws)) as conn:
    print(create_task(conn, title='Rework demo', workflow_definition_id='software-change').id)
")
skillflow resolve-task "$TASK2" >/dev/null
skillflow complete-run --outcome ready --artifact requirements.md:requirements:requirements.md >/dev/null
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
Run run-80a8d717fe444f39a2f7e30918c12ea9 (running) -- step 'implementation' via skill 'implementation'
Execution: model: sonnet, effort: high
Context:
  - requirements.md (requirements v1)
  - plan.md (plan v1)
  - review.md (review v1)
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
TASK3=$("$SKILLFLOW_PY" -c "
import contextlib
from skillflow import store, workspace
from skillflow.service import create_task
ws = workspace.Workspace(root=workspace.find_repo_root())
with contextlib.closing(store.open_store(ws)) as conn:
    print(create_task(conn, title='Decision demo', workflow_definition_id='software-change').id)
")
skillflow resolve-task "$TASK3" >/dev/null
skillflow complete-run --outcome ready --artifact requirements.md:requirements:requirements.md >/dev/null
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
skillflow resolve-task: HumanDecisionRequired: task 'task-cc8a62158265428f8434f67ddec471c8' is 'waiting_for_human'; record a decision with `skillflow decide <decision>`, then run `skillflow resolve-task task-cc8a62158265428f8434f67ddec471c8`
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
TASK4=$("$SKILLFLOW_PY" -c "
import contextlib
from skillflow import store, workspace
from skillflow.service import create_task
ws = workspace.Workspace(root=workspace.find_repo_root())
with contextlib.closing(store.open_store(ws)) as conn:
    print(create_task(conn, title='Failure demo', workflow_definition_id='software-change').id)
")
skillflow resolve-task "$TASK4" >/dev/null
skillflow fail-run --message "Test harness hung; see output.log"
skillflow resolve-task "$TASK4"
```

```text
Run run-a925bd0184354ea8b0f5dba17ecc6252 failed.

Task status:
active

Diagnostics: runs/run-a925bd0184354ea8b0f5dba17ecc6252/output.log

Next action:
Run requirements.
[...]
Task task-0e586e62a1714df5ac99435711eca90d: Branch 4
Run run-c8591f6714bf437e8c81866b062fa824 (running) -- step 'requirements' via skill 'requirements-analysis'
```

The failed Run is never resumed; the retry is a new Run, and the
diagnostics file it references is real:

```bash
cat .skillflow/runs/*/output.log
```

Complete the retried Run normally (`--outcome ready` plus the
requirements artifact routes to decomposition) and continue exactly
like the happy path from there.

## 5. Workflow selection

A Task created without a Workflow refuses to resolve until one is
assigned. SkillFlow never selects one — it lists what is available:

```bash
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
skillflow resolve-task: WorkflowSelectionRequired: task 'task-5ce5ed277c4741f48a30ab97b60b468e' has no Workflow Definition and none was given; SkillFlow never selects one. available Workflow Definitions: 'software-change'; re-run as `skillflow resolve-task task-5ce5ed277c4741f48a30ab97b60b468e --workflow NAME`
No Run was created.
exit: 1
Task task-5ce5ed277c4741f48a30ab97b60b468e: Unassigned
Run run-21782e1e6a204495890adc1116f34206 (running) -- step 'requirements' via skill 'requirements-analysis'
```

Assignment is permanent; from here the Task behaves exactly like §1.
