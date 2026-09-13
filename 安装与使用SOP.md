# Auto Subtitle Sync v7.6 安装与使用 SOP

## 一、首次启动

1. 解压整个 `AutoSubtitleSync_Mac_v7_6_iframe_bridge` 文件夹，不要只拖出单个 `.command`。
2. 右键 `AutoSubtitleSync.command` → **打开**。
3. 如果 macOS Gatekeeper 阻止运行，进入“系统设置 → 隐私与安全性”选择“仍要打开”。
4. 已经安装过 v7.x 的用户，新文件夹仍会建立自己的 `.venv`；第一次启动会安装依赖。
5. 浏览器自动打开本地控制台即表示启动成功。

## 二、安装 Browser Companion

1. 浏览器安装 Tampermonkey。
2. 在本地控制台点击 **安装 Tampermonkey 脚本**，或直接打开文件夹中的 `BrowserCompanion.user.js`。
3. Tampermonkey 确认安装/更新脚本。
4. 关键：允许 Tampermonkey 在**当前网站及播放器 iframe 的域名**运行。若浏览器提供“站点访问”选项，推荐允许“所有站点”。
5. 刷新目标网页。

## 三、给 iframe 在线视频加字幕

1. 打开你有权访问的视频页面。
2. 先点网页自己的播放键，让视频实际播放 3–10 秒；这一步会让真实播放器 iframe、HLS/DASH 请求等完成初始化。
3. 点击右下角 **字幕助手**。
4. 应看到类似：
   - `已检测播放器 · player.example.com`
   - `已发现可供本地识别的媒体入口`，或
   - `媒体由浏览器动态播放；将尝试 iframe 页面解析`
5. 选择输出字幕语言。
6. Semantic Ahead 建议：
   - 60 秒：启动更快
   - 90 秒：默认推荐
   - 120 秒：翻译/长句更稳
   - 180 秒：更重视自然句质量
7. 点击 **在真实播放器上启用字幕**。
8. 程序会暂时暂停网页播放器，后台建立 Semantic Look-ahead；水位足够后自动恢复。

## 四、它如何处理跨域 iframe

顶层脚本不会强行读取跨域 iframe 的 DOM。v7.6 的 userscript 会分别运行在每个可访问 frame：

- 顶层 frame：负责 UI 与挑选播放器。
- 真正播放器 frame：负责读取自己的 `<video>`、暂停/恢复、Seek 监控和字幕 Overlay。
- frame 之间只通过 `postMessage` 交换播放器元数据和控制消息。

所以“页面只是外壳，真正视频在第三方 iframe”不再天然失败。

## 五、如果提示未检测到播放器

按顺序检查：

1. 视频是否已经实际开始加载/播放。
2. Tampermonkey 是否对播放器 iframe 的域名有站点权限。
3. 刷新页面后再等几秒。
4. 某些播放器不是 HTML5 `<video>`，或由 DRM/EME 控制；这类内容不会被强行绕过。

## 六、如果检测到播放器但本机解析失败

v7.6 会依次尝试：

1. 浏览器观察到的普通 HTTP 媒体入口；
2. 当前播放器 iframe URL；
3. 顶层页面 URL；
4. 可用的 Chrome / Safari / Edge / Firefox 浏览器 Session。

可运行 `diagnose_online.command` 检查 yt-dlp、FFmpeg 和浏览器 Profile。

## 七、本地文件功能

原有功能不变：

- 自动从 MP4 生成字幕；
- 中英德法西识别/翻译；
- 校准已有 SRT；
- 导出 SRT；
- 导出带可开关字幕轨的 MP4。
