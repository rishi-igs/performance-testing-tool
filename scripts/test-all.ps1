$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$logDir = Join-Path $env:TEMP "performance-tool-test-all"
$sampleOutLog = Join-Path $logDir "sample-app.out.log"
$sampleErrorLog = Join-Path $logDir "sample-app.error.log"
$dashboardOutLog = Join-Path $logDir "dashboard.out.log"
$dashboardErrorLog = Join-Path $logDir "dashboard.error.log"
$processes = @()

function Stop-StartedProcesses {
    foreach ($process in $processes) {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

function Wait-ForHttp {
    param(
        [string]$Url,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                return
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    } while ((Get-Date) -lt $deadline)

    throw "Timed out waiting for $Url"
}

function Assert-PortFree {
    param([int]$Port)

    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        throw "Port $Port is already in use. Stop the existing service and run npm run test again."
    }
}

try {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw "Python was not found on PATH."
    }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        throw "Node.js was not found on PATH; it is required for the dashboard syntax check."
    }
    if (-not (Test-Path (Join-Path $root "node_modules\@playwright\test"))) {
        throw "Playwright is not installed. Run npm install before npm run test."
    }

    $env:ALLOW_PRIVATE_TARGETS = "true"
    $env:PERF_DATA_DIR = Join-Path $root "data"
    if (-not $env:JMETER_BIN) {
        $defaultJmeter = "C:\apache-jmeter-5.6.3\bin\jmeter.bat"
        if (Test-Path $defaultJmeter) {
            $env:JMETER_BIN = $defaultJmeter
        }
    }

    Assert-PortFree 9000
    Assert-PortFree 8000

    Write-Host "Starting local sample target on http://127.0.0.1:9000..."
    $sample = Start-Process -FilePath "python" `
        -ArgumentList "-m", "uvicorn", "tests.sample_app:app", "--host", "127.0.0.1", "--port", "9000", "--log-level", "warning" `
        -WorkingDirectory $backend -RedirectStandardOutput $sampleOutLog -RedirectStandardError $sampleErrorLog -PassThru
    $processes += $sample

    Write-Host "Starting dashboard on http://127.0.0.1:8000..."
    $dashboard = Start-Process -FilePath "python" `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--log-level", "warning" `
        -WorkingDirectory $backend -RedirectStandardOutput $dashboardOutLog -RedirectStandardError $dashboardErrorLog -PassThru
    $processes += $dashboard

    Wait-ForHttp "http://127.0.0.1:9000/fixed"
    Wait-ForHttp "http://127.0.0.1:8000/health"
    $rootResponse = Invoke-WebRequest -Uri "http://127.0.0.1:8000/" -UseBasicParsing
    if ($rootResponse.StatusCode -ne 200 -or $rootResponse.Content -notmatch "Load test console") {
        throw "Dashboard root page smoke check failed."
    }
    Write-Host "Service smoke checks passed."

    Write-Host "Compiling Python sources..."
    Push-Location $backend
    try {
        python -m compileall -q app tests
        if ($LASTEXITCODE -ne 0) {
            throw "Python compilation failed."
        }
        Write-Host "Running backend tests..."
        python -m pytest -q
        if ($LASTEXITCODE -ne 0) {
            throw "Backend tests failed."
        }
    } finally {
        Pop-Location
    }

    Write-Host "Checking dashboard JavaScript syntax..."
    $dashboardScript = Join-Path $env:TEMP "performance-tool-dashboard-check-$PID.js"
    try {
        $html = Get-Content -Raw (Join-Path $frontend "index.html")
        $match = [regex]::Match($html, "<script>([\s\S]*?)</script>")
        if (-not $match.Success) {
            throw "Dashboard script block was not found."
        }
        [IO.File]::WriteAllText($dashboardScript, $match.Groups[1].Value)
        node --check $dashboardScript
        if ($LASTEXITCODE -ne 0) {
            throw "Dashboard JavaScript syntax check failed."
        }
    } finally {
        if (Test-Path $dashboardScript) {
            Remove-Item $dashboardScript -Force
        }
    }

    Write-Host "Running Playwright browser tests..."
    Push-Location $root
    try {
        npx.cmd playwright test
        if ($LASTEXITCODE -ne 0) {
            throw "Playwright browser tests failed."
        }
    } finally {
        Pop-Location
    }

    Write-Host ""
    Write-Host "ALL CHECKS PASSED" -ForegroundColor Green
    Write-Host "Dashboard: http://127.0.0.1:8000"
} finally {
    Stop-StartedProcesses
    Write-Host "Stopped test services. Logs: $logDir"
}
