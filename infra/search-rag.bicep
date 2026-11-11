targetScope = 'resourceGroup'

param location string = resourceGroup().location
param name string = 'silvercare-rag-${uniqueString(subscription().id, resourceGroup().id)}'

resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: name
  location: location
  tags: { app: 'silvercare', purpose: 'expanded-rag' }
  sku: { name: 'basic' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'Default'
    publicNetworkAccess: 'enabled'
    disableLocalAuth: true
    semanticSearch: 'standard'
  }
}

output endpoint string = 'https://${search.name}.search.windows.net'
output resourceId string = search.id
