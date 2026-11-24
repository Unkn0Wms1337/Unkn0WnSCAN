# Starts the Telegram bot. Avito is loaded through a real Chromium (Playwright),
# so Tor is no longer needed. Telegram connects through the local MTProto proxy.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-Port([int]$port) {
    return (Test-NetConnection -ComputerName 127.0.0.1 -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue)
}

# --- Telegram MTProto proxy (127.0.0.1:1443 by default) ---
$mtPort = 1443
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    $line = Select-String -Path $envFile -Pattern '^\s*TELEGRAM_MT_PROXY_PORT\s*=\s*(\d+)' |
        Select-Object -First 1
    if ($line -and $line.Matches.Count -gt 0) {
        $mtPort = [int]$line.Matches[0].Groups[1].Value
    }
}

if (Test-Port $mtPort) {
    Write-Host "MTProto proxy 127.0.0.1:$mtPort is listening."
} else {
    Write-Warning "MTProto proxy 127.0.0.1:$mtPort is NOT listening. Telegram will not connect."
}

# --- Bot process ---
$bot = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'bot\.py' }
if (-not $bot) {
    Write-Host "Starting bot..."
    $python = Join-Path $root ".venv\Scripts\python.exe"
    $proc = Start-Process -FilePath $python -ArgumentList 'bot.py' `
        -WorkingDirectory $root `
        -RedirectStandardOutput (Join-Path $root "bot.out.log") `
        -RedirectStandardError (Join-Path $root "bot.err.log") `
        -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $root "bot.pid") -Value $proc.Id -Encoding ascii
    Write-Host "Bot PID: $($proc.Id)"
} else {
    Write-Host "Bot is already running (PID $($bot[0].ProcessId))."
}
