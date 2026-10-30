# 模型连接中心

在 Codex 中管理用户自己的模型连接、模型目录和子代理调度。连接可添加、编辑、停用、重新启用或删除；不同连接使用相同模型时分别保留身份。实际可用性由所选接口、用户设置和供应商返回决定。

## 使用

在“模型连接”中添加连接，选择接口类型并填写地址、模型与凭据。支持通用 Responses、Chat Completions、Anthropic Messages、Gemini 原生及 Jev System One 接口；接口名称表示协议能力，不代表预设或要求使用某条线路。密钥只在本机界面输入，不应发送到聊天。

一个连接可包含多个模型。新增模型保存前会进行短测试；测试可能产生供应商用量。已启用连接可停用或删除，正在使用的连接会先提示影响。连接状态、模型设置和凭据保存在当前用户的本机数据目录，不随源码仓库分发。

窗口、压缩阈值和思考档位可按模型配置。用户填写值优先；服务商模型目录提供可识别的元数据时可用来补充缺失项。连接中心不为未知能力写入个人型号或固定窗口；宿主或内核可能使用自己的占位值，不能把占位值或一次短测试当作已验证的供应商能力。

## 子代理调度

自动模式从已启用且有可信身份的模型中，结合用户保存的偏好、用途样例和可比较的公开数据选择；没有保存偏好时不预设具体型号。手动模式遵守用户保存的首选和备选，本轮明确指定的模型和档位不会静默替换。所选模型仍需通过目录、停用状态、绑定和必要的父会话出口检查。

V2 任务正文无法可靠关联时不会补造内容；必要的任务交接检查与个人模型偏好独立。旧会话可能继续使用已加载的旧工具或进程，升级后以实际宿主重新加载结果为准。

可选的本机策略文件为 `%LOCALAPPDATA%/CodexProviderHub/model-policy.json`，用于保存用户自己的原生模型家族和档位偏好；不存在时采用中立策略。该文件不属于源码，不应公开或上传。格式示例：

```json
{"schemaVersion":2,"allowedFamilies":[],"allowedEffortsByFamily":{},"jevAdvisory":{"enabled":false}}
```

宿主原生模型的身份来自宿主模型缓存；升级时可用本机 `native-models.json` 保存需要保留的旧宿主模型身份，格式例如 `{"schemaVersion":1,"models":["host-model-a"]}`。连接别名与宿主身份冲突时会拒绝路由，避免发送到错误线路。该文件仅保存本机配置，不随源码发布。

## Jev 和媒体

Jev 的结构化决策、路由建议、证据筛选、转写排序、画面筛选和使用记录集成在插件中，使用用户选择的 Jev 连接。文字、转写、画面或音视频发送到供应商前，应确认当前任务的外发授权；模型建议不能代替原始证据或用户判断。详见 [Jev 工具说明](jev_tools/README.md)。

Gemini 原生连接可通过 `analyze_gemini_media` 分析用户明确指定的本机音频或视频，文件大小上限为 20 MB。媒体请求会发送所选文件并可能产生用量；音视频能力以实际线路为准。

公开评测仅供参考，不能证明线路质量或模型授权。默认按准确型号查询公开目录；可选的 `%LOCALAPPDATA%/CodexProviderHub/public-reference-sources.json` 允许用户指定额外公开来源，格式和网络边界见源码说明。配置只在本机保存，源站查询不使用供应商凭据。

例如，可按需在该文件中配置公开模型目录或评测页，名称、网址和列名均由用户决定：

```json
{"schemaVersion":1,"allowedReportHosts":["reports.example.org"],"models":{"model-a":{"type":"huggingface","repository":"publisher/model-a"},"model-b":{"type":"report","url":"https://reports.example.org/public","column":"Model B"}}}
```

## 源码运行条件

当前实现面向 Windows x64，需要 Python 3.12、Node.js（可通过 PATH 找到）和支持本地插件的 Codex。创建 `%LOCALAPPDATA%/CodexProviderHub/runtime` 虚拟环境并安装 `requirements.txt`。本机路由还需要 [OpenCodex v2.93.0 Windows 发行包](https://github.com/lidge-jun/opencodex/releases/tag/v2.93.0)中的 `ocx.exe` 和对应 `keyring` 组件，放入 `%LOCALAPPDATA%/CodexProviderHub/engine-bin/v2.93.0/`；启动时会校验文件哈希。

本仓库提供源码和本机插件包；全新 Windows 环境的一键安装尚未验证。Node 测试使用 `npm test`，界面测试需要 Playwright 和可用的 Chrome（可用 `CHROME_BIN` 指定路径）。

语音能力测试音频不随仓库分发。需要时，在装有中文系统语音的 Windows 上运行：

```powershell
powershell -NoProfile -File .\scripts\generate-speech-sample.ps1
```

脚本在本机生成样本并更新校验值。运行 Jev 隔离测试：

```powershell
& "$env:LOCALAPPDATA/CodexProviderHub/runtime/Scripts/python.exe" -B -m unittest test_jev_consolidation test_jev_hub
& "$env:LOCALAPPDATA/CodexProviderHub/runtime/Scripts/python.exe" -B -m unittest discover -s jev_tools -p 'test*.py'
```

## 数据和许可

连接密钥以当前 Windows 用户的 DPAPI 加密保存在本机，活动模型的凭据引用可供内核使用；同一 Windows 账户中的程序仍可能访问该账户的凭据。公开仓库不包含密钥、连接数据库、会话记录或运行日志。

项目自有源码使用 [MIT 许可](LICENSE)。适配或参考的第三方项目及许可保存在 [third_party](third_party/NOTICE.md)，包括 OpenCodex、CC Switch 和 Jev 相关项目；分发其他第三方二进制时也须保留其发行包中的许可声明。
