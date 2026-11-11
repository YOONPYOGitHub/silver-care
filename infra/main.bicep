targetScope = 'resourceGroup'

param location string = resourceGroup().location
param suffix string = uniqueString(subscription().id, resourceGroup().id)
param clusterName string = 'aks-silvercare'
param nodeSize string = 'Standard_D2ds_v5'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'silvercare${suffix}'
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: 'silvercare-${suffix}'
  location: location
  sku: { name: 'free' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'Default'
    publicNetworkAccess: 'enabled'
    disableLocalAuth: true
  }
}

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'silvercare-app'
  location: location
}

resource cluster 'Microsoft.ContainerService/managedClusters@2025-05-01' = {
  name: clusterName
  location: location
  identity: { type: 'SystemAssigned' }
  sku: { name: 'Base', tier: 'Free' }
  properties: {
    dnsPrefix: 'silvercare-${suffix}'
    enableRBAC: true
    oidcIssuerProfile: { enabled: true }
    securityProfile: { workloadIdentity: { enabled: true } }
    agentPoolProfiles: [{
      name: 'system'
      count: 1
      vmSize: nodeSize
      osDiskSizeGB: 64
      osType: 'Linux'
      type: 'VirtualMachineScaleSets'
      mode: 'System'
      maxPods: 30
    }]
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      networkPolicy: 'calico'
      loadBalancerSku: 'standard'
      outboundType: 'loadBalancer'
      serviceCidr: '10.63.0.0/16'
      dnsServiceIP: '10.63.0.10'
      podCidr: '10.244.0.0/16'
      loadBalancerProfile: { managedOutboundIPs: { count: 1 } }
    }
    autoUpgradeProfile: { upgradeChannel: 'stable', nodeOSUpgradeChannel: 'NodeImage' }
  }
}

resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: appIdentity
  name: 'silvercare-aks'
  properties: {
    issuer: cluster.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:silvercare:silvercare'
    audiences: ['api://AzureADTokenExchange']
  }
}

output registryName string = registry.name
output registryServer string = registry.properties.loginServer
output registryId string = registry.id
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output searchId string = search.id
output clusterName string = cluster.name
output identityClientId string = appIdentity.properties.clientId
output identityPrincipalId string = appIdentity.properties.principalId
output kubeletPrincipalId string = cluster.properties.identityProfile.kubeletidentity.objectId
