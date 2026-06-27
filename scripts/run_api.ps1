$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\.."
python -m watch_audio_pipeline.cli serve
