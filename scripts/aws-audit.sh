#!/usr/bin/env bash
# Read-only AWS parity audit. See local_docs/plan2/aws-audit.md for what each section checks.
#
# Every AWS call is a GET/LIST/DESCRIBE. Output is JSON under $AUDIT_DIR (default out/aws-parity-<UTC>),
# which is gitignored and may contain sensitive configuration. Secret values, passwords and Lambda
# environment variables are never fetched; provider/client secrets are stripped before saving.
#
#   scripts/aws-audit.sh                      # profile "retribe"
#   AWS_PROFILE=other scripts/aws-audit.sh
#   AUDIT_BUCKET=b AUDIT_KEY=k AUDIT_VERSION=v scripts/aws-audit.sh   # also sample one object
set -uo pipefail

export AWS_PROFILE="${AWS_PROFILE:-retribe}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_PAGER=''
export AUDIT_DIR="${AUDIT_DIR:-$PWD/out/aws-parity-$(date -u +%Y%m%dT%H%M%SZ)}"
umask 077
mkdir -p "$AUDIT_DIR"

aws sts get-caller-identity --output json > "$AUDIT_DIR/caller.json" || exit 1
AUDIT_ACCOUNT=$(jq -r .Account "$AUDIT_DIR/caller.json")
if [ "$AUDIT_ACCOUNT" != '273268178059' ]; then
  echo 'Wrong account: stop and select the correct profile.' >&2
  exit 1
fi
# Preserve errors: missing configuration is different from AccessDenied.
# All operations below are GET/LIST/DESCRIBE APIs.
collect() {
  local label="$1"
  shift
  local destination="$AUDIT_DIR/$label"
  mkdir -p "$(dirname "$destination")"
  if aws "$@" --output json > "$destination.json" 2> "$destination.error.txt"; then
    rm -f "$destination.error.txt"
  else
    rm -f "$destination.json"
    printf 'Inspect %s.error.txt\n' "$destination" >&2
  fi
}

# Filter potentially sensitive fields before saving the response.
collect_filtered() {
  local label="$1" filter="$2"
  shift 2
  local destination="$AUDIT_DIR/$label"
  mkdir -p "$(dirname "$destination")"
  if (set -o pipefail; aws "$@" --output json | jq "$filter") \
      > "$destination.json" 2> "$destination.error.txt"; then
    rm -f "$destination.error.txt"
  else
    rm -f "$destination.json"
    printf 'Inspect %s.error.txt\n' "$destination" >&2
  fi
}

# ---- CloudFormation
collect stacks cloudformation describe-stacks
collect stack-inventory cloudformation list-stacks

jq -r '.Stacks[].StackName | select(startswith("rc-"))' \
  "$AUDIT_DIR/stacks.json" > "$AUDIT_DIR/stack-names.txt"

while IFS= read -r stack; do
  collect "cfn/$stack/resources" cloudformation list-stack-resources --stack-name "$stack"
  collect "cfn/$stack/original" cloudformation get-template --stack-name "$stack" --template-stage Original
  collect "cfn/$stack/processed" cloudformation get-template --stack-name "$stack" --template-stage Processed
  collect "cfn/$stack/policy" cloudformation get-stack-policy --stack-name "$stack"
done < "$AUDIT_DIR/stack-names.txt"
# ---- S3
collect buckets s3api list-buckets
collect account-public-access s3control get-public-access-block --account-id "$AUDIT_ACCOUNT"

jq -r '.Buckets[].Name | select(startswith("rc-"))' \
  "$AUDIT_DIR/buckets.json" > "$AUDIT_DIR/bucket-names.txt"

while IFS= read -r bucket; do
  for operation in \
    get-bucket-location \
    get-bucket-encryption \
    get-public-access-block \
    get-bucket-ownership-controls \
    get-bucket-acl \
    get-bucket-policy \
    get-bucket-policy-status \
    get-bucket-cors \
    get-bucket-versioning \
    get-bucket-lifecycle-configuration \
    get-bucket-notification-configuration \
    get-bucket-logging \
    get-bucket-replication \
    get-bucket-tagging \
    get-object-lock-configuration \
    get-bucket-website \
    get-bucket-accelerate-configuration \
    get-bucket-request-payment \
    list-bucket-inventory-configurations \
    list-bucket-metrics-configurations \
    list-bucket-analytics-configurations \
    list-bucket-intelligent-tiering-configurations; do
    collect "s3/$bucket/$operation" s3api "$operation" --bucket "$bucket"
  done
done < "$AUDIT_DIR/bucket-names.txt"
if [ -n "${AUDIT_BUCKET:-}" ] && [ -n "${AUDIT_KEY:-}" ] && [ -n "${AUDIT_VERSION:-}" ]; then
  collect object-sample/head s3api head-object --bucket "$AUDIT_BUCKET" --key "$AUDIT_KEY"
  collect object-sample/tags s3api get-object-tagging --bucket "$AUDIT_BUCKET" --key "$AUDIT_KEY"
  collect object-sample/history s3api list-object-versions --bucket "$AUDIT_BUCKET" --prefix "$AUDIT_KEY"
  collect object-sample/historical-head s3api head-object --bucket "$AUDIT_BUCKET" --key "$AUDIT_KEY" --version-id "$AUDIT_VERSION"
  # Run retention/legal-hold reads only if Object Lock is configured.
  collect object-sample/retention s3api get-object-retention --bucket "$AUDIT_BUCKET" --key "$AUDIT_KEY" --version-id "$AUDIT_VERSION"
  collect object-sample/legal-hold s3api get-object-legal-hold --bucket "$AUDIT_BUCKET" --key "$AUDIT_KEY" --version-id "$AUDIT_VERSION"
fi

# ---- Cognito
cat > "$AUDIT_DIR/old-pools.tsv" <<'POOLS'
prod us-east-1_1GIFBpLKf
staging us-east-1_udXBXbTYS
dev us-east-1_cbi939AdR
preview us-east-1_LwH5lWQ0q
POOLS

collect cognito/pool-inventory cognito-idp list-user-pools --max-results 60

# Include all rc-prefixed pools, covering new v3 pools after they exist.
{
  cat "$AUDIT_DIR/old-pools.tsv"
  jq -r '.UserPools[] | select(.Name | startswith("rc-")) | [.Name,.Id] | @tsv' \
    "$AUDIT_DIR/cognito/pool-inventory.json"
} | sort -u > "$AUDIT_DIR/all-pools.tsv"

while read -r profile pool; do
  label="cognito/$pool"
  collect "$label/pool" cognito-idp describe-user-pool --user-pool-id "$pool"
  collect "$label/mfa" cognito-idp get-user-pool-mfa-config --user-pool-id "$pool"
  collect "$label/risk" cognito-idp describe-risk-configuration --user-pool-id "$pool"
  collect "$label/log-delivery" cognito-idp get-log-delivery-configuration --user-pool-id "$pool"
  collect "$label/clients" cognito-idp list-user-pool-clients --user-pool-id "$pool" --max-results 60
  collect "$label/providers" cognito-idp list-identity-providers --user-pool-id "$pool" --max-results 60
  collect "$label/groups" cognito-idp list-groups --user-pool-id "$pool"
  collect "$label/resource-servers" cognito-idp list-resource-servers --user-pool-id "$pool" --max-results 50
  collect "$label/ui-default" cognito-idp get-ui-customization --user-pool-id "$pool" --client-id ALL

  if [ -f "$AUDIT_DIR/$label/clients.json" ]; then
    while IFS= read -r client; do
      collect_filtered "$label/client-$client" 'del(.UserPoolClient.ClientSecret)' \
        cognito-idp describe-user-pool-client --user-pool-id "$pool" --client-id "$client"
      collect "$label/ui-$client" cognito-idp get-ui-customization --user-pool-id "$pool" --client-id "$client"
      collect "$label/branding-$client" cognito-idp describe-managed-login-branding-by-client \
        --user-pool-id "$pool" --client-id "$client"
    done < <(jq -r '.UserPoolClients[].ClientId' "$AUDIT_DIR/$label/clients.json")
  fi

  if [ -f "$AUDIT_DIR/$label/providers.json" ]; then
    while IFS= read -r provider; do
      collect_filtered "$label/provider-$provider" \
        'del(.IdentityProvider.ProviderDetails.client_secret, .IdentityProvider.ProviderDetails.private_key)' \
        cognito-idp describe-identity-provider --user-pool-id "$pool" --provider-name "$provider"
    done < <(jq -r '.Providers[].ProviderName' "$AUDIT_DIR/$label/providers.json")
  fi

  if [ -f "$AUDIT_DIR/$label/pool.json" ]; then
    while IFS= read -r domain; do
      collect "$label/domain-$domain" cognito-idp describe-user-pool-domain --domain "$domain"
    done < <(jq -r '.UserPool | .Domain,.CustomDomain | select(. != null and . != "")' "$AUDIT_DIR/$label/pool.json")

    pool_arn=$(jq -r '.UserPool.Arn' "$AUDIT_DIR/$label/pool.json")
    collect "$label/tags" cognito-idp list-tags-for-resource --resource-arn "$pool_arn"
    collect "$label/waf" wafv2 get-web-acl-for-resource --resource-arn "$pool_arn"
  fi
done < "$AUDIT_DIR/all-pools.tsv"
# ---- SES and IAM
collect ses/account sesv2 get-account
collect ses/identities sesv2 list-email-identities
collect ses/sender sesv2 get-email-identity --email-identity 'no-reply@retribalize.ai'
collect ses/sender-policies sesv2 get-email-identity-policies --email-identity 'no-reply@retribalize.ai'
collect ses/domain sesv2 get-email-identity --email-identity 'retribalize.ai'
collect ses/config-sets sesv2 list-configuration-sets

if [ -f "$AUDIT_DIR/ses/config-sets.json" ]; then
  while IFS= read -r name; do
    collect "ses/config-$name" sesv2 get-configuration-set --configuration-set-name "$name"
    collect "ses/events-$name" sesv2 get-configuration-set-event-destinations --configuration-set-name "$name"
  done < <(jq -r '.ConfigurationSets[]' "$AUDIT_DIR/ses/config-sets.json")
fi

collect iam/roles iam list-roles
collect iam/oidc-providers iam list-open-id-connect-providers
while IFS= read -r provider_arn; do
  provider_key=$(printf '%s' "$provider_arn" | tr '/:' '__')
  collect "iam/oidc-$provider_key" iam get-open-id-connect-provider --open-id-connect-provider-arn "$provider_arn"
done < <(jq -r '.OpenIDConnectProviderList[].Arn' "$AUDIT_DIR/iam/oidc-providers.json")

# Inspect LambdaPower, CodeBuild, new platform roles, and GitHub/OIDC roles.
# Add any differently named role discovered from stacks or your Actions variable.
jq -r '.Roles[].RoleName | select(. == "LambdaPower" or startswith("rc-") or test("github|cognito"; "i"))' \
  "$AUDIT_DIR/iam/roles.json" > "$AUDIT_DIR/role-names.txt"

while IFS= read -r role; do
  collect "iam/$role/role" iam get-role --role-name "$role"
  collect "iam/$role/attached" iam list-attached-role-policies --role-name "$role"
  collect "iam/$role/inline" iam list-role-policies --role-name "$role"
  collect "iam/$role/tags" iam list-role-tags --role-name "$role"
  while IFS= read -r name; do
    collect "iam/$role/inline-$name" iam get-role-policy --role-name "$role" --policy-name "$name"
  done < <(jq -r '.PolicyNames[]' "$AUDIT_DIR/iam/$role/inline.json")

  # Include the permissions boundary as well as attached policies.
  {
    jq -r '.AttachedPolicies[].PolicyArn' "$AUDIT_DIR/iam/$role/attached.json"
    jq -r '.Role.PermissionsBoundary.PermissionsBoundaryArn // empty' "$AUDIT_DIR/iam/$role/role.json"
  } | sort -u | while IFS= read -r policy; do
    policy_key=$(printf '%s' "$policy" | tr '/:' '__')
    collect "iam/policies/$policy_key" iam get-policy --policy-arn "$policy"
    version=$(jq -r '.Policy.DefaultVersionId' "$AUDIT_DIR/iam/policies/$policy_key.json")
    collect "iam/policies/$policy_key-$version" iam get-policy-version --policy-arn "$policy" --version-id "$version"
  done
done < "$AUDIT_DIR/role-names.txt"
# ---- DynamoDB and backups
collect dynamo/tables dynamodb list-tables
jq -r '.TableNames[] | select(startswith("rc-"))' \
  "$AUDIT_DIR/dynamo/tables.json" > "$AUDIT_DIR/table-names.txt"

while IFS= read -r table; do
  label="dynamo/$table"
  collect "$label/table" dynamodb describe-table --table-name "$table"
  collect "$label/ttl" dynamodb describe-time-to-live --table-name "$table"
  collect "$label/backups" dynamodb describe-continuous-backups --table-name "$table"
  collect "$label/insights" dynamodb describe-contributor-insights --table-name "$table"
  collect "$label/kinesis" dynamodb describe-kinesis-streaming-destination --table-name "$table"
  collect "$label/replica-scaling" dynamodb describe-table-replica-auto-scaling --table-name "$table"

  if [ -f "$AUDIT_DIR/$label/table.json" ]; then
    table_arn=$(jq -r '.Table.TableArn' "$AUDIT_DIR/$label/table.json")
    collect "$label/tags" dynamodb list-tags-of-resource --resource-arn "$table_arn"
    collect "$label/resource-policy" dynamodb get-resource-policy --resource-arn "$table_arn"
    while IFS= read -r index; do
      collect "$label/index-$index-insights" dynamodb describe-contributor-insights --table-name "$table" --index-name "$index"
    done < <(jq -r '.Table.GlobalSecondaryIndexes[]?.IndexName' "$AUDIT_DIR/$label/table.json")
  fi
done < "$AUDIT_DIR/table-names.txt"

collect dynamo/scalable-targets application-autoscaling describe-scalable-targets --service-namespace dynamodb
collect dynamo/scaling-policies application-autoscaling describe-scaling-policies --service-namespace dynamodb
collect dynamo/scheduled-scaling application-autoscaling describe-scheduled-actions --service-namespace dynamodb
collect dynamo/global-tables dynamodb list-global-tables
collect backup/plans backup list-backup-plans
collect backup/protected-resources backup list-protected-resources

while IFS= read -r plan; do
  collect "backup/$plan/plan" backup get-backup-plan --backup-plan-id "$plan"
  collect "backup/$plan/selections" backup list-backup-selections --backup-plan-id "$plan"
  while IFS= read -r selection; do
    collect "backup/$plan/selection-$selection" backup get-backup-selection --backup-plan-id "$plan" --selection-id "$selection"
  done < <(jq -r '.BackupSelectionsList[].SelectionId' "$AUDIT_DIR/backup/$plan/selections.json")
done < <(jq -r '.BackupPlansList[].BackupPlanId' "$AUDIT_DIR/backup/plans.json")
# ---- Lambda, queues, schedules, ECR, CodeBuild, secrets metadata
collect lambda/functions lambda list-functions \
  --query 'Functions[].{FunctionName:FunctionName,FunctionArn:FunctionArn,Role:Role,Runtime:Runtime,PackageType:PackageType,Architectures:Architectures,Layers:Layers,MemorySize:MemorySize,Timeout:Timeout,VpcConfig:VpcConfig,LoggingConfig:LoggingConfig}'
collect lambda/event-sources lambda list-event-source-mappings

while IFS= read -r function_name; do
  collect_filtered "lambda/$function_name/config" 'del(.Environment)' \
    lambda get-function-configuration --function-name "$function_name"
  collect "lambda/$function_name/policy" lambda get-policy --function-name "$function_name"
  collect "lambda/$function_name/aliases" lambda list-aliases --function-name "$function_name"
  collect "lambda/$function_name/concurrency" lambda get-function-concurrency --function-name "$function_name"
  collect "lambda/$function_name/provisioned" lambda list-provisioned-concurrency-configs --function-name "$function_name"
  while IFS= read -r alias; do
    collect "lambda/$function_name/policy-$alias" lambda get-policy --function-name "$function_name" --qualifier "$alias"
  done < <(jq -r '.Aliases[].Name' "$AUDIT_DIR/lambda/$function_name/aliases.json")
done < <(jq -r '.[].FunctionName | select(test("post-signup|user-migration|signup-fanout|cognito|identity"))' "$AUDIT_DIR/lambda/functions.json")

# Add any other trigger function name seen in a pool's LambdaConfig to the loop.
collect sqs/queues sqs list-queues --queue-name-prefix rc-
while IFS= read -r queue; do
  name=${queue##*/}
  collect "sqs/$name/attributes" sqs get-queue-attributes --queue-url "$queue" --attribute-names All
  collect "sqs/$name/tags" sqs list-queue-tags --queue-url "$queue"
done < <(jq -r '.QueueUrls[]?' "$AUDIT_DIR/sqs/queues.json")

collect events/rules events list-rules --name-prefix rc-
while IFS= read -r rule; do
  collect "events/$rule/targets" events list-targets-by-rule --rule "$rule"
done < <(jq -r '.Rules[].Name' "$AUDIT_DIR/events/rules.json")
collect logs/groups logs describe-log-groups --log-group-name-prefix /aws/lambda/rc-
collect logs/codebuild logs describe-log-groups --log-group-name-prefix /codebuild/rc-

collect ecr/repositories ecr describe-repositories
collect ecr/scanning ecr get-registry-scanning-configuration
collect ecr/registry-policy ecr get-registry-policy
collect ecr/replication ecr describe-registry
while IFS= read -r repository; do
  collect "ecr/$repository/policy" ecr get-repository-policy --repository-name "$repository"
  collect "ecr/$repository/lifecycle" ecr get-lifecycle-policy --repository-name "$repository"
  arn=$(jq -r --arg n "$repository" '.repositories[] | select(.repositoryName == $n) | .repositoryArn' "$AUDIT_DIR/ecr/repositories.json")
  collect "ecr/$repository/tags" ecr list-tags-for-resource --resource-arn "$arn"
done < <(jq -r '.repositories[].repositoryName | select(startswith("rc-"))' "$AUDIT_DIR/ecr/repositories.json")

collect codebuild/projects codebuild list-projects
while IFS= read -r project; do
  collect_filtered "codebuild/$project" \
    '.projects |= map(.environment.environmentVariables |= map(if .type == "PLAINTEXT" then .value = "<redacted: inspect locally>" else . end))' \
    codebuild batch-get-projects --names "$project"
done < <(jq -r '.projects[] | select(startswith("rc-"))' "$AUDIT_DIR/codebuild/projects.json")
collect codebuild/source-credentials codebuild list-source-credentials
collect connections/list codeconnections list-connections
while IFS= read -r connection; do
  key=$(printf '%s' "$connection" | tr '/:' '__')
  collect "connections/$key" codeconnections get-connection --connection-arn "$connection"
done < <(jq -r '.Connections[].ConnectionArn' "$AUDIT_DIR/connections/list.json")

# Metadata only. Do not run get-secret-value for this audit.
collect secrets/metadata secretsmanager list-secrets

echo "Audit written to $AUDIT_DIR"
