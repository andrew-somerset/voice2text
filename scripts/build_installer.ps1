[CmdletBinding()]
param(
    [string]$ModelPath = "$env:LOCALAPPDATA\voice2text\models\ggml-base.en.bin",
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$buildDir = Join-Path $repoRoot "build\voice2text"
$distDir = Join-Path $repoRoot "dist"
$expectedModelHash = "A03779C86DF3323075F5E796CB2CE5029F00EC8869EEE3FDFB897AFE36C6D002"

Push-Location $repoRoot
try {
    if (-not (Test-Path -LiteralPath $ModelPath -PathType Leaf)) {
        throw "Reviewed model not found at $ModelPath. Run: uv run voice2text --setup-model"
    }
    $actualModelHash = (Get-FileHash -LiteralPath $ModelPath -Algorithm SHA256).Hash
    if ($actualModelHash -ne $expectedModelHash) {
        throw "The installer model did not match the reviewed SHA-256."
    }

    $versionLine = Select-String -Path "pyproject.toml" -Pattern '^version = "([^"]+)"$' |
        Select-Object -First 1
    if (-not $versionLine) {
        throw "Could not determine the Voice2Text version from pyproject.toml."
    }
    $version = $versionLine.Matches[0].Groups[1].Value

    & uv sync --frozen --group build
    if ($LASTEXITCODE -ne 0) {
        throw "uv sync failed with exit code $LASTEXITCODE."
    }

    if (Test-Path -LiteralPath $buildDir) {
        Remove-Item -LiteralPath $buildDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $distDir -Force | Out-Null

    $env:VOICE2TEXT_BUILD_MODEL_PATH = (Resolve-Path $ModelPath).Path
    $env:VOICE2TEXT_BUILD_DIR = $buildDir
    & uv run --frozen --group build python "installer\setup_freeze.py" build_exe
    if ($LASTEXITCODE -ne 0) {
        throw "cx_Freeze failed with exit code $LASTEXITCODE."
    }

    $frozenExecutable = Join-Path $buildDir "Voice2Text.exe"
    if (-not (Test-Path -LiteralPath $frozenExecutable -PathType Leaf)) {
        throw "Frozen Voice2Text.exe was not produced."
    }
    $bundledModel = Join-Path $buildDir "models\ggml-base.en.bin"
    if (-not (Test-Path -LiteralPath $bundledModel -PathType Leaf)) {
        throw "The reviewed model was not included in the one-folder build."
    }
    if ((Get-FileHash -LiteralPath $bundledModel -Algorithm SHA256).Hash -ne $expectedModelHash) {
        throw "The bundled model failed its post-build SHA-256 check."
    }

    if ($SkipInstaller) {
        Write-Host "One-folder application ready: $buildDir"
        exit 0
    }

    $isccCandidates = @(
        (Get-Command iscc.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
    $iscc = $isccCandidates | Select-Object -First 1
    if (-not $iscc) {
        throw "Inno Setup 6 is required. Install it with: winget install --id JRSoftware.InnoSetup -e"
    }

    & $iscc "/DAppVersion=$version" "/DBuildDir=$buildDir" "/DOutputDir=$distDir" `
        "installer\Voice2Text.iss"
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed with exit code $LASTEXITCODE."
    }

    $installer = Join-Path $distDir "Voice2Text-Setup-$version.exe"
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw "Installer output was not produced."
    }
    $artifact = Get-Item -LiteralPath $installer
    $artifactHash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
    Write-Host "Installer ready: $($artifact.FullName)"
    Write-Host "Size: $([math]::Round($artifact.Length / 1MB, 1)) MB"
    Write-Host "SHA-256: $artifactHash"
}
finally {
    Pop-Location
}
