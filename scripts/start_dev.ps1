<#
.SYNOPSIS
  Start RelayDesk locally: backend, frontend, and ngrok.

.DESCRIPTION
  Safe to run again at any time: it stops stale servers on ports 8000/3000 (Python or Node only),
  reuses a running ngrok tunnel and waits until everything answers. The separate local browser
  TwiML app follows ngrok; the production phone number and Retell agent stay on Render.

  Logs:  backend  call_agent_backend\logs\app.log   (follow with scripts\tail_logs.ps1)
         frontend call_agent_frontend\logs\dev.log
         ngrok    call_agent_backend\logs\ngrok.log

  Set NGROK_DOMAIN in call_agent_backend\.env to a free static ngrok domain so the URL never changes.

.EXAMPLE
  .\scripts\start_dev.ps1            # start or restart local browser calling
  .\scripts\start_dev.ps1 -SyncProviderWebhooks  # route Twilio and Retell to this PC for local call tests
  .\scripts\start_dev.ps1 -Stop      # stop backend, frontend and ngrok
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$NoBrowser,
    [switch]$NoNgrok,
    [switch]$SyncProviderWebhooks
)

$ErrorActionPreference = 'Stop'
$backend = Split-Path -Parent $PSScriptRoot
$frontend = Join-Path (Split-Path -Parent $backend) 'call_agent_frontend'
$backendLogs = Join-Path $backend 'logs'
$frontendLogs = Join-Path $frontend 'logs'
New-Item -ItemType Directory -Force -Path $backendLogs, $frontendLogs | Out-Null

function Write-Step([string]$text) { Write-Host "-> $text" -ForegroundColor Cyan }

function Get-EnvValue([string]$name) {
    $line = Get-Content (Join-Path $backend '.env') -ErrorAction SilentlyContinue | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if ($line) { return ($line -replace "^$name=", '').Trim() }
    return ''
}

# Windows PowerShell 5.1 turns any stderr output of a native program into a terminating error
# under ErrorActionPreference=Stop, so native commands run through this helper.
function Invoke-Native([scriptblock]$command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $command 2>&1 | ForEach-Object { "$_" } } finally { $ErrorActionPreference = $previous }
}

function Get-Python {
    $venv = Join-Path $backend '.venv\Scripts\python.exe'
    if (Test-Path $venv) {
        Invoke-Native { & $venv -c "import fastapi, uvicorn, twilio, cryptography" } | Out-Null
        if ($LASTEXITCODE -eq 0) { return $venv }
    }
    return 'python'
}

function Stop-PortOwner([int]$port) {
    $owners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($owner in $owners) {
        $process = Get-Process -Id $owner -ErrorAction SilentlyContinue
        # A listener whose process has exited is held by an orphaned child (e.g. an old --reload worker).
        $targets = if ($process) { @($process) } else { Get-CimInstance Win32_Process -Filter "ParentProcessId=$owner" | ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue } }
        foreach ($target in $targets) {
            if ($target.ProcessName -notin @('python', 'node')) { throw "Port $port is used by $($target.ProcessName) (PID $($target.Id)). Stop it first." }
            Stop-Process -Id $target.Id -Force
            Write-Host "   stopped $($target.ProcessName) (PID $($target.Id)) on port $port"
        }
    }
}

function Wait-Url([string]$url, [int]$seconds, [string]$what) {
    $deadline = (Get-Date).AddSeconds($seconds)
    do {
        try { Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3 | Out-Null; return } catch { if ($_.Exception.Response) { return } }
        Start-Sleep -Milliseconds 700
    } while ((Get-Date) -lt $deadline)
    throw "$what did not start within $seconds s. Check the logs listed in this script's help."
}

function Get-NgrokUrl {
    try { return ((Invoke-RestMethod http://127.0.0.1:4040/api/tunnels -TimeoutSec 2).tunnels | Where-Object { $_.public_url -like 'https://*' } | Select-Object -First 1).public_url } catch { return $null }
}

if ($Stop) {
    Write-Step 'Stopping backend (8000) and frontend (3000)'
    Stop-PortOwner 8000; Stop-PortOwner 3000
    Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
    Write-Host 'Stopped.'
    return
}

Write-Step 'Stopping stale servers'
Stop-PortOwner 8000
Stop-PortOwner 3000

$python = Get-Python
Write-Step "Starting backend with $python"
Start-Process -FilePath $python -ArgumentList '-m', 'uvicorn', 'main:app', '--host', '0.0.0.0', '--port', '8000' `
    -WorkingDirectory $backend -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $backendLogs 'backend-console.log') -RedirectStandardError (Join-Path $backendLogs 'backend-errors.log') | Out-Null
Wait-Url 'http://127.0.0.1:8000/health' 60 'Backend'

if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
    Write-Step 'Installing frontend dependencies (first run)'
    Push-Location $frontend; try { Invoke-Native { npm ci } } finally { Pop-Location }
}
Write-Step 'Starting frontend'
Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'npm run dev -- --port 3000 > logs\dev.log 2>&1' -WorkingDirectory $frontend -WindowStyle Hidden | Out-Null

$publicUrl = $null
if (-not $NoNgrok) {
    $publicUrl = Get-NgrokUrl
    if ($publicUrl) {
        Write-Step "Reusing ngrok tunnel $publicUrl"
    } elseif (Get-Command ngrok -ErrorAction SilentlyContinue) {
        Write-Step 'Starting ngrok'
        $ngrokArgs = @('http', '8000', '--log', (Join-Path $backendLogs 'ngrok.log'))
        $domain = Get-EnvValue 'NGROK_DOMAIN'
        if ($domain) { $ngrokArgs += @('--domain', $domain) }
        Start-Process -FilePath 'ngrok' -ArgumentList $ngrokArgs -WindowStyle Hidden | Out-Null
        $deadline = (Get-Date).AddSeconds(20)
        while (-not $publicUrl -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 700; $publicUrl = Get-NgrokUrl }
        if (-not $publicUrl) { Write-Warning 'ngrok did not report a public URL; calls will not reach this machine.' }
    } else {
        Write-Warning 'ngrok is not installed; calls from Twilio and Retell cannot reach this machine.'
    }
}

if ($publicUrl) {
    $syncScript = Join-Path $PSScriptRoot 'sync_public_url.py'
    if ($SyncProviderWebhooks) {
        Write-Step 'Pointing Twilio and Retell at the local public URL'
        Invoke-Native { & $python $syncScript $publicUrl } | Where-Object { $_ -notmatch 'MongoDB|Connecting' }
    } else {
        Write-Step 'Updating local public URL and local browser app only'
        Invoke-Native { & $python $syncScript $publicUrl --browser-only } | Where-Object { $_ -notmatch 'MongoDB|Connecting' }
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not update local browser calling. Check the Twilio app SIDs in backend .env.' }
}

try {
    $twilioTime = [DateTimeOffset]::Parse((Invoke-WebRequest https://api.twilio.com -Method Head -UseBasicParsing -TimeoutSec 5).Headers['Date'])
    $skew = [math]::Abs(([DateTimeOffset]::UtcNow - $twilioTime).TotalSeconds)
    if ($skew -gt 30) { Write-Warning ("This PC's clock is {0:N0}s off. Settings > Time & language > Date & time > Sync now." -f $skew) }
} catch { }

Wait-Url 'http://localhost:3000' 120 'Frontend'
Write-Host ''
Write-Host 'RelayDesk is running' -ForegroundColor Green
Write-Host '  Dashboard  http://localhost:3000'
Write-Host '  Backend    http://127.0.0.1:8000'
if ($publicUrl) { Write-Host "  Public     $publicUrl" }
Write-Host "  Logs       $(Join-Path $PSScriptRoot 'tail_logs.ps1')"
if (-not $NoBrowser) { Start-Process 'http://localhost:3000' }
