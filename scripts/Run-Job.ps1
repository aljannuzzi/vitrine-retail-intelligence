#requires -Version 7.0
param(
    [Parameter(Mandatory)][string]$SubscriptionId,
    [ValidateSet('vitrine-agent-bootstrap','vitrine-media-seed')][string]$JobName,
    [int]$TimeoutSeconds = 900
)
$ErrorActionPreference = 'Stop'
$execution = az containerapp job start --subscription $SubscriptionId -g rg-vitrine-retail-demo -n $JobName --only-show-errors -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or !$execution.name) { throw 'Job execution did not start.' }
$name = ($execution.name -split '/')[-1]
$deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
do {
    Start-Sleep 10
    $state = az containerapp job execution show --subscription $SubscriptionId -g rg-vitrine-retail-demo -n $JobName --job-execution-name $name --query properties.status -o tsv --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Cannot retrieve execution state.' }
    Write-Host "$name : $state"
    if ($state -eq 'Succeeded') { return $name }
    if ($state -in @('Failed','Canceled','Cancelled')) { throw "Execution $name ended with $state." }
} while ([DateTimeOffset]::UtcNow -lt $deadline)
throw "Execution $name exceeded the wait deadline."
