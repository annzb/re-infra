"""Compare the config with AWS and decide what `apply` would do. Never mutates anything.

Creating and discarding a change set to preview an update is the only write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from rc_infra.aws import Aws, ResourceChange, Stack
from rc_infra.env_config import (
    CORE_STACK_PREFIX,
    IDENTITY_STACK_NAME,
    PLATFORM_STACK_NAME,
    PROTECTED_ENVIRONMENTS,
    EnvConfig,
    environment_from_core_stack,
)
from rc_infra.teardown import TeardownRefused, check_deletable, inventory
from rc_infra.templates import (
    ENVIRONMENT_TEMPLATE_PATH,
    IDENTITY_TEMPLATE_PATH,
    MANAGED_BY_TAG,
    MANAGED_BY_VALUE,
    PLATFORM_TEMPLATE_PATH,
    environment_tags,
    identity_tags,
    load_template,
    platform_tags,
    template_body,
)

PLATFORM_TARGET = "platform"
IDENTITY_TARGET = "identity"
SHARED_TARGETS = frozenset({PLATFORM_TARGET, IDENTITY_TARGET})

# Replacing any of these destroys something irreplaceable: a user pool holds real
# accounts, and the shared Lambda role is named by ARN in every application function.
# main.yml applies unattended, so a plan that would replace one is refused outright
# rather than being left to a reviewer to notice.
REPLACEMENT_PROTECTED_TYPES = frozenset(
    {
        "AWS::Cognito::UserPool",
        "AWS::Cognito::UserPoolClient",
        "AWS::Cognito::UserPoolDomain",
        "AWS::IAM::Role",
    }
)
_REPLACEMENT_VALUES = frozenset({"True", "Conditional"})


class ActionKind(StrEnum):
    # Declaration order is display order: the things a reviewer must not miss come first.
    BLOCKED = "BLOCKED"
    DELETE = "DELETE"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    NOOP = "NOOP"


_ORDER = {kind: index for index, kind in enumerate(ActionKind)}


@dataclass(frozen=True)
class StackSpec:
    """One stack rc-infra deploys, and everything needed to deploy it."""

    target: str
    stack: str
    template_path: Path
    tags: dict[str, str]
    # Termination protection is turned on after every deploy.
    protect: bool


def stack_specs(config: EnvConfig) -> list[StackSpec]:
    return [
        StackSpec(PLATFORM_TARGET, PLATFORM_STACK_NAME, PLATFORM_TEMPLATE_PATH, platform_tags(), protect=True),
        StackSpec(IDENTITY_TARGET, IDENTITY_STACK_NAME, IDENTITY_TEMPLATE_PATH, identity_tags(), protect=True),
        *(
            StackSpec(env.name, env.core_stack, ENVIRONMENT_TEMPLATE_PATH, environment_tags(env), protect=env.protected)
            for env in config.environments
        ),
    ]


def is_halting(target: str) -> bool:
    """Whether blocking this target stops the whole apply, rather than only itself.

    A blocked preview is that preview's problem; a blocked protected environment or
    shared stack means the foundation is wrong, and applying the rest on top of it
    would be building on something nobody has looked at yet.
    """
    return target in PROTECTED_ENVIRONMENTS or target in SHARED_TARGETS


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    target: str
    stack: str
    details: tuple[str, ...] = ()
    # The existing stack must be deleted first (its creation rolled back).
    replace_failed_stack: bool = False


@dataclass(frozen=True)
class Plan:
    actions: tuple[Action, ...]
    notes: tuple[str, ...] = field(default=())

    @property
    def blocked(self) -> list[Action]:
        return [a for a in self.actions if a.kind is ActionKind.BLOCKED]

    @property
    def halting(self) -> list[Action]:
        """Blocked actions that stop the whole apply, rather than only themselves."""
        return [a for a in self.blocked if is_halting(a.target)]

    def of_kind(self, kind: ActionKind) -> list[Action]:
        return [a for a in self.actions if a.kind is kind]


def build_plan(config: EnvConfig, aws: Aws) -> Plan:
    templates: dict[Path, str] = {}
    actions = []
    for spec in stack_specs(config):
        if spec.template_path not in templates:
            templates[spec.template_path] = template_body(load_template(spec.template_path))
        actions.append(_plan_stack(spec, templates[spec.template_path], aws))
    removed, notes = _plan_removed_environments(config, aws)
    actions.extend(removed)
    actions.sort(key=lambda a: (_ORDER[a.kind], a.target))
    return Plan(actions=tuple(actions), notes=tuple(notes))


def _plan_stack(spec: StackSpec, body: str, aws: Aws) -> Action:
    """CREATE what is missing, UPDATE what differs. Only the stack itself is inspected:
    whether some other resource already uses a name is never asked, because every
    name in the templates is either generated or new to this generation."""
    stack: Stack | None = aws.stacks.get_stack(spec.stack)
    if stack is None or stack.is_pending_review or stack.is_failed_create:
        replace = stack is not None and stack.is_failed_create
        return Action(ActionKind.CREATE, spec.target, spec.stack, replace_failed_stack=replace)
    if stack.is_busy or stack.is_broken:
        return Action(
            ActionKind.BLOCKED,
            spec.target,
            spec.stack,
            details=(f"stack status is {stack.status}; resolve it before applying",),
        )
    changes = aws.stacks.preview(spec.stack, body, spec.tags)
    if not changes:
        return Action(ActionKind.NOOP, spec.target, spec.stack)
    destructive = [c for c in changes if _is_destructive(c)]
    if destructive:
        return Action(
            ActionKind.BLOCKED,
            spec.target,
            spec.stack,
            details=(
                "refusing to replace or remove resources that cannot be rebuilt:",
                *(c.describe() for c in destructive),
                "if this is intended, do it by hand -- apply will never carry it out",
            ),
        )
    return Action(ActionKind.UPDATE, spec.target, spec.stack, details=tuple(c.describe() for c in changes))


def _is_destructive(change: ResourceChange) -> bool:
    if change.resource_type not in REPLACEMENT_PROTECTED_TYPES:
        return False
    return change.replacement in _REPLACEMENT_VALUES or change.action == "Remove"


def _plan_removed_environments(config: EnvConfig, aws: Aws) -> tuple[list[Action], list[str]]:
    actions: list[Action] = []
    notes: list[str] = []
    for stack in aws.stacks.list_stacks(CORE_STACK_PREFIX):
        environment = environment_from_core_stack(stack.name)
        if environment is None or environment in config.names:
            continue
        if stack.tags.get(MANAGED_BY_TAG) != MANAGED_BY_VALUE:
            notes.append(f"ignoring {stack.name}: not tagged {MANAGED_BY_TAG}={MANAGED_BY_VALUE}")
            continue
        try:
            check_deletable(environment, stack, config.names)
            found = inventory(environment, aws)
        except TeardownRefused as exc:
            actions.append(Action(ActionKind.BLOCKED, environment, stack.name, details=(str(exc),)))
            continue
        actions.append(Action(ActionKind.DELETE, environment, stack.name, details=tuple(found.describe())))
    return actions, notes


def render_text(plan: Plan) -> str:
    lines = []
    # The destructive case must be impossible to miss in a job log.
    deletions = plan.of_kind(ActionKind.DELETE)
    if deletions:
        names = ", ".join(action.target for action in deletions)
        lines.append(f"WARNING: applying deletes {len(deletions)} environment(s): {names}, including their data.")
        lines.append("")
    halting = {action.target for action in plan.halting}
    skipped = [action for action in plan.blocked if action.target not in halting]
    if halting:
        lines.append(f"WARNING: {', '.join(sorted(halting))} is blocked; applying would refuse the whole plan.")
        lines.append("")
    for action in plan.actions:
        suffix = ""
        if action.kind is ActionKind.BLOCKED:
            suffix = " (halts the apply)" if action.target in halting else " (skipped; the rest still applies)"
        lines.append(f"{action.kind.value:<8} {action.target:<12} {action.stack}{suffix}")
        if action.replace_failed_stack:
            lines.append("         - delete the rolled-back stack first")
        lines.extend(f"         - {detail}" for detail in action.details)
    if skipped and not halting:
        lines.append(f"note: {len(skipped)} blocked target(s) will be skipped; everything else still applies.")
    lines.extend(f"note: {note}" for note in plan.notes)
    lines.append(_summary(plan))
    return "\n".join(lines)


def _summary(plan: Plan) -> str:
    counts = {kind: len(plan.of_kind(kind)) for kind in ActionKind}
    return "Summary: " + ", ".join(f"{count} {kind.value.lower()}" for kind, count in counts.items())
