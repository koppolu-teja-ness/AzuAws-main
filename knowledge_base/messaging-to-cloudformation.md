# Migrating Azure Queues/Topics to AWS Messaging (CloudFormation)

This document covers Azure messaging resource families that commonly appear with
Function triggers:

- `Microsoft.Storage/storageAccounts/queueServices/queues`
- `Microsoft.ServiceBus/namespaces`
- `Microsoft.ServiceBus/namespaces/queues`
- `Microsoft.ServiceBus/namespaces/topics`
- `Microsoft.ServiceBus/namespaces/topics/subscriptions`

It maps them to AWS SQS/SNS resources and highlights trigger-facing edge cases
(batch, retry, dead-letter, FIFO, and ordering semantics).

---

## 1. Key conceptual differences

| Azure messaging | AWS messaging |
|---|---|
| Azure Storage Queue and Service Bus Queue are different services with different feature depth | AWS SQS has Standard/FIFO modes; feature mapping depends on whether the source needs ordering, sessions, transactions, or duplicate detection |
| Service Bus Topic + Subscription has first-class broker-side fan-out and SQL-like subscription filters | AWS uses SNS Topic + per-subscriber protocol endpoints (often SQS queues for Lambda consumers), with optional filter policies |
| Service Bus Namespace is a management boundary with SKU/capacity and shared auth model | AWS has no namespace resource equivalent for SQS/SNS; queues/topics are top-level resources |

---

## 2. Resource mapping

| Bicep / ARM type | CloudFormation mapping |
|---|---|
| `Microsoft.Storage/storageAccounts/queueServices/queues` | `AWS::SQS::Queue` |
| `Microsoft.ServiceBus/namespaces` | No 1:1 CFN resource. Use as naming/tagging context only; do not emit placeholder resources |
| `Microsoft.ServiceBus/namespaces/queues` | `AWS::SQS::Queue` (Standard by default, FIFO when strict ordering/dedup semantics are required) |
| `Microsoft.ServiceBus/namespaces/topics` | `AWS::SNS::Topic` |
| `Microsoft.ServiceBus/namespaces/topics/subscriptions` | `AWS::SNS::Subscription` (+ `AWS::SQS::Queue` when the subscription target should be queue-based for Lambda polling) |

---

## 3. Parameter mapping

| Azure input | CloudFormation parameter | Notes |
|---|---|---|
| Queue/topic/subscription names | `QueueName`, `TopicName`, `SubscriptionQueueName` | Keep intent; add suffixes if needed for global uniqueness conventions |
| Namespace name | `NamePrefix` (optional) | Namespace has no direct AWS resource; use only for generated names/tags |
| Queue lock/visibility style settings | `VisibilityTimeout`, `MessageRetentionPeriod` | Units and limits differ; validate against SQS bounds |

---

## 4. Property mapping and edge cases

| Azure property | AWS equivalent | Notes |
|---|---|---|
| Service Bus queue `maxDeliveryCount` | `RedrivePolicy.maxReceiveCount` on SQS source queue | Requires an explicit dead-letter queue ARN |
| Service Bus queue `lockDuration` | `VisibilityTimeout` | Same intent (hide message while being processed) but different max values and enforcement model |
| Service Bus queue duplicate detection | SQS FIFO (`FifoQueue: true`, message group + dedup IDs) | Standard queues do not provide exactly-once semantics |
| Service Bus queue/topic session ordering | SQS FIFO + `MessageGroupId` discipline in producers | No exact session primitive in SQS/SNS |
| Topic subscription SQL filters | SNS `FilterPolicy` (attribute-based) | SQL rule translation is manual; semantic parity is partial |
| Subscription dead-lettering | SNS delivery policy + SQS DLQ pattern | Usually modeled by queue redrive for Lambda consumers |
| TTL/retention | SQS `MessageRetentionPeriod` | Service limits differ; check min/max bounds |

For Lambda-triggered workloads, map consumer behavior separately:

- Queue consumers use `AWS::Lambda::EventSourceMapping` on SQS.
- Topic events usually flow SNS -> SQS -> Lambda for controlled retries and DLQ handling.

---

## 5. Example converted template (queue + topic fan-out)

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: Azure messaging migration baseline (Service Bus/Storage Queue -> SQS/SNS).

Parameters:
  OrdersQueueName:
    Type: String
    Default: orders
  EventsTopicName:
    Type: String
    Default: app-events
  BillingSubscriptionQueueName:
    Type: String
    Default: billing-events

Resources:
  OrdersDlq:
    Type: AWS::SQS::Queue

  OrdersQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Ref OrdersQueueName
      VisibilityTimeout: 60
      MessageRetentionPeriod: 345600
      RedrivePolicy:
        deadLetterTargetArn: !GetAtt OrdersDlq.Arn
        maxReceiveCount: 10

  EventsTopic:
    Type: AWS::SNS::Topic
    Properties:
      TopicName: !Ref EventsTopicName

  BillingSubscriptionQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Ref BillingSubscriptionQueueName

  BillingSubscription:
    Type: AWS::SNS::Subscription
    Properties:
      TopicArn: !Ref EventsTopic
      Protocol: sqs
      Endpoint: !GetAtt BillingSubscriptionQueue.Arn

Outputs:
  OrdersQueueArn:
    Value: !GetAtt OrdersQueue.Arn
  EventsTopicArn:
    Value: !Ref EventsTopic
```

---

## 6. Gotchas and migration notes

- Namespace-level settings are not first-class infrastructure in AWS SQS/SNS.
  Treat `Microsoft.ServiceBus/namespaces` as metadata context, not as a
  CloudFormation resource.
- Service Bus SQL filters cannot be copied verbatim; SNS filter policies are
  attribute-based and often require producer-side message attribute changes.
- Ordering/session guarantees require FIFO design end-to-end. A queue type
  mismatch (Service Bus sessions -> SQS Standard) changes behavior.
- For Function trigger parity, combine this mapping with
  `functions-to-cloudformation.md` trigger guidance (event source mapping,
  batch size, failure handling, packaging workflow ordering).
- Prefer explicit DLQ resources and redrive policies during migration;
  defaults differ and silent message loss risk is higher if this is omitted.
