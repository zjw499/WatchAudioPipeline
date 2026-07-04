# Watch Audio Pipeline

Local receiver, transcription worker, and email sender for Apple Watch recordings uploaded by an iPhone Shortcuts automation.

## Privacy Boundary

This project keeps the upload receiver, audio files, transcript files, job database, and Whisper transcription on this Windows PC. It does not upload recordings to a third-party transcription service.

This project does not make email or your network HIPAA compliant by itself. If transcripts may contain patient information, use only a compliant SMTP/email provider with the right agreements and safeguards, keep the API on a trusted private network or VPN, and do not expose the upload port to the public internet.

## Local Setup

1. Install Python 3.11 or newer.
2. Create and activate a virtual environment.
3. Install the package in editable mode.
4. Copy `.env.example` to `.env` and fill in the upload token, SMTP settings, and Whisper settings.

```powershell
Set-Location "D:\watch-audio-pipeline"
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Edit `.env`:

```dotenv
WATCH_AUDIO_PROJECT_ROOT=D:\watch-audio-pipeline
WATCH_AUDIO_HOST=0.0.0.0
WATCH_AUDIO_PORT=8787
WATCH_AUDIO_SSL_CERTFILE=D:\watch-audio-pipeline\certs\watch-audio.crt
WATCH_AUDIO_SSL_KEYFILE=D:\watch-audio-pipeline\certs\watch-audio.key
WATCH_AUDIO_UPLOAD_TOKEN=use-a-long-random-token
WATCH_AUDIO_MAX_UPLOAD_BYTES=26214400
WATCH_AUDIO_SMTP_HOST=smtp.example.com
WATCH_AUDIO_SMTP_PORT=587
WATCH_AUDIO_SMTP_USERNAME=
WATCH_AUDIO_SMTP_PASSWORD=
WATCH_AUDIO_SMTP_FROM=watch-audio@example.com
WATCH_AUDIO_SMTP_TO=you@example.com
WATCH_AUDIO_WHISPER_MODEL=small
WATCH_AUDIO_WHISPER_DEVICE=cpu
WATCH_AUDIO_WORKER_POLL_SECONDS=10
```

## Running Locally

Start the API:

```powershell
Set-Location "D:\watch-audio-pipeline"
.\scripts\run_api.ps1
```

Start the worker in a second PowerShell window:

```powershell
Set-Location "D:\watch-audio-pipeline"
.\scripts\run_worker.ps1
```

Health check from the PC:

```powershell
Invoke-WebRequest http://127.0.0.1:8787/health | Select-Object -ExpandProperty Content
```

Expected:

```json
{"status":"ok"}
```

Find the PC LAN address for the iPhone shortcut:

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "169.254*" -and $_.IPAddress -ne "127.0.0.1" } | Select-Object InterfaceAlias,IPAddress
```

Use `https://192.168.1.29:8787/upload` from Voice Record Pro or the iPhone automation. If Windows Firewall blocks the iPhone, allow inbound TCP `8787` only on your private network profile.

The generated public certificate is at `D:\watch-audio-pipeline\certs\watch-audio.crt`. Install only that `.crt` file on the iPhone and enable full trust for it. Keep `D:\watch-audio-pipeline\certs\watch-audio.key` on this PC only.

## Runtime Files

Raw uploads are stored under `data\incoming`.
Transcripts are stored under `data\transcripts`.
Failed transcription inputs are copied under `data\failed`.
Job state is stored in `data\state\jobs.sqlite3`.
Logs are written under `logs\upload.log`, `logs\transcription.log`, and `logs\email.log`.

The worker processes one queued transcription job and one transcribed email job per loop.

## Manual Test

With both processes running, send a small audio file from the iPhone automation or test from the PC:

```powershell
curl.exe -k -X POST "https://192.168.1.29:8787/upload" `
  -H "X-Upload-Token: use-a-long-random-token" `
  -F "source=manual-test" `
  -F "file=@C:\path\to\test.m4a;type=audio/mp4"
```

The API should return `queued`, then the worker should write a transcript and send one email with a generic subject such as `Transcript ready (abcd1234)`.
