# neo/neo-studio.ps1 - start the Neo desktop manager from a PowerShell terminal.
# Everything after the script name goes to neo/manager.py, e.g.:
#   .\neo\neo-studio.ps1 --engine mock --no-webview
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { $py = "py" } else { $py = "python" }
}
if ($py -eq "py") { & py -3 -m neo.manager @args } else { & $py -m neo.manager @args }
