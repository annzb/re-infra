"""Carry out a plan: deploy the platform and environment stacks, then tear down removed ones."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from rc_infra.aws import Aws, ChangeSetKind
from rc_infra.catalog import Catalog
from rc_infra.planner import PLATFORM_TARGET, Action, ActionKind, Plan
from rc_infra.teardown import teardown
from rc_infra.templates import (
    ENVIRONMENT_TEMPLATE_PATH,
    PLATFORM_TEMPLATE_PATH,
    environment_parameters,
    environment_tags,
    import_template,
    load_template,
    platform_tags,
    template_body,
)


class ApplyRefused(Exception):
    pass


@dataclass
class ApplyResult:
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def apply_plan(
    plan: Plan, catalog: Catalog, aws: Aws, log: Callable[[str], None] = print
) -> ApplyResult:
    if plan.blocked:
        targets = ", ".join(a.target for a in plan.blocked)
        raise ApplyRefused(f"plan has blocked actions ({targets}); nothing was applied")

    result = ApplyResult()
    # Creates and updates first, deletions last.
    for action in sorted(plan.actions, key=lambda a: a.kind is ActionKind.DELETE):
        try:
            _apply_action(action, catalog, aws, log)
        except Exception as exc:  # one environment's failure must not stop the others
            log(f"{action.target}: FAILED: {exc}")
            result.failed.append(f"{action.target}: {exc}")
        else:
            result.completed.append(action.target)
    return result


def _apply_action(action: Action, catalog: Catalog, aws: Aws, log: Callable[[str], None]) -> None:
    if action.kind is ActionKind.DELETE:
        teardown(action.target, aws, catalog.names, log)
        return

    if action.target == PLATFORM_TARGET:
        template = load_template(PLATFORM_TEMPLATE_PATH)
        parameters: dict[str, str] = {}
        tags = platform_tags()
        protect = True
    else:
        env = catalog.get(action.target)
        template = load_template(ENVIRONMENT_TEMPLATE_PATH)
        parameters = environment_parameters(env)
        tags = environment_tags(env)
        protect = env.protected

    if action.replace_failed_stack:
        log(f"{action.target}: deleting rolled-back stack {action.stack}")
        aws.stacks.delete_stack(action.stack, aws.cfn_role_arn)

    body = template_body(template)
    if action.kind is ActionKind.IMPORT:
        log(f"{action.target}: importing {', '.join(name for _, name in action.imports)}")
        aws.stacks.deploy(
            action.stack,
            ChangeSetKind.IMPORT,
            template_body(import_template(template, [lid for lid, _ in action.imports])),
            parameters,
            tags,
            aws.cfn_role_arn,
            resources_to_import=[
                {
                    "ResourceType": "AWS::S3::Bucket",
                    "LogicalResourceId": logical_id,
                    "ResourceIdentifier": {"BucketName": bucket},
                }
                for logical_id, bucket in action.imports
            ],
        )
        _deploy(action, ChangeSetKind.UPDATE, body, parameters, tags, aws, log)
    elif action.kind is ActionKind.CREATE:
        _deploy(action, ChangeSetKind.CREATE, body, parameters, tags, aws, log)
    elif action.kind is ActionKind.UPDATE:
        _deploy(action, ChangeSetKind.UPDATE, body, parameters, tags, aws, log)

    if protect:
        stack = aws.stacks.get_stack(action.stack)
        if stack is not None and not stack.termination_protection:
            log(f"{action.target}: enabling termination protection on {action.stack}")
            aws.stacks.set_termination_protection(action.stack, True)


def _deploy(
    action: Action,
    kind: ChangeSetKind,
    body: str,
    parameters: dict[str, str],
    tags: dict[str, str],
    aws: Aws,
    log: Callable[[str], None],
) -> None:
    log(f"{action.target}: {kind.value.lower()} {action.stack}")
    changes = aws.stacks.deploy(action.stack, kind, body, parameters, tags, aws.cfn_role_arn)
    for change in changes:
        log(f"{action.target}:   {change.describe()}")
    if not changes:
        log(f"{action.target}:   no changes")
