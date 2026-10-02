#requires -Version 7.0
param(
    [Parameter(Mandatory)][string]$SubscriptionId,
    [Parameter(Mandatory)][ValidateSet('api','vision','seed')][string]$Component,
    [Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}$')][string]$Tag
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$foundation = Get-Content (Join-Path $root '.local\deployment\Foundation.json') -Raw | ConvertFrom-Json
$registry = $foundation.registryName.value
if ($registry -notmatch '^vitrine[a-z0-9]+acr$') { throw 'Only the independent VITRINE registry is allowed.' }
$context = Join-Path ([IO.Path]::GetTempPath()) "vitrine-build-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $context | Out-Null
try {
    switch ($Component) {
        'api' {
            New-Item -ItemType Directory -Path (Join-Path $context 'backend'), (Join-Path $context 'frontend') | Out-Null
            Copy-Item (Join-Path $root 'Dockerfile') $context
            Copy-Item (Join-Path $root 'backend\requirements.txt') (Join-Path $context 'backend')
            Copy-Item (Join-Path $root 'backend\retail') (Join-Path $context 'backend') -Recurse
            foreach ($file in @('package.json','package-lock.json','index.html','tsconfig.json','vite.config.ts')) {
                Copy-Item (Join-Path $root "frontend\$file") (Join-Path $context 'frontend')
            }
            foreach ($directory in @('src','public')) {
                Copy-Item (Join-Path $root "frontend\$directory") (Join-Path $context 'frontend') -Recurse
            }
        }
        'vision' {
            New-Item -ItemType Directory -Path (Join-Path $context 'vision') | Out-Null
            Copy-Item (Join-Path $root 'vision\Dockerfile') (Join-Path $context 'Dockerfile')
            Copy-Item (Join-Path $root 'vision\*.py') (Join-Path $context 'vision')
            foreach ($file in @('requirements.txt','run-config.json')) {
                Copy-Item (Join-Path $root "vision\$file") (Join-Path $context 'vision')
            }
            Copy-Item (Join-Path $root 'vision\licenses') (Join-Path $context 'vision') -Recurse
        }
        'seed' {
            $assets = Join-Path $root 'assets\generated'
            if (!(Test-Path (Join-Path $assets 'manifest.json'))) { throw 'Complete rendered media manifest is required.' }
            New-Item -ItemType Directory -Path (Join-Path $context 'assets\generated'), (Join-Path $context 'scripts') | Out-Null
            Copy-Item (Join-Path $root 'Dockerfile.seed') (Join-Path $context 'Dockerfile')
            Copy-Item (Join-Path $root 'scripts\seed-media.py') (Join-Path $context 'scripts')
            Copy-Item (Join-Path $assets '*.mp4') (Join-Path $context 'assets\generated')
            Copy-Item (Join-Path $assets '*.json') (Join-Path $context 'assets\generated')
        }
    }
    Get-ChildItem $context -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
    Push-Location $context
    try {
        az acr build --subscription $SubscriptionId --registry $registry --image "vitrine-${Component}:$Tag" --file Dockerfile --no-logs --only-show-errors .
        if ($LASTEXITCODE -ne 0) { throw "VITRINE $Component image build failed." }
    }
    finally { Pop-Location }
}
finally {
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        try {
            Remove-Item -LiteralPath $context -Recurse -Force
            break
        }
        catch [System.IO.IOException] {
            if ($attempt -eq 2) { Write-Warning "Build context is still locked; remove it after the CLI exits: $context" }
            else { Start-Sleep 2 }
        }
    }
}
