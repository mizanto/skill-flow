"""Claude Code acceptance suite (SF-49): the real vertical slice.

Drives the shipped plugin (``plugins/skillflow/``) through ``claude -p``
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

Running:

```bash
SKILLFLOW_ACCEPTANCE=1 uv run pytest tests/acceptance/test_claude_code.py
```

Default ``uv run pytest`` skips the live tests (only
``test_probe_plugin_structure`` runs: pure file reads). The live tests
need the ``claude`` CLI, an authenticated account, and API quota; each
``claude -p`` run costs real money (full module ≈2–3 min at current
model speeds: one driver run plus two short runs). They never run in
CI.

Design notes:

* A1/A2/A3-first-half share one module-scoped ``driver_run`` fixture
  (one expensive invocation, read-only assertions). The slice check
  (research completed + research v1 + decomposition dispatched) is the
  committed prefix of A1.
* Harness assertions precede product assertions in every test, so agent
  non-compliance and product breakage fail differently.
* Calibrated against ``claude`` 2.1.236: ``stream-json`` requires
  ``--verbose``; ``--allowedTools`` is variadic, so the prompt goes
  immediately after ``-p`` and grants are comma-separated; the parent
  link is the top-level ``parent_tool_use_id`` (null in-session,
  ``toolu_…`` in forks); a skill's ``!`` preamble is load context and is
  not streamed as tool calls.
* SF-51 will extend this module (A5 interruption, A7 assumption
  recovery, A6 manual checklist). Helpers stay generic over
  prompt/plugin/grant/timeout for that reuse.
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
PLUGIN_DIR = REPO_ROOT / "plugins" / "skillflow"
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
    tail = steps[2:]
    assert len(tail) % 2 == 0 and tail == ["implementation", "review"] * (
        len(tail) // 2), (
        f"tail after decomposition is not implementation/review pairs: "
        f"{tail}\n{ctx}")
    assert runs[1].outcome == "decomposition/ready", (
        f"decomposition outcome is not ready: {runs[1].outcome}\n{ctx}")
    for run in runs[2:]:
        expected = ("implementation/ready" if run.step == "implementation"
                    else "review/changes_requested")
        if run is runs[-1]:
            expected = "review/approved"
        assert run.outcome == expected, (
            f"unexpected outcome {run.outcome} for {run.step}, "
            f"expected {expected}\n{ctx}")
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
