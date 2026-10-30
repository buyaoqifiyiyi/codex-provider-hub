# 合成能力样例 v1

由调度入口和 Gemini 媒体入口按需调用的小样本探针。结果通过能力记录与实际线路、用途、路径和强度绑定；样例通过并不证明全面能力。

```python
probe_capability(store, alias, purpose, effort=None, *, transport=None,
                 media_inspector=None, provider_id=None, model=None, deadline=None,
                 expected_binding=None)
probe_gemini_media(store, provider_id, model, purpose, *, deadline=None, transport=None,
                   media_inspector=None)
probe_gemini_snapshot(store, profile, secret, model, purpose, *, deadline=None,
                      transport=None, media_inspector=None)
```

`store` 使用现有 ProfileStore 的只读接口。直接媒体函数不要求 alias；明确选择该连接中已配置的 model。通用入口的媒体用途也可通过 alias 选择，但 alias 不代表媒体结果走过子代理。tool/image 始终要求当前有效的 engine alias，使用本机 gateway-state.json 的 loopback 端口、DPAPI 读取 token 和 `/v1/responses` 流接口；不解密供应商密钥。直接媒体才通过 resolve_profile/resolve_route 安全取得密钥，复用 gemini_media.inspect_media，与 analyze_gemini_media 同一媒体实现。

| purpose | channel | 样例通过判据 | scope |
| --- | --- | --- | --- |
| tool | subagent_gateway | 完成 function_call，key=sample；回传随机 receipt，第二次完整返回必须与该 receipt 精确一致 | sample |
| image | subagent_gateway | 64×32 RGB PNG：左半 red、右半 blue，结构化答案精确匹配 | sample |
| video | gemini_media | 红圆向右移动、最终位于蓝方右侧、黄三角静止；忽略视频配音 | visual_sample |
| speech | gemini_media | 编号整数 417、代号 海风；仅证明转写样例 | transcription_sample |
| audio | gemini_media | 3 次提示音，音高递增，无语音 | non_speech_sample |

语音样例即使由供应商内部转录后输入模型，通过也只表示转写，不能证明听原音、音乐或环境声理解。video 只测试画面；audio 独立测试非语音。一般音视频路径及当前音频子代理输入均不由本探针支持或认证；非 gemini_native 媒体连接返回 error/unsupported_path，不隐式转换协议。Gemini 媒体不发送强度，结果 effort 永远为 None；传给通用入口的合法 effort 在媒体用途上被忽略。

返回只包含 status、reason、channel、effort、sample_id、scope，不含原始响应、异常文本、地址、模型名称或凭据。reason 是 capability_probes.REASONS 中有限枚举。passed/sample_match 仅证明本样例；failed/semantic_mismatch 仅用于完整且格式可判定的错误答案或未完成要求的工具行为。HTTP 200、文本 delta、工具调用第一步成功均不足以通过。无响应、超时、401/403、429、服务错误、格式错误、未完整结束等返回 error。它们不表示模型能力失败。非语音明确返回 unavailable=true 时该样例 failed；无法解析的自由文本回答为 error/format。

每次网络请求最多 25 秒，完整探针共享调用方的截止时间；正常请求使用可终止并回收的独立进程，密钥只经本机进程间管道传递。响应体最多 256 KiB，结构化答案最多 8 KiB。网关每次最多请求 512 输出 token；原生媒体沿用现有 helper 的 4096 上限，字节上限仍由探针执行。工具最多两次请求，无重试。默认 transport 禁止重定向；本机网关不走环境代理，媒体线路沿用本机代理设置。注入的 transport/inspector 为可信测试接缝，必须遵守 timeout；无法强制取消不合作的注入函数。已交给服务商的请求即使本地超时仍可能继续并计费。

`capability_service.py` 在调用前后检查身份和绑定、用数据库租约避免并发重复测试，并保存有限元数据。`gemini_media` 和 `subagent_gateway` 分别记录，speech 通过不代表 audio 通过。本模块自身不直接写证据。

子代理网关样例每次发送前检查网关是否支持绑定校验，并将当前版本和账户绑定交给网关核对；旧网关或线路变化时不会继续发送。媒体样例使用原子读取的线路和密钥快照，不复用其他线路的记录。

资产：two-colour.png 使用 stdlib struct/zlib 生成；tone-sample.wav 使用正弦波合成；video-sample.mp4 的几何画面使用 Pillow 和 FFmpeg 生成，分发版移除了系统语音轨道，画面帧保持不变。audio-sample.wav 使用 Windows 中文系统语音在本机生成，不随仓库分发，可运行 `scripts/generate-speech-sample.ps1` 创建并更新 manifest.json。manifest.json 记录 SHA-256，运行前校验；单元测试还核对 PNG 像素和提示音波形。video/speech 的真值来自样例内容，不依赖旧供应商返回。

从仓库检出后，先按上面的说明生成语音样本，再进行本地验证（禁止写入 pycache）：

```powershell
& "$env:LOCALAPPDATA/CodexProviderHub/runtime/Scripts/python.exe" -B -m unittest test_capability_probes -v
```

测试只使用假 store、假凭据、注入的假响应，以及临时 loopback 假网关；不读取真实 token 或请求真实供应商。依赖项目已安装运行环境（系统 Python 可能缺少 zstandard）。隔离 mock 验证不等于真实引擎、真实模型、桌面会话或生产能力验证。
