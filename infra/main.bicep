targetScope = 'resourceGroup'

@allowed(['vitrine'])
param workload string = 'vitrine'
param location string = 'eastus2'
param aiLocation string = 'eastus2'
@secure()
param appPassword string = ''
param deployApps bool = false
param deployApi bool = false
param apiImage string = ''
param visionImage string = ''
param deployBootstrap bool = false
param bootstrapImage string = ''
param deploySeed bool = false
param seedImage string = ''

var suffix = uniqueString(resourceGroup().id, workload)
var compact = '${workload}${suffix}'
var aiName = '${compact}ai'
var storageName = take('${compact}st', 24)
var registryName = '${compact}acr'
var cosmosName = '${compact}db'
var vaultName = take('${workload}-kv-${suffix}', 24)
var projectName = 'vitrine-retail'
var projectEndpoint = 'https://${aiName}.services.ai.azure.com/api/projects/${projectName}'
var openaiEndpoint = 'https://${aiName}.openai.azure.com'
var acrRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var blobRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
var secretsRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var aiUserRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
var openaiRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')

resource runtime 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${workload}-runtime'
  location: location
}
resource bootstrap 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${workload}-bootstrap'
  location: location
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false, publicNetworkAccess: 'Enabled' }
}
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Disabled'
  }
}
resource password 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(appPassword)) {
  parent: vault
  name: 'app-password'
  properties: { value: appPassword }
}
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${workload}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Disabled'
  }
}
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: { deleteRetentionPolicy: { enabled: true, days: 7 } }
}
resource media 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'retail'
  properties: { publicAccess: 'None' }
}
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: cosmosName
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    locations: [{ locationName: location, failoverPriority: 0 }]
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    minimalTlsVersion: 'Tls12'
  }
}
resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmos
  name: 'vitrine'
  properties: { resource: { id: 'vitrine' } }
}
resource container 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'retail'
  properties: {
    options: { throughput: 400 }
    resource: {
      id: 'retail'
      partitionKey: { paths: ['/pk'], kind: 'Hash' }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [{ path: '/*' }]
        excludedPaths: [{ path: '/"_etag"/?' }]
      }
    }
  }
}
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${workload}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.86.0.0/16'] }
    subnets: [
      {
        name: 'apps'
        properties: {
          addressPrefix: '10.86.0.0/23'
          delegations: [{ name: 'container-apps', properties: { serviceName: 'Microsoft.App/environments' } }]
        }
      }
      {
        name: 'private-endpoints'
        properties: { addressPrefix: '10.86.2.0/24', privateEndpointNetworkPolicies: 'Disabled' }
      }
    ]
  }
}
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: aiName
  location: aiLocation
  kind: 'AIServices'
  identity: { type: 'SystemAssigned' }
  sku: { name: 'S0' }
  properties: {
    allowProjectManagement: true
    customSubDomainName: aiName
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    restrictOutboundNetworkAccess: false
  }
}
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: projectName
  location: aiLocation
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'VITRINE Retail Intelligence'
    description: 'Cloud-only synthetic retail demonstration with managed advisory agents.'
  }
}
resource agentModel 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  dependsOn: [project]
  name: 'retail-agent'
  sku: { name: 'GlobalStandard', capacity: 20 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-4.1', version: '2025-04-14' }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
  }
}
resource visionModel 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  dependsOn: [agentModel]
  name: 'retail-vision'
  sku: { name: 'GlobalStandard', capacity: 20 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-5.4', version: '2026-03-05' }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
  }
}
var identityIds = [runtime.id, bootstrap.id]
resource pullRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (id, i) in identityIds: {
  name: guid(registry.id, id, acrRole)
  scope: registry
  properties: { principalId: i == 0 ? runtime.properties.principalId : bootstrap.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: acrRole }
}]
resource blobRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (id, i) in identityIds: {
  name: guid(storage.id, id, blobRole)
  scope: storage
  properties: { principalId: i == 0 ? runtime.properties.principalId : bootstrap.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: blobRole }
}]
resource cosmosRoles 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = [for (id, i) in identityIds: {
  parent: cosmos
  name: guid(cosmos.id, id, 'data-contributor')
  properties: {
    principalId: i == 0 ? runtime.properties.principalId : bootstrap.properties.principalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: cosmos.id
  }
}]
resource agentRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (id, i) in identityIds: {
  name: guid(project.id, id, aiUserRole)
  scope: project
  properties: { principalId: i == 0 ? runtime.properties.principalId : bootstrap.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: aiUserRole }
}]
resource projectRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, project.id, aiUserRole)
  scope: foundry
  properties: { principalId: project.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: aiUserRole }
}
resource modelRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, runtime.id, openaiRole)
  scope: foundry
  properties: { principalId: runtime.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: openaiRole }
}
resource secretRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, runtime.id, secretsRole)
  scope: vault
  properties: { principalId: runtime.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: secretsRole }
}
var zoneNames = [
  'privatelink.vaultcore.azure.net'
  'privatelink.blob.core.windows.net'
  'privatelink.documents.azure.com'
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
]
resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for zone in zoneNames: {
  name: zone
  location: 'global'
}]
resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (zone, i) in zoneNames: {
  name: '${zones[i].name}/${workload}-link'
  location: 'global'
  properties: { registrationEnabled: false, virtualNetwork: { id: vnet.id } }
}]
var endpointTargets = [
  { name: 'vault', id: vault.id, group: 'vault', zones: [0] }
  { name: 'blob', id: storage.id, group: 'blob', zones: [1] }
  { name: 'cosmos', id: cosmos.id, group: 'Sql', zones: [2] }
  { name: 'foundry', id: foundry.id, group: 'account', zones: [3, 4, 5] }
]
resource endpoints 'Microsoft.Network/privateEndpoints@2024-05-01' = [for target in endpointTargets: {
  name: '${workload}-${target.name}-pe'
  location: location
  dependsOn: [visionModel]
  properties: {
    subnet: { id: '${vnet.id}/subnets/private-endpoints' }
    privateLinkServiceConnections: [{
      name: target.name
      properties: { privateLinkServiceId: target.id, groupIds: [target.group] }
    }]
  }
}]
resource zoneGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (target, i) in endpointTargets: {
  name: '${endpoints[i].name}/default'
  properties: {
    privateDnsZoneConfigs: [for zone in target.zones: {
      name: replace(zoneNames[zone], '.', '-')
      properties: { privateDnsZoneId: zones[zone].id }
    }]
  }
}]
resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${workload}-environment'
  location: location
  properties: {
    vnetConfiguration: { infrastructureSubnetId: '${vnet.id}/subnets/apps', internal: false }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}
var commonEnv = [
  { name: 'ENVIRONMENT', value: 'azure' }
  { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
  { name: 'COSMOS_DATABASE', value: 'vitrine' }
  { name: 'COSMOS_CONTAINER', value: 'retail' }
  { name: 'AZURE_STORAGE_ACCOUNT', value: storage.name }
  { name: 'AZURE_STORAGE_CONTAINER', value: 'retail' }
  { name: 'FOUNDRY_PROJECT_ENDPOINT', value: projectEndpoint }
  { name: 'AGENT_MODEL_DEPLOYMENT', value: 'retail-agent' }
  { name: 'AZURE_OPENAI_ENDPOINT', value: openaiEndpoint }
  { name: 'VISION_MODEL_DEPLOYMENT', value: 'retail-vision' }
  { name: 'PYTHONUNBUFFERED', value: '1' }
]
resource api 'Microsoft.App/containerApps@2024-03-01' = if (deployApps || deployApi) {
  name: '${workload}-api'
  location: location
  dependsOn: [zoneGroups, links, pullRoles, secretRole, password, cosmosRoles, agentRoles]
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${runtime.id}': {} } }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: true, allowInsecure: false, targetPort: 8080, transport: 'auto' }
      registries: [{ server: registry.properties.loginServer, identity: runtime.id }]
      secrets: [{ name: 'app-password', identity: runtime.id, keyVaultUrl: '${vault.properties.vaultUri}secrets/app-password' }]
    }
    template: {
      containers: [{
        name: 'api'
        image: apiImage
        env: concat(commonEnv, [
          { name: 'AZURE_CLIENT_ID', value: runtime.properties.clientId }
          { name: 'APP_PASSWORD', secretRef: 'app-password' }
          { name: 'PUBLIC_ORIGIN', value: 'https://${workload}-api.${environment.properties.defaultDomain}' }
        ])
        resources: { cpu: json('1.0'), memory: '2Gi' }
        probes: [
          { type: 'Startup', httpGet: { path: '/health', port: 8080 }, initialDelaySeconds: 10, periodSeconds: 10, failureThreshold: 30 }
          { type: 'Readiness', httpGet: { path: '/health', port: 8080 }, periodSeconds: 10 }
          { type: 'Liveness', httpGet: { path: '/health', port: 8080 }, periodSeconds: 30, failureThreshold: 5 }
        ]
      }]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}
resource vision 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: '${workload}-vision'
  location: location
  dependsOn: [zoneGroups, links, pullRoles, cosmosRoles, blobRoles, modelRole]
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${runtime.id}': {} } }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [{ server: registry.properties.loginServer, identity: runtime.id }]
    }
    template: {
      containers: [{
        name: 'vision'
        image: visionImage
        env: concat(commonEnv, [{ name: 'AZURE_CLIENT_ID', value: runtime.properties.clientId }])
        resources: { cpu: json('4.0'), memory: '8Gi' }
        probes: [
          { type: 'Startup', httpGet: { path: '/healthz', port: 8081 }, initialDelaySeconds: 20, periodSeconds: 10, failureThreshold: 60 }
          { type: 'Readiness', httpGet: { path: '/healthz', port: 8081 }, periodSeconds: 15 }
          { type: 'Liveness', httpGet: { path: '/healthz', port: 8081 }, periodSeconds: 30, failureThreshold: 10 }
        ]
      }]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}
resource bootstrapJob 'Microsoft.App/jobs@2024-03-01' = if (deployBootstrap) {
  name: '${workload}-agent-bootstrap'
  location: location
  dependsOn: [zoneGroups, links, pullRoles, cosmosRoles, blobRoles, agentRoles, projectRole]
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${bootstrap.id}': {} } }
  properties: {
    environmentId: environment.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 900
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: registry.properties.loginServer, identity: bootstrap.id }]
    }
    template: {
      containers: [{
        name: 'bootstrap'
        image: bootstrapImage
        command: ['python', '-m', 'retail.bootstrap']
        env: concat(commonEnv, [{ name: 'AZURE_CLIENT_ID', value: bootstrap.properties.clientId }])
        resources: { cpu: json('1.0'), memory: '2Gi' }
      }]
    }
  }
}
resource seedJob 'Microsoft.App/jobs@2024-03-01' = if (deploySeed) {
  name: '${workload}-media-seed'
  location: location
  dependsOn: [zoneGroups, links, pullRoles, blobRoles]
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${bootstrap.id}': {} } }
  properties: {
    environmentId: environment.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 900
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: registry.properties.loginServer, identity: bootstrap.id }]
    }
    template: {
      containers: [{
        name: 'seed'
        image: seedImage
        command: ['python', 'scripts/seed-media.py']
        env: concat(commonEnv, [{ name: 'AZURE_CLIENT_ID', value: bootstrap.properties.clientId }])
        resources: { cpu: json('1.0'), memory: '2Gi' }
      }]
    }
  }
}

output registryName string = registry.name
output registryServer string = registry.properties.loginServer
output environmentName string = environment.name
output apiUrl string = 'https://${workload}-api.${environment.properties.defaultDomain}'
output storageAccount string = storage.name
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output foundryName string = foundry.name
output foundryProjectEndpoint string = projectEndpoint
output runtimeClientId string = runtime.properties.clientId
output bootstrapClientId string = bootstrap.properties.clientId
output keyVaultName string = vault.name
