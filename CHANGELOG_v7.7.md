# AutoSubtitleSync Mac v7.7 Browser First Bridge

## Architecture change

v7.7 keeps yt-dlp as fallback but changes the priority philosophy:

Browser playback first → audio bridge → AI subtitle pipeline

The browser handles:
- login/session
- JavaScript player execution
- iframe navigation
- site-specific playback logic

AutoSubtitleSync handles:
- audio understanding
- Whisper transcription
- translation
- subtitle timing
- overlay rendering

## Changes

1. Browser session priority:
Chrome → Edge → Firefox → Safari

2. yt-dlp remains fallback only.

3. Added Browser Audio Bridge design notes.

4. Error messages should guide users toward browser mode instead of repeatedly retrying extractors.
