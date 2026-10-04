// Standalone VNet + subnet sample, migration scope: Microsoft.Network/virtualNetworks (+subnets) -> AWS VPC.
param location string = resourceGroup().location
param vnetName string = 'vnet-migration-demo'
param subnetName string = 'subnet-app'
param vnetAddressPrefix string = '10.30.0.0/16'
param subnetAddressPrefix string = '10.30.1.0/24'

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
  }
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' = {
  parent: vnet
  name: subnetName
  properties: {
    addressPrefix: subnetAddressPrefix
    serviceEndpoints: [
      {
        service: 'Microsoft.Storage'
      }
    ]
  }
}

output vnetName string = vnet.name
output subnetName string = subnet.name
output vnetAddressPrefix string = vnetAddressPrefix
output subnetAddressPrefix string = subnetAddressPrefix
