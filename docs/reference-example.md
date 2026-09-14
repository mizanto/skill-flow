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
task-a0668ec1db08495f93cb7c4b7fe77dbe
Task task-a0668ec1db08495f93cb7c4b7fe77dbe: Ship it (active)
Workflow: software-change
Created: 2026-09-14T16:06:28.766317+00:00 Updated: 2026-09-14T16:06:28.766317+00:00

Runs: none

Events (1):
  2026-09-14T16:06:28.766317+00:00 task.created status=active, workflow_definition_id=software-change
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
Task task-a0668ec1db08495f93cb7c4b7fe77dbe: Ship it
Run run-ad56d693ea464e0795e6b4dc5e9d00a3 (running) -- step 'research' via skill 'skillflow:research'
Execution: model: opus, effort: high
Context: none selected
Unresolved context types: review, plan, research
Expected outputs:
  - research (required)
Next: do the bounded work for this step, then run `/skillflow:prepare-artifacts`.
[...]
✗ research (required)
[...]
Run run-ad56d693ea464e0795e6b4dc5e9d00a3 completed.

Next action:
Run decomposition.

Start the next Run in a new Claude Code session:

/skillflow:resolve-task task-a0668ec1db08495f93cb7c4b7fe77dbe
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
Run run-1b1449e1752b43a8aea36d99a0d157e9 (running) -- step 'decomposition' via skill 'skillflow:decomposition'
Execution: model: opus, effort: high
Context:
  - research.md (research v1)
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
Run run-a90ce8cd6151496ba345f0eee5230a6a (running) -- step 'implementation' via skill 'skillflow:implementation'
Execution: model: sonnet, effort: high
Context:
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
Task task-a0668ec1db08495f93cb7c4b7fe77dbe: Ship it (completed)
[...]
Runs (4):

  [1] run-ad56d693ea464e0795e6b4dc5e9d00a3 (completed) -- step 'research', workflow 'software-change'
      trigger: initial
      started: 2026-09-14T16:06:28.935463+00:00  completed: 2026-09-14T16:06:29.111525+00:00
      Result result-82f118bb43af487c95ce4f27ddd041c7 (completed)
        outcome: research/ready
      Artifacts:
        - research.md (research v1) id artifact-a03394584ddb4458a301257464d9e3be path task-a0668ec1db08495f93cb7c4b7fe77dbe/research-v1.md
      Decisions: none

  [2..4] ... (decomposition, implementation, review — same shape)

Events (17):
  2026-09-14T16:06:28.766317+00:00 task.created status=active, workflow_definition_id=software-change
  [...]
  2026-09-14T16:06:29.643907+00:00 task.status_changed run run-5e15788647ed4c678434819306cfd7d5 action=complete, from=active, reason=approved, to=completed
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
TASK2=$("$SKILLFLOW_PY" -c "
import contextlib
from skillflow import store, workspace
from skillflow.service import create_task
ws = workspace.Workspace(root=workspace.find_repo_root())
with contextlib.closing(store.open_store(ws)) as conn:
    print(create_task(conn, title='Rework demo', workflow_definition_id='software-change').id)
")
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
Run run-bd1c636e5b9141458a7c6b55e3834ecf (running) -- step 'implementation' via skill 'skillflow:implementation'
Execution: model: sonnet, effort: high
Context:
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
skillflow resolve-task: HumanDecisionRequired: task 'task-54ecbb15ab874aee88859dd477483539' is 'waiting_for_human'; record a decision with `skillflow decide <decision>`, then run `skillflow resolve-task task-54ecbb15ab874aee88859dd477483539`
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
Run run-ed789fc5e1ef4c1088210aaad88191c1 failed.

Task status:
active

Diagnostics: runs/run-ed789fc5e1ef4c1088210aaad88191c1/output.log

Next action:
Run research.
[...]
Task task-0c4415abcb99407ca7e06ff7941bea91: Failure demo
Run run-04e434a1d082483a96d290ce3006a429 (running) -- step 'research' via skill 'skillflow:research'
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
skillflow resolve-task: WorkflowSelectionRequired: task 'task-2ec04ec7382348a2b845c648fdf83f03' has no Workflow Definition and none was given; SkillFlow never selects one. available Workflow Definitions: 'software-change'; re-run as `skillflow resolve-task task-2ec04ec7382348a2b845c648fdf83f03 --workflow NAME`
No Run was created.
exit: 1
Task task-2ec04ec7382348a2b845c648fdf83f03: Unassigned
Run run-8938f82c939845b3850798a0bf820c11 (running) -- step 'research' via skill 'skillflow:research'
```

Assignment is permanent; from here the Task behaves exactly like §1.
