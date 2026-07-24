# Voice Record Pro iPhone and Apple Watch Setup

This setup uses `Voice Record Pro` on the Apple Watch for recording and the paired iPhone for HTTPS upload to the local Windows receiver.

## Tailscale HTTPS Endpoint

Use Tailscale for iPhone-to-PC uploads when the iPhone is on cellular or away from the home LAN. Do not expose port `8787` with router port forwarding.

Use this upload URL on the iPhone:

```text
https://<pc-name>.<tailnet>.ts.net:8787/upload
```

Fallback URL if MagicDNS is unavailable:

```text
https://100.x.y.z:8787/upload
```

The local LAN address is still included in the server certificate for troubleshooting:

```text
https://192.168.1.10:8787/upload
```

The Basic Auth username and password are stored only in the local PC file:

```text
D:\watch-audio-pipeline\.env
```

Use `WATCH_AUDIO_BASIC_AUTH_USERNAME` and `WATCH_AUDIO_BASIC_AUTH_PASSWORD` when the iPhone app asks for HTTP Basic Auth credentials. Do not store the password in Notion.

The root CA certificate to install on the iPhone is:

```text
D:\watch-audio-pipeline\certs\watch-audio-ca.crt
```

These private keys must stay on the PC:

```text
D:\watch-audio-pipeline\certs\watch-audio-ca.key
D:\watch-audio-pipeline\certs\watch-audio.key
```

The API uses this server certificate:

```text
D:\watch-audio-pipeline\certs\watch-audio.crt
```

The current server certificate includes these names and IP addresses:

```text
DNS:localhost
DNS:watch-audio-pipeline.local
DNS:<pc-name>
DNS:<pc-name>.<tailnet>.ts.net
IP:127.0.0.1
IP:192.168.1.10
IP:100.x.y.z
```

The API is currently configured to listen on the Tailscale IP:

```text
WATCH_AUDIO_HOST=100.x.y.z
WATCH_AUDIO_PORT=8787
```

Because another local process is already listening on `127.0.0.1:8787`, do not switch this service to `0.0.0.0:8787` unless that conflict is removed.

## Tailscale Setup

1. Install Tailscale on the Windows PC.
2. Install Tailscale on the iPhone.
3. Sign both into the same tailnet.
4. Confirm the PC shows as `<PC name>` / `<pc-name>.<tailnet>.ts.net`.
5. Confirm the iPhone Tailscale VPN is connected before uploading from cellular.
6. Keep Tailscale enabled on the iPhone when using Voice Record Pro away from home.

## iPhone Certificate Trust

1. Send only `watch-audio-ca.crt` to the iPhone.
2. Open the certificate on the iPhone and install the profile.
3. Open `Settings`.
4. Go to `General` > `About` > `Certificate Trust Settings`.
5. Enable full trust for the installed certificate.
6. Confirm that Safari can open `https://<pc-name>.<tailnet>.ts.net:8787/health`.

Expected health response:

```json
{"status":"ok","server_version":"<commit>","api_version":"1"}
```

## PC Runtime

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

Current Gmail SMTP values are stored in the local `.env` file:

```text
WATCH_AUDIO_SMTP_HOST=smtp.gmail.com
WATCH_AUDIO_SMTP_PORT=587
WATCH_AUDIO_SMTP_USERNAME=you@example.com
WATCH_AUDIO_SMTP_FROM=you@example.com
WATCH_AUDIO_SMTP_TO=you@example.com
```

Completed transcripts can also be sent to the department Narrative writer Gem. This uses a dedicated persistent Chrome profile on the PC because Gemini Gems do not expose a messaging API:

```dotenv
WATCH_AUDIO_GEMINI_ENABLED=true
WATCH_AUDIO_GEMINI_GEM_URL=https://gemini.google.com/gem/REPLACE_WITH_GEM_ID
WATCH_AUDIO_GEMINI_PROFILE_DIR=C:\Users\you\.config\watch-audio\gemini-chrome-profile
WATCH_AUDIO_NTFY_ENABLED=true
WATCH_AUDIO_NTFY_URL=https://ntfy.sh/your-private-topic
```

Run `python -m watch_audio_pipeline.cli gemini-login` once and sign in with the covered department account. The profile preserves the Google and Okta session between recordings and reboots. Organization-enforced Okta session expiration still requires an interactive login; the Gemini queue pauses rather than repeatedly retrying or duplicating a transcript.

The first delivery that detects an expired Okta session sends one PHI-free ntfy
alert. Additional queued recordings do not create duplicate alerts during the
same authentication episode.

`WATCH_AUDIO_SMTP_PASSWORD` is a generated Google app password and must remain only in `.env`.

The worker also watches the local Taildrop landing folder:

```text
WATCH_AUDIO_WATCH_FOLDER_ENABLED=true
WATCH_AUDIO_WATCH_FOLDER=C:\Users\you\Downloads
WATCH_AUDIO_WATCH_FOLDER_MIN_AGE_SECONDS=10
```

When an audio file is shared from the iPhone with `Share` > `Tailscale` > PC name, Tailscale saves it to `C:\Users\you\Downloads`. The worker copies supported audio files from that folder into `D:\watch-audio-pipeline\data\incoming`, queues them by content hash, and leaves the original Downloads file untouched.

## Voice Record Pro Recording

1. Install `Voice Record Pro` on the iPhone and Apple Watch.
2. In the app settings, avoid cloud sync and app-provided transcription for these recordings where possible.
3. Record on the Apple Watch.
4. Confirm the recording syncs or appears in `Voice Record Pro` on the iPhone.
5. Keep recordings in M4A/AAC, MP3, or WAV format.

## Voice Record Pro Upload

Use the app's export option for `Post to any web based script` or equivalent custom web upload.

Configure the upload:

1. URL: `https://<pc-name>.<tailnet>.ts.net:8787/upload`
2. Method: `POST`
3. Body type: multipart form upload
4. File field: `file`
5. Source field: `source` with value `voice-record-pro`
6. Authentication: HTTP Basic Auth
7. Username: value of `WATCH_AUDIO_BASIC_AUTH_USERNAME` from local `.env`
8. Password: value of `WATCH_AUDIO_BASIC_AUTH_PASSWORD` from local `.env`

Expected response after a new upload:

```json
{"status":"queued"}
```

Expected response after the same file is uploaded again:

```json
{"status":"duplicate"}
```

## If Voice Record Pro Does Not Expose Form Fields

Some app screens may not expose custom form fields or headers. If that happens, use the iPhone share sheet or Shortcuts as the fallback:

1. In `Voice Record Pro`, share or export the selected recording to Shortcuts.
2. In Shortcuts, use `Get Contents of URL`.
3. Set URL to `https://<pc-name>.<tailnet>.ts.net:8787/upload`.
4. Set method to `POST`.
5. Set request body to `Form`.
6. Add form field `source` with value `voice-record-pro`.
7. Add form field `file` with the selected recording.
8. Set authentication to HTTP Basic Auth with the username and password from local `.env`.

## HIPAA-Safe Operation Notes

Use only the trusted private network or a private VPN. Do not expose port `8787` to the public internet.
If the iPhone cannot reach `/health` over Tailscale, add a Windows Firewall inbound allow rule for TCP `8787` on the Private profile from an elevated PowerShell session:

```powershell
New-NetFirewallRule -DisplayName "Watch Audio Pipeline HTTPS 8787 (Tailscale)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8787 -Profile Private
```

Use a compliant email provider and account for transcript delivery if the transcript can contain patient information.
Keep iCloud, third-party transcription, and app vendor cloud sync disabled for these recordings where possible.
Keep the PC disk protected with Windows account security and full-disk encryption.
The email subject generated by the pipeline does not include the recording filename.
