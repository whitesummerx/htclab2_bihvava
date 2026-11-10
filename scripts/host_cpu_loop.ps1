# Samples host CPU every ~1 s in typeperf CSV format: "MM/dd/yyyy HH:mm:ss.fff","total","core0","core1",...
# Does NOT use performance counters (they are broken on some machines): reads per-core idle/kernel/user times
# directly from the kernel (NtQuerySystemInformation, class 8). Utilization = 100 * (1 - d(idle) / d(kernel + user)).
# Total = mean over all logical cores. Started by run-series.ps1.
param([string]$Out, [string]$Cores = "0,1,2,3")
$ErrorActionPreference = "Continue"
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CpuTimes {
    [DllImport("ntdll.dll")]
    static extern int NtQuerySystemInformation(int cls, IntPtr buf, int len, out int ret);
    public static long[][] Read() {
        int n = Environment.ProcessorCount;
        int sz = 48;
        IntPtr p = Marshal.AllocHGlobal(n * sz);
        try {
            int ret;
            int st = NtQuerySystemInformation(8, p, n * sz, out ret);
            if (st != 0) { throw new Exception("NtQuerySystemInformation status " + st); }
            long[][] r = new long[n][];
            for (int i = 0; i < n; i++) {
                r[i] = new long[] { Marshal.ReadInt64(p, i * sz), Marshal.ReadInt64(p, i * sz + 8), Marshal.ReadInt64(p, i * sz + 16) };
            }
            return r;
        } finally { Marshal.FreeHGlobal(p); }
    }
}
'@
$inv = [Globalization.CultureInfo]::InvariantCulture
$ids = $Cores.Split(",")
$prev = $null
while ($true) {
    $t0 = Get-Date
    $cur = [CpuTimes]::Read()
    if ($prev -ne $null) {
        $n = $cur.Length
        $u = New-Object double[] $n
        for ($i = 0; $i -lt $n; $i++) {
            $dIdle = [double]($cur[$i][0] - $prev[$i][0])
            $dTot = [double](($cur[$i][1] - $prev[$i][1]) + ($cur[$i][2] - $prev[$i][2]))
            if ($dTot -gt 0) { $u[$i] = [math]::Min(100.0, [math]::Max(0.0, 100.0 * (1.0 - $dIdle / $dTot))) }
        }
        $total = ($u | Measure-Object -Average).Average
        $line = '"' + (Get-Date).ToString("MM/dd/yyyy HH:mm:ss.fff", $inv) + '","' + ([double]$total).ToString("F3", $inv) + '"'
        foreach ($c in $ids) { $line += ',"' + $u[[int]$c].ToString("F3", $inv) + '"' }
        Add-Content -Path $Out -Value $line -Encoding ASCII
    }
    $prev = $cur
    $left = 1000 - ((Get-Date) - $t0).TotalMilliseconds
    if ($left -gt 0) { Start-Sleep -Milliseconds ([int]$left) }
}
