#!/usr/bin/env python3
import json, os, platform, sys
from pathlib import Path

def browser_sessions():
    h=Path.home()
    checks=[
      ('chrome','Chrome',[h/'Library/Application Support/Google/Chrome',Path('/Applications/Google Chrome.app')]),
      ('safari','Safari',[h/'Library/Safari',h/'Library/Containers/com.apple.Safari',Path('/Applications/Safari.app')]),
      ('edge','Edge',[h/'Library/Application Support/Microsoft Edge',Path('/Applications/Microsoft Edge.app')]),
      ('firefox','Firefox',[h/'Library/Application Support/Firefox/Profiles',Path('/Applications/Firefox.app')]),
    ]
    return [(k,l,any(p.exists() for p in paths)) for k,l,paths in checks]

print('=== Auto Subtitle Sync v7.6 · Online Diagnostics ===')
print('Python:',sys.version.split()[0])
print('macOS:',platform.mac_ver()[0], 'arch:',platform.machine())
try:
    import yt_dlp
    print('yt-dlp:',yt_dlp.version.__version__)
except Exception as e: print('yt-dlp: ERROR',repr(e))
try:
    from imageio_ffmpeg import get_ffmpeg_exe
    ff=get_ffmpeg_exe(); print('FFmpeg:',ff, 'exists=',Path(ff).exists())
except Exception as e: print('FFmpeg: ERROR',repr(e))
print('\nBrowser profiles (detected only; this does not read/export cookies):')
for k,l,ok in browser_sessions(): print(f'  {l:8s}', 'READY-TO-TRY' if ok else 'NOT DETECTED')
print('\nCookie privacy: v7.6 passes cookiesfrombrowser directly to yt-dlp per attempt; no cookies.txt is written by AutoSubtitleSync.')
print('If a site says “not a bot”, log in normally in your browser and select Auto or that browser in Online mode.')
print('\nIframe Bridge: Browser Companion v7.6 needs Tampermonkey site access on both the top page and third-party player iframe domains.')
print('Tip: start the web video for a few seconds before enabling subtitles so the real player and media requests are visible.')
