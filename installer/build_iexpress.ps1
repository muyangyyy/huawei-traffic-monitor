param(
    [string]$OutputDir = "..\dist"
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$output = [System.IO.Path]::GetFullPath((Join-Path $root $OutputDir))
$payload = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "payload"))
$zipPath = Join-Path $payload "huawei_traffic_monitor.zip"
$sedPath = Join-Path $PSScriptRoot "HuaweiTrafficMonitorSetup.sed"
$exePath = Join-Path $output "HuaweiTrafficMonitorSetup.exe"

if (Test-Path $payload) {
    Remove-Item -LiteralPath $payload -Recurse -Force
}
New-Item -ItemType Directory -Path $payload | Out-Null
New-Item -ItemType Directory -Path $output -Force | Out-Null
if (Test-Path $exePath) {
    Remove-Item -LiteralPath $exePath -Force
}

$staging = Join-Path $payload "staging"
New-Item -ItemType Directory -Path $staging | Out-Null

$standaloneDist = Join-Path $root "standalone_dist"
$standaloneWork = Join-Path $root "standalone_build"
$standaloneSpec = Join-Path $root "standalone_spec"
$standaloneExe = Join-Path $standaloneDist "HuaweiTrafficMonitor.exe"
function Resolve-PythonWithPyInstaller {
    $bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    $candidates = @("python", "py", $bundledPython)
    foreach ($candidate in $candidates) {
        if ($candidate -eq $bundledPython -and -not (Test-Path $candidate)) {
            continue
        }
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $result = & $candidate -m PyInstaller --version 2>$null
            $exitCode = $LASTEXITCODE
        } catch {
            $result = $null
            $exitCode = 1
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($exitCode -eq 0 -and $result) {
            return $candidate
        }
    }
    throw "PyInstaller is required. Install it with: python -m pip install pyinstaller"
}

$pythonForBuild = Resolve-PythonWithPyInstaller

if (Test-Path $standaloneDist) {
    Remove-Item -LiteralPath $standaloneDist -Recurse -Force
}
if (Test-Path $standaloneWork) {
    Remove-Item -LiteralPath $standaloneWork -Recurse -Force
}
if (Test-Path $standaloneSpec) {
    Remove-Item -LiteralPath $standaloneSpec -Recurse -Force
}

& $pythonForBuild -m PyInstaller `
    --clean `
    --onefile `
    --windowed `
    --name HuaweiTrafficMonitor `
    --distpath $standaloneDist `
    --workpath $standaloneWork `
    --specpath $standaloneSpec `
    (Join-Path $root "monitor.py")

Copy-Item -LiteralPath (Join-Path $root "app") -Destination (Join-Path $staging "app") -Recurse
Copy-Item -LiteralPath (Join-Path $root "monitor.py") -Destination $staging
Copy-Item -LiteralPath (Join-Path $root "README.md") -Destination $staging
if (Test-Path $standaloneExe) {
    Copy-Item -LiteralPath $standaloneExe -Destination $staging
}

Get-ChildItem -LiteralPath $staging -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $zipPath -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install.cmd") -Destination (Join-Path $payload "install.cmd")

$sed = @"
[Version]
Class=IEXPRESS
SEDVersion=3

[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=
DisplayLicense=
FinishMessage=Huawei Traffic Monitor setup completed.
TargetName=$exePath
FriendlyName=Huawei Traffic Monitor Setup
AppLaunched=install.cmd
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
SourceFiles=SourceFiles

[Strings]
FILE0="install.cmd"
FILE1="huawei_traffic_monitor.zip"

[SourceFiles]
SourceFiles0=$payload

[SourceFiles0]
%FILE0%=
%FILE1%=
"@

Set-Content -LiteralPath $sedPath -Value $sed -Encoding ASCII
iexpress.exe /N $sedPath

for ($i = 0; $i -lt 20 -and -not (Test-Path $exePath); $i++) {
    Start-Sleep -Milliseconds 500
}

if (-not (Test-Path $exePath)) {
    throw "IExpress did not create $exePath"
}

Get-Item -LiteralPath $exePath
