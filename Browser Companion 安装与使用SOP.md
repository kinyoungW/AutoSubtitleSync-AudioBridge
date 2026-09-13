# Browser Companion v7.6 · Iframe Bridge SOP

## 安装

1. 启动 AutoSubtitleSync v7.6。
2. 安装 Tampermonkey。
3. 安装 `BrowserCompanion.user.js`。
4. 在浏览器扩展的“站点访问”中，允许 Tampermonkey 在当前网站和第三方播放器 iframe 域名运行；最省事的是允许“所有站点”。
5. 刷新网页。

## 使用

1. 先让网页视频播放几秒。
2. 点击页面右下角“字幕助手”。
3. Companion 会从顶层页面和所有 iframe Agent 中自动挑选面积最大的真实视频播放器。
4. 若显示“已发现媒体入口”，说明浏览器已观察到普通 m3u8/MPD/MP4 等媒体地址；若没有，也会尝试直接解析播放器 iframe 页面。
5. 选择 90–120 秒 Semantic Ahead，点击“在真实播放器上启用字幕”。
6. 字幕会直接覆盖在真正的视频 frame 上；即使播放器来自跨域 iframe，也不要求父页面读取子页面 DOM。

## 注意

- 如果右下角脚本存在，但一直“未检测到播放器”，最常见原因是 Tampermonkey 没有权限注入播放器 iframe 域名。
- `blob:` / MediaSource 不是可由本机 FFmpeg直接读取的媒体地址。v7.6 会继续寻找 m3u8/MPD 等网络线索，或回退到 iframe URL 解析。
- 不处理 DRM / EME 访问控制绕过。
