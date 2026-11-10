# Samples docker stats every ~2 s into a file (UTC timestamp line, then one line per container). Started by run-series.ps1.
param([string]$Out)
$ErrorActionPreference = "Continue"
while ($true) {
    $lines = docker stats --no-stream --format "{{.Name}}  {{.CPUPerc}}  {{.MemUsage}}"
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fff") + "+00:00"
    Add-Content -Path $Out -Value $ts -Encoding UTF8
    foreach ($l in $lines) { Add-Content -Path $Out -Value $l -Encoding UTF8 }
}
