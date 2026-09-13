# 音频桥测试脚本（可选，给开发者用）

这三个脚本是搭这套音频桥时用的端到端验证，不参与软件运行，删掉也不影响使用。

- `serve.py` —— 起一个只用于测试的本机服务（可指定端口与本地模型目录，跳过联网下载模型）
- `test_audio_bridge.py` —— 模拟浏览器：开音频会话 → 按真实时间推 16kHz PCM → 轮询字幕 → 停止 → 导出 SRT，并断言覆盖范围 / 时间戳 / 延迟
- `bridge_client.mjs` —— 用 Node 运行 `BrowserAudioBridge.user.js` 里**真实的重采样与分块代码**（48kHz → 16kHz），把结果推给服务端，统计词覆盖率

跑法（需要已装 faster-whisper 的 Python 环境，以及一份 CT2 模型目录）：

```bash
# 终端 1：起测试服务
TEST_MODEL_DIR=/path/to/faster-whisper-base AS_TEST_PORT=8767 python3 tests/serve.py

# 终端 2：端到端
TEST_MODEL=base TEST_MODEL_DIR=/path/to/faster-whisper-base python3 tests/test_audio_bridge.py

# 终端 2：浏览器端脚本（需要 Node 18+，脚本里的端口/模型按需修改）
node tests/bridge_client.mjs
```
