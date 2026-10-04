@description('Name of the Key Vault. Becomes the naming prefix on the AWS side, e.g. "myapp-prod".')
param keyVaultName string

@description('Azure region to deploy the Key Vault into.')
param location string = resourceGroup().location

@description('Azure AD tenant ID that owns the vault.')
param tenantId string = subscription().tenantId

@secure()
@description('Database username to store as a secret.')
param dbUsername string

@secure()
@description('Database password to store as a secret.')
param dbPassword string

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

resource dbUsernameSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'db-username'
  properties: {
    value: dbUsername
    contentType: 'text/plain'
  }
}

resource dbPasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'db-password'
  properties: {
    value: dbPassword
    contentType: 'text/plain'
  }
}

output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
