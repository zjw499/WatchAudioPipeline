# Near-live Notion meetings

The watch continues uploading its existing audio chunks through the phone to the
authenticated PC backend. The backend sends each contiguous chunk to Notion's
native meeting-note transcription API with summarization disabled, then copies
the verified text onto one meeting page. No Groq or local speech model is used
for enrolled clients. The raw audio stays saved for recovery.

This is batched near-live transcription, not a Notion microphone session or a
word-by-word stream. The first text requires the first watch chunk to finish,
phone delivery, several API operations, and Notion processing. Later text updates
arrive in chunk order. Network delays or sleeping/offline devices increase delay.

When the watch stops and all chunks arrive, the existing complete-audio worker
asks Notion to transcribe and summarize the entire recording on the SAME page.
The full meeting is authoritative; previews may split sentences or have different
speaker labels across parts. Both preview and final audio are sent to Notion, so
the account processes approximately twice the recorded duration. Account limits
and quotas still apply. There is no dependency on a browser microphone, an open
Notion window, or a virtual audio driver.

## Configuration

Existing native Notion configuration and token are required, plus:

```dotenv
WATCH_AUDIO_NOTION_LIVE_PREVIEW_ENABLED=true
WATCH_AUDIO_NOTION_LIVE_CLIENT_IDS=["explicit-client-id"]
WATCH_AUDIO_NOTION_LIVE_START_AFTER=2026-01-01T00:00:00+00:00
```

Use the actual enablement timestamp to avoid resubmitting historical recordings.
Live enrollment requires a timezone-aware cutoff and explicit client IDs; a
wildcard never enrolls another tester. Other users keep their existing routing.
Disabling preview leaves complete-audio native transcription enabled.

The existing exclusive `notion-worker` process owns both workers. Per-session
SQLite checkpoints survive process restarts. Page/block creation and text appends
are reconciled after uncertain responses rather than blindly repeated. Missing
indexes wait; duplicate audio does not duplicate text; changed published audio
pauses previews but remains available to the full-meeting worker. A preview error
cannot mark the complete recording done or delete source audio.

## Phone Use

1. Keep the phone connected to the configured private backend (Tailscale for the
   current deployment). Record in the existing watch app.
2. Open the configured Scribe Pilot Meetings database in Notion on the phone.
3. Open the new `Watch meeting` page. The Live transcript section updates as audio
   parts arrive. The nested Live audio parts page holds the original Notion
   transcription blocks.
4. Stop on the watch. The complete native meeting transcript and summary appear
   below the preview after all audio has arrived and Notion finishes processing.

Use only an appropriately authorized Notion workspace and account for sensitive
recordings. This routing configuration does not itself establish HIPAA compliance.

## Acceptance Checks

- Upload a synthetic non-final chunk and verify text on the page before finalizing.
- Resend that chunk and verify no duplicate text or meeting page.
- Send a later/final chunk before its predecessor; verify ordered waiting.
- Complete the missing upload and verify the full transcript and summary on the
  same page, with source files retained and no Groq/legacy delivery created.
- Check another client's destination is unchanged. Physical watch and phone
  synchronization requires an actual device recording, not just an API test.
