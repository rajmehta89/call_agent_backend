<#
.SYNOPSIS
  Follow backend, Retell, frontend and ngrok logs in one window, coloured by level.
.EXAMPLE
  .\scripts\tail_logs.ps1 -Level WARN
  .\scripts\tail_logs.ps1 -Filter ogtest01     # one call, by the id shown in brackets
#>
[CmdletBinding()]
param(
    # Lines of history to show from each file before following.
    [int]$Tail = 20,
    # Only show lines at or above this level: DEBUG, INFO, WARN, ERROR.
    [ValidateSet('DEBUG', 'INFO', 'WARN', 'ERROR')]
    [string]$Level = 'DEBUG',
    # Optional text/regex filter, e.g. -Filter 'retell|/api/auth', or a call id like -Filter 4278336c.
    [string]$Filter
)

$backend = Split-Path -Parent $PSScriptRoot
$workspace = Split-Path -Parent $backend
# ngrok started by start_dev.ps1 logs next to the backend; an older manual one logged in the workspace folder.
$ngrokLog = Join-Path $backend 'logs\ngrok.log'
if (-not (Test-Path $ngrokLog)) { $ngrokLog = Join-Path $workspace 'logs\ngrok.log' }
$sources = [ordered]@{
    'backend'  = Join-Path $backend 'logs\app.log'
    'retell'   = Join-Path $backend 'logs\retell-debug.log'
    'frontend' = Join-Path $workspace 'call_agent_frontend\logs\dev.log'
    'ngrok'    = $ngrokLog
}
$colors = @{ backend = 'Cyan'; retell = 'Magenta'; frontend = 'Green'; ngrok = 'DarkYellow' }
$rank = @{ DEBUG = 0; INFO = 1; WARN = 2; ERROR = 3 }

function Get-LineLevel([string]$line) {
    # Backend lines carry their level after the timestamp: "2026-10-17 21:08:19 WARNING ..."
    if ($line -match '^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (DEBUG|INFO|WARNING|ERROR|CRITICAL)') {
        return @{ DEBUG = 'DEBUG'; INFO = 'INFO'; WARNING = 'WARN'; ERROR = 'ERROR'; CRITICAL = 'ERROR' }[$Matches[1]]
    }
    if ($line -match '(?i)\b(error|eror|crit|critical|exception|traceback|failed|fatal)\b|" [5]\d\d ') { return 'ERROR' }
    if ($line -match '(?i)\b(warn|warning)\b|" 4\d\d ') { return 'WARN' }
    if ($line -match '(?i)\b(debug|dbug)\b') { return 'DEBUG' }
    return 'INFO'
}

$jobs = foreach ($name in $sources.Keys) {
    $path = $sources[$name]
    if (!(Test-Path $path)) {
        Write-Host "[$name] not found yet: $path" -ForegroundColor DarkGray
        continue
    }
    Write-Host "[$name] following $path" -ForegroundColor $colors[$name]
    Start-Job -Name $name -ArgumentList $path, $Tail -ScriptBlock {
        param($p, $t)
        Get-Content -LiteralPath $p -Tail $t -Wait
    }
}

if (!$jobs) { Write-Host 'No log files found.' -ForegroundColor Red; return }
Write-Host "Showing level >= $Level. Press Ctrl+C to stop.`n" -ForegroundColor DarkGray

try {
    while ($true) {
        foreach ($job in $jobs) {
            foreach ($line in (Receive-Job $job)) {
                $text = "$line".TrimEnd()
                if (!$text) { continue }
                if ($Filter -and $text -notmatch $Filter) { continue }
                $lvl = Get-LineLevel $text
                if ($rank[$lvl] -lt $rank[$Level]) { continue }
                $lineColor = switch ($lvl) { 'ERROR' { 'Red' } 'WARN' { 'Yellow' } 'DEBUG' { 'DarkGray' } default { 'Gray' } }
                Write-Host ('[{0,-8}] ' -f $job.Name) -ForegroundColor $colors[$job.Name] -NoNewline
                Write-Host ('{0,-5} ' -f $lvl) -ForegroundColor $lineColor -NoNewline
                Write-Host $text -ForegroundColor $lineColor
            }
        }
        Start-Sleep -Milliseconds 300
    }
}
finally {
    $jobs | Stop-Job -PassThru | Remove-Job -Force
}
