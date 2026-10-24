# Offline desk against the mock gateway, native Windows: no credentials, no real orders. Ctrl+C stops both.
# Usage: powershell -ExecutionPolicy Bypass -File run_dev.ps1   then open http://127.0.0.1:8613/?ai=1
# Other ports (run two at once): $env:CHART_PORT=8623; $env:DEV_GATEWAY_PORT=8710; .\run_dev.ps1
# Freeze the mock market (deterministic demos): $env:DEV_GATEWAY_WALK=0; .\run_dev.ps1
# Python: uses .\.venv if present, else python. Override with $env:PYTHON. The same steps as run_dev.sh.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$GWP = if ($env:DEV_GATEWAY_PORT) { $env:DEV_GATEWAY_PORT } else { "8700" }
$APPP = if ($env:CHART_PORT) { $env:CHART_PORT } else { "8613" }
if ($env:PYTHON) { $PY = $env:PYTHON } elseif (Test-Path .venv\Scripts\python.exe) { $PY = (Resolve-Path .venv\Scripts\python.exe).Path } else { $PY = "python" }
$D = Join-Path ([IO.Path]::GetTempPath()) ("projectx-desk-" + [IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $D | Out-Null

function Test-Up($url) {
    try { Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}
# A venv python.exe is a launcher that starts the real interpreter as a child: kill the whole tree, not just the launcher.
function Stop-Tree($p) {
    if ($p -and -not $p.HasExited) { & taskkill.exe /T /F /PID $p.Id 2>&1 | Out-Null }
}

# Dot-run in an open window, $env: changes would outlive the script (PX_TRADING=1 would then reach a real desk started
# from the same window, since shell variables beat .env.projectx). Save everything and put it back on the way out.
$savedEnv = @{}
Get-ChildItem Env: | ForEach-Object { $savedEnv[$_.Name] = $_.Value }
$GW = $null; $APP = $null
try {
    $env:DEV_GATEWAY_PORT = $GWP
    $GW = Start-Process -FilePath $PY -ArgumentList "-m", "projectx_desk.dev_gateway" -NoNewWindow -PassThru `
        -RedirectStandardOutput "$D\gw.log" -RedirectStandardError "$D\gw.err.log"
    for ($i = 0; $i -lt 60; $i++) { if (Test-Up "http://127.0.0.1:$GWP/dev/state") { break }; Start-Sleep -Milliseconds 250 }

    # MCP: mounts only with a strong token. For the mock we make a throwaway one (kept in a file, not printed).
    if (-not $env:PX_MCP_TOKEN) {
        $env:PX_MCP_TOKEN = (& $PY -c "import secrets; print(secrets.token_hex(32))").Trim()
        [IO.File]::WriteAllText("$D\mcp.token", $env:PX_MCP_TOKEN)
    }
    # The mock must never inherit the real desk's settings: no .env.projectx, no advisory feeds, its own tape file.
    foreach ($k in "PX_FEED_URL", "PX_FEED_KEY", "PX_GEX_KEY", "PX_GEX_URL", "TD_API_KEY", "PX_MLL_FLOOR", "PX_FLAT_BY") {
        Remove-Item "Env:$k" -ErrorAction SilentlyContinue
    }
    $vars = @{
        PX_WEEKEND_QUIET = "0"; PX_NO_ENV_FILE = "1"; PX_FLOW_FILE = "$D\flow.json"; PX_LOCK_FILE = "$D\desk.lock"
        PX_GRADES_FILE = "$D\grades.jsonl"
        PROJECTX_API = "http://127.0.0.1:$GWP"; PROJECTX_HUB_BASE = "ws://127.0.0.1:$GWP/hubs"
        PROJECTX_USERNAME = "dev"; PROJECTX_API_KEY = "dev"; PX_ACCOUNT_ID = "4242"; PX_ACCOUNT_KIND = "sim"
        PX_BRACKET_MODE = "position"; PX_DAILY_LOSS_LIMIT = "1000"; PX_DRAWDOWN_BUFFER = "2000"; PX_NO_NEW_AFTER = "16:55"
        PX_TRADING = "1"; CHART_PORT = $APPP
        PX_STATE_PATH = "$D\day.json"; PX_INTENT_PATH = "$D\intents.json"; PX_AUDIT_PATH = "$D\audit.jsonl"; PX_HALT_FILE = "$D\HALT"
    }
    foreach ($k in $vars.Keys) { Set-Item "Env:$k" $vars[$k] }

    $APP = Start-Process -FilePath $PY -ArgumentList "-m", "projectx_desk" -NoNewWindow -PassThru `
        -RedirectStandardOutput "$D\app.log" -RedirectStandardError "$D\app.err.log"
    for ($i = 0; $i -lt 80; $i++) {
        if (Test-Up "http://127.0.0.1:$APPP/healthz") { break }
        if ($APP.HasExited) { break }
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-Up "http://127.0.0.1:$APPP/healthz")) {
        Write-Host "The desk did not start. Last lines of its log ($D\app.err.log):" -ForegroundColor Red
        Get-Content "$D\app.err.log", "$D\app.log" -Tail 15 -ErrorAction SilentlyContinue
        exit 1
    }
    Write-Host "desk: http://127.0.0.1:$APPP/?ai=1   (mock gateway, SIM account; PX_TRADING=1 applies to the mock only)"
    Write-Host "AI digest:  curl.exe 'http://127.0.0.1:$APPP/ai/digest?contract=CON.F.US.MES.Z26&risk_pct=0.05'"
    if (Test-Path "$D\mcp.token") {
        Write-Host "MCP:        claude mcp add --transport http projectx http://127.0.0.1:$APPP/mcp/ --header `"Authorization: Bearer `$(Get-Content $D\mcp.token)`""
    } else {
        Write-Host "MCP:        mounted at http://127.0.0.1:$APPP/mcp/ only if your PX_MCP_TOKEN is strong (32+ characters, no placeholder words); see $D\app.log"
    }
    Write-Host "logs: $D"
    Write-Host "Ctrl+C stops the desk and the mock gateway."
    while (-not $APP.HasExited) { Start-Sleep -Milliseconds 500 }
}
finally {
    Stop-Tree $APP
    Stop-Tree $GW
    Get-ChildItem Env: | Where-Object { -not $savedEnv.ContainsKey($_.Name) } | ForEach-Object { Remove-Item "Env:$($_.Name)" }
    foreach ($k in $savedEnv.Keys) { Set-Item "Env:$k" $savedEnv[$k] }
}
