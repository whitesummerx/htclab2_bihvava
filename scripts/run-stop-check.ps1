# Lab 2, step 3: graceful shutdown check under load.
# 2 web instances, 400 req/s (one 60 s step), docker stop of one instance ~15 s into the step.
# Output: results/stop_check.json, results/stop_check_meta.json, results/stop_check_exitcode.txt
# Then: python analysis\stop_check.py
param(
    [string]$GenCpus = "4,5",
    [int]$GenProcs = 2,
    [string]$Py = "C:\Users\racal\PycharmProjects\hpc_lab2\.venv\Scripts\python.exe"
)
$ErrorActionPreference = "Continue"
$inv = [Globalization.CultureInfo]::InvariantCulture
New-Item -ItemType Directory -Force results | Out-Null
docker compose up -d --scale web=2
Start-Sleep -Seconds 5
docker compose restart lb
for ($i = 0; $i -lt 60; $i++) {
    try { if ((Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8080/appointments?limit=1" -TimeoutSec 3).StatusCode -eq 200) { break } } catch { }
    Start-Sleep -Seconds 2
}
Start-Sleep -Seconds 10
$ids = @(docker compose ps -q web)
$cid = $ids[1]
$cname = (docker inspect --format '{{.Name}}' $cid).TrimStart("/")
$argsList = "-u load_generator\step_generator.py run --instances 2 --label stop_check --start-rps 400 --step-rps 200 --max-rps 400 --step-seconds 60 --warmup 10 --cpus $GenCpus --procs $GenProcs --force"
$gen = Start-Process -FilePath $Py -ArgumentList $argsList -PassThru -NoNewWindow -RedirectStandardOutput results\stop_check_generator.log
# generator start ~ 3 + 0.5*procs + a few seconds of startup; warmup 10 s; stop ~15 s into the step
Start-Sleep -Seconds ([int](3 + 0.5 * $GenProcs + 3 + 10 + 15))
$t1 = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
docker stop $cid | Out-Null
$t2 = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
$exitCode = (docker inspect --format '{{.State.ExitCode}}' $cid)
$line = "docker inspect --format '{{.State.ExitCode}}' " + $cname + "  ->  " + $exitCode
Write-Host $line
Set-Content -Path results\stop_check_exitcode.txt -Value $line -Encoding UTF8
$meta = '{ "container": "' + $cname + '", "exit_code": "' + $exitCode + '", "stop_begin_epoch": "' + $t1.ToString("F3", $inv) + '", "stop_end_epoch": "' + $t2.ToString("F3", $inv) + '" }'
Set-Content -Path results\stop_check_meta.json -Value $meta -Encoding UTF8
$gen.WaitForExit()
Write-Host "Done. Next: python analysis\stop_check.py"
