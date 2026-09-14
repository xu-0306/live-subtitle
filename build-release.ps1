[CmdletBinding()]
param(
    [string]$Python = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$pythonPath = [IO.Path]::GetFullPath((Join-Path $root $Python))
$distRoot = [IO.Path]::GetFullPath((Join-Path $root "dist"))
$appDir = [IO.Path]::GetFullPath((Join-Path $distRoot "live-subtitle"))
$releaseDir = [IO.Path]::GetFullPath((Join-Path $distRoot "release"))
$archive = [IO.Path]::GetFullPath((Join-Path $releaseDir "Live-Subtitle-windows-x64.zip"))
$extensionDir = [IO.Path]::GetFullPath((Join-Path $root "chrome-extension"))
$extensionManifest = [IO.Path]::GetFullPath((Join-Path $extensionDir "manifest.json"))
$originalPath = $env:PATH
$originalQtPlatform = $env:QT_QPA_PLATFORM
$originalAppData = $env:APPDATA

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Python executable not found: $pythonPath"
}

if (-not (Test-Path -LiteralPath $extensionManifest -PathType Leaf)) {
    throw "Chrome extension manifest not found: $extensionManifest"
}

$manifest = Get-Content -LiteralPath $extensionManifest -Raw | ConvertFrom-Json
if ($manifest.manifest_version -ne 3) {
    throw "Expected a Manifest V3 Chrome extension, found: $($manifest.manifest_version)"
}
if ($manifest.version -notmatch '^\d+\.\d+\.\d+(\.\d+)?$') {
    throw "Invalid Chrome extension version: $($manifest.version)"
}

$extensionArchive = [IO.Path]::GetFullPath((Join-Path $releaseDir "Live-Subtitle-chrome-extension-v$($manifest.version).zip"))

if (-not $appDir.StartsWith($distRoot + [IO.Path]::DirectorySeparatorChar)) {
    throw "Refusing to build outside the project dist directory: $appDir"
}

foreach ($staleArtifact in @($archive, "$archive.sha256", $extensionArchive, "$extensionArchive.sha256")) {
    if (Test-Path -LiteralPath $staleArtifact) {
        Remove-Item -LiteralPath $staleArtifact -Force
    }
}

$env:PYTHONNOUSERSITE = "1"
$env:PYTHONUTF8 = "1"
$env:TORCH_DISABLE_DYNAMO = "1"
$env:TORCHDYNAMO_DISABLE = "1"
$env:PATH = (($originalPath -split ";") | Where-Object {
    $_ -and $_ -notmatch "(?i)[\\/]\.cache[\\/]codex-runtimes[\\/]"
}) -join ";"

Push-Location $root
try {
    & $pythonPath -m PyInstaller --clean --noconfirm "live-subtitle.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    $exePath = Join-Path $appDir "live-subtitle.exe"
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        throw "Build completed without the expected executable: $exePath"
    }

    $collectToc = Join-Path $root "build\live-subtitle\COLLECT-00.toc"
    if (
        (Test-Path -LiteralPath $collectToc) -and
        (Select-String -LiteralPath $collectToc -SimpleMatch "codex-runtimes" -Quiet)
    ) {
        throw "Build contains DLLs from an external Codex runtime: $collectToc"
    }

    $env:QT_QPA_PLATFORM = "offscreen"
    $smokeAppData = Join-Path $root "build\smoke-appdata"
    New-Item -ItemType Directory -Path $smokeAppData -Force | Out-Null
    $env:APPDATA = $smokeAppData
    & $exePath --smoke-test-gui
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged GUI smoke test failed with exit code $LASTEXITCODE"
    }

    New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
    if (Test-Path -LiteralPath $archive) {
        Remove-Item -LiteralPath $archive -Force
    }
    $appEntries = Get-ChildItem -LiteralPath $appDir -Force
    if (-not $appEntries) {
        throw "Packaged application directory is empty: $appDir"
    }
    Compress-Archive -LiteralPath $appEntries.FullName -DestinationPath $archive -CompressionLevel Optimal

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $appZip = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        $exeEntry = $appZip.Entries | Where-Object { $_.FullName -eq "live-subtitle.exe" }
        if (-not $exeEntry) {
            throw "Packaged application does not contain live-subtitle.exe at the ZIP root"
        }
    }
    finally {
        $appZip.Dispose()
    }

    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksumPath = "$archive.sha256"
    "$hash  $([IO.Path]::GetFileName($archive))" | Set-Content -LiteralPath $checksumPath -Encoding ascii

    $extensionEntries = Get-ChildItem -LiteralPath $extensionDir -Force
    if (-not $extensionEntries) {
        throw "Chrome extension directory is empty: $extensionDir"
    }
    Compress-Archive -LiteralPath $extensionEntries.FullName -DestinationPath $extensionArchive -CompressionLevel Optimal

    $extensionZip = [IO.Compression.ZipFile]::OpenRead($extensionArchive)
    try {
        $manifestEntry = $extensionZip.Entries | Where-Object { $_.FullName -eq "manifest.json" }
        if (-not $manifestEntry) {
            throw "Packaged extension does not contain manifest.json at the ZIP root"
        }
    }
    finally {
        $extensionZip.Dispose()
    }

    $extensionHash = (Get-FileHash -LiteralPath $extensionArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    $extensionChecksumPath = "$extensionArchive.sha256"
    "$extensionHash  $([IO.Path]::GetFileName($extensionArchive))" | Set-Content -LiteralPath $extensionChecksumPath -Encoding ascii

    Write-Host "Executable: $exePath"
    Write-Host "Archive:    $archive"
    Write-Host "SHA-256:    $hash"
    Write-Host "Extension:  $extensionArchive"
    Write-Host "SHA-256:    $extensionHash"
}
finally {
    $env:PATH = $originalPath
    if ($null -eq $originalQtPlatform) {
        Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    }
    else {
        $env:QT_QPA_PLATFORM = $originalQtPlatform
    }
    if ($null -eq $originalAppData) {
        Remove-Item Env:APPDATA -ErrorAction SilentlyContinue
    }
    else {
        $env:APPDATA = $originalAppData
    }
    Pop-Location
}
