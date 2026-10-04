param location string = resourceGroup().location
param vaultName string = 'kv-e2e-demo'
param vnetName string = 'vnet-e2e-demo'
param subnetName string = 'subnet-e2e-app'
param functionAppName string = 'func-e2e-demo'
param storageAccountName string = 'ste2edemo'
param expressRouteCircuitName string = 'er-e2e-demo'

var appInsightsName = '${functionAppName}-appi'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    accessPolicies: []
    enableRbacAuthorization: true
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.20.0.0/16'
      ]
    }
  }
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' = {
  name: '${vnetName}/${subnetName}'
  dependsOn: [
    vnet
  ]
  properties: {
    addressPrefix: '10.20.1.0/24'
    serviceEndpoints: [
      {
        service: 'Microsoft.Storage'
      }
    ]
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {}
}

resource functionApp 'Microsoft.Web/sites@2022-09-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp'
  dependsOn: [
    storage
  ]
  properties: {
    serverFarmId: '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Web/serverfarms/plan'
    siteConfig: {
      appSettings: [
        {
          name: 'APPINSIGHTS_NAME'
          value: appInsightsName
        }
      ]
    }
  }
}

// Intentionally out-of-scope resource: ExpressRoute is not in the migration scope table
// and MUST be flagged as a manual-handling dependency / UNMAPPED, never auto-migrated.
resource expressRouteCircuit 'Microsoft.Network/expressRouteCircuits@2023-11-01' = {
  name: expressRouteCircuitName
  location: location
  properties: {
    serviceProviderProperties: {
      serviceProviderName: 'Contoso-ExpressRoute'
      peeringLocation: 'Silicon Valley'
      bandwidthInMbps: 200
    }
  }
}
