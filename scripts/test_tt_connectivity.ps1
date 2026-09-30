$ErrorActionPreference = 'Stop'

$targets = @(
    @{ Name = 'Order Routing'; HostName = 'fixorderrouting-ext-uat-cert.trade.tt'; Port = 11502 },
    @{ Name = 'Market Data'; HostName = 'fixmarketdata-ext-uat-cert.trade.tt'; Port = 11503 }
)

$failed = $false
foreach ($target in $targets) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($target.HostName, $target.Port)
        if (-not $task.Wait(15000)) {
            Write-Host "FAIL $($target.Name): connection timed out ($($target.HostName):$($target.Port))" -ForegroundColor Red
            $failed = $true
        } elseif ($client.Connected) {
            Write-Host "PASS $($target.Name): TCP connection opened ($($target.HostName):$($target.Port))" -ForegroundColor Green
        } else {
            Write-Host "FAIL $($target.Name): TCP connection did not open" -ForegroundColor Red
            $failed = $true
        }
    } catch {
        $reason = $_.Exception.GetBaseException().Message
        Write-Host "FAIL $($target.Name): $reason" -ForegroundColor Red
        if ($reason -match 'forbidden by its access permissions|10013') {
            Write-Host '  Windows or endpoint security blocked the socket before FIX Logon.' -ForegroundColor Yellow
            Write-Host '  Allow the project Python executable outbound TCP on ports 11502 and 11503.' -ForegroundColor Yellow
        }
        $failed = $true
    } finally {
        $client.Dispose()
    }
}

if ($failed) { exit 1 }
