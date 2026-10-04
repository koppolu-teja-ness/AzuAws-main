# Migrating a Key Vault Bicep Template to AWS Secrets Manager (CloudFormation)

This document explains how the Azure Bicep template that creates a Key Vault with two secrets (`db-username`, `db-password`) was converted to an AWS CloudFormation YAML template that uses AWS Secrets Manager, and lists the commands to deploy and use it.

There is no automatic Bicep-to-CloudFormation converter, so the conversion is a manual mapping.

---

## 1. The key conceptual difference

| Azure Key Vault | AWS Secrets Manager |
|---|---|
| A **vault** is a container that holds many secrets | There is **no vault**. Each secret is a standalone resource |
| Secret identity = vault + secret name | Secret identity = name (within account + region) and an ARN |
| Access via vault URI (`https://<vault>.vault.azure.net`) | Access via regional API endpoint, addressed by secret name or ARN |

Because there is no container, the vault resource disappears. The vault name becomes a **naming prefix** (e.g. `myapp/prod/db-username`), which also makes IAM policies easy to scope.

---

## 2. Resource mapping

| Bicep | CloudFormation |
|---|---|
| `Microsoft.KeyVault/vaults` | *(no equivalent, replaced by a name prefix)* |
| `Microsoft.KeyVault/vaults/secrets` (`db-username`) | `AWS::SecretsManager::Secret` |
| `Microsoft.KeyVault/vaults/secrets` (`db-password`) | `AWS::SecretsManager::Secret` |
| `parent: keyVault` | Not needed. Secrets are independent resources |

## 3. Parameter mapping

| Bicep | CloudFormation | Notes |
|---|---|---|
| `param keyVaultName string` | `SecretNamePrefix` (String) | Used as `${SecretNamePrefix}/db-username` |
| `param location string` | *(removed)* | The stack deploys to the region you pass with `--region` |
| `@secure() param dbUsername` | `DbUsername` with `NoEcho: true` | |
| `@secure() param dbPassword` | `DbPassword` with `NoEcho: true` | |
| *(none)* | `KmsKeyId` (optional) | Custom KMS key, see below |

## 4. Property mapping

| Bicep property | CloudFormation equivalent |
|---|---|
| `tenantId` | Not applicable (AWS uses the account) |
| `sku` (`standard`) | Not applicable |
| `enableRbacAuthorization: true` | Access is always controlled by **IAM policies** (optionally a secret resource policy). See section 7 |
| `networkAcls` (`bypass`, `defaultAction`) | No direct equivalent. Use a VPC interface endpoint plus `aws:SourceVpce` conditions in a resource policy if you need network restriction |
| `properties.value` | `SecretString` |
| `properties.contentType` | `Description` (Secrets Manager has no content type field) |
| Microsoft-managed encryption | Encrypted by default with `aws/secretsmanager`; use `KmsKeyId` for a customer managed key |
| `output keyVaultName` | `DbUsernameSecretName`, `DbPasswordSecretName` outputs |
| `output keyVaultUri` | `DbUsernameSecretArn`, `DbPasswordSecretArn` outputs (`!Ref` on a secret returns its ARN) |

---

## 5. Converted template

Saved as `secrets-manager.yaml`.

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: >-
  Stores database credentials in AWS Secrets Manager.
  Converted from an Azure Bicep template (Key Vault + db-username + db-password).

Parameters:
  SecretNamePrefix:
    Type: String
    Description: >-
      Namespace for the secrets, e.g. "myapp/prod". Replaces the Key Vault name,
      because Secrets Manager has no vault container. Secrets are created as
      <prefix>/db-username and <prefix>/db-password.
    MinLength: 1
    MaxLength: 200
    AllowedPattern: '^[A-Za-z0-9/_+=.@-]+$'
    ConstraintDescription: Use letters, numbers and the characters /_+=.@- only.

  DbUsername:
    Type: String
    NoEcho: true
    MinLength: 1
    Description: Database username to store in Secrets Manager.

  DbPassword:
    Type: String
    NoEcho: true
    MinLength: 1
    Description: Database password to store in Secrets Manager.

  KmsKeyId:
    Type: String
    Default: ''
    Description: >-
      Optional. KMS key ID, ARN or alias used to encrypt the secrets.
      Leave empty to use the AWS managed key aws/secretsmanager.

Conditions:
  UseCustomKmsKey: !Not [!Equals [!Ref KmsKeyId, '']]

Resources:
  DbUsernameSecret:
    Type: AWS::SecretsManager::Secret
    Properties:
      Name: !Sub '${SecretNamePrefix}/db-username'
      Description: Database username
      SecretString: !Ref DbUsername
      KmsKeyId: !If [UseCustomKmsKey, !Ref KmsKeyId, !Ref 'AWS::NoValue']

  DbPasswordSecret:
    Type: AWS::SecretsManager::Secret
    Properties:
      Name: !Sub '${SecretNamePrefix}/db-password'
      Description: Database password
      SecretString: !Ref DbPassword
      KmsKeyId: !If [UseCustomKmsKey, !Ref KmsKeyId, !Ref 'AWS::NoValue']

Outputs:
  DbUsernameSecretName:
    Description: Name of the db-username secret
    Value: !Sub '${SecretNamePrefix}/db-username'

  DbPasswordSecretName:
    Description: Name of the db-password secret
    Value: !Sub '${SecretNamePrefix}/db-password'

  DbUsernameSecretArn:
    Description: ARN of the db-username secret
    Value: !Ref DbUsernameSecret

  DbPasswordSecretArn:
    Description: ARN of the db-password secret
    Value: !Ref DbPasswordSecret
```

---

## 6. Commands

### Prerequisites

```bash
aws --version                 # AWS CLI v2 installed
aws configure                 # or: aws sso login --profile <profile>
aws sts get-caller-identity   # confirm the right account
```

The deploying identity needs `cloudformation:*` on the stack and `secretsmanager:CreateSecret`, `secretsmanager:TagResource`, `secretsmanager:DeleteSecret`, `secretsmanager:DescribeSecret` (plus `kms:*` permissions if using a custom key). No `--capabilities` flag is required because the template creates no IAM resources.

### Command equivalents

| Task | Azure | AWS |
|---|---|---|
| Deploy | `az deployment group create --resource-group <rg> --template-file main.bicep --parameters ...` | `aws cloudformation deploy --stack-name <name> --template-file secrets-manager.yaml --parameter-overrides ...` |
| Scope | Resource group | Stack (region + account) |

### Optional: lint and validate

```bash
pip install cfn-lint
cfn-lint secrets-manager.yaml

aws cloudformation validate-template \
  --template-body file://secrets-manager.yaml
```

### Deploy

Avoid typing the password inline, since it ends up in your shell history:

```bash
read -rs DB_PASSWORD        # prompts silently
export AWS_REGION=us-east-1

aws cloudformation deploy \
  --stack-name db-secrets \
  --template-file secrets-manager.yaml \
  --region "$AWS_REGION" \
  --parameter-overrides \
      SecretNamePrefix=myapp/prod \
      DbUsername=appuser \
      DbPassword="$DB_PASSWORD"

unset DB_PASSWORD
```

To use a customer managed KMS key, add `KmsKeyId=alias/my-key` to `--parameter-overrides`.

Equivalent using `create-stack`:

```bash
aws cloudformation create-stack \
  --stack-name db-secrets \
  --template-body file://secrets-manager.yaml \
  --parameters \
      ParameterKey=SecretNamePrefix,ParameterValue=myapp/prod \
      ParameterKey=DbUsername,ParameterValue=appuser \
      ParameterKey=DbPassword,ParameterValue="$DB_PASSWORD"

aws cloudformation wait stack-create-complete --stack-name db-secrets
```

### Verify

```bash
# Stack status and outputs
aws cloudformation describe-stacks \
  --stack-name db-secrets \
  --query "Stacks[0].{Status:StackStatus,Outputs:Outputs}"

# List the secrets under the prefix
aws secretsmanager list-secrets \
  --filters Key=name,Values=myapp/prod/ \
  --query "SecretList[].Name"
```

### Read a secret (equivalent of `az keyvault secret show`)

```bash
aws secretsmanager get-secret-value \
  --secret-id myapp/prod/db-username \
  --query SecretString --output text

aws secretsmanager get-secret-value \
  --secret-id myapp/prod/db-password \
  --query SecretString --output text
```

### Update a value

Re-run the `deploy` command with the new parameter values. CloudFormation updates the secret in place (a new secret version is created).

### Delete

```bash
aws cloudformation delete-stack --stack-name db-secrets
aws cloudformation wait stack-delete-complete --stack-name db-secrets
```

Deleted secrets are **not removed immediately**. Secrets Manager keeps them in a recovery window (30 days by default), which is similar to Key Vault soft delete. During that time you cannot recreate a secret with the same name, so redeploying the same stack right after deleting it will fail.

```bash
# Bring a secret back
aws secretsmanager restore-secret --secret-id myapp/prod/db-username

# Or remove it permanently so the name can be reused (irreversible)
aws secretsmanager delete-secret \
  --secret-id myapp/prod/db-username \
  --force-delete-without-recovery
```

---

## 7. Granting access (replaces Azure RBAC role assignments)

The Bicep template enabled RBAC, which means roles such as "Key Vault Secrets User" would be assigned separately. In AWS you attach an IAM policy to the role or user that needs the secrets.

`read-db-secrets.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret"
      ],
      "Resource": "arn:aws:secretsmanager:us-east-1:111122223333:secret:myapp/prod/*"
    }
  ]
}
```

```bash
aws iam put-role-policy \
  --role-name my-app-role \
  --policy-name read-db-secrets \
  --policy-document file://read-db-secrets.json
```

Notes:

- Secret ARNs end with a random 6-character suffix, so scope policies with the `myapp/prod/*` wildcard instead of an exact ARN.
- If you use a customer managed KMS key, the role also needs `kms:Decrypt` on that key.

---

## 8. Gotchas and improvements

- **`NoEcho` only masks the value** in the console, `describe-stacks`, and events. The value is still passed through CloudFormation, so never put it in template Outputs and avoid committing parameter files that contain it.
- **Prefer generated passwords.** Rather than passing a password in, let Secrets Manager generate it and store both values as one JSON secret. This is the format RDS and most AWS integrations expect, and it costs one secret instead of two:

  ```yaml
  DbCredentialsSecret:
    Type: AWS::SecretsManager::Secret
    Properties:
      Name: !Sub '${SecretNamePrefix}/db-credentials'
      GenerateSecretString:
        SecretStringTemplate: !Sub '{"username": "${DbUsername}"}'
        GenerateStringKey: password
        PasswordLength: 32
        ExcludeCharacters: '"@/\'
  ```

  Avoid building the JSON yourself with `!Sub '{"password":"${DbPassword}"}'`, because quotes or backslashes in the password will break the JSON.
- **Pricing:** Secrets Manager charges per secret per month plus per API call, so combining username and password into one JSON secret is cheaper.
- **Rotation:** for automatic rotation, add an `AWS::SecretsManager::RotationSchedule` resource.
- **Network restriction:** the Bicep `networkAcls` had `defaultAction: 'Allow'`, so it was already open. Secrets Manager is likewise reachable over its public regional endpoint by default. To keep traffic private, create a `com.amazonaws.<region>.secretsmanager` VPC interface endpoint.
- **Regions:** secrets are regional. Deploy the stack in each region that needs them, or use secret replication.
