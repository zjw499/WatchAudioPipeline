param(
    [switch] $CoreOnly,
    [switch] $GeminiOnly,
    [switch] $SkipUpdate
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$logs = Join-Path $root "logs"
$python = Join-Path $root ".venv\Scripts\python.exe"
$runtimeEntry = Join-Path $root "scripts\runtime_entry.py"
$runtime = Join-Path $root ".runtime"
$activePointer = Join-Path $runtime "active-server-path.txt"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Pipeline Python environment was not found at $python"
}
if (-not (Test-Path -LiteralPath $runtimeEntry)) {
    throw "Pipeline runtime entry was not found at $runtimeEntry"
}

New-Item -ItemType Directory -Path $logs -Force | Out-Null

if ($CoreOnly -and $GeminiOnly) {
    throw "CoreOnly and GeminiOnly cannot be used together"
}

if (-not $SkipUpdate -and -not $GeminiOnly) {
    & (Join-Path $PSScriptRoot "update_server.ps1")
}

$serverRoot = $root
$serverVersion = "working-tree"
if (Test-Path -LiteralPath $activePointer) {
    $selected = (Get-Content -LiteralPath $activePointer -Raw).Trim()
    $resolvedSelected = [System.IO.Path]::GetFullPath($selected)
    $resolvedReleases = [System.IO.Path]::GetFullPath((Join-Path $runtime "releases")) +
        [System.IO.Path]::DirectorySeparatorChar
    if ($resolvedSelected.StartsWith($resolvedReleases, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath (Join-Path $resolvedSelected "src\watch_audio_pipeline"))) {
        $serverRoot = $resolvedSelected
        $serverVersion = Split-Path -Leaf $resolvedSelected
    }
}

$env:PYTHONPATH = Join-Path $serverRoot "src"
$env:WATCH_AUDIO_ENV_FILE = Join-Path $root ".env"
$env:WATCH_AUDIO_PROJECT_ROOT = $root
$env:WATCH_AUDIO_SERVER_VERSION = if ($serverVersion.Length -ge 12) {
    $serverVersion.Substring(0, 12)
} else {
    $serverVersion
}

if (-not $GeminiOnly) {
    $tailscale = Get-Service -Name "Tailscale" -ErrorAction SilentlyContinue
    if ($tailscale -and $tailscale.Status -ne "Running") {
        try {
            Start-Service -Name "Tailscale"
        } catch {
            # The startup task runs as SYSTEM; a limited logon task may only observe the service.
        }
    }
}

$hostLine = Get-Content (Join-Path $root ".env") |
    Where-Object { $_ -match '^\s*WATCH_AUDIO_HOST\s*=' } |
    Select-Object -First 1
$listenHost = if ($hostLine) { ($hostLine -split '=', 2)[1].Trim() } else { "127.0.0.1" }
$geminiEnabledLine = Get-Content (Join-Path $root ".env") |
    Where-Object { $_ -match '^\s*WATCH_AUDIO_GEMINI_ENABLED\s*=' } |
    Select-Object -First 1
$geminiEnabled = if ($geminiEnabledLine) {
    (($geminiEnabledLine -split '=', 2)[1].Trim()) -match '^(1|true|yes|on)$'
} else {
    $false
}

if (-not $GeminiOnly -and $listenHost -like "100.*") {
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

function Get-PipelineProcesses([string] $Mode) {
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^python(?:w)?\.exe$' -and
            $_.CommandLine -and
            $_.CommandLine -match '(watch_audio_pipeline\.cli|runtime_entry\.py)' -and
            $_.CommandLine -match "\s$Mode(?:\s|$)"
        })
}

function Test-CurrentPipelineProcess([string] $Mode) {
    $processes = Get-PipelineProcesses $Mode
    $versionPattern = "--runtime-version\s+$([regex]::Escape($env:WATCH_AUDIO_SERVER_VERSION))(?:\s|$)"
    $sourcePattern = [regex]::Escape($env:PYTHONPATH)
    $current = @($processes | Where-Object {
        $_.CommandLine -match 'runtime_entry\.py' -and
        $_.CommandLine -match $versionPattern -and
        $_.CommandLine -match $sourcePattern
    })
    $stale = @($processes | Where-Object { $current.ProcessId -notcontains $_.ProcessId })
    $stale | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($current.Count -gt 0) {
        return $true
    }
    if ($stale.Count -gt 0) {
        Start-Sleep -Milliseconds 500
    }
    return $false
}

$runtimeArguments = @(
    $runtimeEntry,
    "--source-root", $env:PYTHONPATH,
    "--runtime-version", $env:WATCH_AUDIO_SERVER_VERSION
)

if (-not $GeminiOnly -and -not (Test-CurrentPipelineProcess "serve")) {
    Start-Process `
        -FilePath $python `
        -ArgumentList ($runtimeArguments + "serve") `
        -WorkingDirectory $root `
        -RedirectStandardOutput (Join-Path $logs "service-api.out.log") `
        -RedirectStandardError (Join-Path $logs "service-api.err.log") `
        -WindowStyle Hidden
}

if (-not $GeminiOnly -and -not (Test-CurrentPipelineProcess "worker")) {
    Start-Process `
        -FilePath $python `
        -ArgumentList ($runtimeArguments + "worker") `
        -WorkingDirectory $root `
        -RedirectStandardOutput (Join-Path $logs "service-worker.out.log") `
        -RedirectStandardError (Join-Path $logs "service-worker.err.log") `
        -WindowStyle Hidden
}

if (-not $CoreOnly -and $geminiEnabled -and -not (Test-CurrentPipelineProcess "gemini-worker")) {
    Start-Process `
        -FilePath $python `
        -ArgumentList ($runtimeArguments + "gemini-worker") `
        -WorkingDirectory $root `
        -RedirectStandardOutput (Join-Path $logs "service-gemini.out.log") `
        -RedirectStandardError (Join-Path $logs "service-gemini.err.log") `
        -WindowStyle Hidden
}
