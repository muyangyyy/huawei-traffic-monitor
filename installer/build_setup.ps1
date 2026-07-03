param(
    [string]$OutputDir = "..\dist"
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$workspaceDist = [System.IO.Path]::GetFullPath((Join-Path $root $OutputDir))
$payload = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "payload_pyinstaller"))
$zipPath = Join-Path $payload "huawei_traffic_monitor.zip"
$setupExe = Join-Path $workspaceDist "HuaweiTrafficMonitorSetup.exe"

$standaloneDist = Join-Path $root "standalone_dist"
$standaloneWork = Join-Path $root "standalone_build"
$standaloneSpec = Join-Path $root "standalone_spec"
$setupDist = Join-Path $root "setup_dist"
$setupWork = Join-Path $root "setup_build"
$setupSpec = Join-Path $root "setup_spec"
$standaloneExe = Join-Path $standaloneDist "HuaweiTrafficMonitor.exe"

$bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$pythonForBuild = if (Test-Path $bundledPython) { $bundledPython } else { "python" }

foreach ($path in @($payload, $standaloneDist, $standaloneWork, $standaloneSpec, $setupDist, $setupWork, $setupSpec)) {
    if (Test-Path $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $payload | Out-Null
New-Item -ItemType Directory -Path $workspaceDist -Force | Out-Null
if (Test-Path $setupExe) {
    Remove-Item -LiteralPath $setupExe -Force
}

& $pythonForBuild -m PyInstaller `
    --clean `
    --onefile `
    --name HuaweiTrafficMonitor `
    --distpath $standaloneDist `
    --workpath $standaloneWork `
    --specpath $standaloneSpec `
    (Join-Path $root "monitor.py")

if (-not (Test-Path $standaloneExe)) {
    throw "Standalone app was not created: $standaloneExe"
}

$staging = Join-Path $payload "staging"
New-Item -ItemType Directory -Path $staging | Out-Null
Copy-Item -LiteralPath (Join-Path $root "app") -Destination (Join-Path $staging "app") -Recurse
Copy-Item -LiteralPath (Join-Path $root "monitor.py") -Destination $staging
Copy-Item -LiteralPath (Join-Path $root "README.md") -Destination $staging
Copy-Item -LiteralPath $standaloneExe -Destination $staging
Get-ChildItem -LiteralPath $staging -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $zipPath -Force

& $pythonForBuild -m PyInstaller `
    --clean `
    --onefile `
    --name HuaweiTrafficMonitorSetup `
    --add-data "$zipPath;." `
    --distpath $setupDist `
    --workpath $setupWork `
    --specpath $setupSpec `
    (Join-Path $PSScriptRoot "setup_installer.py")

$builtSetup = Join-Path $setupDist "HuaweiTrafficMonitorSetup.exe"
if (-not (Test-Path $builtSetup)) {
    throw "Setup app was not created: $builtSetup"
}

Copy-Item -LiteralPath $builtSetup -Destination $setupExe -Force
Get-Item -LiteralPath $setupExe
