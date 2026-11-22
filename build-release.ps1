[CmdletBinding()]
param(
    [string]$Python = ".venv\Scripts\python.exe",
    # ffmpeg.exe to bundle (default: the first one on PATH). Without it the release fails on
    # machines that have no FFmpeg of their own.
    [string]$FfmpegPath = ""
)

$ErrorActionPreference = "Stop"
function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [IO.File]::OpenRead($Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha256.ComputeHash($stream)
        return [BitConverter]::ToString($digest).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}
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

if (-not $FfmpegPath) {
    # PATH was filtered above, so an ffmpeg inside a developer tool's runtime is never picked.
    $foundFfmpeg = Get-Command ffmpeg.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($foundFfmpeg) { $FfmpegPath = $foundFfmpeg.Source }
}
if (-not $FfmpegPath -or -not (Test-Path -LiteralPath $FfmpegPath -PathType Leaf)) {
    throw "ffmpeg.exe not found. Put it on PATH or pass -FfmpegPath; the release must bundle it."
}
$FfmpegPath = [IO.Path]::GetFullPath($FfmpegPath)
& $FfmpegPath -version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "ffmpeg.exe does not run: $FfmpegPath"
}
$env:LIVE_SUBTITLE_FFMPEG = $FfmpegPath

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

    $bundledFfmpeg = Join-Path $appDir "_internal\ffmpeg\ffmpeg.exe"
    if (-not (Test-Path -LiteralPath $bundledFfmpeg -PathType Leaf)) {
        throw "Build completed without the bundled FFmpeg: $bundledFfmpeg"
    }
    & $bundledFfmpeg -version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Bundled ffmpeg.exe does not run: $bundledFfmpeg"
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
    # The exe is a windowed app: "&" returns immediately (and leaves $LASTEXITCODE stale), so the ZIP step
    # could race the still-running smoke test for its files. Wait for it and read its real exit code.
    $smoke = Start-Process -FilePath $exePath -ArgumentList "--smoke-test-gui" -Wait -PassThru
    if ($smoke.ExitCode -ne 0) {
        throw "Packaged GUI smoke test failed with exit code $($smoke.ExitCode)"
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

    $hash = Get-Sha256Hex -Path $archive
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

    $extensionHash = Get-Sha256Hex -Path $extensionArchive
    $extensionChecksumPath = "$extensionArchive.sha256"
    "$extensionHash  $([IO.Path]::GetFileName($extensionArchive))" | Set-Content -LiteralPath $extensionChecksumPath -Encoding ascii

    Write-Host "Executable: $exePath"
    Write-Host "Archive:    $archive"
    Write-Host "SHA-256:    $hash"
    Write-Host "Extension:  $extensionArchive"
    Write-Host "SHA-256:    $extensionHash"
}
finally {
    Remove-Item Env:LIVE_SUBTITLE_FFMPEG -ErrorAction SilentlyContinue
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
