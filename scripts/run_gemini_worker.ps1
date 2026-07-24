$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\.."
$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}
& $python -m watch_audio_pipeline.cli gemini-worker
