# Launch the Finance Team Toolkit and open it in the browser.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Port 8000 is used by another app on this machine, so default to 8801.
$Port = if ($args.Count -ge 1) { $args[0] } else { 8801 }
Write-Host "Starting Finance Team Toolkit on http://127.0.0.1:$Port ..." -ForegroundColor Cyan
Start-Process "http://127.0.0.1:$Port"
python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
