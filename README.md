# AutoSubtitleSync · Audio Bridge（v8.1）

**直接在网页播放器上显示实时字幕**：浏览器抓取当前播放的声音 → 送到你自己电脑上识别 → 字幕叠回画面上。
不解析视频链接、不下载视频、不碰网站登录，绕开了"链接解析失败 / DRM / 地址过期"这一整类问题。

> 这个仓库是 **v8.1 音频桥版**，和 [AutoSubtitleSync](https://github.com/kinyoungW/AutoSubtitleSync)（v6.4 本地 Mac 版）是两个独立的软件，代码不共享、各自演化。

## 它和旧版的区别

| | 旧版 v6.4 | 本仓库 v8.1 |
| --- | --- | --- |
| 输入 | 本机视频文件、粘贴的视频链接 | **浏览器正在播的声音** |
| 字幕出现方式 | 处理完导出 SRT / 带字幕 MP4 | 边播边出，叠加在画面上（约慢 5–8 秒）|
| 典型场景 | 下载好的视频做字幕 | 在线看视频、直播、任何网页播放器 |

v8.1 同时**保留了** v6.4 的本地/链接两种模式，只是新增了音频桥这条路。

## 环境要求

- macOS（Windows/Linux 理论上可用，但启动脚本是 macOS 的）
- Python 3.9–3.13
- 实时字幕需要 **Chrome 或 Edge** + [Tampermonkey](https://www.tampermonkey.net/)（Safari 不支持从播放器抓音）

## 快速开始

1. 双击 `AutoSubtitleSync.command`（首次会自动装依赖，等几分钟；之后直接启动）；
2. 浏览器会打开本机控制台。右下角「**实时字幕（音频桥）**」卡片里点「**安装实时字幕脚本**」；
   - 装了 Tampermonkey 会直接弹安装页，点"安装"即可；
3. 打开任意视频网站，播放几秒，点右下角「实时字幕（音频桥）」→「**开始实时字幕**」；
4. 约 3–6 秒后字幕开始逐句出现。

详细图文步骤见 [`实时字幕（音频桥）使用说明.md`](实时字幕（音频桥）使用说明.md)；
改动细节与实测数据见 [`CHANGELOG_v8.md`](CHANGELOG_v8.md)。

## 目录说明

| 文件 | 作用 |
| --- | --- |
| `AutoSubtitleSync.command` | macOS 一键启动 |
| `server.py` | 本机服务（网页控制台 + API + whisper/翻译/封装管线）|
| `audio_stream.py` | **音频桥核心**：消费浏览器推来的 PCM，驱动实时字幕管线 |
| `browser_audio_bridge.py` | PCM 入队与队列保护（丢最旧块并计数）|
| `BrowserAudioBridge.user.js` | 浏览器端脚本：抓音、重采样、字幕条、控制面板 |
| `BrowserCompanion.user.js` | 另一条路：iframe 里叠加字幕（配合「粘贴链接」模式）|
| `requirements.txt` | Python 依赖 |
| `tests/` | 端到端 / 浏览器端重采样测试（可选，不参与运行）|

## 它是怎么工作的

```
网页播放器 ──captureStream──> AudioWorklet（重采样成 16kHz 单声道 PCM16）
        │                               │  每 0.5 秒一块
        │                               v
        │                     http://127.0.0.1:<port>/api/browser-audio/push
        │                               v
        │              audio_stream.py：不重叠定长窗口 → Whisper 识别
        │                               │  只提交完整词、按标点切断句
        └──── 字幕条 <──── /api/companion/sync（复用原有字幕管线）
```

## 实测（4 核沙箱，Apple Silicon 上更快）

- 27.4 秒语音按真实时间推流 → 覆盖 0.00→26.87 秒，**词覆盖率 96.5%**
- **平均延迟 4.6 秒**，首条字幕约 4 秒出现
- 解码速度（6 秒窗口）：`tiny` 0.3s / `base` 0.5s / `small` 1.2s

## 隐私与边界

- 声音只发送到你自己电脑上的 `127.0.0.1`，**不经过任何第三方服务器**；不保存音频文件（识别后即丢）。
- 不使用任何绕过 DRM、付费墙或访问控制的手段。受 DRM / 跨域保护（如 Netflix）的播放器抓不到声音，会有明确提示，可改用「粘贴链接」模式。
- 字幕为机器识别结果，可能有误；请勿用于需要精确引用的场合。

## 许可

随原始项目提供，仅供个人学习与自用。
