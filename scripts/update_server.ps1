param(
    [string] $Remote = "origin",
    [string] $Branch = "main"
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtime = Join-Path $root ".runtime"
$releases = Join-Path $runtime "releases"
$logs = Join-Path $root "logs"
$python = Join-Path $root ".venv\Scripts\python.exe"
$activePointer = Join-Path $runtime "active-server-path.txt"
$lockPath = Join-Path $runtime "update.lock"

New-Item -ItemType Directory -Path $runtime, $releases, $logs -Force | Out-Null

function Write-UpdateLog([string] $Message) {
    $line = "{0:o} {1}" -f (Get-Date), $Message
    Add-Content -LiteralPath (Join-Path $logs "service-update.log") -Value $line
}

$lock = $null
try {
    try {
        $lock = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch {
        Write-UpdateLog "another update process owns the lock; keeping the selected release"
        return
    }

    if (-not (Test-Path -LiteralPath $python)) {
        throw "Pipeline Python environment was not found at $python"
    }

    $remoteUrl = (& git -C $root remote get-url $Remote 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $remoteUrl) {
        Write-UpdateLog "remote '$Remote' is not configured; keeping the selected release"
        return
    }

    Write-UpdateLog "checking $Remote/$Branch for a newer server"
    & git -C $root fetch --quiet --prune $Remote "refs/heads/$Branch"
    if ($LASTEXITCODE -ne 0) {
        throw "git fetch failed"
    }

    $commit = (& git -C $root rev-parse FETCH_HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') {
        throw "fetched server commit could not be resolved"
    }

    $release = Join-Path $releases $commit
    $releaseSource = Join-Path $release "src"
    if (-not (Test-Path -LiteralPath $releaseSource)) {
        $archive = Join-Path $runtime "server-$commit.zip"
        $temporaryRelease = Join-Path $releases "$commit.preparing"
        if (Test-Path -LiteralPath $temporaryRelease) {
            $resolvedTemporary = [System.IO.Path]::GetFullPath($temporaryRelease)
            $resolvedReleases = [System.IO.Path]::GetFullPath($releases) + [System.IO.Path]::DirectorySeparatorChar
            if (-not $resolvedTemporary.StartsWith($resolvedReleases, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "temporary release path escaped the runtime releases directory"
            }
            Remove-Item -LiteralPath $temporaryRelease -Recurse -Force
        }

        & git -C $root archive --format=zip --output=$archive $commit
        if ($LASTEXITCODE -ne 0) {
            throw "git archive failed"
        }
        Expand-Archive -LiteralPath $archive -DestinationPath $temporaryRelease -Force
        Remove-Item -LiteralPath $archive -Force
        Move-Item -LiteralPath $temporaryRelease -Destination $release
    }

    if (-not (Test-Path -LiteralPath (Join-Path $release "pyproject.toml")) -or
        -not (Test-Path -LiteralPath (Join-Path $releaseSource "watch_audio_pipeline"))) {
        throw "fetched release is missing the server package"
    }

    $dependenciesReady = Join-Path $release ".dependencies-ready"
    if (-not (Test-Path -LiteralPath $dependenciesReady)) {
        & $python -m pip install --disable-pip-version-check --quiet "$release[gemini]"
        if ($LASTEXITCODE -ne 0) {
            throw "server dependency installation failed"
        }
        New-Item -ItemType File -Path $dependenciesReady -Force | Out-Null
    }

    $previousPythonPath = $env:PYTHONPATH
    $previousEnvFile = $env:WATCH_AUDIO_ENV_FILE
    $previousProjectRoot = $env:WATCH_AUDIO_PROJECT_ROOT
    $previousServerVersion = $env:WATCH_AUDIO_SERVER_VERSION
    try {
        $env:PYTHONPATH = $releaseSource
        $env:WATCH_AUDIO_ENV_FILE = Join-Path $root ".env"
        $env:WATCH_AUDIO_PROJECT_ROOT = $root
        $env:WATCH_AUDIO_SERVER_VERSION = $commit.Substring(0, 12)
        & $python -m compileall -q $releaseSource
        if ($LASTEXITCODE -ne 0) {
            throw "server source compilation failed"
        }
        & $python -c "from watch_audio_pipeline.cli import main; from watch_audio_pipeline.config import load_settings; load_settings()"
        if ($LASTEXITCODE -ne 0) {
            throw "server import validation failed"
        }
    } finally {
        $env:PYTHONPATH = $previousPythonPath
        $env:WATCH_AUDIO_ENV_FILE = $previousEnvFile
        $env:WATCH_AUDIO_PROJECT_ROOT = $previousProjectRoot
        $env:WATCH_AUDIO_SERVER_VERSION = $previousServerVersion
    }

    $pointerTemporary = "$activePointer.tmp"
    Set-Content -LiteralPath $pointerTemporary -Value $release -NoNewline
    Move-Item -LiteralPath $pointerTemporary -Destination $activePointer -Force
    Write-UpdateLog "selected validated server $($commit.Substring(0, 12))"
} catch {
    Write-UpdateLog "update failed; keeping the previously selected release: $($_.Exception.Message)"
} finally {
    if ($lock) {
        $lock.Dispose()
    }
}
