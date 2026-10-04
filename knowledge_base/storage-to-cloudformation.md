# Migrating a Blob Storage Account to AWS S3 (CloudFormation)

This document explains how a general-purpose Azure Storage Account used for **Blob storage**
(`Microsoft.Storage/storageAccounts` + `blobServices` + one or more `containers`) is converted
to AWS S3 buckets, and lists the commands to deploy, verify and delete it.

This is a *different* use case from the storage account covered in
[functions-to-cloudformation.md](functions-to-cloudformation.md): that doc covers a storage
account that exists only as a Lambda deployment-code dependency (no CFN resource created for
it at all, since it's treated as a pre-existing bucket). This doc covers a storage account used
for its own sake — application blob data — where real `AWS::S3::Bucket` resources ARE created.

> **Disambiguation rule for Agent 3:** if a `Microsoft.Storage/storageAccounts` resource's name
> is referenced by a sibling `Microsoft.Web/sites` (function app)'s `AzureWebJobsStorage`
> connection string and it has **no** `blobServices/containers` children of its own, treat it
> per functions-to-cloudformation.md (no bucket created, referenced by parameter only). If it
> **does** have one or more `blobServices/containers` children, each container still maps to
> its own bucket per this doc, independently of whatever the account is also used for.

There is no automatic Bicep-to-CloudFormation converter, so the conversion is a manual mapping.

---

## 1. The key conceptual difference

| Azure Blob Storage | AWS S3 |
|---|---|
| One **storage account** is a namespace holding many **containers**, each an independent collection of blobs | AWS has no "account" layer above a bucket — each **bucket** is already the top-level namespace. A storage account with *N* containers therefore maps to *N* independent buckets, not one bucket with *N* "sub-buckets" |
| Container-level `publicAccess` (`None`/`Blob`/`Container`) controls anonymous read per container | Bucket-level `PublicAccessBlockConfiguration` + bucket policy control anonymous read per bucket — the Azure three-state enum collapses to "fully blocked" (`None`, the default and recommended) vs. "selectively allowed via an explicit policy statement" (`Blob`/`Container`) |
| `blobServices.deleteRetentionPolicy` (soft delete) is a time-boxed recovery window, not full version history | `VersioningConfiguration: Enabled` + an optional `LifecycleConfiguration` rule expiring noncurrent versions after N days is the nearest equivalent — not identical (S3 versioning keeps every version until it's explicitly expired/deleted, Azure soft-delete keeps only deleted blobs for the retention window) |
| `minimumTlsVersion` / `supportsHttpsTrafficOnly` are account-wide settings | TLS/HTTPS enforcement has no bucket *property* — it's done via a bucket policy statement denying any request where `aws:SecureTransport` is `false` (see converted template) |
| `kind`/`sku` (e.g. `StorageV2`/`Standard_LRS`) select the storage tier and replication | `AWS::S3::Bucket` has no SKU; storage/replication tier is chosen per-object (`StorageClass`) or via a `LifecycleConfiguration` transition rule, not a bucket-wide setting |

---

## 2. Resource mapping

| Bicep | CloudFormation |
|---|---|
| `Microsoft.Storage/storageAccounts/blobServices/containers` | `AWS::S3::Bucket` (one per container — see conceptual difference above) |
| `Microsoft.Storage/storageAccounts/blobServices` | *(no resource of its own — folded into the CNR properties of the parent storage account; its `cors`/`deleteRetentionPolicy` settings apply to every sibling container's bucket, see property mapping)* |
| `Microsoft.Storage/storageAccounts` (standalone, with `blobServices/containers` children) | *(no 1:1 resource — its account-wide settings, e.g. `minimumTlsVersion`, are applied individually to each child container's bucket policy, since S3 has no account-level resource to attach them to)* |

## 3. Parameter mapping

| Bicep | CloudFormation | Notes |
|---|---|---|
| `param storageAccountName string` | *(used as a prefix/tag only)* | S3 bucket names are globally unique across all AWS accounts, so the Azure account name can't be reused verbatim — append a stable suffix (e.g. account id) if the literal name might collide |
| container `name` | `BucketName` (String, one parameter per container) or a generated name (`!Sub '${NamePrefix}-${ContainerName}'`) if the source has several containers | Must be globally unique, lowercase, DNS-compliant — Azure container names (lowercase + hyphens, 3-63 chars) already satisfy S3's rules in practice, but always validate rather than assume |
| `param location string` | *(removed)* | The stack deploys to the region passed with `--region` |

## 4. Property mapping

| Bicep property | CloudFormation equivalent |
|---|---|
| container `properties.publicAccess` (`None`) | `PublicAccessBlockConfiguration` with all four flags `true` (default, recommended) |
| container `properties.publicAccess` (`Blob`/`Container`) | `PublicAccessBlockConfiguration` flags set to `false` + an explicit `AWS::S3::BucketPolicy` statement allowing `s3:GetObject` for `Principal: "*"` — never flip the access-block flags without also adding the scoping policy, or the bucket becomes fully public instead of selectively public |
| `blobServices.properties.deleteRetentionPolicy.enabled`/`.days` (folded child) | `VersioningConfiguration.Status: Enabled` + a `LifecycleConfiguration` rule with `NoncurrentVersionExpiration.NoncurrentDays` set to the same `days` value |
| `blobServices.properties.cors.corsRules` (folded child) | `CorsConfiguration.CorsRules` — `allowedOrigins`/`allowedMethods`/`allowedHeaders`/`maxAgeInSeconds` map 1:1 by name (lowerCamelCase -> PascalCase only) |
| storage account `properties.minimumTlsVersion` / `supportsHttpsTrafficOnly` | `AWS::S3::BucketPolicy` statement: `Effect: Deny`, `Condition: {Bool: {"aws:SecureTransport": "false"}}` on every bucket derived from this account |
| storage account `properties.encryption` (default, Microsoft-managed keys) | S3's default encryption (`AES256`, `BucketEncryption.ServerSideEncryptionConfiguration`) — enabled by default on new buckets, no extra resource needed unless a customer-managed KMS key is required |
| `output storageAccountName` | One output per bucket (`Bucket<N>Name`), not a single account-level output |

---

## 5. Converted template

Saved as `storage.yaml`. Shows one container (`documents`) for brevity; repeat the
`Bucket`/`BucketPolicy` pair per additional container.

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: >-
  Creates an S3 bucket per blob container, with TLS-only enforcement and
  versioning matching the source soft-delete retention. Converted from an
  Azure Bicep template (storageAccounts + blobServices + containers).

Parameters:
  BucketName:
    Type: String
    Description: >-
      Globally unique bucket name for the 'documents' container (S3 bucket
      names are unique across all AWS accounts, unlike Azure container names).

  RetentionDays:
    Type: Number
    Default: 7
    Description: Maps from blobServices.deleteRetentionPolicy.days.

Resources:
  DocumentsBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Ref BucketName
      VersioningConfiguration:
        Status: Enabled
      PublicAccessBlockConfiguration:
        BlockPublicAcls: true
        BlockPublicPolicy: true
        IgnorePublicAcls: true
        RestrictPublicBuckets: true
      BucketEncryption:
        ServerSideEncryptionConfiguration:
          - ServerSideEncryptionByDefault:
              SSEAlgorithm: AES256
      LifecycleConfiguration:
        Rules:
          - Id: expire-noncurrent-versions
            Status: Enabled
            NoncurrentVersionExpiration:
              NoncurrentDays: !Ref RetentionDays

  DocumentsBucketPolicy:
    Type: AWS::S3::BucketPolicy
    Properties:
      Bucket: !Ref DocumentsBucket
      PolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Sid: DenyInsecureTransport
            Effect: Deny
            Principal: '*'
            Action: 's3:*'
            Resource:
              - !GetAtt DocumentsBucket.Arn
              - !Sub '${DocumentsBucket.Arn}/*'
            Condition:
              Bool:
                'aws:SecureTransport': 'false'

Outputs:
  DocumentsBucketName:
    Description: Name of the S3 bucket (migrated from container 'documents')
    Value: !Ref DocumentsBucket

  DocumentsBucketArn:
    Description: ARN of the S3 bucket
    Value: !GetAtt DocumentsBucket.Arn
```

---

## 6. Commands

### Prerequisites

```bash
aws --version                 # AWS CLI v2 installed
aws configure                 # or: aws sso login --profile <profile>
aws sts get-caller-identity   # confirm the right account
```

The deploying identity needs `cloudformation:*` on the stack and `s3:CreateBucket`,
`s3:DeleteBucket`, `s3:PutBucketPolicy`, `s3:PutBucketVersioning`,
`s3:PutBucketPublicAccessBlock`, `s3:PutLifecycleConfiguration`,
`s3:PutEncryptionConfiguration`. No `--capabilities` flag is required (no IAM resources).

### Command equivalents

| Task | Azure | AWS |
|---|---|---|
| Deploy | `az deployment group create --resource-group <rg> --template-file main.bicep --parameters ...` | `aws cloudformation deploy --stack-name <name> --template-file storage.yaml --parameter-overrides ...` |
| Scope | Resource group | Stack (region + account) |

### Optional: lint and validate

```bash
pip install cfn-lint
cfn-lint storage.yaml

aws cloudformation validate-template --template-body file://storage.yaml
```

### Deploy

```bash
export AWS_REGION=us-east-1

aws cloudformation deploy \
  --stack-name storage-migration-demo \
  --template-file storage.yaml \
  --region "$AWS_REGION" \
  --parameter-overrides \
      BucketName=storage-migration-demo-documents \
      RetentionDays=7
```

### Verify

```bash
aws cloudformation describe-stacks \
  --stack-name storage-migration-demo \
  --query "Stacks[0].{Status:StackStatus,Outputs:Outputs}"

aws s3api get-bucket-versioning --bucket storage-migration-demo-documents
aws s3api get-bucket-policy --bucket storage-migration-demo-documents
```

### Delete

```bash
# Non-empty buckets must be emptied first -- CloudFormation won't force-delete objects
aws s3 rm "s3://storage-migration-demo-documents" --recursive
aws cloudformation delete-stack --stack-name storage-migration-demo
aws cloudformation wait stack-delete-complete --stack-name storage-migration-demo
```

Unlike Azure's soft-delete (automatic recovery window), an emptied+deleted S3 bucket with
versioning enabled still retains delete markers/noncurrent versions until the lifecycle rule
expires them — budget for that when calculating actual storage cost after "deletion".

---

## 7. Gotchas and improvements

- **One container == one bucket, not one account == one bucket.** Don't try to collapse
  multiple containers from one storage account into prefixes within a single bucket purely
  to mirror Azure's one-account-many-containers shape — S3 bucket policies, lifecycle rules
  and versioning are all bucket-wide, so merging containers with different retention/public-access
  needs would force the strictest policy onto all of them.
- **Bucket names are globally unique, container names are not.** A direct `container.name`
  reuse as `BucketName` can fail at deploy time with `BucketAlreadyExists` even though the
  Azure source never had a naming conflict — always parameterize `BucketName` rather than
  hardcoding the Azure container name.
- **TLS enforcement is a policy, not a property.** There is no S3 bucket property equivalent
  to `minimumTlsVersion`/`supportsHttpsTrafficOnly` — it must be an explicit `Deny` statement
  in the bucket policy (as shown above); omitting it silently allows plaintext HTTP access,
  unlike Azure where `supportsHttpsTrafficOnly: true` is enforced platform-side.
- **Soft delete vs. versioning is not a 1:1 feature match.** Azure's `deleteRetentionPolicy`
  only recovers *deleted* blobs for N days; S3 versioning keeps every *overwritten* version
  too, not just deleted ones, until the lifecycle rule expires them — this is a strictly
  broader (and more expensive, more storage) recovery model than the source had, flag it
  during review rather than treating it as a silent drop-in replacement.
- **Pricing:** S3 storage + request pricing differs from Azure's per-GB + per-operation tiers;
  enabling versioning roughly doubles storage cost for frequently-overwritten objects unless a
  lifecycle rule expires noncurrent versions promptly (as shown in the converted template).
