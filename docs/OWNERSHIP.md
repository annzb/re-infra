# Resource ownership

This file lists every nontrivial AWS resource behind Retribalize, says which repository owns it, and states its lifecycle. `re-infra` owns resources that outlive a deployment: shared platform resources, identity, and each environment's durable buckets. `retribalize-core` owns the application runtime it deploys into each slot. Core reads every physical identifier it needs from `rc-infra outputs` and never builds one from a naming convention.

## Categories

| Category | Meaning | Lifecycle |
|---|---|---|
| **platform-shared** | One per account, used by every environment (ECR, shared execution role, identity) | Retain; never replaced; changed only through `rc-infra apply` |
| **persistence-environment** | Durable data belonging to one environment/slot (buckets; DynamoDB tables) | Created when the env is added to `envs.yaml`; Retain; deleted only when the env is removed |
| **application-slot** | Created and destroyed with one core app stack `rc-app-<slot>` | Owned by core's SAM template; safe to replace |
| **deployment-pipeline** | Build/deploy machinery (CodeBuild, artifact/cache buckets, deploy locks) | Core today; AWS-native CI/CD deferred |
| **secret/config only** | Values core consumes but no stack creates (Secrets Manager names, external URLs) | Managed by hand; referenced by name |
| **legacy/retire** | Superseded resources and templates still in the repo | Remove once nothing deployed uses them |

## re-infra resources

| Stack | Logical resource (type) | Category |
|---|---|---|
| rc-platform (`infra/platform.yaml`) | `LambdaBaseImageRepository` (ECR, `rc-lambda-base`, untagged expire 30d) | platform-shared |
| rc-platform | `ApiImageRepository` (ECR, `rc-api-v2`, no lifecycle) | platform-shared |
| rc-platform | `MatchingImageRepository` (ECR, `rc-matching-v2`, no lifecycle) | platform-shared |
| rc-platform | `SharedLambdaExecutionRole` (IAM::Role, generated name, PowerUserAccess) -> `SharedLambdaExecutionRoleArn` | platform-shared |
| rc-identity (`infra/identity.yaml`) | `{Prod,Staging,Dev,Preview}UserPool` (Cognito, `rc-<profile>-user-pool-v3`) | platform-shared |
| rc-identity | `{Prod,Staging,Dev,Preview}UserPoolClient` (no secret, COGNITO only) | platform-shared |
| rc-identity | `{Prod,Staging,Dev,Preview}UserPoolDomain` (`rc-<profile>-v3`) | platform-shared |
| rc-env-&lt;env&gt; (`infra/environment.yaml`) | `EmbeddingsBucket`, `UserCorpusBucket` (uploads/ 7d), `AvatarsBucket` (public), `RecordingsBucket`, `SchemaDumpsBucket` (30d), `PropertyRegistryBucket` (versioned); all S3, generated names, outputs `<Logical>Name`/`<Logical>Arn` | persistence-environment |

Every resource above is `Retain`. One `rc-env-<env>` stack exists per `envs.yaml` entry (prod, staging, dev, preview1-8, preview67, preview89). Each slot gets its own buckets.

## retribalize-core resources

Line numbers are approximate and point into `backend/template-v2.yaml` unless another file is named.

| Resource | Category | Owner after migration | Notes |
|---|---|---|---|
| 180 `AWS::Serverless::Function` (8 `PackageType: Image`) | application-slot | core | All named `${OrganizationName}-${Slot}-*`. 184 literal `Role:` ARNs, see below |
| `CommonLayer` (LayerVersion, ~488) | application-slot | core | `RetentionPolicy: Retain` |
| `ApiGateway` (Serverless::Api, ~417) + `CognitoAuthorizerFunction` (~400) | application-slot | core | The authorizer reads the `CognitoUserPoolId` parameter. 181 `Api` events |
| `PresenceWebSocketApi`/Stage/3 Integrations/3 Routes (~2439-2535) | application-slot | core | |
| SQS: Email, Push, Broadcast, SignupFanout queues + their 4 DLQs (~540-611, ~812-826) | application-slot | core | Named per slot |
| Step Functions: `ClarificationPipeline` (~6311), `DocumentUploadPipeline` (~6537), `WelcomeEmailStateMachine` (~7043) + 2 log groups | application-slot | core | |
| IAM: `ContributionPipelineRole` (~6281, named per slot, PowerUserAccess), `WelcomeEmailStateMachineRole` (~7024) | application-slot | core | These are state-machine roles. Lambdas use the shared role |
| `ScoutCloudFrontDistribution` (~5548) + Scout function URL (~5523) | application-slot | core | |
| `OpsAlertTopic`/`OpsAlertEmailSubscription` (SNS, ~6945), 3 CloudWatch alarms (~6959-7002) | application-slot | core | Prod only (`HasOpsAlerts`) |
| 8 `Schedule` events (EventBridge rules via SAM) | application-slot | core | |
| 6 `AWS::Lambda::Permission`: PostSignup (~803), UserMigration (~868), 3 Presence (~2536-2556), `MatchingContributionsS3Permission` (~4721) | application-slot | core | The S3 permission's `SourceArn` is built from the `UserCorpusBucket` mapping value. The two Cognito permissions use the `CognitoUserPoolId` parameter |
| User-corpus S3 notifications (`backend/scripts/setup_corpus_triggers.py`, run from `deployment/buildspec-deploy.yml` ~276) | application-slot | core | Writes notification config onto a re-infra bucket. It has to merge per slot only because buckets are shared today; with per-slot buckets it can own the whole config |
| Cognito triggers (pool `LambdaConfig`) | platform-shared wiring | deferred (re-infra) | The v3 pools have no `LambdaConfig`. Core owns the functions and permissions |
| DynamoDB tables `rc-<env>-*` (`deploy_dynamo_tables.py`, via `rc_dynamo` schema sync) | persistence-environment | core | Decision D3. `ensure_deployment_lock_table.py` creates `rc-deployment-locks` (deployment-pipeline) |
| Schema dump bucket (`deploy_dynamo_tables.py` ~361, creates on demand; falls back to `EMBEDDINGS_BUCKET` ~792) | persistence-environment | re-infra (`SchemaDumpsBucket`) | Core should stop calling `create_bucket` |
| `infra/ecr.yaml` stack `rc-ecr`: `rc-api`, `rc-matching` + 7 per-function `rc-matching-*` repos | platform-shared | re-infra (`rc-api-v2`, `rc-matching-v2`); old repos retire | Retire once no slot runs an image from them. Also `prepare_ecr_catalog.py`, `resolve_image_repositories.py` |
| `infra/codebuild.yaml` stack `rc-codebuild`: `CodeBuildRole` (`rc-codebuild-deploy`), `rc-build`, `rc-deploy-{dev,staging,prod,preview}`, `ArtifactsBucket`, `CacheBucket` | deployment-pipeline | deferred (core for now) | `prepare_codebuild_infrastructure.py`. `CacheBucket` has a literal name (~227) |
| `backend/scripts/create-cognito-pools.sh` (v2 pools, domains, IdPs, clients, triggers) | legacy/retire | retire | Replaced by rc-identity. Contains secrets, see below |
| `backend/scripts/create-durable-persistence.sh` (users/messages/query-history tables; embeddings/avatars/user-corpus buckets) | legacy/retire | retire | Bucket half replaced by rc-env-&lt;env&gt;; table half by schema sync |
| `backend/scripts/create-property-registry-bucket.sh` | legacy/retire | retire | Replaced by `PropertyRegistryBucket` |
| `backend/scout-standalone.yaml`, `backend/tenet-pipelines-standalone.yaml` | legacy/retire | retire | Standalone test stacks. The tenet one has 12 `LambdaPower` roles and the `retribalize-users`/`retribalize-embeddings` names |
| `backend/src/_legacy/deploy.sh.legacy` | legacy/retire | retire | Has Cognito literals |
| Secrets Manager names in `Mappings.EnvironmentConfig` (Stripe, Supabase, LiveKit, email, GHL, Kit, Anthropic, Datadog ARN ~77) | secret/config only | core | Referenced by name. Not created by any stack |

## Hardcoded identifiers to remove from core

| Where | What | Replaced by (`rc-infra outputs`) |
|---|---|---|
| `config/deployment-environments.json` (`cognito_pool`/`cognito_client`/`cognito_domain` in prod, staging, dev, preview, preview67, preview89, ~lines 10-72) | v2 pool IDs, client IDs, `rc-*-v2` domains | `identity.user_pool_id`, `identity.client_id`, `identity.oauth_domain` |
| `deployment/buildspec-deploy.yml` ~220-222 | Passes the above as `CognitoUserPoolId/ClientId/OAuthDomain` | Same fields, read from the contract |
| `backend/template-v2.yaml` ~787 `FANOUT_TARGETS` | All 4 v2 pool IDs + `rc-<tier>-users` table names | `identity.user_pool_id` per profile (needs a cross-env contract or a redesign; see D2) |
| `frontend/.env.{dev,staging,preview,prod}` lines 11-13; `frontend/eas.json` ~20-22, ~33-35 | `EXPO_PUBLIC_COGNITO_*` pool/client/domain | `identity.*` injected at build time |
| `backend/scripts/seed-cognito.py` ~20-23; `backend/scripts/reset-dev-user.sh` ~39-63 | Pool IDs per env | `identity.user_pool_id` |
| `backend/template-v2.yaml` `Mappings.EnvironmentConfig` ~129-141, 165-171, 195-201, 225-233, 261-269, 297-307 | 30 literal `rc-<tier>-{property-registry,embeddings,user-corpus,avatars,recordings}-273268178059` names (6 tiers x 5) | `buckets.<purpose>.name` (`<Logical>BucketName`); ARNs from `buckets.<purpose>.arn` |
| `backend/scripts/resolve_environment_config.py` ~46-48 | Reads bucket names from that mapping | Contract `buckets.*.name` |
| `backend/template-v2.yaml`, 184 occurrences (first ~403, last ~6910) | `Role: arn:aws:iam::273268178059:role/LambdaPower` | One template parameter set from `platform.lambda_execution_role_arn` (`SharedLambdaExecutionRoleArn`) |
| `backend/template-v2.yaml` ~54, ~62 (`AllowedPattern`) | `rc-api` / `rc-matching` repo names | `platform.api_repository_uri` / `platform.matching_repository_uri` (repos `rc-api-v2`/`rc-matching-v2`) |
| `infra/ecr.yaml`; `backend/scripts/resolve_image_repositories.py` ~68; `validate_build_manifest.py` ~59-60; `verify_container_images.py` | rc-ecr stack and repo names | `platform.*_repository_uri`; delete the rc-ecr preparation step |
| `backend/scripts/deploy_dynamo_tables.py` ~792 | Dump bucket defaults to `EMBEDDINGS_BUCKET` | `buckets.schema_dumps.name` |
| `backend/scripts/build-corpus-index.py` ~36, `migrate-avatars-to-s3.py` ~150, `validate_group_formation.py` ~9 | Bucket names built from a convention | `buckets.*.name` |

## Secret remediation

`backend/scripts/create-cognito-pools.sh` lines 25-30 hold literal OAuth client IDs and **client secrets** for **Google, Discord and LinkedIn**. The values are not reproduced here. Git history shows 4 commits to the file, one of which is "update LinkedIn creds", so older credential values are in history too. The repo's origin is GitHub (`lucasnewman11/retribalize-core`). Sign in with Apple is configured on the live v2 pools but does not appear in this script.

1. **Rotate or revoke** the client secret for all three providers in each provider's console (Google Cloud, Discord developer portal, LinkedIn developer portal). Assume they are compromised.
2. **Store** the new credentials in Secrets Manager, one secret per provider, with a JSON body `{client_id, client_secret}`.
3. **Remove** the script from core. rc-identity replaces it. If the script is kept, it must read from Secrets Manager.
4. **Update** the identity providers on the live v2 pools to use the new secrets until those pools retire.
5. **Consider a history scrub** (`git filter-repo`, then a force-push and fresh clones). Rotation is what actually closes the exposure. A scrub only removes the old values from history.

## Open decisions

- **D2: Cognito user migration.** The v3 pools are new and empty, and prod v2 has about 7k users. The options are a forced password reset, or a first-login `UserMigration` trigger that authenticates against the v2 pool. A trigger lets most users carry over without noticing; a reset is simpler. Either way, `SignupFanoutFunction`/`FANOUT_TARGETS` (cross-pool replication) must be reconsidered.
- **D3: DynamoDB tables stay in core** (rc_dynamo schema sync). Needs confirmation. Under per-slot isolation, preview1-8 get their own tables: today they all share `rc-preview-*` (`resolve_environment_config.py` ~11).
- **D5: identity providers in CloudFormation.** Declare `UserPoolIdentityProvider` with `{{resolve:secretsmanager:...}}` for `client_secret`, after the rotation above. Until then the v3 clients support only `COGNITO`, so social login is a regression on cutover.
- **D6: base package distribution.** How core consumes `rc_dynamo` and the `rc-lambda-base` image (registry package vs. image parent vs. vendoring), and which tag core pins.
- **Trigger wiring.** The v3 pools have no `LambdaConfig`. Someone must decide which slot's `post-signup:live`/`user-migration` functions back the shared preview pool. It is preview1 today. The wiring has to be in the template, because `UpdateUserPool` would otherwise clear it.
- **Inconsistencies between core config and per-slot isolation:**
  - `preview67` and `preview89` have their own tables and property-registry bucket but share `rc-preview-{embeddings,user-corpus,avatars,recordings}` (~264-269, ~302-307). preview1-8 share everything `rc-preview-*`. re-infra gives every slot its own six buckets.
  - `deployment-environments.json` uses `slot_pattern ^preview[1-8]$` plus separate preview67/89 entries, and `IsPreview` (~109) hardcodes preview67/89. `envs.yaml` lists all ten slots directly.
  - preview67's mapped `FrontendUrl` is preview1's (~281). The `IsPreview` condition overrides it, but the mapping value is wrong.
  - Avatar URLs stored in DynamoDB embed the bucket name (`migrate-avatars-to-s3.py` ~123). Moving to generated bucket names needs a data rewrite or a redirect, not just a copy.
  - `MatchingContributionsS3Permission` and `setup_corpus_triggers.py` assume the shared corpus bucket. Notifications from several slots merge onto one bucket today.
