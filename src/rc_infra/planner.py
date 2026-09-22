"""Compare the config with AWS and decide what `apply` would do. Never mutates anything.

Creating and discarding a change set to preview an update is the only write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from rc_infra.aws import Aws, ImportTarget, ResourceChange, Stack
from rc_infra.buckets import config_diff
from rc_infra.env_config import (
    BUCKET_LOGICAL_IDS,
    CORE_STACK_PREFIX,
    PLATFORM_STACK_NAME,
    PROTECTED_ENVIRONMENTS,
    RESOURCE_PREFIX,
    EnvConfig,
    Environment,
    environment_from_core_stack,
)
from rc_infra.teardown import TeardownRefused, check_deletable, inventory
from rc_infra.templates import (
    ENVIRONMENT_TEMPLATE_PATH,
    MANAGED_BY_TAG,
    MANAGED_BY_VALUE,
    PLATFORM_TEMPLATE_PATH,
    bucket_properties,
    environment_parameters,
    environment_tags,
    load_template,
    platform_import_targets,
    platform_tags,
    template_body,
)

PLATFORM_TARGET = "platform"

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
    IMPORT = "IMPORT"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    NOOP = "NOOP"


_ORDER = {kind: index for index, kind in enumerate(ActionKind)}


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    target: str
    stack: str
    details: tuple[str, ...] = ()
    # Existing resources adopted by an IMPORT.
    imports: tuple[ImportTarget, ...] = ()
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
        return [a for a in self.blocked if a.target in PROTECTED_ENVIRONMENTS or a.target == PLATFORM_TARGET]

    def of_kind(self, kind: ActionKind) -> list[Action]:
        return [a for a in self.actions if a.kind is kind]


def build_plan(config: EnvConfig, aws: Aws) -> Plan:
    actions = [_plan_platform(config, aws)]
    environment_template = load_template(ENVIRONMENT_TEMPLATE_PATH)
    actions.extend(_plan_environment(env, aws, environment_template) for env in config.environments)
    removed, notes = _plan_removed_environments(config, aws)
    actions.extend(removed)
    actions.sort(key=lambda a: (_ORDER[a.kind], a.target))
    return Plan(actions=tuple(actions), notes=tuple(notes))


def _plan_platform(config: EnvConfig, aws: Aws) -> Action:
    template = load_template(PLATFORM_TEMPLATE_PATH)
    stack = aws.stacks.get_stack(PLATFORM_STACK_NAME)

    def on_missing() -> Action:
        replace = stack is not None and stack.is_failed_create
        # Almost everything in rc-platform predates this repository: the Cognito pools
        # hold real accounts and the role is named by ARN across the application stack.
        # Creating a second copy is never right, so whatever exists is adopted and only
        # the genuinely new resources are created by the update that follows.
        targets = platform_import_targets(config, template)
        existing = [target for target in targets if aws.resources.exists(target.resource_type, target.identifier)]
        if not existing:
            return Action(ActionKind.CREATE, PLATFORM_TARGET, PLATFORM_STACK_NAME, replace_failed_stack=replace)
        adopted = {target.logical_id for target in existing}
        created = sorted(set(template["Resources"]) - adopted)
        return Action(
            ActionKind.IMPORT,
            PLATFORM_TARGET,
            PLATFORM_STACK_NAME,
            details=tuple(f"then create {logical_id}" for logical_id in created),
            imports=tuple(existing),
            replace_failed_stack=replace,
        )

    return _plan_existing_or_create(
        target=PLATFORM_TARGET,
        stack_name=PLATFORM_STACK_NAME,
        stack=stack,
        aws=aws,
        body=template_body(template),
        parameters={},
        tags=platform_tags(),
        on_missing=on_missing,
    )


def _plan_environment(env: Environment, aws: Aws, template: dict[str, Any]) -> Action:
    stack = aws.stacks.get_stack(env.core_stack)

    def on_missing() -> Action:
        replace = stack is not None and stack.is_failed_create
        existing = [(BUCKET_LOGICAL_IDS[purpose], purpose, name) for purpose, name in env.buckets.items() if aws.buckets.exists(name)]
        if not existing:
            return Action(ActionKind.CREATE, env.name, env.core_stack, replace_failed_stack=replace)

        problems: list[str] = []
        advisories: list[str] = []
        for _, purpose, name in existing:
            owner = aws.buckets.owner_stack(name)
            if owner and owner != env.core_stack:
                problems.append(f"bucket {name} already belongs to stack {owner}")
                continue
            for difference in config_diff(bucket_properties(template, purpose), aws.buckets.live_config(name)):
                target = problems if difference.blocking else advisories
                target.append(f"bucket {name}: {difference.message}")
        if problems:
            return Action(
                ActionKind.BLOCKED,
                env.name,
                env.core_stack,
                details=(
                    "existing buckets cannot be imported until the template matches them:",
                    *problems,
                ),
            )
        created = sorted(set(env.buckets.values()) - {name for _, _, name in existing})
        return Action(
            ActionKind.IMPORT,
            env.name,
            env.core_stack,
            details=(
                *(f"the update after the import will reconcile {advisory}" for advisory in advisories),
                *(f"then create bucket {name}" for name in created),
            ),
            imports=tuple(ImportTarget(logical_id, "AWS::S3::Bucket", {"BucketName": name}, name) for logical_id, _, name in existing),
            replace_failed_stack=replace,
        )

    return _plan_existing_or_create(
        target=env.name,
        stack_name=env.core_stack,
        stack=stack,
        aws=aws,
        body=template_body(template),
        parameters=environment_parameters(env),
        tags=environment_tags(env),
        on_missing=on_missing,
    )


def _plan_existing_or_create(
    *,
    target: str,
    stack_name: str,
    stack: Stack | None,
    aws: Aws,
    body: str,
    parameters: dict[str, str],
    tags: dict[str, str],
    on_missing: Any,
) -> Action:
    if stack is None or stack.is_pending_review or stack.is_failed_create:
        action: Action = on_missing()
        return action
    if stack.is_busy or stack.is_broken:
        return Action(
            ActionKind.BLOCKED,
            target,
            stack_name,
            details=(f"stack status is {stack.status}; resolve it before applying",),
        )
    changes = aws.stacks.preview(stack_name, body, parameters, tags)
    if not changes:
        return Action(ActionKind.NOOP, target, stack_name)
    destructive = [c for c in changes if _is_destructive(c)]
    if destructive:
        return Action(
            ActionKind.BLOCKED,
            target,
            stack_name,
            details=(
                "refusing to replace or remove resources that cannot be rebuilt:",
                *(c.describe() for c in destructive),
                "if this is intended, do it by hand -- apply will never carry it out",
            ),
        )
    return Action(ActionKind.UPDATE, target, stack_name, details=tuple(c.describe() for c in changes))


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
        details = found.describe()
        legacy_app_stack = f"{RESOURCE_PREFIX}-{environment}"
        if aws.stacks.get_stack(legacy_app_stack) is not None:
            details.append(f"legacy app stack {legacy_app_stack} is not managed here and is kept")
        actions.append(Action(ActionKind.DELETE, environment, stack.name, details=tuple(details)))
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
        lines.extend(f"         - import {target.logical_id} ({target.describe})" for target in action.imports)
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
