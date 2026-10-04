// Standalone messaging sample, migration scope:
// - Microsoft.Storage/storageAccounts/queueServices/queues
// - Microsoft.ServiceBus/namespaces/queues
// - Microsoft.ServiceBus/namespaces/topics/subscriptions
// -> AWS SQS/SNS resources.
param location string = resourceGroup().location
param storageAccountName string = 'stmsgmigrationdemo'
param storageQueueName string = 'jobs'
param serviceBusNamespaceName string = 'sb-migration-demo'
param serviceBusQueueName string = 'orders'
param serviceBusTopicName string = 'events'
param serviceBusSubscriptionName string = 'billing'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {}
}

resource storageQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueService
  name: storageQueueName
  properties: {}
}

resource serviceBusNamespace 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: serviceBusNamespaceName
  location: location
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {
    publicNetworkAccess: 'Enabled'
    minimumTlsVersion: '1.2'
  }
}

resource serviceBusQueue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: serviceBusNamespace
  name: serviceBusQueueName
  properties: {
    lockDuration: 'PT1M'
    maxDeliveryCount: 10
    requiresSession: false
  }
}

resource serviceBusTopic 'Microsoft.ServiceBus/namespaces/topics@2022-10-01-preview' = {
  parent: serviceBusNamespace
  name: serviceBusTopicName
  properties: {
    defaultMessageTimeToLive: 'P14D'
  }
}

resource serviceBusSubscription 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2022-10-01-preview' = {
  parent: serviceBusTopic
  name: serviceBusSubscriptionName
  properties: {
    maxDeliveryCount: 10
    deadLetteringOnMessageExpiration: true
  }
}

output storageQueueId string = storageQueue.id
output serviceBusQueueId string = serviceBusQueue.id
output serviceBusTopicId string = serviceBusTopic.id
output serviceBusSubscriptionId string = serviceBusSubscription.id
