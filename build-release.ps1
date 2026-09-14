[CmdletBinding()]
param(
    [string]$Python = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$pythonPath = [IO.Path]::GetFullPath((Join-Path $root $Python))
$distRoot = [IO.Path]::GetFullPath((Join-Path $root "dist"))
$appDir = [IO.Path]::GetFullPath((Join-Path $distRoot "stt-gui"))
$releaseDir = [IO.Path]::GetFullPath((Join-Path $distRoot "release"))
$archive = [IO.Path]::GetFullPath((Join-Path $releaseDir "STT-Subtitle-Capture-windows-x64.zip"))
$originalPath = $env:PATH
$originalQtPlatform = $env:QT_QPA_PLATFORM
$originalAppData = $env:APPDATA

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Python executable not found: $pythonPath"
}

if (-not $appDir.StartsWith($distRoot + [IO.Path]::DirectorySeparatorChar)) {
    throw "Refusing to build outside the project dist directory: $appDir"
}

foreach ($staleArtifact in @($archive, "$archive.sha256")) {
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
    & $pythonPath -m PyInstaller --clean --noconfirm "stt-gui.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    $exePath = Join-Path $appDir "stt-gui.exe"
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        throw "Build completed without the expected executable: $exePath"
    }

    $collectToc = Join-Path $root "build\stt-gui\COLLECT-00.toc"
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
    Compress-Archive -LiteralPath $appDir -DestinationPath $archive -CompressionLevel Optimal

    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksumPath = "$archive.sha256"
    "$hash  $([IO.Path]::GetFileName($archive))" | Set-Content -LiteralPath $checksumPath -Encoding ascii

    Write-Host "Executable: $exePath"
    Write-Host "Archive:    $archive"
    Write-Host "SHA-256:    $hash"
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
