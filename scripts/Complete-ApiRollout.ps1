#requires -Version 7.0
param([Parameter(Mandatory)][string]$SubscriptionId, [string]$PreviousRevision = '')
$ErrorActionPreference = 'Stop'
function AzJson([string[]]$Arguments) {
    $text = az @Arguments --subscription $SubscriptionId --only-show-errors -o json
    if ($LASTEXITCODE -ne 0) { throw "Azure rollout operation failed: $($Arguments[0..2] -join ' ')" }
    if ($text) { $text | ConvertFrom-Json }
}
$app = AzJson @('containerapp','show','-g','rg-vitrine-retail-demo','-n','vitrine-api')
$target = $app.properties.latestRevisionName
if (!$target -or !$target.StartsWith('vitrine-api--')) { throw 'Unexpected VITRINE revision.' }
if (!$PreviousRevision) { $PreviousRevision = $app.properties.latestReadyRevisionName }
$released = $false
try {
    if ($PreviousRevision -and $PreviousRevision -ne $target -and $app.properties.latestReadyRevisionName -ne $target) {
        $startupDeadline = [DateTimeOffset]::UtcNow.AddMinutes(3)
        do {
            $replicas = AzJson @('containerapp','replica','list','-g','rg-vitrine-retail-demo','-n','vitrine-api','--revision',$target)
            $running = @($replicas | Where-Object { $_.properties.containers.runningState -contains 'Running' }).Count -gt 0
            if ($running) { break }
            if ([DateTimeOffset]::UtcNow -ge $startupDeadline) {
                throw 'New image did not start; previous revision was not stopped.'
            }
            Start-Sleep 8
        } while ($true)
        # Single-writer simulation intentionally uses a brief controlled handoff.
        AzJson @('containerapp','revision','deactivate','-g','rg-vitrine-retail-demo','-n','vitrine-api','--revision',$PreviousRevision) | Out-Null
        $released = $true
    }
    $deadline = [DateTimeOffset]::UtcNow.AddMinutes(4)
    $url = "https://$($app.properties.configuration.ingress.fqdn)/health"
    do {
        $current = AzJson @('containerapp','show','-g','rg-vitrine-retail-demo','-n','vitrine-api')
        if ($current.properties.latestReadyRevisionName -eq $target) {
            $response = Invoke-WebRequest $url -TimeoutSec 15 -SkipHttpErrorCheck
            if ($response.StatusCode -eq 200 -and ($response.Content | ConvertFrom-Json).status -eq 'ready') {
                Write-Host "VITRINE ready: $target"
                return
            }
        }
        Start-Sleep 8
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw 'VITRINE readiness deadline exceeded.'
}
catch {
    if ($released) {
        AzJson @('containerapp','revision','deactivate','-g','rg-vitrine-retail-demo','-n','vitrine-api','--revision',$target) | Out-Null
        AzJson @('containerapp','revision','activate','-g','rg-vitrine-retail-demo','-n','vitrine-api','--revision',$PreviousRevision) | Out-Null
        Write-Warning 'Previous VITRINE revision reactivated after failed handoff.'
    }
    throw
}
