$ErrorActionPreference = "Stop"

$serviceDir = Join-Path $PSScriptRoot "local_whatsapp"
Set-Location $serviceDir

if (-not (Test-Path "node_modules\whatsapp-web.js")) {
    Write-Host "Installing local WhatsApp Web service dependencies..."
    npm install
}

Write-Host "Starting local WhatsApp Web bridge"
Write-Host "Scan the QR code shown in this terminal."
npm start
