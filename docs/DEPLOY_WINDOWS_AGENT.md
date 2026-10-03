# Agent runbook: deploy the Scalping Terminal locally on Windows 10

**Audience:** an autonomous AI agent (for example a Nous Hermes agent) with a PowerShell tool on
the user's Windows 10 PC. **Goal:** the terminal runs at <http://127.0.0.1:8000>, streams live
Binance data and restarts at logon. Paper trading only.

Follow the steps in order. Every step has a **check**. Do not move on until the check passes. If a
check fails, use the **Troubleshooting** table, and if that doesn't fix it, stop and report to the
user (see "Reporting").

---

## 0. Rules (read first, never break)

1. **Paper trading only.** Never change `execution.mode` and never set `BINANCE_API_KEY` /
   `BINANCE_API_SECRET`. In this version all orders are simulated.
2. **Localhost only.** Keep the server bound to `127.0.0.1`. Do not open firewall ports, port-forward
   or set the host to `0.0.0.0`.
3. **No geo-restriction workarounds.** If Binance answers HTTP 451 or "restricted location", stop
   and tell the user. Do not use VPNs, proxies or alternative endpoints to get around it.
4. **Secrets:** put user-provided tokens (Telegram, Discord, CryptoPanic) only in `.env` in the
   repo folder. Never print them back, commit them or paste them into logs.
5. **Ask before installing software** (Git, Python, Node) if the user has not already approved it.
   Never uninstall or upgrade other software.
6. **Don't modify repository code.** Only create `config\local.yaml`, `.env` and the scripts named
   below. Do not `git push`.
7. Run every command in **Windows PowerShell 5.1 or PowerShell 7**, not cmd.exe. Prefer full paths
   to the venv's executables over "activating" the venv.

---

## 1. Check the machine

```powershell
[System.Environment]::OSVersion.Version          # need 10.0.17763 (1809) or newer for winget
(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB   # want >= 4 (8 recommended)
Get-PSDrive C | Select-Object Free                # want >= 3 GB free
winget --version                                  # App Installer; if missing see Troubleshooting T1
```

**Check:** Windows build ≥ 17763, RAM ≥ 4 GB, ≥ 3 GB free on the install drive.

## 2. Install prerequisites (skip any that are already present)

```powershell
git --version;  py -3.12 --version;  node --version     # what's already installed?

winget install -e --id Git.Git              --accept-package-agreements --accept-source-agreements
winget install -e --id Python.Python.3.12   --accept-package-agreements --accept-source-agreements
winget install -e --id OpenJS.NodeJS.LTS    --accept-package-agreements --accept-source-agreements

# Refresh PATH in this session so the new tools are found without reopening PowerShell
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
```

**Check:**
```powershell
git --version          # any
py -3.12 --version     # Python 3.12.x  (3.11 also works: use -3.11 everywhere below)
node --version         # v20 or newer
npm --version
```

## 3. Get the code

Install into `%USERPROFILE%\Scalping`. If that folder already exists, update it instead of cloning.

```powershell
$Repo = "$env:USERPROFILE\Scalping"
if (Test-Path "$Repo\.git") {
    git -C $Repo fetch origin ccr-3af9c995-hrvmcx
    git -C $Repo checkout ccr-3af9c995-hrvmcx
    git -C $Repo pull --ff-only origin ccr-3af9c995-hrvmcx
} else {
    git clone https://github.com/ikhanai777/Scalping.git $Repo
    git -C $Repo checkout ccr-3af9c995-hrvmcx
}
Set-Location $Repo
```

> If the work has since been merged, use the repository's default branch instead of
> `ccr-3af9c995-hrvmcx`. If cloning asks for credentials, the repository is private: ask the user to
> sign in to GitHub (Git Credential Manager pops up) or to give you access.

**Check:** `Test-Path "$Repo\backend\pyproject.toml"` and `Test-Path "$Repo\frontend\package.json"` both return `True`.

## 4. Python environment and backend

```powershell
Set-Location $Repo
py -3.12 -m venv .venv
$Py = "$Repo\.venv\Scripts\python.exe"
& $Py -m pip install --upgrade pip
& $Py -m pip install -e ".\backend[ml]"
```

**Check:**
```powershell
& $Py -c "import scalper, lightgbm, fastapi; print('backend ok', scalper.__version__)"
& "$Repo\.venv\Scripts\scalper.exe" --help
```

> Optional tests: `& $Py -m pip install -e ".\backend[dev]"` then
> `& $Py -m pytest "$Repo\backend\tests" -q`. If `TA-Lib` fails to install on this machine, skip
> it: the indicator-conformance tests skip themselves and the other tests still run.

## 5. Build the web UI

```powershell
Set-Location "$Repo\frontend"
npm ci
npm run build
Set-Location $Repo
```

**Check:** `Test-Path "$Repo\frontend\dist\index.html"` returns `True`.

## 6. Configuration (optional but recommended)

Create `config\local.yaml` only for the settings you change. It overrides `config\default.yaml`.
A moderate start for an average PC:

```powershell
@"
universe:
  trend_universe_size: 30      # pairs with full trend tracking (default 60; lower = lighter)
  radar_max_symbols: 250       # pairs scanned by the pump/dump radar each second
server:
  host: 127.0.0.1
  port: 8000
"@ | Set-Content -Encoding UTF8 "$Repo\config\local.yaml"
```

Alerts and news keys (only if the user gave them). Write `.env` in the repo root:

```powershell
@"
SCALPER_ALERTS__TELEGRAM_TOKEN=<token from user>
SCALPER_ALERTS__TELEGRAM_CHAT_ID=<chat id from user>
SCALPER_NEWS__CRYPTOPANIC_TOKEN=<optional>
"@ | Set-Content -Encoding UTF8 "$Repo\.env"
```

`.env` is read by Docker only. For the native run, the start script in step 7 loads it into the
process environment.

Optional: to start with the 60-day futures statistics instead of the automatic 7-day spot bootstrap,
copy them and switch the market to futures. Only do this if the user asked.

```powershell
New-Item -ItemType Directory -Force "$Repo\data" | Out-Null
Copy-Item "$Repo\results\stats-futures-60d.json" "$Repo\data\stats.json"
Add-Content "$Repo\config\local.yaml" "execution:`n  market: futures"
```

## 7. Create start/stop scripts

```powershell
New-Item -ItemType Directory -Force "$Repo\scripts\windows", "$Repo\data\logs" | Out-Null

@'
# Starts the Scalping Terminal in the background and writes logs to data\logs.
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $Repo
$env:PYTHONUTF8 = "1"
if (Test-Path "$Repo\.env") {
    Get-Content "$Repo\.env" | Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*=' } | ForEach-Object {
        $k, $v = $_ -split '=', 2
        if ($v.Trim()) { [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process") }
    }
}
$running = Get-CimInstance Win32_Process -Filter "Name='scalper.exe'" -ErrorAction SilentlyContinue
if ($running) { Write-Output "already running (pid $($running.ProcessId -join ','))"; exit 0 }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Start-Process -FilePath "$Repo\.venv\Scripts\scalper.exe" -ArgumentList "serve" -WorkingDirectory $Repo `
    -WindowStyle Hidden -RedirectStandardOutput "$Repo\data\logs\out-$stamp.log" `
    -RedirectStandardError "$Repo\data\logs\err-$stamp.log"
Write-Output "started; open http://127.0.0.1:8000"
'@ | Set-Content -Encoding UTF8 "$Repo\scripts\windows\start.ps1"

@'
# Stops the Scalping Terminal (the server and its worker processes).
Get-CimInstance Win32_Process -Filter "Name='scalper.exe'" -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$repo*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Output "stopped"
'@ | Set-Content -Encoding UTF8 "$Repo\scripts\windows\stop.ps1"
```

## 8. First start and verification

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$Repo\scripts\windows\start.ps1"

# Wait up to 5 minutes for the engine to finish backfilling and go live
$deadline = (Get-Date).AddMinutes(5)
do {
    Start-Sleep -Seconds 5
    try { $s = Invoke-RestMethod http://127.0.0.1:8000/api/status -TimeoutSec 5 } catch { $s = $null }
    if ($s) { Write-Output "phase=$($s.phase) streams=$($s.ws.streams) connected=$($s.ws.connected)/$($s.ws.connections)" }
} until (($s -and ($s.phase -eq "live" -or $s.phase -eq "error")) -or (Get-Date) -gt $deadline)
```

**Checks (all must pass):**

| Check | Command | Expected |
|---|---|---|
| Engine live | `(Invoke-RestMethod http://127.0.0.1:8000/api/status).phase` | `live` |
| Streams connected | `(Invoke-RestMethod http://127.0.0.1:8000/api/status).ws` | `connected` = `connections`, `streams` > 20, `messages` increasing over 30 s |
| Market loaded | `(Invoke-RestMethod http://127.0.0.1:8000/api/watchlist).Count` | > 100 |
| Chart data | `(Invoke-RestMethod "http://127.0.0.1:8000/api/chart/BTCUSDT?tf=5m").bars.Count` | > 100 |
| UI served | `(Invoke-WebRequest http://127.0.0.1:8000/ -UseBasicParsing).StatusCode` | `200` |
| News | `(Invoke-RestMethod http://127.0.0.1:8000/api/status).news_health` | most sources `ok: True` |

Expected but **not** failures:
- `futures_data: False` when Binance futures is blocked in the user's region. Spot features keep working.
- `bootstrap.status = running` for a few minutes after the first start. It is computing strategy
  statistics in a worker process.
- Signals marked **unvalidated** until a strategy has enough measured results.

Then open the UI for the user: `Start-Process "http://127.0.0.1:8000"`.

## 9. Start automatically at logon (optional, ask the user)

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
           -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Repo\scripts\windows\start.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "ScalpingTerminal" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Starts the Scalping Terminal (paper trading) at logon" -Force
```

**Check:** `Get-ScheduledTask -TaskName ScalpingTerminal` shows State `Ready`.
To remove it: `Unregister-ScheduledTask -TaskName ScalpingTerminal -Confirm:$false`.

## 10. Daily operations

| Task | Command |
|---|---|
| Start | `powershell -ExecutionPolicy Bypass -File "$Repo\scripts\windows\start.ps1"` |
| Stop | `powershell -ExecutionPolicy Bypass -File "$Repo\scripts\windows\stop.ps1"` |
| Status | `Invoke-RestMethod http://127.0.0.1:8000/api/status` |
| Latest error log | `Get-ChildItem "$Repo\data\logs\err-*.log" \| Sort-Object LastWriteTime \| Select-Object -Last 1 \| Get-Content -Tail 50` |
| Update | stop → `git -C $Repo pull --ff-only` → `& $Py -m pip install -e ".\backend[ml]"` → `cd frontend; npm ci; npm run build` → start |
| Backtest (saves stats for live use) | `& "$Repo\.venv\Scripts\scalper.exe" backtest --days 60 --save-stats` (add `--market futures` if configured) |
| Trend Catcher check | `& "$Repo\.venv\Scripts\scalper.exe" evaluate-trends --days 30` |
| Train ML filter | `& "$Repo\.venv\Scripts\scalper.exe" train --days 90` |

Run CLI commands with `$env:PYTHONUTF8 = "1"` set in the session. Stop the server before a backtest
with `--save-stats`, or restart it afterwards, so the live engine picks up the new statistics.

## Troubleshooting

| # | Symptom | Fix |
|---|---|---|
| T1 | `winget` not found | Ask the user to install "App Installer" from the Microsoft Store, or download installers manually: Git <https://git-scm.com/download/win>, Python 3.12 <https://www.python.org/downloads/windows/> (tick "Add python.exe to PATH" and "py launcher"), Node.js LTS <https://nodejs.org/>. Then refresh PATH (step 2). |
| T2 | `py` not found but `python` works | Use `python -m venv .venv` in step 4; check `python --version` is ≥ 3.11. |
| T3 | `running scripts is disabled on this system` | Always launch scripts with `powershell -ExecutionPolicy Bypass -File ...` (as shown). Do not change the machine-wide policy. |
| T4 | pip error building `lightgbm` / `pyarrow` / `numpy` | Make sure you are on 64-bit Python 3.11 or 3.12 (`& $Py -c "import struct; print(struct.calcsize('P')*8)"` → 64). These packages have prebuilt Windows wheels for those versions; very new Python versions may not yet. |
| T5 | `npm ci` fails | Run `npm install` instead, then `npm run build`. Check `node --version` ≥ 20. |
| T6 | Status stays `universe`/`error`, err log shows **451** or "restricted location" | Binance is not available from this network/region. **Stop and report to the user (rule 3).** |
| T7 | Port 8000 in use | `Get-NetTCPConnection -LocalPort 8000 \| Select OwningProcess`. If it isn't the terminal, set `server: {port: 8010}` in `config\local.yaml` and use that port. |
| T8 | Windows Firewall prompt for Python | The app only listens on 127.0.0.1. "Cancel"/deny is fine. Do not allow public networks. |
| T9 | `UnicodeEncodeError` in CLI output | Set `$env:PYTHONUTF8 = "1"` in the session (the start script already does). |
| T10 | High CPU / slow UI | Lower `universe.trend_universe_size` (e.g. 20) and `universe.radar_max_symbols` (e.g. 120) in `config\local.yaml`, then restart. |
| T11 | Antivirus quarantines `scalper.exe` | It is the standard pip console-script launcher in `.venv\Scripts`. Ask the user whether to allow it; alternatively start with `"$Repo\.venv\Scripts\python.exe" -m scalper serve`. |
| T12 | Two servers / duplicate windows | Run `stop.ps1`, then `start.ps1` once. The start script refuses to start a second copy. |

## Reporting back to the user

When done, report:
1. The URL (`http://127.0.0.1:8000`) and whether it opened.
2. The values from the step 8 check table, including `futures_data` and any `news_health` sources that failed.
3. Whether logon autostart was set up.
4. Anything you skipped or could not do, with the exact error line from `data\logs\err-*.log`.

Remind the user: signals are probabilistic, everything is paper trading, and the measured strategy
results so far are in `docs\IMPLEMENTATION.md` (no strategy has yet shown a positive expectancy after fees).
