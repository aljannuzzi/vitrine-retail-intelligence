#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SubscriptionId,
    [Parameter(Mandatory)][string]$ExpectedTenantId,
    [ValidateSet('Foundation','Bootstrap','Seed','Api','Apps')][string]$Phase = 'Foundation',
    [string]$ApiTag = 'retail5',
    [string]$VisionTag = 'retail3',
    [string]$SeedTag = 'retail1',
    [string]$Location = 'eastus2',
    [string]$AiLocation = 'eastus2'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$group = 'rg-vitrine-retail-demo'
$local = Join-Path $root '.local\deployment'
New-Item -ItemType Directory -Path $local -Force | Out-Null
function AzJson([string[]]$Arguments) {
    $text = & az @Arguments --subscription $SubscriptionId --only-show-errors -o json
    if ($LASTEXITCODE -ne 0) { throw "Azure command failed: $($Arguments[0..2] -join ' ')" }
    if ($text) { return $text | ConvertFrom-Json -Depth 100 }
}
$account = AzJson @('account','show')
if ($account.tenantId -ne $ExpectedTenantId -or $account.id -ne $SubscriptionId) { throw 'Azure target does not match the requested tenant/subscription.' }
AzJson @('group','create','-n',$group,'-l',$Location) | Out-Null
$parameters = @{
    location = @{value=$Location}
    aiLocation = @{value=$AiLocation}
    deployApps = @{value=($Phase -eq 'Apps')}
    deployApi = @{value=($Phase -eq 'Api')}
    deployBootstrap = @{value=($Phase -in @('Bootstrap','Api','Apps'))}
    deploySeed = @{value=($Phase -eq 'Seed')}
}
if ($Phase -eq 'Foundation') {
    $credentialPath = Join-Path $root '.local\vitrine-login.clixml'
    if (!(Test-Path -LiteralPath $credentialPath)) {
        $password = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(24))
        ConvertTo-SecureString $password -AsPlainText -Force | Export-Clixml -LiteralPath $credentialPath
        $parameters.appPassword = @{value=$password}
        $password = $null
    }
    else {
        $secret = Import-Clixml -LiteralPath $credentialPath
        if ($secret -isnot [Security.SecureString]) { throw 'Invalid encrypted VITRINE credential.' }
        try { $parameters.appPassword = @{value=[Net.NetworkCredential]::new('', $secret).Password} }
        finally { $secret.Dispose() }
    }
}
else {
    $foundation = Get-Content (Join-Path $local 'Foundation.json') -Raw | ConvertFrom-Json
    $registry = $foundation.registryServer.value
    if (!$registry.StartsWith('vitrine') -or !$registry.EndsWith('.azurecr.io')) { throw 'Unexpected independent registry.' }
    $parameters.apiImage = @{value="$registry/vitrine-api:$ApiTag"}
    $parameters.bootstrapImage = @{value="$registry/vitrine-api:$ApiTag"}
    $parameters.visionImage = @{value="$registry/vitrine-vision:$VisionTag"}
    $parameters.seedImage = @{value="$registry/vitrine-seed:$SeedTag"}
    $images = switch ($Phase) {
        'Seed' { "vitrine-seed:$SeedTag" }
        'Apps' { "vitrine-api:$ApiTag"; "vitrine-vision:$VisionTag" }
        default { "vitrine-api:$ApiTag" }
    }
    foreach ($image in $images) {
        AzJson @('acr','repository','show','--name',$foundation.registryName.value,'--image',$image) | Out-Null
    }
}
$parameterFile = [IO.Path]::GetTempFileName()
try {
    @{ '$schema'='https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
       contentVersion='1.0.0.0'; parameters=$parameters } | ConvertTo-Json -Depth 20 |
       Set-Content -LiteralPath $parameterFile -Encoding utf8
    $result = AzJson @('deployment','group','create','-g',$group,'-n',"vitrine-$($Phase.ToLowerInvariant())",
        '--template-file',(Join-Path $root 'infra\main.bicep'),'--parameters',"@$parameterFile")
    $result.properties.outputs | ConvertTo-Json -Depth 30 | Set-Content (Join-Path $local "$Phase.json") -Encoding utf8
}
finally {
    Remove-Item -LiteralPath $parameterFile -Force
    $parameters.Remove('appPassword')
}
Write-Host "VITRINE $Phase completed in $group. Non-secret outputs: $local"
if ($Phase -in @('Api','Apps')) {
    & (Join-Path $PSScriptRoot 'Complete-ApiRollout.ps1') -SubscriptionId $SubscriptionId
    Write-Host $result.properties.outputs.apiUrl.value
}
