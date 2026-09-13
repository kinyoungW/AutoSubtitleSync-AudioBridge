> **实现说明（v8.1 补充）**：本文档描述的是设想方案。真正落地的实现有两处不同，原因见 CHANGELOG_v8.md：
> ① 采用**不重叠定长窗口**（重叠窗口在短窗口下会丢文本/重复）；
> ② 捕获端优先用 **AudioWorklet**，`connect(ctx.destination)` 改为**零增益**输出（原设计会导致回声）。
> 音频流由 `audio_stream.py` 消费，浏览器端脚本为 `BrowserAudioBridge.user.js`（v2）。

# Browser Audio Bridge v7.7

## Goal

Move from URL extraction to browser-native subtitle assistance.

## Pipeline

Website Player
↓
Browser Companion
↓
Audio Capture Layer
↓
localhost bridge
↓
Whisper/VAD
↓
Subtitle Overlay

## Capture priority

1. HTMLVideoElement.captureStream()
2. Browser tab audio capture extension
3. System audio fallback

## Why

Some streaming sites solve authentication and dynamic playback inside the browser. Re-implementing this logic in yt-dlp creates unnecessary fragility.

## Compatibility

DRM protected media is not bypassed. Browser bridge only consumes audio that the user can already play.
