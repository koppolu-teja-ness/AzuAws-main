// Standalone general-purpose Blob Storage sample, migration scope:
// Microsoft.Storage/storageAccounts (+ blobServices + containers) -> AWS S3 buckets.
// See knowledge_base/storage-to-cloudformation.md -- this is a different use case from the
// storage account in resources/functions/main.bicep (that one is a Lambda deployment-code
// dependency with no bucket created; this one has real containers, so real buckets ARE created).
param location string = resourceGroup().location
param storageAccountName string = 'stblobmigrationdemo'
param containerName string = 'documents'

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

resource blobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobServices
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

output storageAccountName string = storage.name
output containerName string = container.name
