// Standalone Function App sample, migration scope: Microsoft.Storage/storageAccounts,
// Microsoft.Web/serverfarms, Microsoft.Web/sites (kind=functionapp) -> AWS S3 + Lambda + IAM role.
// MVP: no HTTP trigger / API Gateway, no Application Insights (stretch goals, out of scope).
param location string = resourceGroup().location
param functionAppName string = 'func-migration-demo'
param storageAccountName string = 'stfuncmigrationdemo'
param servicePlanName string = '${functionAppName}-plan'

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

resource servicePlan 'Microsoft.Web/serverfarms@2022-09-01' = {
  name: servicePlanName
  location: location
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  kind: 'functionapp'
  properties: {}
}

resource functionApp 'Microsoft.Web/sites@2022-09-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp'
  properties: {
    serverFarmId: servicePlan.id
    siteConfig: {
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storage.listKeys().keys[0].value}'
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'node'
        }
      ]
    }
  }
}

output functionAppName string = functionApp.name
output storageAccountName string = storage.name
output servicePlanName string = servicePlan.name
