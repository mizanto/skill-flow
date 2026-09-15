"""Claude Code acceptance suite (SF-49): the real vertical slice.

Drives the shipped plugin (the repository root) through ``claude -p``
with ``--plugin-dir`` and ``--output-format stream-json``, asserting
lifecycle outcomes via ``skillflow show-task`` and the stream:

* A1 happy path: ``/skillflow:work "Create hello.txt containing hello"``
  in one invocation completes the Task through research → decomposition
  → implementation → review/``approved``.
* A2 artifact context: decomposition/review forks read the printed
  ``.skillflow/artifacts/`` paths with no preceding find/Glob/Grep.
* A3 no leakage: forked tool calls carry a non-null parent link, and the
  ``probe:canary`` fork skill cannot see a driver-prompt canary.
* A4 wrong Skill: dispatching ``skillflow:implementation`` while a
  research Run runs yields ``AssignmentMismatch`` with no state change.
* A5 interruption: CLI ``start`` + ``resolve-task`` leaves research Run R1
  running; ``/skillflow:work`` re-dispatches ``skillflow:research`` for R1
  (no second research Run), R1 completes ``research/ready``, and the loop
  proceeds to a completed Task.
* A7 assumption recovery: CLI drives research → decomposition →
  implementation → review ending ``fundamental_assumption_wrong``;
  ``/skillflow:work`` resolves the next Run as step ``research`` with
  review v1, plan v1, research v1 as context, whose outcome
  (``replan``/``ready``) leads through decomposition (plan v2) →
  implementation → review to a completed Task. The runtime decides the
  research re-entry; the seeded review only reported the verdict.
* A6 human decision: manual checklist below (interactive session
  required; executed once by a human, result recorded on SF-51).

Running:

```bash
SKILLFLOW_ACCEPTANCE=1 uv run pytest tests/acceptance/test_claude_code.py
```

Default ``uv run pytest`` skips the live tests (only the pure tests run:
``test_probe_plugin_structure``, ``test_repo_rel_resolves_symlinked_repo``,
``test_review_tail_shape``). The live tests need the ``claude`` CLI, an
authenticated account, and API quota; each ``claude -p`` run costs real
money (full module ≈6–8 min at current model speeds: three driver runs
plus two short runs). They never run in CI.

Design notes:

* A1/A2/A3-first-half share one module-scoped ``driver_run`` fixture
  (one expensive invocation, read-only assertions). The slice check
  (research completed + research v1 + decomposition dispatched) is the
  committed prefix of A1. A5 and A7 seed their own repos (different
  pre-state) and each runs the driver once; helpers stay generic over
  prompt/plugin/grant/timeout for that reuse.
* Harness assertions precede product assertions in every test, so agent
  non-compliance and product breakage fail differently.
* Calibrated against ``claude`` 2.1.236: ``stream-json`` requires
  ``--verbose``; ``--allowedTools`` is variadic, so the prompt goes
  immediately after ``-p`` and grants are comma-separated; the parent
  link is the top-level ``parent_tool_use_id`` (null in-session,
  ``toolu_…`` in forks); a skill's ``!`` preamble is load context and is
  not streamed as tool calls.

A6 manual checklist (cwd = a scratch repo; ``TASK`` is the started id):

```text
1. skillflow start --title "A6 manual" \
     --description "Create hello.txt containing hello"  → TASK
2. CLI-drive research (ready + research.md), decomposition (ready +
   plan.md), implementation (ready, no artifact) exactly as
   `_seed_assumption_loop` does (same file).
3. skillflow resolve-task TASK  → review R4 running; write review.md
   (verdict human_required plus the decision question); skillflow
   complete-run --outcome human_required
   --artifact review.md:review:<path>  → Task waiting_for_human.
4. skillflow resolve-task TASK  → expect HumanDecisionRequired listing
   exactly `approve, request_changes, cancel` plus the review v1 path.
5. Interactive claude session (--plugin-dir <repo root>):
   /skillflow:work → answer AskUserQuestion `approve` + a comment →
   expect the decision (+ comment) recorded in show-task and the Task
   completed.
6. Repeat 1–4 in a fresh repo; answer `request_changes` → expect an
   implementation Run dispatched and the loop continuing.
7. Record pass/fail + transcript excerpts as a comment on SF-51.
```
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from skillflow import store, workspace
from skillflow.domain import TaskStatus

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT
PROBE_PLUGIN_DIR = Path(__file__).resolve().parent / "probe-plugin"
WORKFLOWS_DIR = REPO_ROOT / "workflows"

ACCEPTANCE_ENV = "SKILLFLOW_ACCEPTANCE"

#: Applied (with ``pytest.mark.acceptance``) to every live-Claude test.
requires_acceptance = pytest.mark.skipif(
    os.environ.get(ACCEPTANCE_ENV) != "1",
    reason=f"live Claude Code test: set {ACCEPTANCE_ENV}=1 to run",
)

CLI_TIMEOUT = 120
DRIVER_TIMEOUT = 1800
PROBE_TIMEOUT = 600
A4_TIMEOUT = 600

A1_PROMPT = '/skillflow:work "Create hello.txt containing hello"'
A1_GRANT = ("Skill", "Bash", "Read", "Write", "Edit")

#: Blank ``$ARGUMENTS``: the driver takes the resume path (assignment
#: re-dispatch when a Run is running, else resolve-task).
CONTINUE_PROMPT = "/skillflow:work"

A5_TIMEOUT = 1800
A7_TIMEOUT = 1800

_ASSIGNMENT_COMMAND = re.compile(r"(?:^|[;&|])\s*skillflow assignment\b")


def _claude_binary() -> str:
    """Return the ``claude`` path, skipping when it is missing."""
    path = shutil.which("claude")
    if path is None:
        pytest.skip("claude CLI not on PATH")
    assert path is not None
    return path


def _accept_env() -> dict[str, str]:
    """Environment for every subprocess: venv CLI first, bundled flows pinned."""
    env = dict(os.environ)
    env["SKILLFLOW_BUNDLED_WORKFLOWS"] = str(WORKFLOWS_DIR)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    return env


def _git_repo(path: Path) -> Path:
    """Init a scratch repo: local identity, empty init commit, README commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "."], cwd=path, check=True,
                   timeout=CLI_TIMEOUT)
    subprocess.run(
        ["git", "-c", "user.email=acceptance@example",
         "-c", "user.name=Acceptance",
         "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=path, check=True, timeout=CLI_TIMEOUT,
    )
    (path / "README.md").write_text("# scratch\n", encoding="utf-8")
    subprocess.run(
        ["git", "-c", "user.email=acceptance@example",
         "-c", "user.name=Acceptance",
         "add", "README.md"],
        cwd=path, check=True, timeout=CLI_TIMEOUT,
    )
    subprocess.run(
        ["git", "-c", "user.email=acceptance@example",
         "-c", "user.name=Acceptance",
         "commit", "-q", "-m", "readme"],
        cwd=path, check=True, timeout=CLI_TIMEOUT,
    )
    return path


@dataclass(frozen=True)
class Stream:
    """Parsed ``stream-json`` stdout: events in order plus skipped lines."""

    events: tuple[dict, ...] = ()
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolUse:
    """One ``tool_use`` block with its enclosing event's parent link."""

    id: str
    name: str
    input: dict
    parent: str | None
    index: int


@dataclass(frozen=True)
class ToolResult:
    """One ``tool_result`` block with its enclosing event's parent link."""

    tool_use_id: str
    parent: str | None
    is_error: bool | None
    text: str


@dataclass(frozen=True)
class ClaudeRun:
    """A finished ``claude -p`` invocation: exit code, stream, context."""

    returncode: int
    stream: Stream
    stderr: str


def parse_stream(stdout: str) -> Stream:
    """Parse JSONL stdout; unparsable lines are kept as ``skipped``."""
    events: list[dict] = []
    skipped: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            skipped.append(line[:200])
    return Stream(events=tuple(events), skipped=tuple(skipped))


def stream_census(stream: Stream) -> dict[str, int]:
    """Count events by ``type`` (plus ``skipped``) for failure context."""
    counts: Counter[str] = Counter()
    for event in stream.events:
        counts[str(event.get("type"))] += 1
    if stream.skipped:
        counts["skipped-lines"] = len(stream.skipped)
    return dict(counts)


def _content_text(content: object) -> str:
    """Coerce a tool_result ``content`` (str or blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            else:
                parts.append(json.dumps(block)[:500])
        return "\n".join(parts)
    return json.dumps(content)[:500]


def collect_tool_uses(stream: Stream) -> list[ToolUse]:
    """Return every ``tool_use`` block in stream order with parent links."""
    uses: list[ToolUse] = []
    for index, event in enumerate(stream.events):
        if event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        for block in message.get("content") or ():
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            raw_input = block.get("input")
            uses.append(ToolUse(
                id=str(block.get("id") or ""),
                name=str(block.get("name") or ""),
                input=raw_input if isinstance(raw_input, dict) else {},
                parent=event.get("parent_tool_use_id"),
                index=index,
            ))
    return uses


def collect_tool_results(stream: Stream) -> list[ToolResult]:
    """Return every ``tool_result`` block in stream order with parent links."""
    results: list[ToolResult] = []
    for event in stream.events:
        if event.get("type") != "user":
            continue
        message = event.get("message") or {}
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            results.append(ToolResult(
                tool_use_id=str(block.get("tool_use_id") or ""),
                parent=event.get("parent_tool_use_id"),
                is_error=block.get("is_error"),
                text=_content_text(block.get("content")),
            ))
    return results


def stream_text(stream: Stream) -> str:
    """Join every text block and tool_result text in the stream."""
    parts: list[str] = []
    for event in stream.events:
        if event.get("type") == "result" and event.get("result"):
            parts.append(str(event["result"]))
            continue
        message = event.get("message") or {}
        for block in message.get("content") or ():
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
            elif block.get("type") == "tool_result":
                parts.append(_content_text(block.get("content")))
    return "\n".join(parts)


def run_claude(*, cwd: Path, prompt: str, plugin_dirs: tuple[Path, ...],
               allowed_tools: tuple[str, ...], timeout: int) -> ClaudeRun:
    """Run ``claude -p`` with the prompt first; fail loudly, never hang."""
    argv = [_claude_binary(), "-p", prompt, "--verbose",
            "--output-format", "stream-json", "--forward-subagent-text"]
    for plugin_dir in plugin_dirs:
        argv += ["--plugin-dir", str(plugin_dir)]
    argv += ["--allowedTools", ",".join(allowed_tools)]
    try:
        proc = subprocess.run(argv, cwd=cwd, env=_accept_env(),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        tail = (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else ""
        pytest.fail(
            f"claude -p timed out after {timeout}s in {cwd}\n"
            f"prompt: {prompt[:200]}\npartial stdout tail:\n{tail}",
        )
        raise AssertionError("unreachable") from exc
    return ClaudeRun(returncode=proc.returncode,
                     stream=parse_stream(proc.stdout),
                     stderr=proc.stderr)


def run_skillflow(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the ``skillflow`` CLI in a scratch repo (caller checks results)."""
    return subprocess.run(["skillflow", *args], cwd=cwd, env=_accept_env(),
                          capture_output=True, text=True, timeout=CLI_TIMEOUT)


def validate_plugin(plugin_dir: Path) -> None:
    """Fail fast when a fixture plugin does not validate (fatal, not skip)."""
    proc = subprocess.run([_claude_binary(), "plugin", "validate", str(plugin_dir)],
                          cwd=REPO_ROOT, env=_accept_env(),
                          capture_output=True, text=True, timeout=CLI_TIMEOUT)
    assert proc.returncode == 0, (
        f"claude plugin validate {plugin_dir} failed:\n{proc.stderr[-2000:]}"
    )


def find_skill_dispatches(uses: list[ToolUse], skill: str,
                          top_level_only: bool = True) -> list[ToolUse]:
    """Return ``Skill`` invocations for ``skill`` (harness check helper)."""
    return [use for use in uses
            if use.name == "Skill"
            and use.input.get("skill") == skill
            and (not top_level_only or use.parent is None)]


_RUN_LINE = re.compile(
    r"^  \[(\d+)\] (\S+) \((\w+)\) -- step (?:'([^']*)'|none), workflow .*$"
)
_OUTCOME_LINE = re.compile(r"^        outcome: (?:none|(\S+)/(\S+))$")
_HEADER_LINE = re.compile(r"^Task (\S+): (.*) \((\w+)\)$")


@dataclass(frozen=True)
class RunRecord:
    """One numbered Run block parsed from ``show-task`` stdout."""

    step: str | None
    status: str
    outcome: str | None


def parse_show_task(text: str) -> tuple[str, str, list[RunRecord]]:
    """Parse ``show-task`` stdout into (task id, task status, Run records)."""
    task_id = ""
    task_status = ""
    runs: list[RunRecord] = []
    current: dict[str, str | None] = {}
    for line in text.splitlines():
        header = _HEADER_LINE.match(line)
        if header:
            task_id, _, task_status = header.groups()
            continue
        run = _RUN_LINE.match(line)
        if run:
            if current:
                runs.append(RunRecord(step=current.get("step"),
                                      status=str(current.get("status")),
                                      outcome=current.get("outcome")))
            _index, _rid, status, step = run.groups()
            current = {"step": step, "status": status, "outcome": None}
            continue
        outcome = _OUTCOME_LINE.match(line)
        if outcome and current:
            kind, decision = outcome.groups()
            current["outcome"] = (
                None if kind is None else f"{kind}/{decision}")
    if current:
        runs.append(RunRecord(step=current.get("step"),
                              status=str(current.get("status")),
                              outcome=current.get("outcome")))
    return task_id, task_status, runs


def _context_block(run: ClaudeRun, show_stdout: str = "") -> str:
    """Failure context: exit code, stderr, census, show-task, stream tail."""
    uses = collect_tool_uses(run.stream)
    skill_names = sorted({u.input.get("skill", "?") for u in uses
                          if u.name == "Skill"})
    return (
        f"exit={run.returncode} stderr={run.stderr[-2000:]}\n"
        f"census={stream_census(run.stream)} "
        f"skill-dispatches={skill_names}\n"
        f"show-task:\n{show_stdout[-4000:]}\n"
        f"stream tail:\n{stream_text(run.stream)[-4000:]}"
    )


def test_probe_plugin_structure():
    """The probe fixture plugin keeps its manifest + fork-skill shape."""
    manifest_path = PROBE_PLUGIN_DIR / ".claude-plugin" / "plugin.json"
    assert manifest_path.is_file(), "probe plugin manifest missing"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.get("name") == "probe", "probe plugin must be named probe"

    skill_path = PROBE_PLUGIN_DIR / "skills" / "canary" / "SKILL.md"
    assert skill_path.is_file(), "probe:canary skill missing"
    lines = skill_path.read_text(encoding="utf-8").split("\n")
    assert lines[0] == "---", "canary: frontmatter must start on line 1"
    end = lines.index("---", 1)
    frontmatter = yaml.safe_load("\n".join(lines[1:end])) or {}
    assert frontmatter.get("context") == "fork", "canary must run forked"
    assert frontmatter.get("background") is False
    assert frontmatter.get("user-invocable") is False
    body = "\n".join(lines[end + 1:])
    assert "NO_CANARY" in body, "canary must define its negative reply"


def _only_task_id(repo: Path) -> str:
    """Return the single Task id in a scratch repo (read-only store open)."""
    ws = workspace.Workspace(root=repo)
    with contextlib.closing(store.open_store(ws)) as conn:
        found = [task for status in TaskStatus
                 for task in store.list_tasks_by_status(conn, status)]
    assert len(found) == 1, f"expected one Task, found {len(found)}"
    return found[0].id


@dataclass(frozen=True)
class DriverRun:
    """One full ``/skillflow:work`` invocation and its observed state."""

    repo: Path
    task_id: str
    run: ClaudeRun
    show_stdout: str


def _assert_review_tail(runs: list[RunRecord], start: int, ctx: str) -> None:
    """Assert ``runs[start:]`` is implementation/review pairs ending approved.

    The tolerated rework shape: one or more ``implementation``/``review``
    pairs, every implementation ``ready``, every mid-loop review
    ``changes_requested``, the terminal review ``approved``, every Run
    completed. Shared by A1/A5/A7.
    """
    tail = [run.step for run in runs[start:]]
    assert len(tail) >= 2 and len(tail) % 2 == 0 and tail == (
        ["implementation", "review"] * (len(tail) // 2)), (
        f"tail from index {start} is not implementation/review pairs: "
        f"{tail}\n{ctx}")
    for run in runs[start:]:
        expected = ("implementation/ready" if run.step == "implementation"
                    else "review/changes_requested")
        if run is runs[-1]:
            expected = "review/approved"
        assert run.outcome == expected, (
            f"unexpected outcome {run.outcome} for {run.step}, "
            f"expected {expected}\n{ctx}")
    assert all(run.status == "completed" for run in runs[start:]), (
        f"not every tail Run completed: "
        f"{[(r.step, r.status) for r in runs[start:]]}\n{ctx}")


@pytest.fixture(scope="module")
def driver_run(tmp_path_factory):
    """Run the driver once; A1/A2/A3-first-half assert read-only on it."""
    repo = _git_repo(tmp_path_factory.mktemp("sf-a1") / "work")
    run = run_claude(cwd=repo, prompt=A1_PROMPT, plugin_dirs=(PLUGIN_DIR,),
                     allowed_tools=A1_GRANT, timeout=DRIVER_TIMEOUT)
    if run.returncode != 0:
        pytest.fail("driver invocation failed (harness, not product):\n"
                    + _context_block(run))
    task_id = _only_task_id(repo)
    proc = run_skillflow(["show-task", task_id], cwd=repo)
    assert proc.returncode == 0, (
        f"show-task failed after driver run:\n{proc.stderr[-2000:]}")
    return DriverRun(repo=repo, task_id=task_id, run=run,
                     show_stdout=proc.stdout)


@pytest.mark.acceptance
@requires_acceptance
def test_a1_happy_path_completes_task(driver_run):
    """A1: one ``/skillflow:work`` run completes the hello.txt Task."""
    ctx = _context_block(driver_run.run, driver_run.show_stdout)
    uses = collect_tool_uses(driver_run.run.stream)

    # Harness first: the driver dispatched each Execution Skill in order.
    order = [use.input.get("skill") for use in uses
             if use.name == "Skill" and use.parent is None]
    firsts = list(dict.fromkeys(order))
    assert firsts[:4] == ["skillflow:research", "skillflow:decomposition",
                          "skillflow:implementation", "skillflow:code-review"], (
        f"driver did not dispatch the four skills in order "
        f"(saw {firsts})\n{ctx}")

    _parsed_id, task_status, runs = parse_show_task(driver_run.show_stdout)
    assert runs, f"show-task recorded no Runs\n{ctx}"

    # M1 slice prefix: research completed with research v1, decomposition
    # dispatched by the driver.
    assert (runs[0].step, runs[0].status) == ("research", "completed"), (
        f"first Run is not completed research: {runs[0]}\n{ctx}")
    assert runs[0].outcome == "research/ready", (
        f"first research outcome is not ready: {runs[0].outcome}\n{ctx}")
    ws = workspace.Workspace(root=driver_run.repo)
    with contextlib.closing(store.open_store(ws)) as conn:
        artifacts = store.list_artifacts_for_task(conn, driver_run.task_id)
    found = [(a.name, a.type, a.version) for a in artifacts]
    assert any(a.type == "research" and a.version == 1 for a in artifacts), (
        f"no research v1 artifact: {found}\n{ctx}")
    assert len(runs) >= 2 and runs[1].step == "decomposition", (
        f"driver did not dispatch decomposition second: "
        f"{[r.step for r in runs]}\n{ctx}")

    # Full A1: terminal Task, tolerated rework shape, per-step outcomes.
    assert task_status == "completed", (
        f"Task is not completed (status {task_status})\n{ctx}")
    steps = [run.step for run in runs]
    assert steps[:3] == ["research", "decomposition", "implementation"], (
        f"unexpected step prefix {steps}\n{ctx}")
    assert runs[1].outcome == "decomposition/ready", (
        f"decomposition outcome is not ready: {runs[1].outcome}\n{ctx}")
    _assert_review_tail(runs, 2, ctx)
    assert all(run.status == "completed" for run in runs), (
        f"not every Run completed: {[(r.step, r.status) for r in runs]}\n{ctx}")

    hello = driver_run.repo / "hello.txt"
    assert hello.is_file(), f"hello.txt was not created\n{ctx}"
    assert "hello" in hello.read_text(encoding="utf-8"), (
        f"hello.txt does not contain hello\n{ctx}")


_FIND_WORD = re.compile(r"\bfind\b")
_SEARCH_WORDS = re.compile(r"\b(grep|rg|fd)\b")
_CONTEXT_LINE = re.compile(r"^  - (\S+) \((\S+) v(\d+)\): (\S+)$")
_RUN_INPUT_LINE = re.compile(
    r"^Run (\S+) \(running\) -- step '([^']*)' via skill '([^']*)'$")
_RESOLVE_COMMAND = re.compile(r"(?:^|[;&|])\s*skillflow resolve-task\b")
_SKILLFLOW_COMMAND = re.compile(r"(?:^|[;&|])\s*skillflow ([a-z-]+|--version)\b")


def _resolve_task_outputs(
        uses: list[ToolUse], results: list[ToolResult]
) -> list[tuple[int, str, list[str]]]:
    """Parse top-level ``resolve-task`` results into (index, step, paths)."""
    by_id = {result.tool_use_id: result for result in results}
    outputs: list[tuple[int, str, list[str]]] = []
    for use in uses:
        if use.parent is not None or use.name != "Bash":
            continue
        if not _RESOLVE_COMMAND.search(use.input.get("command", "")):
            continue
        result = by_id.get(use.id)
        if result is None:
            continue
        step: str | None = None
        paths: list[str] = []
        for line in result.text.splitlines():
            run_line = _RUN_INPUT_LINE.match(line)
            if run_line:
                step = run_line.group(2)
            context = _CONTEXT_LINE.match(line)
            if context:
                paths.append(context.group(4))
        if step is not None:
            outputs.append((use.index, step, paths))
    return sorted(outputs)


def _repo_rel(repo: Path, file_path: str) -> str:
    """Normalize a Read path to repo-relative POSIX form for comparison.

    Both sides resolve symlinks: scratch repos live under a symlinked
    tmp root on macOS (``/var`` → ``private/var``) while shells report
    physical paths, so a lexical ``relative_to`` alone would miss an
    absolute fork path.
    """
    path = Path(file_path)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(repo.resolve()).as_posix()
        except (ValueError, OSError):
            return file_path
    return path.as_posix()


def test_repo_rel_resolves_symlinked_repo(tmp_path):
    """Absolute fork paths match printed paths across a symlinked root."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    printed = ".skillflow/artifacts/task-x/research-v1.md"
    assert _repo_rel(link, str(real / printed)) == printed
    assert _repo_rel(link, printed) == printed
    assert _repo_rel(link, "/elsewhere/research-v1.md") == (
        "/elsewhere/research-v1.md")


def _tail_record(step: str, outcome: str,
                 status: str = "completed") -> RunRecord:
    """Build a ``RunRecord`` for ``_assert_review_tail`` cases."""
    return RunRecord(step=step, status=status, outcome=outcome)


def test_review_tail_shape():
    """``_assert_review_tail`` accepts pairs, rejects anything else."""
    ok_one = [_tail_record("implementation", "implementation/ready"),
              _tail_record("review", "review/approved")]
    _assert_review_tail(ok_one, 0, "ctx")
    ok_two = [_tail_record("implementation", "implementation/ready"),
              _tail_record("review", "review/changes_requested"),
              _tail_record("implementation", "implementation/ready"),
              _tail_record("review", "review/approved")]
    _assert_review_tail(ok_two, 0, "ctx")
    # A leading prefix is skipped via ``start``.
    _assert_review_tail(
        [_tail_record("research", "research/ready"),
         _tail_record("decomposition", "decomposition/ready"), *ok_one],
        2, "ctx")

    bad_step = [_tail_record("implementation", "implementation/ready"),
                _tail_record("research", "research/ready")]
    with pytest.raises(AssertionError):
        _assert_review_tail(bad_step, 0, "ctx")
    bad_mid_outcome = [_tail_record("implementation", "implementation/ready"),
                       _tail_record("review", "review/approved"),
                       _tail_record("implementation", "implementation/ready"),
                       _tail_record("review", "review/approved")]
    with pytest.raises(AssertionError):
        _assert_review_tail(bad_mid_outcome, 0, "ctx")
    bad_terminal = [_tail_record("implementation", "implementation/ready"),
                    _tail_record("review", "review/changes_requested")]
    with pytest.raises(AssertionError):
        _assert_review_tail(bad_terminal, 0, "ctx")
    bad_status = [_tail_record("implementation", "implementation/ready"),
                  _tail_record("review", "review/approved",
                               status="running")]
    with pytest.raises(AssertionError):
        _assert_review_tail(bad_status, 0, "ctx")
    with pytest.raises(AssertionError):
        _assert_review_tail(ok_one, 2, "ctx")


def _chain_row(artifact_id: str, version: int, run_id: str,
               supersedes_id: str | None) -> tuple[str, int, str, str | None]:
    """Build an ``_assert_version_chain`` row: (id, version, run, parent)."""
    return (artifact_id, version, run_id, supersedes_id)


def test_version_chain_shape():
    """``_assert_version_chain`` accepts v1 → vN, rejects broken chains."""
    live = {"r-live-1", "r-live-2"}
    ok_two = [_chain_row("a1", 1, "r-seed", None),
              _chain_row("a2", 2, "r-live-1", "a1")]
    _assert_version_chain(ok_two, first_owner="r-seed", live_ids=live,
                          label="review", ctx="ctx")
    # A tolerated rework cycle grows the review chain past v2: still valid.
    ok_three = [*ok_two, _chain_row("a3", 3, "r-live-2", "a2")]
    _assert_version_chain(ok_three, first_owner="r-seed", live_ids=live,
                          label="review", ctx="ctx")

    with pytest.raises(AssertionError):
        _assert_version_chain(ok_two[:1], first_owner="r-seed",
                              live_ids=live, label="review", ctx="ctx")
    gap = [_chain_row("a1", 1, "r-seed", None),
           _chain_row("a3", 3, "r-live-1", "a1")]
    with pytest.raises(AssertionError):
        _assert_version_chain(gap, first_owner="r-seed", live_ids=live,
                              label="review", ctx="ctx")
    wrong_owner = [_chain_row("a1", 1, "r-other", None),
                   _chain_row("a2", 2, "r-live-1", "a1")]
    with pytest.raises(AssertionError):
        _assert_version_chain(wrong_owner, first_owner="r-seed",
                              live_ids=live, label="review", ctx="ctx")
    broken_link = [_chain_row("a1", 1, "r-seed", None),
                   _chain_row("a2", 2, "r-live-1", "a0")]
    with pytest.raises(AssertionError):
        _assert_version_chain(broken_link, first_owner="r-seed",
                              live_ids=live, label="review", ctx="ctx")
    foreign_run = [_chain_row("a1", 1, "r-seed", None),
                   _chain_row("a2", 2, "r-seed", "a1")]
    with pytest.raises(AssertionError):
        _assert_version_chain(foreign_run, first_owner="r-seed",
                              live_ids=live, label="review", ctx="ctx")


@pytest.mark.acceptance
@requires_acceptance
def test_a2_forks_read_declared_context(driver_run):
    """A2: decomposition/review forks read the printed artifact paths."""
    ctx = _context_block(driver_run.run, driver_run.show_stdout)
    uses = collect_tool_uses(driver_run.run.stream)
    results = collect_tool_results(driver_run.run.stream)

    # Harness: resolve-task outputs name the printed context per step.
    resolved = _resolve_task_outputs(uses, results)
    assert resolved, f"no resolve-task output found in stream\n{ctx}"

    dispatches = [use for use in uses
                  if use.name == "Skill" and use.parent is None]
    repo = driver_run.repo.resolve()
    checked = 0
    for dispatch in dispatches:
        skill = dispatch.input.get("skill", "")
        if skill not in ("skillflow:decomposition", "skillflow:code-review"):
            continue
        # Pair the dispatch with its most recent preceding resolve output.
        preceding = [entry for entry in resolved if entry[0] < dispatch.index]
        assert preceding, f"no resolve-task output precedes {skill}\n{ctx}"
        _resolve_index, step, expected = preceding[-1]
        assert expected, (
            f"{skill} (step {step}) printed no context paths\n{ctx}")
        fork_uses = [use for use in uses if use.parent == dispatch.id]
        assert fork_uses, f"{skill} fork shows no tool activity\n{ctx}"
        # A context access is a Read of a printed path or a Bash command
        # naming one (the review skill permits Bash inspection): both
        # consume declared context without repo discovery. Absolute forms
        # use the resolved repo (see _repo_rel).
        forms = set(expected) | {
            (repo / path).as_posix() for path in expected}
        accesses: list[tuple[int, str]] = []
        for use in fork_uses:
            if use.name == "Read":
                rel = _repo_rel(repo, use.input.get("file_path", ""))
                if rel in expected:
                    accesses.append((use.index, f"Read {rel}"))
                elif ".skillflow/artifacts" in rel:
                    pytest.fail(f"{skill} fork read {rel}, not a printed "
                                f"path ({expected})\n{ctx}")
            elif use.name == "Bash":
                command = use.input.get("command", "")
                if _FIND_WORD.search(command):
                    # A find enumerates; even over a printed path it is a
                    # discovery operation, never a context read.
                    if ".skillflow/artifacts" in command and not any(
                            form in command for form in forms):
                        pytest.fail(f"{skill} fork referenced an unprinted "
                                    f"artifact path: {command[:200]}\n{ctx}")
                elif any(form in command for form in forms):
                    accesses.append((use.index, f"Bash {command[:120]}"))
                elif ".skillflow/artifacts" in command:
                    pytest.fail(f"{skill} fork referenced an unprinted "
                                f"artifact path: {command[:200]}\n{ctx}")
        summary = [(use.name, str(use.input.get("command")
                                   or use.input.get("file_path", ""))[:100])
                   for use in fork_uses]
        assert accesses, (
            f"{skill} fork never accessed a printed context path "
            f"(printed {expected}; fork {summary})\n{ctx}")
        first = min(index for index, _ in accesses)
        for use in fork_uses:
            if use.index >= first:
                continue
            assert use.name not in ("Glob", "Grep"), (
                f"{skill} fork used {use.name} before its first "
                f"context access\n{ctx}")
            if use.name == "Bash":
                command = use.input.get("command", "")
                assert not _FIND_WORD.search(command), (
                    f"{skill} fork ran find before its first context "
                    f"access: {command[:200]}\n{ctx}")
                if _SEARCH_WORDS.search(command):
                    assert any(form in command for form in forms), (
                        f"{skill} fork searched the repo before its first "
                        f"context access: {command[:200]}\n{ctx}")
        checked += 1
    assert checked >= 2, (
        f"expected decomposition + review forks, checked {checked}\n{ctx}")
    assert not [use for use in uses if use.name in ("Glob", "Grep")], (
        f"Glob/Grep used despite the session grant omitting them\n{ctx}")


@pytest.mark.acceptance
@requires_acceptance
def test_a3_fork_tool_calls_have_parent(driver_run):
    """A3 (first half): forked tool calls carry a non-null parent link."""
    ctx = _context_block(driver_run.run, driver_run.show_stdout)
    uses = collect_tool_uses(driver_run.run.stream)

    dispatches = [use for use in uses
                  if use.name == "Skill" and use.parent is None
                  and str(use.input.get("skill", "")).startswith("skillflow:")]
    assert dispatches, f"no Execution Skill dispatch found\n{ctx}"
    dispatch_ids = {use.id for use in dispatches}

    fork_uses = [use for use in uses if use.parent is not None]
    assert fork_uses, f"no forwarded fork tool calls in the stream\n{ctx}"
    orphans = [use for use in fork_uses if use.parent not in dispatch_ids]
    assert not orphans, (
        f"fork tool calls with unknown parents: "
        f"{[(u.name, u.parent) for u in orphans[:5]]}\n{ctx}")
    for dispatch in dispatches:
        assert any(use.parent == dispatch.id for use in fork_uses), (
            f"dispatch {dispatch.input.get('skill')} has no parent-linked "
            f"fork activity\n{ctx}")
    assert not [use for use in fork_uses if use.name == "Skill"], (
        f"a fork dispatched another Skill (SF-A-7 I12)\n{ctx}")

    for use in uses:
        if use.parent is not None:
            continue
        if use.name == "Skill":
            assert use.id in dispatch_ids, (
                f"unexpected top-level Skill dispatch "
                f"{use.input.get('skill')}\n{ctx}")
        elif use.name == "Bash":
            match = _SKILLFLOW_COMMAND.search(use.input.get("command", ""))
            assert match and match.group(1) in (
                "start", "assignment", "resolve-task", "--version"), (
                f"unexpected top-level Bash: "
                f"{use.input.get('command', '')[:200]}\n{ctx}")
        else:
            pytest.fail(f"unexpected top-level {use.name} use\n{ctx}")


@pytest.mark.acceptance
@requires_acceptance
def test_a3_probe_cannot_see_canary(tmp_path):
    """A3 (second half): a fork skill cannot see a driver-prompt canary."""
    validate_plugin(PROBE_PLUGIN_DIR)
    repo = _git_repo(tmp_path / "probe")
    canary_hex = uuid.uuid4().hex[:12]
    run = run_claude(
        cwd=repo,
        prompt=("Isolation check. The canary token is CANARY-"
                f"{canary_hex}. Invoke the Skill tool with skill "
                "'probe:canary' exactly once, with no arguments, then "
                "report what it replies. Do not pass the token to the skill."),
        plugin_dirs=(PROBE_PLUGIN_DIR,),
        allowed_tools=("Skill",),
        timeout=PROBE_TIMEOUT,
    )
    ctx = _context_block(run)
    assert run.returncode == 0, f"probe invocation failed\n{ctx}"
    uses = collect_tool_uses(run.stream)

    # Harness: the agent actually dispatched the probe skill.
    dispatches = find_skill_dispatches(uses, "probe:canary")
    assert dispatches, f"probe:canary was never invoked\n{ctx}"
    dispatch = dispatches[0]

    # The fork's reply is observable only in its Skill tool_result: a
    # tool-less fork emits no forwarded blocks (calibrated). That result
    # text is harness-delivered, not parent-authored, so it is the
    # authentic fork output; the parent session legitimately echoes the
    # canary when reporting and is out of scope here.
    results = collect_tool_results(run.stream)
    replies = [result.text for result in results
               if result.tool_use_id == dispatch.id]
    assert replies, f"no Skill result for the probe dispatch\n{ctx}"
    probe_text = replies[0]
    assert canary_hex not in probe_text, (
        f"canary leaked into the fork: {probe_text[:500]}\n{ctx}")
    assert "NO_CANARY" in probe_text, (
        f"probe did not report NO_CANARY: {probe_text[:500]}\n{ctx}")


@pytest.mark.acceptance
@requires_acceptance
def test_a4_wrong_skill_rejected(tmp_path):
    """A4: the wrong skill yields AssignmentMismatch with no state change."""
    repo = _git_repo(tmp_path / "a4")
    proc = run_skillflow(["start", "--title", "A4 wrong skill",
                          "--description", "A4"], cwd=repo)
    assert proc.returncode == 0, f"start failed:\n{proc.stderr[-2000:]}"
    task_id = proc.stdout.strip().splitlines()[-1]
    proc = run_skillflow(["resolve-task", task_id], cwd=repo)
    assert proc.returncode == 0, (
        f"resolve-task failed:\n{proc.stderr[-2000:]}")
    match = re.search(r"^Run (\S+) \(running\)", proc.stdout, re.M)
    assert match, f"no running Run in resolve-task output:\n{proc.stdout}"
    run_id = match.group(1)

    run = run_claude(
        cwd=repo,
        prompt=("Invoke the Skill tool with skill "
                "'skillflow:implementation' exactly once, with no "
                "arguments. Do nothing else; then report the result "
                "verbatim."),
        plugin_dirs=(PLUGIN_DIR,),
        allowed_tools=("Skill", "Bash"),
        timeout=A4_TIMEOUT,
    )
    show = run_skillflow(["show-task", task_id], cwd=repo)
    ctx = _context_block(run, show.stdout)
    assert run.returncode == 0, f"wrong-skill invocation failed\n{ctx}"
    uses = collect_tool_uses(run.stream)

    # Harness: the agent actually dispatched the wrong skill.
    dispatches = find_skill_dispatches(uses, "skillflow:implementation")
    assert dispatches, "skillflow:implementation was never invoked\n" + ctx

    # Product: the assignment guard rejected the mismatch.
    assert "AssignmentMismatch" in stream_text(run.stream), (
        "no AssignmentMismatch in the stream\n" + ctx)

    # Post-state: R1 still running, no artifacts, no Result.
    assert show.returncode == 0, f"show-task failed:\n{show.stderr[-2000:]}"
    _parsed_id, _status, runs = parse_show_task(show.stdout)
    assert len(runs) == 1, f"expected one Run, saw {len(runs)}\n{ctx}"
    assert runs[0].status == "running", (
        f"Run is no longer running: {runs[0]}\n{ctx}")
    assert runs[0].outcome is None, (
        f"Run gained an outcome: {runs[0].outcome}\n{ctx}")
    assert "Artifacts: none" in show.stdout, (
        f"Run gained artifacts\n{ctx}")
    assert "Result: none" in show.stdout, (
        f"Run gained a Result\n{ctx}")
    ws = workspace.Workspace(root=repo)
    with contextlib.closing(store.open_store(ws)) as conn:
        assert store.get_result_for_run(conn, run_id) is None, (
            f"Result row exists for {run_id}\n{ctx}")
        assert store.list_artifacts_for_run(conn, run_id) == [], (
            f"artifacts exist for {run_id}\n{ctx}")
    proc = run_skillflow(["assignment"], cwd=repo)
    assert proc.returncode == 0 and run_id in proc.stdout, (
        f"assignment no longer shows {run_id}:\n{proc.stderr[-2000:]}\n{ctx}")

    # Secondary: the skill body was never reached.
    first_dispatch = min(dispatch.index for dispatch in dispatches)
    for use in uses:
        if use.index <= first_dispatch:
            continue
        if use.name == "Bash":
            command = use.input.get("command", "")
            assert "complete-run" not in command and "fail-run" not in command, (
                f"wrong skill completed/failed the Run: {command[:200]}\n{ctx}")
        if use.name == "Write":
            assert ".skillflow/runs/" not in use.input.get("file_path", ""), (
                f"wrong skill wrote Run output\n{ctx}")


def _start_task(repo: Path, title: str, description: str) -> str:
    """Start a Task via the CLI; fail with stderr when it is rejected."""
    proc = run_skillflow(["start", "--title", title,
                          "--description", description], cwd=repo)
    assert proc.returncode == 0, f"start failed:\n{proc.stderr[-2000:]}"
    return proc.stdout.strip().splitlines()[-1]


def _resolve_running(repo: Path, task_id: str) -> str:
    """Resolve one running Run via the CLI and return its Run id."""
    proc = run_skillflow(["resolve-task", task_id], cwd=repo)
    assert proc.returncode == 0, (
        f"resolve-task failed:\n{proc.stderr[-2000:]}")
    match = re.search(r"^Run (\S+) \(running\)", proc.stdout, re.M)
    assert match, f"no running Run in resolve-task output:\n{proc.stdout}"
    return match.group(1)


def _complete_cli_run(repo: Path, *, outcome: str,
                      submissions: list[tuple[str, str, Path]]) -> None:
    """Complete the workspace's running Run via the CLI.

    ``submissions`` are (name, type, file) triples; files may live
    anywhere (absolute paths keep the scratch repo pristine).
    """
    args = ["complete-run", "--outcome", outcome]
    for name, type_, path in submissions:
        args += ["--artifact", f"{name}:{type_}:{path}"]
    proc = run_skillflow(args, cwd=repo)
    assert proc.returncode == 0, (
        f"complete-run --outcome {outcome} failed:\n{proc.stderr[-2000:]}")


#: Fixed seed contents for the A7 assumption loop (first four STEPS rows of
#: ``test_e2e_fundamental_assumption.py``): minimal but directive, so the
#: live research skill can replan and the live implementation skill treats
#: the superseded verdict as stale.
_SEED_RESEARCH = ("# research\n"
                  "The task needs hello.txt containing the greeting.\n")
_SEED_PLAN = "# plan\n1. Write hello.txt containing hello.\n"
_SEED_REVIEW = ("# review\nverdict: fundamental_assumption_wrong\n"
                "The plan assumed the required content without checking the "
                "task description; re-verify the exact file content before "
                "implementing.\n")


def _seed_assumption_loop(repo: Path, scratch: Path) -> tuple[str, list[str]]:
    """Seed research → decomposition → implementation → review via the CLI.

    The review completes with ``fundamental_assumption_wrong``; seed files
    live under ``scratch`` (outside the repo). Returns (task id, [R1..R4]).
    """
    scratch.mkdir(parents=True, exist_ok=True)
    research_path = scratch / "research.md"
    research_path.write_text(_SEED_RESEARCH, encoding="utf-8")
    plan_path = scratch / "plan.md"
    plan_path.write_text(_SEED_PLAN, encoding="utf-8")
    review_path = scratch / "review.md"
    review_path.write_text(_SEED_REVIEW, encoding="utf-8")

    task_id = _start_task(repo, "A7 assumption recovery",
                          "Create hello.txt containing hello")
    # Strictly interleaved: each running Run completes before the next
    # resolves (a second resolve while one runs is ActiveRunExists).
    run_ids = [_resolve_running(repo, task_id)]
    _complete_cli_run(
        repo, outcome="ready",
        submissions=[("research.md", "research", research_path)])
    run_ids.append(_resolve_running(repo, task_id))
    _complete_cli_run(
        repo, outcome="ready",
        submissions=[("plan.md", "plan", plan_path)])
    run_ids.append(_resolve_running(repo, task_id))
    _complete_cli_run(repo, outcome="ready", submissions=[])
    run_ids.append(_resolve_running(repo, task_id))
    _complete_cli_run(
        repo, outcome="fundamental_assumption_wrong",
        submissions=[("review.md", "review", review_path)])
    return task_id, run_ids


def _top_level_skill_order(uses: list[ToolUse]) -> list[str | None]:
    """Top-level ``Skill`` dispatch values in stream order (harness helper)."""
    return [use.input.get("skill") for use in uses
            if use.name == "Skill" and use.parent is None]


def _top_level_bash_commands(uses: list[ToolUse]) -> list[tuple[int, str]]:
    """(index, command) of every top-level ``Bash`` use (harness helper)."""
    return [(use.index, use.input.get("command", "")) for use in uses
            if use.name == "Bash" and use.parent is None]


def _assert_version_chain(
        entries: list[tuple[str, int, str, str | None]], *,
        first_owner: str, live_ids: set[str], label: str, ctx: str) -> None:
    """Assert artifact rows form a contiguous v1 → vN version chain.

    ``entries`` are (id, version, run_id, supersedes_id) in version order:
    versions run 1..N without gaps (a tolerated rework cycle grows the
    review chain past v2, so no fixed length is assumed), v1 is owned by
    ``first_owner``, and every later version supersedes its predecessor
    and is owned by a live Run. Shared by the A7 chain checks; covered by
    ``test_version_chain_shape``.
    """
    versions = [version for _, version, _, _ in entries]
    assert len(versions) >= 2 and versions == list(
        range(1, len(versions) + 1)), (
        f"no contiguous v1 → vN chain for {label}: {entries}\n{ctx}")
    assert entries[0][2] == first_owner, (
        f"{label} v1 is not owned by {first_owner}: {entries[0]}\n{ctx}")
    for prev, cur in zip(entries, entries[1:], strict=False):
        assert cur[3] == prev[0], (
            f"{label} v{cur[1]} does not supersede v{prev[1]}: "
            f"{cur} vs {prev}\n{ctx}")
        assert cur[2] in live_ids, (
            f"{label} v{cur[1]} is not owned by a live Run: {cur}\n{ctx}")


def _assert_hello_built(repo: Path, ctx: str) -> None:
    """Assert the hello.txt product outcome (shared by A5/A7)."""
    hello = repo / "hello.txt"
    assert hello.is_file(), f"hello.txt was not created\n{ctx}"
    assert "hello" in hello.read_text(encoding="utf-8"), (
        f"hello.txt does not contain hello\n{ctx}")


@pytest.mark.acceptance
@requires_acceptance
def test_a5_interrupted_run_redispatched(tmp_path):
    """A5: ``/skillflow:work`` re-dispatches the interrupted research Run."""
    repo = _git_repo(tmp_path / "a5")
    task_id = _start_task(repo, "A5 interruption",
                          "Create hello.txt containing hello")
    r1 = _resolve_running(repo, task_id)

    run = run_claude(cwd=repo, prompt=CONTINUE_PROMPT,
                     plugin_dirs=(PLUGIN_DIR,), allowed_tools=A1_GRANT,
                     timeout=A5_TIMEOUT)
    show = run_skillflow(["show-task", task_id], cwd=repo)
    ctx = _context_block(run, show.stdout)
    assert run.returncode == 0, f"driver invocation failed\n{ctx}"
    uses = collect_tool_uses(run.stream)
    results = collect_tool_results(run.stream)

    # Harness: the driver re-dispatched research without resolving anew.
    firsts = list(dict.fromkeys(_top_level_skill_order(uses)))
    assert firsts[:1] == ["skillflow:research"], (
        f"driver did not re-dispatch research first (saw {firsts})\n{ctx}")
    first_dispatch = min(use.index for use in uses
                         if use.name == "Skill" and use.parent is None)
    assert not [entry for entry in _resolve_task_outputs(uses, results)
                if entry[0] < first_dispatch], (
        f"a resolve-task output precedes the first dispatch: "
        f"a second Run was resolved before R1 completed\n{ctx}")
    assert not [command for index, command in _top_level_bash_commands(uses)
                if index < first_dispatch
                and _RESOLVE_COMMAND.search(command)], (
        f"a resolve-task command precedes the first dispatch: the driver "
        f"did not take the assignment re-dispatch path\n{ctx}")
    assert any(_ASSIGNMENT_COMMAND.search(command)
               and index < first_dispatch
               for index, command in _top_level_bash_commands(uses)), (
        f"no top-level skillflow assignment before the first dispatch\n{ctx}")
    assert _only_task_id(repo) == task_id, (
        f"driver created or switched Tasks\n{ctx}")

    # Product: R1 itself completed research/ready; the loop ran to completion.
    assert show.returncode == 0, f"show-task failed:\n{show.stderr[-2000:]}"
    _parsed_id, task_status, runs = parse_show_task(show.stdout)
    assert runs, f"show-task recorded no Runs\n{ctx}"
    assert (runs[0].step, runs[0].status) == ("research", "completed"), (
        f"first Run is not completed research: {runs[0]}\n{ctx}")
    assert runs[0].outcome == "research/ready", (
        f"first research outcome is not ready: {runs[0].outcome}\n{ctx}")
    assert task_status == "completed", (
        f"Task is not completed (status {task_status})\n{ctx}")
    steps = [run.step for run in runs]
    assert steps[:3] == ["research", "decomposition", "implementation"], (
        f"unexpected step prefix {steps}\n{ctx}")
    assert runs[1].outcome == "decomposition/ready", (
        f"decomposition outcome is not ready: {runs[1].outcome}\n{ctx}")
    _assert_review_tail(runs, 2, ctx)
    assert all(run.status == "completed" for run in runs), (
        f"not every Run completed: {[(r.step, r.status) for r in runs]}\n{ctx}")
    _assert_hello_built(repo, ctx)

    # R1's identity survived: the completed first Run is the seeded one,
    # with its Result and research v1 — no second research Run exists.
    ws = workspace.Workspace(root=repo)
    with contextlib.closing(store.open_store(ws)) as conn:
        stored = store.list_runs_for_task(conn, task_id)
        assert stored and stored[0].id == r1, (
            f"first stored Run is not the seeded R1 {r1}: "
            f"{[r.id for r in stored]}\n{ctx}")
        assert [r.step_id for r in stored].count("research") == 1, (
            f"more than one research Run: "
            f"{[(r.id, r.step_id) for r in stored]}\n{ctx}")
        result = store.get_result_for_run(conn, r1)
        assert result is not None and result.outcome is not None and (
            result.outcome.type, result.outcome.decision) == (
            "research", "ready"), (
            f"R1 has no research/ready Result: {result}\n{ctx}")
        r1_artifacts = store.list_artifacts_for_run(conn, r1)
        assert any(a.type == "research" and a.version == 1
                   for a in r1_artifacts), (
            f"R1 holds no research v1: "
            f"{[(a.name, a.type, a.version) for a in r1_artifacts]}\n{ctx}")


@pytest.mark.acceptance
@requires_acceptance
def test_a7_assumption_recovery(tmp_path):
    """A7: the driver recovers from ``fundamental_assumption_wrong``."""
    repo = _git_repo(tmp_path / "a7")
    task_id, seeded = _seed_assumption_loop(repo, tmp_path / "a7-seed")

    run = run_claude(cwd=repo, prompt=CONTINUE_PROMPT,
                     plugin_dirs=(PLUGIN_DIR,), allowed_tools=A1_GRANT,
                     timeout=A7_TIMEOUT)
    show = run_skillflow(["show-task", task_id], cwd=repo)
    ctx = _context_block(run, show.stdout)
    assert run.returncode == 0, f"driver invocation failed\n{ctx}"
    uses = collect_tool_uses(run.stream)
    results = collect_tool_results(run.stream)

    # Harness: research re-entry first, then the normal skill order.
    firsts = list(dict.fromkeys(_top_level_skill_order(uses)))
    assert firsts[:4] == ["skillflow:research", "skillflow:decomposition",
                          "skillflow:implementation", "skillflow:code-review"], (
        f"driver did not dispatch the four skills in order "
        f"(saw {firsts})\n{ctx}")
    first_dispatch = min(use.index for use in uses
                         if use.name == "Skill" and use.parent is None
                         and use.input.get("skill") == "skillflow:research")
    preceding = [entry for entry in _resolve_task_outputs(uses, results)
                 if entry[0] < first_dispatch and entry[1] == "research"]
    assert preceding, (
        f"no research resolve-task output precedes the first research "
        f"dispatch\n{ctx}")
    _resolve_index, _step, paths = preceding[-1]
    store_prefix = f".skillflow/artifacts/{task_id}"
    assert paths == [f"{store_prefix}/review-v1.md",
                     f"{store_prefix}/plan-v1.md",
                     f"{store_prefix}/research-v1.md"], (
        f"re-entered research context is not review/plan/research v1: "
        f"{paths}\n{ctx}")

    # Product: seeded prefix, live research re-entry, completion.
    assert show.returncode == 0, f"show-task failed:\n{show.stderr[-2000:]}"
    _parsed_id, task_status, runs = parse_show_task(show.stdout)
    assert len(runs) >= 8, f"expected at least 8 Runs, saw {len(runs)}\n{ctx}"
    assert [run.step for run in runs[:4]] == [
        "research", "decomposition", "implementation", "review"], (
        f"unexpected seeded prefix {[r.step for r in runs[:4]]}\n{ctx}")
    assert [run.outcome for run in runs[:4]] == [
        "research/ready", "decomposition/ready", "implementation/ready",
        "review/fundamental_assumption_wrong"], (
        f"unexpected seeded outcomes {[r.outcome for r in runs[:4]]}\n{ctx}")
    assert task_status == "completed", (
        f"Task is not completed (status {task_status})\n{ctx}")
    assert runs[4].step == "research" and runs[4].status == "completed", (
        f"fifth Run is not completed research: {runs[4]}\n{ctx}")
    assert runs[4].outcome in ("research/replan", "research/ready"), (
        f"re-entered research outcome is not replan/ready: "
        f"{runs[4].outcome}\n{ctx}")
    assert (runs[5].step, runs[5].status) == (
        "decomposition", "completed"), (
        f"sixth Run is not completed decomposition: {runs[5]}\n{ctx}")
    assert runs[5].outcome == "decomposition/ready", (
        f"re-decomposition outcome is not ready: {runs[5].outcome}\n{ctx}")
    _assert_review_tail(runs, 6, ctx)
    assert all(run.status == "completed" for run in runs), (
        f"not every Run completed: {[(r.step, r.status) for r in runs]}\n{ctx}")
    _assert_hello_built(repo, ctx)

    # The runtime decided the re-entry: R5's provenance names the seeded
    # review Run with the verdict as reason; each artifact type forms a
    # v1 → v2 chain owned by the seeded then the live Run.
    ws = workspace.Workspace(root=repo)
    with contextlib.closing(store.open_store(ws)) as conn:
        stored = store.list_runs_for_task(conn, task_id)
        assert [r.id for r in stored[:4]] == seeded, (
            f"seeded Runs differ: {[r.id for r in stored[:4]]} vs "
            f"{seeded}\n{ctx}")
        assert stored[4].triggered_by_run_id == seeded[3], (
            f"R5 was not triggered by the seeded review: "
            f"{stored[4].triggered_by_run_id}\n{ctx}")
        assert stored[4].trigger_reason == "fundamental_assumption_wrong", (
            f"R5 trigger reason is not the verdict: "
            f"{stored[4].trigger_reason}\n{ctx}")
        live_ids = {r.id for r in stored[4:]}
        artifacts = store.list_artifacts_for_task(conn, task_id)
        rows: dict[str, list[tuple[str, int, str, str | None]]] = {}
        for artifact in artifacts:
            rows.setdefault(artifact.type, []).append(
                (artifact.id, artifact.version, artifact.run_id,
                 artifact.supersedes_id))
        # Research and decomposition each run exactly once live, so their
        # chains stop at v2; review re-runs on every tolerated rework
        # cycle, so its chain only promises contiguity from v1.
        for type_, seed_index in (("research", 0), ("plan", 1)):
            chain = sorted(rows.get(type_, []), key=lambda row: row[1])
            assert len(chain) == 2, (
                f"expected exactly v1 → v2 for {type_}: {chain}\n{ctx}")
            _assert_version_chain(chain, first_owner=seeded[seed_index],
                                  live_ids=live_ids, label=type_, ctx=ctx)
        review_chain = sorted(rows.get("review", []),
                              key=lambda row: row[1])
        _assert_version_chain(review_chain, first_owner=seeded[3],
                              live_ids=live_ids, label="review", ctx=ctx)
