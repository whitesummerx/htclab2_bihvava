# Lab 2: series of runs on 1..4 instances (and G) with the generator pinned to its own cores.
# Run from the repo root, in PowerShell as Administrator (needed to pin the Docker VM to cores):
#   .\scripts\run-series.ps1                       # everything: G, N=1..4
#   .\scripts\run-series.ps1 -Series 4 -SkipNoop   # N=4 only (docker stats and typeperf are taken during it)
# Default cores assume 6 logical cores: stand 0-3, generator 4-5.
param(
    [int[]]$Series = @(1, 2, 3, 4),
    [switch]$SkipNoop,
    [string]$GenCpus = "4,5",
    [int]$GenProcs = 2,
    [string]$VmCpus = "0,1,2,3",
    [string]$Py = "C:\Users\racal\PycharmProjects\hpc_lab2\.venv\Scripts\python.exe"
)
$ErrorActionPreference = "Continue"
$inv = [Globalization.CultureInfo]::InvariantCulture
New-Item -ItemType Directory -Force results | Out-Null

function Set-VmAffinity($cpus) {
    if (-not $cpus) { return }
    $mask = [int64]0
    foreach ($c in $cpus.Split(",")) { $mask = $mask + ([int64]1 -shl [int]$c) }
    $found = $false
    foreach ($p in (Get-Process | Where-Object { $_.ProcessName -like "vmmem*" })) {
        try { $p.ProcessorAffinity = [IntPtr]$mask; $found = $true; Write-Host ("Affinity of " + $p.ProcessName + " set to cores " + $cpus) }
        catch { Write-Host ("Could not set affinity of " + $p.ProcessName + " (run as Administrator): " + $_.Exception.Message) }
    }
    if (-not $found) { Write-Host "vmmem process not found: Docker VM is not pinned (generator is still pinned to its own cores)." }
}

function Wait-Stack {
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8080/appointments?limit=1" -TimeoutSec 3
            if ($r.StatusCode -eq 200) { return }
        } catch { }
        Start-Sleep -Seconds 2
    }
    Write-Host "Stack did not become ready in 2 minutes."
}

function Start-Stack($n) {
    docker compose up -d --scale web=$n
    Start-Sleep -Seconds 6
    docker compose restart lb
    Wait-Stack
    Start-Sleep -Seconds 15      # Prometheus must see all N targets (dns_sd, refresh 5 s)
}

Set-VmAffinity $VmCpus
$gArg = "5999.8"

if (-not $SkipNoop) {
    Start-Stack 1
    & $Py load_generator\step_generator.py run --target noop --instances 0 --cpus $GenCpus --procs $GenProcs --force
    $g = (Get-Content results\step_noop.json -Raw | ConvertFrom-Json).summary.X
    $gArg = $g.ToString($inv)
    Write-Host ("G = " + $gArg)
} elseif (Test-Path results\step_noop.json) {
    $g = (Get-Content results\step_noop.json -Raw | ConvertFrom-Json).summary.X
    $gArg = $g.ToString($inv)
}

foreach ($n in $Series) {
    Write-Host ("===== N = " + $n + " =====")
    Start-Stack $n
    $tp = $null; $ds = $null
    if ($n -eq 4) {
        $repo = (Get-Location).Path
        $hostOut = Join-Path $repo "results\host_cpu_4web.txt"
        $dsOut = Join-Path $repo "results\docker_stats_4web.txt"
        Remove-Item $hostOut, $dsOut -ErrorAction SilentlyContinue
        # host CPU sampler (total + every stand core) and docker stats start BEFORE the generator and run for the whole N=4 run
        $tp = Start-Process -FilePath powershell -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $repo "scripts\host_cpu_loop.ps1") + '" -Out "' + $hostOut + '" -Cores "' + $VmCpus + '"') -WorkingDirectory $repo -PassThru -WindowStyle Hidden
        $ds = Start-Process -FilePath powershell -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $repo "scripts\docker_stats_loop.ps1") + '" -Out "' + $dsOut + '"') -WorkingDirectory $repo -PassThru -WindowStyle Hidden
        Start-Sleep -Seconds 6
        if (-not (Test-Path $hostOut)) { Write-Host "WARNING: results\host_cpu_4web.txt was not created by scripts\host_cpu_loop.ps1" }
    }
    & $Py load_generator\step_generator.py run --instances $n --g-limit $gArg --cpus $GenCpus --procs $GenProcs --force
    & $Py load_generator\collect_prometheus.py --label ("step_" + $n + "web")
    Start-Sleep -Seconds 3
    if ($tp) { Stop-Process -Id $tp.Id -Force -ErrorAction SilentlyContinue }
    if ($ds) { Stop-Process -Id $ds.Id -Force -ErrorAction SilentlyContinue }
}
Write-Host "Done. Next: python analysis\lab2_analysis.py"
