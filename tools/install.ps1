$ErrorActionPreference = "Stop"

$source = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $env:LOCALAPPDATA "Programs\Nexis Order"

Write-Host "Installing Nexis Order to $dest"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

foreach ($item in @("nexis", "pyproject.toml", "README.md", "LICENSE", "Nexis App.bat")) {
    Copy-Item -Path (Join-Path $source $item) -Destination $dest -Recurse -Force
}
Get-ChildItem -Path (Join-Path $dest "nexis") -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

$builtExe = Join-Path $source "Nexis App.exe"
if (-not (Test-Path $builtExe)) {
    & (Join-Path $PSScriptRoot "build-launcher.ps1")
}
$exe = Join-Path $dest "Nexis App.exe"
Copy-Item -Path $builtExe -Destination $exe -Force

$venvPython = Join-Path $dest ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    $sourcePython = Join-Path $source ".venv\Scripts\python.exe"
    if (-not (Test-Path $sourcePython)) {
        throw "No Python environment found in the project folder."
    }
    $base = (& $sourcePython -c "import sys; print(sys.base_prefix)").Trim()
    & (Join-Path $base "python.exe") -m venv (Join-Path $dest ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the virtual environment."
    }
}

Push-Location $dest
try {
    & $venvPython -m pip install --disable-pip-version-check --quiet ".[app,voice]"
    if ($LASTEXITCODE -ne 0) {
        throw "pip install failed."
    }
} finally {
    Pop-Location
}

$shell = New-Object -ComObject WScript.Shell
$links = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Nexis Order.lnk"),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Nexis Order.lnk")
)
foreach ($link in $links) {
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = $exe
    $shortcut.WorkingDirectory = $dest
    $shortcut.IconLocation = "$exe,0"
    $shortcut.Description = "Nexis Order - local AI agent"
    $shortcut.Save()
}

Write-Host "Done. Launch 'Nexis Order' from the Start menu or the desktop shortcut."
