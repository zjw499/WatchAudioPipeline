$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$logs = Join-Path $root "logs"
$python = Join-Path $root ".venv\Scripts\python.exe"
$tailscaleApp = "C:\Program Files\Tailscale\tailscale-ipn.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Pipeline Python environment was not found at $python"
}

New-Item -ItemType Directory -Path $logs -Force | Out-Null

if (-not (Get-Process -Name "tailscale-ipn" -ErrorAction SilentlyContinue) -and
    (Test-Path -LiteralPath $tailscaleApp)) {
    Start-Process -FilePath $tailscaleApp -WindowStyle Hidden
}

$hostLine = Get-Content (Join-Path $root ".env") |
    Where-Object { $_ -match '^\s*WATCH_AUDIO_HOST\s*=' } |
    Select-Object -First 1
$listenHost = if ($hostLine) { ($hostLine -split '=', 2)[1].Trim() } else { "127.0.0.1" }

if ($listenHost -like "100.*") {
    $deadline = (Get-Date).AddSeconds(90)
    do {
        $addressReady = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -eq $listenHost }
        if (-not $addressReady) {
            Start-Sleep -Seconds 2
        }
    } until ($addressReady -or (Get-Date) -ge $deadline)

    if (-not $addressReady) {
        throw "Tailscale address $listenHost was not ready after 90 seconds"
    }
}

function Test-PipelineProcess([string] $Mode) {
    return [bool](Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^python(?:w)?\.exe$' -and
            $_.CommandLine -and
            $_.CommandLine -match 'watch_audio_pipeline\.cli' -and
            $_.CommandLine -match "\s$Mode(?:\s|$)"
        } |
        Select-Object -First 1)
}

if (-not (Test-PipelineProcess "serve")) {
    Start-Process `
        -FilePath $python `
        -ArgumentList @("-m", "watch_audio_pipeline.cli", "serve") `
        -WorkingDirectory $root `
        -RedirectStandardOutput (Join-Path $logs "service-api.out.log") `
        -RedirectStandardError (Join-Path $logs "service-api.err.log") `
        -WindowStyle Hidden
}

if (-not (Test-PipelineProcess "worker")) {
    Start-Process `
        -FilePath $python `
        -ArgumentList @("-m", "watch_audio_pipeline.cli", "worker") `
        -WorkingDirectory $root `
        -RedirectStandardOutput (Join-Path $logs "service-worker.out.log") `
        -RedirectStandardError (Join-Path $logs "service-worker.err.log") `
        -WindowStyle Hidden
}
