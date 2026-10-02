#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$ArchivePath = '',
    [string]$Destination = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$manifest = Get-Content (Join-Path $root 'assets\demo-media.json') -Raw | ConvertFrom-Json
if (!$Destination) { $Destination = Join-Path $root 'assets\generated' }
if (!$ArchivePath) {
    $downloads = Join-Path $root '.local\media-download'
    New-Item -ItemType Directory -Path $downloads -Force | Out-Null
    $ArchivePath = Join-Path $downloads $manifest.asset
    if (!(Test-Path -LiteralPath $ArchivePath)) {
        & gh release download $manifest.release --repo $manifest.repository --pattern $manifest.asset --dir $downloads
        if ($LASTEXITCODE -ne 0) { throw 'Cannot download the media release. Use gh auth login for a private repository, or supply -ArchivePath.' }
    }
}
$ArchivePath = (Resolve-Path -LiteralPath $ArchivePath).Path
$actual = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $manifest.sha256) { throw 'Media archive SHA256 mismatch; no files were installed.' }
$staging = Join-Path $root ".local\media-verify-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $staging -Force | Out-Null
try {
    $zip = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        $expected = @($manifest.expectedFiles)
        if ($zip.Entries.Count -ne $expected.Count) { throw 'Unexpected number of media archive entries.' }
        $names = @($zip.Entries | ForEach-Object { $_.FullName })
        if (@($names | Sort-Object -Unique).Count -ne $names.Count) { throw 'Duplicate archive entries are not allowed.' }
        foreach ($entry in $zip.Entries) {
            if ($entry.FullName -cnotin $expected -or $entry.FullName -match '[/\\]' -or $entry.Length -gt 104857600) {
                throw 'Unexpected or oversized media archive entry.'
            }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $staging $entry.FullName), $false)
        }
    }
    finally { $zip.Dispose() }
    $clips = Get-Content (Join-Path $staging 'manifest.json') -Raw | ConvertFrom-Json
    if ($clips.videos.Count -ne 6) { throw 'Six original synthetic videos are required.' }
    foreach ($video in $clips.videos) {
        if ($video.filename -cnotin $manifest.expectedFiles -or $video.provenance -ne 'synthetic-3d') {
            throw 'Unexpected media provenance or filename.'
        }
        $path = Join-Path $staging $video.filename
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $video.sha256) {
            throw "Video hash mismatch: $($video.filename)"
        }
    }
    foreach ($name in $manifest.expectedFiles) {
        $existing = Join-Path $Destination $name
        if ((Test-Path -LiteralPath $existing) -and
            (Get-FileHash -LiteralPath $existing).Hash -ne (Get-FileHash -LiteralPath (Join-Path $staging $name)).Hash) {
            throw "Existing media differs: $name. Preserve your files or choose another -Destination."
        }
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($name in $manifest.expectedFiles) {
        $target = Join-Path $Destination $name
        if (!(Test-Path -LiteralPath $target)) { Copy-Item -LiteralPath (Join-Path $staging $name) -Destination $target }
    }
    Write-Host "Six verified synthetic clips and provenance installed in $Destination"
}
finally { Remove-Item -LiteralPath $staging -Recurse -Force }
