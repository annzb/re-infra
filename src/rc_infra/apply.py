"""Carry out a plan: deploy the shared stacks, then the environment stacks, then tear down removed ones.

Stacks are applied in dependency order (planner.stack_specs). If a shared stack or a
protected environment fails, nothing after it is attempted: it is the foundation the
rest is built on. A failed preview is only that preview's problem.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from rc_infra.aws import Aws, ChangeSetKind
from rc_infra.env_config import EnvConfig
from rc_infra.planner import Action, ActionKind, Plan, StackSpec, foreign_tags, is_halting, refuse_destructive, stack_specs
from rc_infra.teardown import teardown
from rc_infra.templates import load_template, template_body

_CHANGE_SETS = {ActionKind.CREATE: ChangeSetKind.CREATE, ActionKind.UPDATE: ChangeSetKind.UPDATE}


class ApplyRefused(Exception):
    pass


@dataclass
class ApplyResult:
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def apply_plan(plan: Plan, config: EnvConfig, aws: Aws, log: Callable[[str], None] = print) -> ApplyResult:
    if plan.halting:
        targets = ", ".join(a.target for a in plan.halting)
        raise ApplyRefused(f"plan blocks {targets}; nothing was applied")

    ordered = stack_specs(config)
    specs = {spec.target: spec for spec in ordered}
    position = {spec.target: index for index, spec in enumerate(ordered)}
    result = ApplyResult()
    foundation_failed: str | None = None
    # Dependency order: shared stacks, then environments; deletions last.
    for action in sorted(plan.actions, key=lambda a: (a.kind is ActionKind.DELETE, position.get(a.target, len(position)))):
        if foundation_failed is not None:
            log(f"{action.target}: SKIPPED: {foundation_failed} failed, and everything after it depends on it")
            result.failed.append(f"{action.target}: skipped after {foundation_failed} failed")
            continue
        if action.kind is ActionKind.BLOCKED:
            log(f"{action.target}: SKIPPED: blocked, see the plan above")
            result.failed.append(f"{action.target}: blocked")
            continue
        try:
            if action.kind is ActionKind.DELETE:
                teardown(action.target, aws, config.names, log)
            elif action.kind in _CHANGE_SETS:
                _deploy(action, specs[action.target], aws, log)
            elif action.repair_protection:
                _protect(action, aws, log)
        except Exception as exc:  # one environment's failure must not stop the others
            log(f"{action.target}: FAILED: {exc}")
            result.failed.append(f"{action.target}: {exc}")
            if action.kind is not ActionKind.DELETE and is_halting(action.target):
                foundation_failed = action.target
        else:
            result.completed.append(action.target)
    return result


def _deploy(action: Action, spec: StackSpec, aws: Aws, log: Callable[[str], None]) -> None:
    if action.replace_failed_stack:
        # The plan checked ownership, but the stack is read again right before deleting it.
        stack = aws.stacks.get_stack(action.stack)
        if stack is not None and (foreign := foreign_tags(spec, stack)):
            raise RuntimeError(f"{action.stack} is not owned by re-infra (mismatched tags: {foreign}); refusing to delete it")
        log(f"{action.target}: deleting rolled-back stack {action.stack}")
        aws.stacks.delete_stack(action.stack)

    kind = _CHANGE_SETS[action.kind]
    log(f"{action.target}: {kind.value.lower()} {action.stack}")
    # The executed change set is not the one the plan previewed, so it is checked again.
    guard = refuse_destructive if kind is ChangeSetKind.UPDATE else None
    body = template_body(load_template(spec.template_path))
    changes = aws.stacks.deploy(action.stack, kind, body, spec.tags, guard=guard, parameters=spec.parameters)
    for change in changes:
        log(f"{action.target}:   {change.describe()}")
    if not changes:
        log(f"{action.target}:   no changes")

    if spec.protect:
        _protect(action, aws, log)


def _protect(action: Action, aws: Aws, log: Callable[[str], None]) -> None:
    stack = aws.stacks.get_stack(action.stack)
    if stack is not None and not stack.termination_protection:
        log(f"{action.target}: enabling termination protection on {action.stack}")
        aws.stacks.set_termination_protection(action.stack, True)
