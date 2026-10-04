# Migrating a Function App Bicep Template to AWS Lambda (CloudFormation)

This document explains how the Azure Bicep template that creates a storage account, a
Consumption (Y1) App Service plan and a Function App (`resources/functions/main.bicep`) is
converted to an AWS CloudFormation YAML template that uses Lambda + IAM + S3, and lists the
commands to deploy, verify and delete it.

There is no automatic Bicep-to-CloudFormation converter, so the conversion is a manual mapping.

MVP scope: the base template in section 5 covers a Lambda function with no event source wired
up (the Bicep source declares the Function App resource itself, not trigger bindings, which
live in function code/`function.json`, not ARM). Section 8 extends this with the actual
trigger-type mappings (HTTP, Timer, Queue, Blob, Event Grid, Service Bus) and the code
packaging workflows (zip-via-S3, container image, inline, CI/CD) needed once a real trigger
and a real deployment pipeline are in scope.

---

## 1. The key conceptual difference

| Azure Functions | AWS Lambda |
|---|---|
| A **Function App** (`Microsoft.Web/sites`, `kind=functionapp`) always runs on top of an **App Service plan** (`Microsoft.Web/serverfarms`) — even the "Consumption" (`Y1`) plan is a distinct resource | Lambda has **no plan/always-on resource at all**. A function is just `AWS::Lambda::Function`; scaling is automatic and fully managed |
| Needs a **storage account** for the runtime's internal bookkeeping (`AzureWebJobsStorage` app setting — triggers, locks, logs) | No storage requirement for the function to run. A bucket is only needed if you deploy code as a zip via S3 (optional — can also upload inline/CLI) |
| Identity/permissions via **managed identity** + Azure RBAC role assignments (not shown in this minimal template) | Identity/permissions via an **IAM execution role** attached directly to the function — always required, since Lambda cannot run without one |
| App settings (`siteConfig.appSettings`) are environment variables plus a few Functions-runtime-specific keys (`FUNCTIONS_EXTENSION_VERSION`, `FUNCTIONS_WORKER_RUNTIME`) | `Environment.Variables` map; there is no runtime-version app setting because the Lambda `Runtime` property on the function resource itself selects the language/version |

---

## 2. Resource mapping

| Bicep | CloudFormation |
|---|---|
| `Microsoft.Web/serverfarms` (Y1 / Consumption) | *(no AWS equivalent — Lambda scales automatically, there is no plan resource to create)* |
| `Microsoft.Web/sites` (`kind=functionapp`) | `AWS::Lambda::Function` + `AWS::IAM::Role` (execution role, always required) |
| `Microsoft.Storage/storageAccounts` (AzureWebJobsStorage backing store, no `blobServices/containers` children) | *(no CFN resource — an existing S3 bucket, created and populated with the deployment package *before* this stack runs, is referenced by name only; see gotchas for why the stack cannot create this bucket itself)* |
| `Microsoft.Storage/storageAccounts/blobServices/containers` (application blob data, if present alongside the function app) | `AWS::S3::Bucket` per container — this is a *different* storage use case, see [storage-to-cloudformation.md](storage-to-cloudformation.md) |


## 3. Parameter mapping

| Bicep | CloudFormation | Notes |
|---|---|---|
| `param functionAppName string` | `FunctionName` (String) | Used for both the Lambda function name and the IAM role name suffix |
| `param storageAccountName string` | `DeploymentBucketName` (String) | Name of a bucket that already exists and already holds the deployment package zip; this stack only reads from it (via `Ref`), it never declares an `AWS::S3::Bucket` resource for it |
| `param servicePlanName string` | *(removed)* | No AWS resource needs this; Lambda has no plan concept |
| `param location string` | *(removed)* | The stack deploys to the region you pass with `--region` |
| *(none)* | `LambdaRuntime` (String, default `nodejs20.x`) | Maps from the `FUNCTIONS_WORKER_RUNTIME` app setting (`node` -> `nodejs20.x`) |
| *(none)* | `DeploymentPackageKey` (String) | New required input — S3 object key for the function's zipped code, since ARM templates don't carry function code either but CFN needs a `Code` source at creation time |

## 4. Property mapping

| Bicep property | CloudFormation equivalent |
|---|---|
| `serverFarmId` | Not applicable (no plan resource to reference) |
| `siteConfig.appSettings[AzureWebJobsStorage]` | Not applicable directly; replaced by the Lambda `Code.S3Bucket`/`S3Key` pointing at the new bucket |
| `siteConfig.appSettings[FUNCTIONS_EXTENSION_VERSION]` | Not applicable (no separate runtime-generation setting; implied by `Runtime`) |
| `siteConfig.appSettings[FUNCTIONS_WORKER_RUNTIME]` | `Runtime` property on `AWS::Lambda::Function` (e.g. `node` -> `nodejs20.x`) |
| `siteConfig.appSettings[WEBSITE_RUN_FROM_PACKAGE]` | Deployment strategy decision: either keep zip artifact in S3 (`Code.S3Bucket`/`S3Key`) or switch to container image (`PackageType: Image`, `Code.ImageUri`) |
| `identity.type` / `identity.userAssignedIdentities` on `Microsoft.Web/sites` | IAM execution role (`AWS::IAM::Role`) + optional cross-account assume-role/resource policies (there is no direct user-assigned-identity object on Lambda) |
| `kind` containing `container` or `siteConfig.linuxFxVersion` image reference | `AWS::Lambda::Function` with `PackageType: Image` and ECR image URI; drop `Runtime`/`Handler` from function properties |
| Folded `Microsoft.Web/sites/functions` child bindings (`config.bindings[]`) | Drives event-source resources and `AWS::Lambda::EventSourceMapping`/`AWS::Lambda::Permission` properties (batch, retry, source ARN), not a separate AWS resource per function child |
| storage account `sku`/`kind`/`minimumTlsVersion` | Not applicable to `AWS::S3::Bucket` directly; TLS-only access is enforced via a bucket policy instead (see gotchas) |
| `output functionAppName` | `FunctionArn` / `FunctionName` outputs |
| `output storageAccountName` | `DeploymentBucketName` output |

---

## 5. Converted template

Saved as `lambda.yaml`.

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: >-
  Creates an IAM execution role and a Lambda function that reads its deployment
  package from a pre-existing S3 bucket. Converted from an Azure Bicep template
  (storageAccounts + serverfarms + sites/functionapp).

Parameters:
  FunctionName:
    Type: String
    Default: func-migration-demo
    Description: Name of the Lambda function (replaces the Function App name).

  DeploymentBucketName:
    Type: String
    Description: >-
      Name of an EXISTING S3 bucket that already holds the function's deployment
      package. This stack does not create or delete this bucket (see gotchas) --
      create it and upload the zip before running this stack.

  DeploymentPackageKey:
    Type: String
    Default: function.zip
    Description: S3 object key of the zipped function code, uploaded before this stack runs.

  LambdaRuntime:
    Type: String
    Default: nodejs20.x
    Description: Maps from the source FUNCTIONS_WORKER_RUNTIME app setting.

Resources:
  FunctionExecutionRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub '${FunctionName}-execution-role'
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal:
              Service: lambda.amazonaws.com
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

  Function:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: !Ref FunctionName
      Runtime: !Ref LambdaRuntime
      Handler: index.handler
      Role: !GetAtt FunctionExecutionRole.Arn
      Code:
        S3Bucket: !Ref DeploymentBucketName
        S3Key: !Ref DeploymentPackageKey
      MemorySize: 128
      Timeout: 30

Outputs:
  FunctionArn:
    Description: ARN of the Lambda function
    Value: !GetAtt Function.Arn

  FunctionName:
    Description: Name of the Lambda function
    Value: !Ref Function

  DeploymentBucketName:
    Description: Name of the S3 deployment bucket (pre-existing, not managed by this stack)
    Value: !Ref DeploymentBucketName
```

---

## 6. Commands

### Prerequisites

```bash
aws --version                 # AWS CLI v2 installed
aws configure                 # or: aws sso login --profile <profile>
aws sts get-caller-identity   # confirm the right account
```

The deploying identity needs `iam:CreateRole`, `iam:AttachRolePolicy`,
`iam:DeleteRole`, `iam:DetachRolePolicy`, `lambda:CreateFunction`,
`lambda:GetFunction`, `lambda:InvokeFunction`, `lambda:DeleteFunction`,
`s3:GetObject` on the deployment bucket (read by CloudFormation/Lambda), plus
`s3:CreateBucket`/`s3:PutObject` used directly via the AWS CLI below to
pre-create the bucket and upload the zip (the stack itself never creates or
deletes this bucket). Because the template creates an `AWS::IAM::Role`, the
deploy commands below need `--capabilities CAPABILITY_NAMED_IAM`.

### Command equivalents

| Task | Azure | AWS |
|---|---|---|
| Deploy | `az deployment group create --resource-group <rg> --template-file main.bicep --parameters ...` | `aws cloudformation deploy --stack-name <name> --template-file lambda.yaml --parameter-overrides ... --capabilities CAPABILITY_NAMED_IAM` |
| Scope | Resource group | Stack (region + account) |

### Optional: lint and validate

```bash
pip install cfn-lint
cfn-lint lambda.yaml

aws cloudformation validate-template \
  --template-body file://lambda.yaml
```

### Deploy

A deployment package must exist in the bucket *before* the stack creates the
function, since `AWS::Lambda::Function` reads `Code.S3Bucket`/`S3Key` at
creation time. Create the bucket first (or reuse an existing one), upload the
code, then deploy:

```bash
export AWS_REGION=us-east-1
BUCKET=func-migration-demo-deploy

aws s3 mb "s3://$BUCKET" --region "$AWS_REGION"
echo 'exports.handler = async () => ({ statusCode: 200, body: "ok" });' > index.js
zip function.zip index.js
aws s3 cp function.zip "s3://$BUCKET/function.zip"

aws cloudformation deploy \
  --stack-name functions-migration-demo \
  --template-file lambda.yaml \
  --region "$AWS_REGION" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      FunctionName=func-migration-demo \
      DeploymentBucketName="$BUCKET" \
      DeploymentPackageKey=function.zip \
      LambdaRuntime=nodejs20.x
```

Equivalent using `create-stack`:

```bash
aws cloudformation create-stack \
  --stack-name functions-migration-demo \
  --template-body file://lambda.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
      ParameterKey=FunctionName,ParameterValue=func-migration-demo \
      ParameterKey=DeploymentBucketName,ParameterValue="$BUCKET" \
      ParameterKey=DeploymentPackageKey,ParameterValue=function.zip \
      ParameterKey=LambdaRuntime,ParameterValue=nodejs20.x

aws cloudformation wait stack-create-complete --stack-name functions-migration-demo
```

### Verify

```bash
# Stack status and outputs
aws cloudformation describe-stacks \
  --stack-name functions-migration-demo \
  --query "Stacks[0].{Status:StackStatus,Outputs:Outputs}"

# Function details (equivalent of `az functionapp show`)
aws lambda get-function --function-name func-migration-demo

# No-op smoke invoke
aws lambda invoke --function-name func-migration-demo --payload '{}' /tmp/out.json
cat /tmp/out.json
```

### Delete

```bash
aws cloudformation delete-stack --stack-name functions-migration-demo
aws cloudformation wait stack-delete-complete --stack-name functions-migration-demo

# The bucket is never stack-owned (created manually above via `s3 mb`); remove it separately
aws s3 rb "s3://$BUCKET" --force
```

---

## 7. Gotchas and improvements

- **The deployment package must exist before `create_stack`/`update_stack` runs,
  and the bucket must NOT be an `AWS::S3::Bucket` resource in this stack.**
  Unlike Azure's Kudu/zip-deploy (which can push code after the Function App
  exists), CloudFormation's `AWS::Lambda::Function` requires a valid
  `Code.S3Bucket`/`S3Key` at creation time. If the bucket were declared as a
  resource in this same stack, CloudFormation would create it empty and then
  immediately try to read the zip from it in the same `create_stack` call --
  there is no step in between to upload the code, so the function resource
  would always fail with a missing-key error. Always treat the bucket as
  pre-existing (created + populated once, outside this stack, and reused
  across deploys by only changing `DeploymentPackageKey` per version).
- **`serverfarms` truly disappears.** Even the Consumption (`Y1`) plan is a
  billable, named resource in Azure; in Lambda there is nothing to create,
  configure, or pay for beyond the function itself (pay-per-invocation +
  duration, no idle/base cost).
- **Runtime version mapping is manual.** `FUNCTIONS_WORKER_RUNTIME: node` has
  no generation info by itself (that's `FUNCTIONS_EXTENSION_VERSION` plus the
  Node version installed in the Azure runtime image) — pick the closest
  supported Lambda `Runtime` value explicitly (e.g. `nodejs20.x`) rather than
  assuming a 1:1 version match.
- **Cold starts differ.** Azure Functions Consumption plan and Lambda both
  have cold-start behavior, but tuning knobs differ (Lambda: `MemorySize`
  also scales CPU; provisioned concurrency available as an add-on resource
  `AWS::Lambda::ProvisionedConcurrencyConfig` if needed).
- **Trigger type changes the AWS resources needed, not just the Lambda function.**
  An HTTP trigger needs API Gateway wired in front of the function; a Queue/Blob/
  Timer/Event Grid trigger needs the matching event-source resource instead —
  see section 8 for the full per-trigger mapping, including the `AWS::Lambda::Permission`
  or `AWS::Lambda::EventSourceMapping` each one requires.
- **IAM execution role is not optional.** Lambda cannot run without a role,
  unlike Azure where a managed identity is opt-in. The minimal
  `AWSLambdaBasicExecutionRole` managed policy (CloudWatch Logs only) is the
  floor; add further scoped policies only for what the function code actually
  calls (least privilege).
- **Function-level binding metadata matters.** In exported ARM, trigger details
  may appear under folded `Microsoft.Web/sites/functions` child resources, not
  just top-level app settings; map those fields into event-source settings
  (`BatchSize`, DLQ, retry/visibility tuning), otherwise trigger behavior
  shifts silently after migration.
- **Pricing:** Lambda charges per invocation + GB-seconds, no idle cost,
  unlike an always-provisioned App Service plan tier (though Consumption/`Y1`
  is also pay-per-execution on the Azure side).

---

## 8. Trigger mapping and packaging workflows

### 8.1 Trigger type -> AWS event source

Azure trigger type lives in the function code's `function.json` binding
(or an attribute in newer isolated-worker models), not in the `Microsoft.Web/sites`
ARM resource itself — so recognizing which trigger is in play requires reading
the function app's `siteConfig.appSettings`/connection-string names and the
deployed code, not just the ARM template.

| Azure trigger | AWS equivalent | New CFN resources needed |
|---|---|---|
| **HTTP trigger** (`authLevel: anonymous/function/admin`) | API Gateway in front of Lambda | `AWS::ApiGatewayV2::Api` (HTTP API, cheapest/simplest) or `AWS::ApiGateway::RestApi` (REST API, needed for request validation/API keys/usage plans) + `AWS::ApiGatewayV2::Integration`/`Stage` + `AWS::Lambda::Permission` (`Action: lambda:InvokeFunction`, `Principal: apigateway.amazonaws.com`) |
| **Timer trigger** (CRON expression) | EventBridge Scheduler | `AWS::Scheduler::Schedule` (`ScheduleExpression: cron(...)` or `rate(...)`) + `AWS::Lambda::Permission` (`Principal: scheduler.amazonaws.com`) — prefer this over the older `AWS::Events::Rule` scheduled-rule pattern for new migrations |
| **Queue trigger** (Azure Storage Queue) | SQS poller | `AWS::SQS::Queue` + `AWS::Lambda::EventSourceMapping` (`EventSourceArn: !GetAtt Queue.Arn`) — Lambda polls SQS directly, no permission resource needed beyond the execution role's `sqs:ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` |
| **Blob trigger** (new blob in a container) | S3 event notification | `AWS::S3::Bucket` with a `NotificationConfiguration.LambdaConfigurations` entry (`Event: s3:ObjectCreated:*`) + `AWS::Lambda::Permission` (`Principal: s3.amazonaws.com`, `SourceArn` scoped to the bucket) — note the bucket's notification config and the Lambda permission must both exist before the first upload, and CFN resolves the circular bucket<->permission dependency via the bucket resource depending on the permission, not the other way round |
| **Event Grid trigger** | EventBridge rule | `AWS::Events::Rule` (`EventPattern` matching the equivalent AWS service's events) + `AWS::Lambda::Permission` (`Principal: events.amazonaws.com`) — there's rarely a 1:1 source-event match, so the event pattern needs re-authoring, not just renaming |
| **Service Bus trigger** (queue or topic/subscription) | SQS (queue) or SNS+SQS (topic/subscription fan-out) | Queue: same as Queue trigger above. Topic: `AWS::SNS::Topic` + one `AWS::SQS::Queue` per subscription + `AWS::SNS::Subscription` + `AWS::Lambda::EventSourceMapping` per queue (Lambda still polls SQS, never subscribes to SNS directly) |
| **Cosmos DB trigger** (change feed) | DynamoDB Streams | `AWS::Lambda::EventSourceMapping` with `EventSourceArn` pointing at the table's stream ARN — only applies once the Cosmos DB side of a migration is also in scope (not covered by this doc) |

### 8.2 Packaging workflows

The base template (section 5) assumes the simplest case: a pre-built zip
already sitting in S3. Real migrations need one of these, chosen by how the
source Function App builds today:

- **Zip via S3 (default, shown in section 5).** Best when the existing Azure
  deployment already produces a zip artifact (`func azure functionapp
  publish --no-build` output, or a CI build step). Upload once per version to
  a versioned key (`function-v${BUILD_NUMBER}.zip`) and update only
  `DeploymentPackageKey` on redeploy — never overwrite the same key in place,
  since CloudFormation won't detect an in-place S3 object change as a diff
  and will skip updating the function.
- **Inline code (`Code.ZipFile`).** Only valid for a single-file, dependency-free
  Node.js/Python handler under ~4 KB of source — convenient for a trivial
  migrated stub/smoke-test function, never for a real migrated workload with
  npm/pip dependencies (`ZipFile` has no `node_modules`/`site-packages`).
- **Container image (`PackageType: Image`).** Best when the source Function
  App already deploys via a custom container (`Microsoft.Web/sites` with
  `kind: functionapp,linux,container`). Requires an ECR repository
  (`AWS::ECR::Repository`, typically created once outside this stack, same
  reasoning as the deployment bucket) holding an image built with the
  `public.ecr.aws/lambda/<runtime>` base image; `Code.ImageUri` replaces
  `Code.S3Bucket`/`S3Key`, and `Handler`/`Runtime` are both dropped (the
  `CMD` in the image's Dockerfile takes over).
- **CI/CD build pipeline.** Regardless of which packaging style is picked,
  the actual `zip`/`docker build` + upload step belongs in the CI pipeline
  that runs *before* `aws cloudformation deploy`, mirroring Azure DevOps/GitHub
  Actions building then calling `az functionapp deployment` — this stack
  only ever consumes an already-built artifact, matching the "package must
  pre-exist" rule in section 7's first gotcha.

### 8.3 Trigger-behavior tuning (edge cases)

| Azure trigger/binding knob | AWS target | Migration note |
|---|---|---|
| Queue trigger `batchSize` | `AWS::Lambda::EventSourceMapping.BatchSize` | Keep conservative for first cut; large batches increase retry blast radius |
| Queue trigger `newBatchThreshold` / host concurrency | `MaximumBatchingWindowInSeconds` + reserved concurrency | Behavior is not 1:1; treat as load-test-tuned settings, not literal copies |
| Queue poison-message handling (`maxDequeueCount`) | SQS `RedrivePolicy.maxReceiveCount` + DLQ | Must be modeled explicitly; default SQS retry behavior may differ materially |
| Service Bus lock renewal assumptions | SQS visibility timeout + function timeout coordination | Ensure `VisibilityTimeout` > max function runtime to avoid duplicate concurrent processing |
| Event Grid subject/type filters | EventBridge rule pattern or SNS filter policy | Translation is semantic, not syntactic; validate with representative events |

### 8.4 Packaging workflow guardrails

- Use immutable artifact identifiers (`function-v<build>.zip` or image digests),
  not mutable tags/keys, so CloudFormation detects updates reliably.
- For zip-based deploys, publish dependencies into the artifact itself; unlike
  Azure zip deploy conveniences, CFN/Lambda will not perform post-deploy build
  steps.
- For image-based deploys, pin architecture (`x86_64` vs `arm64`) explicitly and
  verify native dependency compatibility before cutover.
- Introduce `AWS::Lambda::Version` + `AWS::Lambda::Alias` once baseline parity
  is proven, so trigger cutovers can be staged with weighted traffic instead of
  all-at-once updates.
