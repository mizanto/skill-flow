"""Read a Workflow Definition YAML file into the SF-6 schema (SF-7).

:mod:`skillflow.workflow` defines the shape of a Workflow Definition and owns
every *semantic* rule; this module owns *syntax and shape* and nothing else:

1. **Parse** with a ``SafeLoader`` subclass that rejects duplicate mapping keys.
   PyYAML silently keeps the last duplicate, so two ``approved:`` entries under
   one ``outcomes:`` -- or two ``skill:`` keys in one step -- would otherwise
   load as a valid definition with a silently discarded rule. The schema layer
   cannot see this: the duplicate is gone before any object exists.
2. **Validate the document shape** -- the parts that likewise vanish before
   construction: the top level is a mapping, required keys are present, and
   **unknown keys are rejected at every level**, so ``outcome:`` (a typo for
   ``outcomes:``) fails loudly instead of producing an outcome-less step.
3. **Construct** the SF-6 value objects and let their ``ValueError`` carry every
   semantic rule, re-raised as :class:`WorkflowLoadError` prefixed with the
   source and the location within it (``steps[3].outcomes['approved']``), per
   AGENTS.md principle 13.

No validation rule from :mod:`skillflow.workflow` is re-implemented here.

Three boundaries:

* **No discovery.** ``load_workflow(path)`` reads the path it is given. The
  reference definition lives at ``workflows/software-change.yaml``; how a target
  repository selects one is SF-11's decision, so this module imports
  neither :mod:`skillflow.store` nor :mod:`skillflow.workspace` and writes
  nothing to ``.skillflow/``.
* **No persistence.** A loaded ``Workflow`` is configuration, not a runtime
  entity, and binding it to ``domain.WorkflowDefinition`` by id is SF-10.
* **Not a DSL gateway.** A fixed key set at every level with unknown keys
  rejected is precisely what stops the file format drifting into a generic
  workflow DSL. Only the mapping form of an outcome rule
  (``{action: run, step: X}``) is accepted -- the shorthand ``approved:
  complete`` is not, because two syntaxes for one concept is the first step
  toward that DSL.

YAML anchors and aliases are permitted (harmless in a hand-written definition),
but **merge keys** (``<<: *base``) are rejected: inheriting one step's keys into
another is definition reuse, and that is the DSL drift above. A deliberate
alias-expansion bomb is not defended against, as definition files are local and
author-controlled.
"""

import io
from collections.abc import Mapping
from pathlib import Path

import yaml

from skillflow.workflow import (
    ExpectedOutput,
    OutcomeRule,
    Workflow,
    WorkflowStep,
)

__all__ = [
    "WorkflowLoadError",
    "load_workflow",
    "parse_workflow",
]


class WorkflowLoadError(Exception):
    """A Workflow Definition could not be read, parsed, or validated.

    One flat error class, mirroring ``store.InvariantViolationError``: callers
    distinguish causes by message, and every message names the source and the
    location within it. Deliberately **not** a ``ValueError`` subclass, so a
    caller catching this does not also swallow programming errors raised by the
    schema layer.
    """


# --- parsing -------------------------------------------------------------


_MERGE_TAG = "tag:yaml.org,2002:merge"


class _StrictYamlError(Exception):
    """Internal signal from :class:`_StrictLoader`. Never escapes parsing."""

    def __init__(self, detail: str, mark: object) -> None:
        # str(Mark) is indented and already begins 'in "<file>", line N' -- strip
        # the indent and let it continue the sentence.
        super().__init__(f"{detail} {str(mark).strip()}")


class _StrictLoader(yaml.SafeLoader):
    """``SafeLoader`` that rejects duplicate mapping keys and merge keys.

    PyYAML keeps the last duplicate silently. Overriding ``construct_mapping``
    is the standard recipe for closing that hole and is stable across PyYAML
    5.x-6.x; fixture tests fail loudly if the behaviour ever changes.

    Scanning keys here also means ``<<`` never reaches ``flatten_mapping``, so
    merge keys are rejected rather than expanded. That is deliberate: merge-key
    inheritance is definition reuse, which is exactly the DSL drift this module
    exists to prevent. Rejecting it explicitly, with a message that says so,
    beats failing with PyYAML's unregistered-tag error.
    """

    def construct_mapping(self, node, deep=False):
        seen: set = set()
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                raise _StrictYamlError(
                    "merge keys ('<<') are not supported; write each step out in full",
                    key_node.start_mark,
                )
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in seen:
                    # Locate the repeated key itself, not the mapping that
                    # contains it -- the enclosing mapping can start many lines
                    # above the fault.
                    raise _StrictYamlError(
                        f"duplicate key {key!r}", key_node.start_mark
                    )
                seen.add(key)
            except TypeError:
                # Unhashable key (e.g. `? [a, b] :`). SafeLoader rejects it
                # below with its own ConstructorError; nothing to check here.
                pass
        return super().construct_mapping(node, deep=deep)


# --- shape helpers -------------------------------------------------------

_TOP_KEYS = frozenset({"name", "steps"})
_STEP_KEYS = frozenset(
    {"id", "skill", "model", "effort", "outputs", "outcomes", "decisions"}
)
_OUTPUT_KEYS = frozenset({"type", "required"})
_RULE_KEYS = frozenset({"action", "step", "skill"})


def _fail(source: str, where: str, detail: str) -> WorkflowLoadError:
    """Build the one error shape this module raises: source, location, cause."""
    return WorkflowLoadError(f"{source}: {where}: {detail}")


def _require_mapping(
    value: object, source: str, where: str, *, empty: str | None = None
) -> Mapping:
    """Return ``value`` as a mapping, or raise naming what it actually is.

    A key present but null (``outcomes:`` with nothing under it) parses to
    ``None`` and is **rejected**, not coerced to empty. Pass ``empty="{}"`` for
    an *optional* key, to suggest writing empty explicitly; omit it where that
    advice would not work (a required key, or one that may not be empty).
    """
    if not isinstance(value, Mapping):
        raise _fail(source, where, f"expected a mapping, got {_describe(value, empty)}")
    return value


def _require_list(
    value: object, source: str, where: str, *, empty: str | None = None
) -> list:
    """Return ``value`` as a list. A ``str`` is not a list of anything here."""
    if not isinstance(value, list):
        raise _fail(source, where, f"expected a list, got {_describe(value, empty)}")
    return value


def _require_not_null(value: object, source: str, where: str) -> object:
    """Reject a key written with no value where a scalar is expected.

    ``model:`` with nothing under it is an authoring slip, not a way to say
    "unset" -- the schema would silently treat it as absent, so catch it here
    and keep "a key present with no value is an error" true everywhere.
    """
    if value is None:
        raise _fail(
            source, where, "expected a value, got nothing (remove the key if unset)"
        )
    return value


def _describe(value: object, empty: str | None) -> str:
    """Name a value for an error message, hinting at the null-key case.

    A null key is the one failure whose cause is invisible in the file, so
    where an empty literal is actually accepted the message says how to write
    it. ``empty=None`` suppresses the hint, so no message advises a fix that
    would fail (``steps: []`` is rejected as empty, for instance).
    """
    if value is None:
        if empty is None:
            return "nothing"
        return f"nothing (write '{empty}' for an empty value, or remove the key)"
    return type(value).__name__


def _check_keys(
    mapping: Mapping, allowed: frozenset[str], source: str, where: str
) -> None:
    """Reject unknown keys, naming them and the ones that are accepted."""
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    if unknown:
        raise _fail(
            source,
            where,
            f"unknown key(s) {', '.join(repr(k) for k in unknown)}; "
            f"allowed: {', '.join(sorted(allowed))}",
        )


def _require_present(mapping: Mapping, key: str, source: str, where: str) -> object:
    """Return ``mapping[key]``, or raise naming the missing required key."""
    if key not in mapping:
        raise _fail(source, where, f"missing required key {key!r}")
    return mapping[key]


def _construct(factory, kwargs: dict, source: str, where: str):
    """Build a schema object, re-raising its ``ValueError`` with a location.

    This is the single seam through which every semantic rule in
    :mod:`skillflow.workflow` reaches the caller.
    """
    try:
        return factory(**kwargs)
    except ValueError as exc:
        raise _fail(source, where, str(exc)) from exc


# --- construction --------------------------------------------------------


def _build_output(raw: object, source: str, where: str) -> ExpectedOutput:
    mapping = _require_mapping(raw, source, where)
    _check_keys(mapping, _OUTPUT_KEYS, source, where)
    _require_present(mapping, "type", source, where)
    return _construct(ExpectedOutput, dict(mapping), source, where)


def _build_rule(raw: object, source: str, where: str) -> OutcomeRule:
    if isinstance(raw, str):
        # `approved: complete` -- the shorthand from SF-A-6 §2's prose. Not
        # supported on purpose (one syntax per concept), so say so rather than
        # let the author read "expected a mapping, got str" as a typo.
        raise _fail(
            source,
            where,
            f"expected a mapping, got str: the shorthand {raw!r} is not "
            "supported; write it as '{ action: ... }'",
        )
    mapping = _require_mapping(raw, source, where)
    _check_keys(mapping, _RULE_KEYS, source, where)
    _require_present(mapping, "action", source, where)
    return _construct(OutcomeRule, dict(mapping), source, where)


def _build_rules(
    raw: object, source: str, where: str, *, empty: str | None = None
) -> dict[str, OutcomeRule]:
    mapping = _require_mapping(raw, source, where, empty=empty)
    return {
        key: _build_rule(value, source, f"{where}[{key!r}]")
        for key, value in mapping.items()
    }


def _build_step(raw: object, source: str, index: int) -> WorkflowStep:
    where = f"steps[{index}]"
    mapping = _require_mapping(raw, source, where)
    _check_keys(mapping, _STEP_KEYS, source, where)

    kwargs: dict = {
        "id": _require_present(mapping, "id", source, where),
        "skill": _require_present(mapping, "skill", source, where),
    }
    # Pass optional keys only when present, so the schema's own defaults apply
    # to absent 'model' / 'effort' / 'outputs' / 'outcomes' / 'decisions'.
    for key in ("model", "effort"):
        if key in mapping:
            kwargs[key] = _require_not_null(mapping[key], source, f"{where}.{key}")

    if "outputs" in mapping:
        raw_outputs = _require_list(
            mapping["outputs"], source, f"{where}.outputs", empty="[]"
        )
        kwargs["outputs"] = tuple(
            _build_output(item, source, f"{where}.outputs[{i}]")
            for i, item in enumerate(raw_outputs)
        )
    for key in ("outcomes", "decisions"):
        if key in mapping:
            kwargs[key] = _build_rules(
                mapping[key], source, f"{where}.{key}", empty="{}"
            )

    return _construct(WorkflowStep, kwargs, source, where)


def parse_workflow(text: str, *, source: str = "<string>") -> Workflow:
    """Parse Workflow Definition YAML into a validated ``Workflow``.

    ``source`` names the origin in error messages. Raises
    :class:`WorkflowLoadError` for any syntax, shape, or schema violation.
    """
    # Parse from a named stream, not the bare string: PyYAML labels its
    # line/column marks with the stream's 'name', so the marks in a syntax
    # error name the actual file instead of "<unicode string>".
    stream = io.StringIO(text)
    stream.name = source
    try:
        # _StrictLoader subclasses SafeLoader -- no arbitrary object construction.
        document = yaml.load(stream, Loader=_StrictLoader)
    except _StrictYamlError as exc:
        raise _fail(source, "workflow", str(exc)) from exc
    except yaml.YAMLError as exc:
        # PyYAML's message carries the line/column marks -- keep them.
        raise _fail(source, "workflow", f"invalid YAML: {exc}") from exc

    mapping = _require_mapping(document, source, "workflow")
    _check_keys(mapping, _TOP_KEYS, source, "workflow")
    name = _require_present(mapping, "name", source, "workflow")
    raw_steps = _require_present(mapping, "steps", source, "workflow")
    # No 'empty' hint: 'steps: []' is rejected as empty and removing the key is
    # a missing-key error, so neither suggestion would help the author.
    steps = _require_list(raw_steps, source, "workflow.steps")

    built = tuple(_build_step(raw, source, i) for i, raw in enumerate(steps))
    # Cross-step rules -- duplicate step ids, unknown 'step' references in both
    # 'outcomes' and 'decisions' -- are enforced by Workflow.__post_init__.
    return _construct(Workflow, {"name": name, "steps": built}, source, "workflow")


def load_workflow(path: Path | str) -> Workflow:
    """Read a Workflow Definition YAML file and return a validated ``Workflow``.

    Every failure -- a missing path, a directory, undecodable bytes, invalid
    YAML, or a schema violation -- raises :class:`WorkflowLoadError` naming the
    path. A bad path is a definition problem for this caller, not an ``OSError``
    to be handled separately.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowLoadError(f"{path}: cannot read file: {exc}") from exc
    except UnicodeDecodeError as exc:
        # UnicodeDecodeError is a ValueError, not an OSError -- without this it
        # would escape as a bare ValueError from a function that promises
        # WorkflowLoadError.
        raise WorkflowLoadError(f"{path}: not valid UTF-8: {exc}") from exc
    return parse_workflow(text, source=str(path))
