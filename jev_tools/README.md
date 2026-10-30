# Jev 本机工具

Jev 工具集成在模型连接中心的 MCP 服务中，使用连接中心选定的 Jev 线路及凭据。服务自身不保存或打印密钥。更换提供商时以实际接口、模型和返回格式重新验证。

## 工具

- `jev_decide`：原有文本决策接口，按需作结构化复核。
- `jev_route_advice`：对任务摘要和已知事实提供路由建议。它不会切换模型或创建代理；阈值为初步试验值，用户明确指定、现有模型守卫、真实证据和可用模型目录优先。遇到权限/网络等外部阻塞应处理阻塞，不靠升级模型重复尝试。
- `jev_select_evidence`：只读取指定根目录内明确列出的文本文件，返回原文、路径和行号。默认本地筛选；仅在用户授权覆盖外发时设置 `allow_remote=true`，以便使用当前 Jev 对有界候选评分。结果中的截断、跳过和警告决定覆盖边界；无匹配不等于没有证据。
- `jev_rank_transcripts`：对已提供的转写候选，按任务目标、独立完整性和开头吸引力提出评分，保留原有源时间。需按现有用户授权明确设置 `allow_external=true`；批量请求控制大小，失败返回未评分状态。文字评分不代表已看过画面或听过声音，不会直接剪辑。
- `jev_screen_frames`：对用户明确选定的少量 JPEG/PNG 画面评分；按现有用户授权明确设置 `allow_external=true`。发送前在内存中缩放并去除元数据，实际图片内容会发至配置的提供商及上游；文件路径不会随请求发送。静帧只提供可见画面线索，不能证明连续动作、声音或成片效果。
- `jev_usage_status`：读取本地的实际 Jev 请求记录，不产生模型调用。记录仅含时间、工具、调用方式、模型、结果、耗时及返回的 token 数，不含问题、答案、路径、素材或密钥；无法补录启用前的历史，也不区分测试和真实任务。记录存放在当前 Codex 用户目录中的 `jev-activity.sqlite`；旧 MCP 进程未重载或记录写入失败时可能缺记录。

只使用必要且获准外发的材料；API 密钥及无关私人信息不应送入判断。模型分数是待核对的建议，不能代替测试、原始证据、权限判断或用户审片。第三方图片接口当前属实验能力，实际素材仍需在具体任务中验证。

## 旧会话兼容

如果当前 Codex 会话仍缓存旧的 MCP 工具列表，可以使用同目录的 `jev_cli.py` 调用同名工具：第一个命令行参数是工具名，参数对象通过标准输入传入 UTF-8 JSON；输出为 `{ok, result}` 或不回显输入的错误类型。使用 `%LOCALAPPDATA%/CodexProviderHub/runtime/Scripts/python.exe` 运行。该入口与 MCP 调用同一份实现、配置和外发边界，不启动额外服务。

## 方法来源与边界

按本机结构独立实现，参考 [Jev Codex Token Saver](https://github.com/jcressler/jev-codex-token-saver) 的有界证据、[Hermes Jev Skills](https://github.com/kerpopule/hermes-jev-skills) 的按需路由建议、[Jev Highlight Cutter](https://github.com/gnipbao/jev-highlight-cutter) 的可追溯候选及 [jevreel](https://github.com/DeepAleph/jevreel) 的静帧筛选思路。未覆盖原生 Codex 上下文压缩、剪辑宿主准入或现有模型守卫。

具体验收结果和当前聊天是否加载新增工具，以本次交付及实际宿主测试为准。合成样本的接口通过不等于真实项目效果或额度节省。
