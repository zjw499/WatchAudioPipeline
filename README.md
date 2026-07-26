# Watch Audio Pipeline

Private receiver, Groq transcription worker, email sender, and Gemini Gem delivery worker for Apple Watch recordings uploaded by the Codex Watch app, Voice Record Pro, or an iPhone Shortcuts automation.

## Privacy Boundary

This project keeps the upload receiver, queued audio files, transcript files, and job database on this Windows PC. Audio chunks are sent to GroqCloud for transcription with `whisper-large-v3-turbo`; the API key remains in a local file outside the repository.

This project does not make the workflow HIPAA compliant by itself. Do not transmit PHI to Groq, SMTP, or Gemini unless the applicable agreements and Business Associate Addenda are effective for your organization and each service is included functionality. Enable Zero Data Retention in Groq Data Controls, keep the API on a trusted private network or VPN, and do not expose the upload port to the public internet.

## Local Setup

1. Install Python 3.11 or newer.
2. Create and activate a virtual environment.
3. Install the package in editable mode.
4. Copy `.env.example` to `.env` and fill in the Basic Auth credentials, SMTP settings, and Groq key-file path.

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
WATCH_AUDIO_HOST=100.x.y.z
WATCH_AUDIO_PORT=8787
WATCH_AUDIO_SSL_CERTFILE=D:\watch-audio-pipeline\certs\watch-audio.crt
WATCH_AUDIO_SSL_KEYFILE=D:\watch-audio-pipeline\certs\watch-audio.key
WATCH_AUDIO_BASIC_AUTH_USERNAME=watch-audio
WATCH_AUDIO_BASIC_AUTH_PASSWORD=use-a-long-random-password
WATCH_AUDIO_MAX_UPLOAD_BYTES=536870912
WATCH_AUDIO_SMTP_HOST=smtp.example.com
WATCH_AUDIO_SMTP_PORT=587
WATCH_AUDIO_SMTP_USERNAME=
WATCH_AUDIO_SMTP_PASSWORD=
WATCH_AUDIO_SMTP_FROM=watch-audio@example.com
WATCH_AUDIO_SMTP_TO=you@example.com
WATCH_AUDIO_TRANSCRIPTION_PROVIDER=groq
WATCH_AUDIO_GROQ_API_KEY_FILE=C:\Users\you\.config\watch-audio\groq-api-key.txt
WATCH_AUDIO_GROQ_MODEL=whisper-large-v3-turbo
WATCH_AUDIO_GROQ_LANGUAGE=en
WATCH_AUDIO_DIARIZATION_ENABLED=false
WATCH_AUDIO_DIARIZATION_MODEL=pyannote/speaker-diarization-community-1
WATCH_AUDIO_DIARIZATION_TOKEN=
WATCH_AUDIO_DIARIZATION_TOKEN_FILE=
WATCH_AUDIO_DIARIZATION_DEVICE=cpu
WATCH_AUDIO_DIARIZATION_MIN_SPEAKERS=
WATCH_AUDIO_DIARIZATION_MAX_SPEAKERS=
WATCH_AUDIO_WATCH_FOLDER_ENABLED=true
WATCH_AUDIO_WATCH_FOLDER=C:\Users\you\Downloads
WATCH_AUDIO_WATCH_FOLDER_MIN_AGE_SECONDS=10
WATCH_AUDIO_WORKER_POLL_SECONDS=10
WATCH_AUDIO_GEMINI_ENABLED=false
WATCH_AUDIO_GEMINI_GEM_URL=https://gemini.google.com/gem/REPLACE_WITH_GEM_ID
WATCH_AUDIO_GEMINI_PROFILE_DIR=C:\Users\you\.config\watch-audio\gemini-chrome-profile
WATCH_AUDIO_GEMINI_CHROME_CHANNEL=chrome
WATCH_AUDIO_GEMINI_HEADLESS=true
WATCH_AUDIO_GEMINI_TIMEOUT_SECONDS=300
WATCH_AUDIO_GEMINI_POLL_SECONDS=15
WATCH_AUDIO_GEMINI_MAX_RETRIES=5
WATCH_AUDIO_GEMINI_RETRY_BASE_SECONDS=30
WATCH_AUDIO_NTFY_ENABLED=false
WATCH_AUDIO_NTFY_URL=https://ntfy.sh/your-private-topic
WATCH_AUDIO_NTFY_TIMEOUT_SECONDS=10
```

Current Gmail SMTP values are stored in the local `.env` file:

```dotenv
WATCH_AUDIO_SMTP_HOST=smtp.gmail.com
WATCH_AUDIO_SMTP_PORT=587
WATCH_AUDIO_SMTP_USERNAME=you@example.com
WATCH_AUDIO_SMTP_FROM=you@example.com
WATCH_AUDIO_SMTP_TO=you@example.com
```

`WATCH_AUDIO_SMTP_PASSWORD` is a generated Google app password and must remain only in `.env`.

## Optional Local Speaker Labels

The optional diarization stage labels different voices as `Speaker 1`, `Speaker 2`, and so on. It does not infer names. This is disabled in the Groq-only live configuration because it loads a local AI model.

Install the optional runtime:

```powershell
python -m pip install -e ".[diarization]"
```

Accept the terms for the [pyannote community diarization model](https://huggingface.co/pyannote/speaker-diarization-community-1), create a Hugging Face access token, and store it in a local file outside Git:

```dotenv
WATCH_AUDIO_DIARIZATION_ENABLED=true
WATCH_AUDIO_DIARIZATION_TOKEN_FILE=C:\Users\you\.config\watch-audio\huggingface-token.txt
```

Keep the token file private. The first enabled run downloads the model; later jobs use the local cache. `WATCH_AUDIO_DIARIZATION_MIN_SPEAKERS` and `WATCH_AUDIO_DIARIZATION_MAX_SPEAKERS` are optional constraints when the expected speaker count is known. Pyannote telemetry is disabled by the worker; the token is not written to transcript files or email bodies.

The worker can also watch the Tailscale Taildrop landing folder. Files shared from the iPhone with `Share` > `Tailscale` > PC name land in `C:\Users\you\Downloads`; the worker copies supported audio files from there into `data\incoming`, queues them by content hash, and leaves the originals in Downloads.

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

Test SMTP after filling in the SMTP settings:

```powershell
python -m watch_audio_pipeline.cli send-test-email
```

Retry one transcript that previously failed during email delivery:

```powershell
python -m watch_audio_pipeline.cli retry-email-failed
```

## Gemini Narrative Writer

Install the local browser automation dependency:

```powershell
python -m pip install -e ".[gemini]"
```

Gemini Gems do not expose a supported messaging API. The Gemini worker therefore uses a dedicated persistent Chrome profile stored outside the repository. Google and Okta session cookies remain in that profile across recordings and PC restarts; the worker does not read, export, or store account passwords.

Before enabling delivery, open the one-time login browser:

```powershell
python -m watch_audio_pipeline.cli gemini-login
```

Sign in with the covered department account, confirm that the configured Gem opens, and then close the dedicated Chrome window. Verify the saved session:

```powershell
python -m watch_audio_pipeline.cli gemini-check
```

Set `WATCH_AUDIO_GEMINI_ENABLED=true` only after that command succeeds, then run `scripts\start_services.ps1`. The startup script launches `gemini-worker` automatically. The browser process remains alive between deliveries and reuses the persistent profile.

Optionally enable a PHI-free operational notification when department Okta
requires interactive verification:

```dotenv
WATCH_AUDIO_NTFY_ENABLED=true
WATCH_AUDIO_NTFY_URL=https://ntfy.sh/your-private-topic
```

The notification contains no recording name, transcript text, job identifier, or
patient information. An unauthenticated ntfy topic is discoverable to anyone who
knows its name, so use an unguessable topic or a protected self-hosted ntfy server.

Install the startup tasks once so the API and transcription worker start at
Windows boot and the user-profile Gemini worker starts at logon:

```powershell
.\scripts\install_startup_task.ps1
```

The boot task checks the configured public Git remote for the newest `main`
commit. It expands the commit into `.runtime\releases`, validates dependencies
and imports, and atomically updates `.runtime\active-server-path.txt`. A failed
fetch or validation keeps the previously selected release. Local `.env`,
certificates, recordings, transcripts, databases, logs, and browser profiles are
excluded from Git and are never copied to the remote.

If department Okta policy expires the session, deliveries move to `authentication_required`, stop retrying, and send one ntfy alert for that authentication episode. Run `gemini-login` once, close the login browser, and run `gemini-check`; blocked deliveries are requeued after authentication succeeds. The worker cannot and should not bypass an organization-mandated Okta reauthentication or MFA challenge.

Failures before message submission use bounded exponential backoff. Once the worker crosses the Enter-key submission boundary, an unconfirmed result is marked `confirmation_needed` and is not automatically resent. This prevents duplicate patient transcripts from nested retries.

Health check from the PC:

```powershell
curl.exe --noproxy "*" --ssl-no-revoke --cacert "D:\watch-audio-pipeline\certs\watch-audio-ca.crt" "https://<pc-name>.<tailnet>.ts.net:8787/health"
```

Expected:

```json
{"status":"ok","server_version":"<commit>","api_version":"1"}
```

The recommended iPhone upload URL is the PC's Tailscale MagicDNS name:

```text
https://<pc-name>.<tailnet>.ts.net:8787/upload
```

Use this Tailscale URL from Voice Record Pro or the iPhone automation so uploads work from cellular without public port forwarding. The current Tailscale IP fallback is:

```text
https://100.x.y.z:8787/upload
```

The generated server certificate includes `<pc-name>.<tailnet>.ts.net`, `100.x.y.z`, and `192.168.1.10`. If Windows Firewall blocks the iPhone, allow inbound TCP `8787` only on the Private profile or Tailscale interface:

```powershell
New-NetFirewallRule -DisplayName "Watch Audio Pipeline HTTPS 8787 (Tailscale)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8787 -Profile Private
```

The generated root CA certificate is at `D:\watch-audio-pipeline\certs\watch-audio-ca.crt`. Install only that CA `.crt` file on the iPhone and enable full trust for it. Keep `D:\watch-audio-pipeline\certs\watch-audio-ca.key` and `D:\watch-audio-pipeline\certs\watch-audio.key` on this PC only.

## Runtime Files

Raw uploads are stored under `data\incoming`.
Transcripts are stored under `data\transcripts`.
Failed transcription inputs are copied under `data\failed`.
Job state is stored in `data\state\jobs.sqlite3`.
Watch-folder import state is stored in `data\state\watch-folder-imports.json`.
Logs are written under `logs\upload.log`, `logs\transcription.log`, and `logs\email.log`.
Gemini delivery state is stored in the `gemini_deliveries` table, and its operational log is `logs\gemini.log`.
Update decisions are written to `logs\service-update.log`; ntfy delivery is logged in `logs\notification.log`.

The transcription worker imports ready watch-folder audio files, then processes one queued transcription job and one transcribed delivery job per loop. The independent Gemini worker submits queued transcripts to the configured Gem without blocking transcription or email.

## Memo API

The authenticated memo endpoints are available at the same HTTPS host:

```text
GET    /memos
GET    /memos/{memo_id}
POST   /memos/{memo_id}/retry
DELETE /memos/{memo_id}
GET    /preferences
PUT    /preferences
```

The Codex Watch iPhone app stores the tester's transcript email locally and
sends it with each upload. The API stores that address on the job or watch
recording session and sends app-originated mail only to that address, rather
than adding the legacy SMTP default recipients. The app also sends a
persistent installation ID in `X-Codex-Client-ID`; memo lists and preferences
are filtered by that ID. This is beta isolation, not user authentication, so
the shared Basic Auth setup must not be treated as production multi-tenant
security.

The worker is PC-canonical: it sends each uploaded chunk to Groq for transcription, stores the transcript and memo metadata in SQLite, emails the configured Gmail recipient, and queues the transcript for the department Gemini Gem when enabled. Local Ollama summaries are disabled in the Groq-only live configuration. Raw audio is removed from the incoming and failed folders only after successful email delivery. SMTP credentials and the dedicated Gemini browser profile remain on the PC.

## Manual Test

With both processes running, send a small audio file from the iPhone automation or test from the PC:

```powershell
curl.exe --noproxy "*" --ssl-no-revoke --cacert "D:\watch-audio-pipeline\certs\watch-audio-ca.crt" -X POST "https://<pc-name>.<tailnet>.ts.net:8787/upload" `
  -u "watch-audio:use-a-long-random-password" `
  -F "source=manual-test" `
  -F "file=@C:\path\to\test.m4a;type=audio/mp4"
```

The API should return `queued`, then the worker should write a transcript and send one email with a generic subject such as `Transcript ready (abcd1234)`.
