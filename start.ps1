param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
function Test-Python312 {
    param([string]$Executable, [string[]]$Arguments = @())
    try {
        & $Executable @Arguments -c "import sys, sysconfig; raise SystemExit(sys.version_info[:2] != (3, 12) or sysconfig.get_platform() != 'win-amd64')" *> $null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    # The bundled search engine (vendor/wheels) is built for Python 3.12.
    $runtime = $null
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        if (Test-Python312 -Executable $launcher.Source -Arguments @('-3.12')) {
            $runtime = @($launcher.Source, '-3.12')
        }
    }
    if (-not $runtime) {
        foreach ($name in @('python', 'python3')) {
            $found = Get-Command $name -ErrorAction SilentlyContinue
            if (-not $found) { continue }
            if (Test-Python312 -Executable $found.Source) { $runtime = @($found.Source); break }
        }
    }
    if (-not $runtime) {
        throw 'Python 3.12 (x64) is required. Install it from https://www.python.org/downloads/ (check "Add Python to PATH").'
    }
    Write-Host 'Creating the Python environment...'
    if ($runtime.Count -gt 1) { & $runtime[0] $runtime[1] -m venv (Join-Path $PSScriptRoot '.venv') } else { & $runtime[0] -m venv (Join-Path $PSScriptRoot '.venv') }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
}
if (-not (Test-Python312 -Executable $python)) {
    throw 'The .venv environment requires Python 3.12 (x64). Recreate it with the supported Python version.'
}
& $python -c "import importlib.util as u; raise SystemExit(0 if all(u.find_spec(m) for m in ('poke_env', 'webview', 'psutil', 'aiohttp', 'poke_engine', 'torch', 'dateutil')) else 1)" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing dependencies (first run needs internet)...'
    & $python -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Installing the dependencies failed. Check your connection and try again.' }
}
# Node.js and the Showdown simulator live in runtime/ (not in git: ~250 MB).
$runtime = Join-Path $PSScriptRoot 'runtime'
$node = Join-Path $runtime 'node.exe'
if (-not (Test-Path -LiteralPath $node) -or -not (Test-Path -LiteralPath (Join-Path $runtime 'node_modules\pokemon-showdown'))) {
    Write-Host 'Downloading Node.js 24.16.0 and the Pokemon Showdown simulator (first run only)...'
    $release = 'node-v24.16.0-win-x64'
    $zip = Join-Path $env:TEMP "$release.zip"
    $unpacked = Join-Path $env:TEMP 'unaware-node'
    Invoke-WebRequest -Uri "https://nodejs.org/dist/v24.16.0/$release.zip" -OutFile $zip -UseBasicParsing
    Expand-Archive -LiteralPath $zip -DestinationPath $unpacked -Force
    Copy-Item -LiteralPath (Join-Path $unpacked "$release\node.exe") -Destination $node -Force
    Push-Location $runtime
    try {
        & (Join-Path $unpacked "$release\npm.cmd") ci --omit=optional --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Installing the Showdown simulator failed.' }
    } finally { Pop-Location }
    Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $unpacked -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host 'Checking the simulator...'
& $python -c 'from unaware.search import SimulatorSearch; s=SimulatorSearch(); s.ready(); s.close()'
if ($LASTEXITCODE -ne 0) { throw 'The simulator did not start. Delete the runtime\node_modules folder and run start.bat again.' }
# Shortcut with the app icon next to start.bat (a .bat cannot carry an icon).
$shortcut = Join-Path $PSScriptRoot 'Unaware.lnk'
$icon = Join-Path $PSScriptRoot 'assets\icon.ico'
# Rewritten on every run so an icon change in an update reaches the shortcut.
if (Test-Path -LiteralPath $icon) {
    try {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($shortcut)
        $link.TargetPath = Join-Path $PSScriptRoot 'start.bat'
        $link.WorkingDirectory = $PSScriptRoot
        $link.IconLocation = "$icon,0"
        $link.WindowStyle = 7
        $link.Save()
    } catch { }
}
if ($CheckOnly) {
    & $python -m unaware --help
    exit $LASTEXITCODE
}
$windowPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
Start-Process -FilePath $windowPython -ArgumentList @('-m', 'unaware', '--base-dir', ('"' + $PSScriptRoot + '"'), 'gui') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
