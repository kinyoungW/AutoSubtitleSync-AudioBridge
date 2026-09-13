# AutoSubtitleSync v8 Browser AI Subtitle Agent

## Architecture

Browser playback is the source of truth.

```
Any webpage video
      |
      v
Browser Audio Bridge
      |
      v
PCM16 audio stream
      |
      v
ASR pipeline (Whisper/faster-whisper)
      |
      v
Subtitle overlay
```

Fallback remains:

```
URL -> yt-dlp -> audio -> ASR
```

## v8 changes

- Browser audio becomes primary input.
- Added PCM chunk queue between browser and ASR.
- yt-dlp remains fallback, not the main dependency.
- Cookie/iframe/media URL problems are avoided when browser playback works.

## Current capture layers

1. video.captureStream()
2. Chrome extension tabCapture (recommended next production layer)
3. system audio loopback fallback
