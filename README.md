# SkillFlow

SkillFlow is a lightweight orchestration system for composing reusable AI agent skills into declarative workflows.

## Core idea

A **Skill** is an independent, reusable skill compatible with agents such as Claude Code and Codex. Skills are not aware of SkillFlow, workflows, or any orchestration system and can be used independently.

A **Workflow** composes existing skills into a structured sequence of steps. Workflow-specific configuration defines how a skill is used, what inputs it receives, what outputs are expected, how results affect routing, and what model level should be used.

```text
Skill
  │
  ├── standalone use
  │
  └── Workflow Step
          │
          ▼
       Workflow
          │
          ▼
      Skill Result
          │
          ▼
        Router
          │
          ▼
    Next Action / Decision
```

## Design principles

- **Skills are independent** — a skill must not depend on SkillFlow or a specific workflow.
- **Skills are reusable** — the same skill can be used standalone or in multiple workflows.
- **Workflows are declarative** — workflow behavior should be described by configuration rather than hardcoded logic.
- **Runtime is generic** — adding a new workflow should not require changes to the engine.
- **Deterministic first** — routing, state management, validation, and other mechanical operations should be handled by code, not LLMs.
- **LLMs for reasoning** — models should be used only where semantic understanding, analysis, generation, or reasoning is actually required.
- **Model-agnostic** — workflows and skills should express model requirements by capability/level rather than by a specific model.
- **Human-in-the-loop** — the initial system recommends the next action; future versions may safely automate execution while retaining engineer approval for ambiguous or risky decisions.
- **Keep it simple** — introduce abstractions only when they are justified by real requirements.
