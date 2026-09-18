# Native Notion narrative report

The instruction reference is `pinellas-narrative-instructions.md`. It contains
the owner's complete checklist plus evidence-only drafting safeguards. It is
not independently verified clinical guidance, and outputs require clinician
review against the recording and PCR.
The owner-supplied instruction text is local-only and gitignored; it is not
published to the source repository. A verified copy is kept in the owner's
Notion workspace by `scripts/publish_narrative_instructions.py`.

## Capability boundary

Notion's documented audio-upload endpoint accepts `kickoff_summary`, not a
custom instruction prompt/template. Custom instructions must be created in
Notion's meeting UI. A normal reference page is NOT an installed template.
Do not claim the reference page alone activates custom generation. Verify
whether the selected default applies to API-created meetings using synthetic
audio; otherwise select the instructions and Retry summary for each meeting.
Do not fall back to another cloud AI, local model, or undocumented private API.
Notion's separate Agent API is beta and is not a suitable silent substitution
for the user's PHI-sensitive native-meeting workflow.

## Explicit enrollment

```dotenv
WATCH_AUDIO_NOTION_REPORT_CLIENT_IDS=["explicit-native-notion-client-id"]
WATCH_AUDIO_NOTION_REPORT_INSTRUCTIONS_URL=https://www.notion.so/actual-page-id
```

Only explicitly enrolled native Notion clients get the draft layout and the
retained full-audio attachment. Wildcards do not enroll other testers. Their
transcription, email, and Notion routing remain unchanged. Past done recordings
are not automatically reprocessed or uploaded.

After finalization, pages have a clinician-review notice, a Narrative draft section and an
Interventions documented section, above the live transcript. Full native
transcript/summary and a Notion-hosted playable complete M4A are retained on
the same page after finalization. Source audio remains on the PC for recovery.
Audio attachment extends retention in Notion; apply an authorized retention
policy and do not treat LLM zero-retention as deletion of workspace files.

The native summary is copied into the draft slots only if it matches the plain
paragraph contract: FirstPass line, narrative longer than 325 characters,
separate `Interventions documented in transcript:` paragraph, and no prohibited
formatting/time/numeric vital patterns. This check is NOT clinical validation
and cannot guarantee factual entailment, completeness, de-identification, or
FirstPass compliance. Sparse evidence stays for review instead of being padded.

A generic summary leaves the slots visibly pending. Recording processing still
finishes; a pending narrative never requests another audio upload. The worker
checks pending summaries every five minutes without invoking another model or
starting another summary. After Notion's Retry summary produces the desired
output, the text is projected above the live transcript. Completed projections
are not continuously overwritten. Human edits are preserved.
Late source audio produces a new, source-revision-labeled draft section at the
top rather than reusing a narrative from the earlier incomplete recording.

Lost attachment/layout responses are reconciled before any repeat create. A
deleted or modified attachment/layout pauses for review rather than silently
recreating it. No transcript blocks are edited. The raw transcript may contain
names and numeric vitals even though the narrative omits them.

## Verification

Run the report, native audio, and live-preview tests. A live acceptance check
must use synthetic audio, verify the native transcript, GET the private audio
attachment successfully, and confirm page section order. Do not infer that a
matching test fixture proves Notion generated a correct clinical narrative.
Do not claim HIPAA compliance until the BAA, feature coverage, permissions,
and complete recording workflow have been separately approved.

References:
- https://developers.notion.com/reference/create-meeting-note
- https://www.notion.com/help/ai-meeting-notes
- https://developers.notion.com/guides/data-apis/uploading-small-files
