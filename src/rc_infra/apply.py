"""Carry out a plan: deploy the platform, identity and environment stacks, then tear down removed ones."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from rc_infra.aws import Aws, ChangeSetKind
from rc_infra.env_config import EnvConfig
from rc_infra.planner import Action, ActionKind, Plan, StackSpec, stack_specs
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

    specs = {spec.target: spec for spec in stack_specs(config)}
    result = ApplyResult()
    # Creates and updates first, deletions last.
    for action in sorted(plan.actions, key=lambda a: a.kind is ActionKind.DELETE):
        if action.kind is ActionKind.BLOCKED:
            log(f"{action.target}: SKIPPED: blocked, see the plan above")
            result.failed.append(f"{action.target}: blocked")
            continue
        try:
            if action.kind is ActionKind.DELETE:
                teardown(action.target, aws, config.names, log)
            elif action.kind in _CHANGE_SETS:
                _deploy(action, specs[action.target], aws, log)
        except Exception as exc:  # one environment's failure must not stop the others
            log(f"{action.target}: FAILED: {exc}")
            result.failed.append(f"{action.target}: {exc}")
        else:
            result.completed.append(action.target)
    return result


def _deploy(action: Action, spec: StackSpec, aws: Aws, log: Callable[[str], None]) -> None:
    if action.replace_failed_stack:
        log(f"{action.target}: deleting rolled-back stack {action.stack}")
        aws.stacks.delete_stack(action.stack)

    kind = _CHANGE_SETS[action.kind]
    log(f"{action.target}: {kind.value.lower()} {action.stack}")
    changes = aws.stacks.deploy(action.stack, kind, template_body(load_template(spec.template_path)), spec.tags)
    for change in changes:
        log(f"{action.target}:   {change.describe()}")
    if not changes:
        log(f"{action.target}:   no changes")

    if spec.protect:
        stack = aws.stacks.get_stack(action.stack)
        if stack is not None and not stack.termination_protection:
            log(f"{action.target}: enabling termination protection on {action.stack}")
            aws.stacks.set_termination_protection(action.stack, True)
