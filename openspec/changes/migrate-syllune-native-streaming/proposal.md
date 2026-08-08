## Why

当前实时路径每次局部更新都会重新离线解码不断增长的语音段。即使采集块已缩短，用户仍不能获得原生在线识别器应有的连续局部文本。对于普通话夹英文术语的本地语音输入，这既拖慢可见反馈，也让最终准确性路径与实时反馈相互牵制。

现有项目名称和命令 `type4me-linux` 不符合产品方向。用户已明确选择 `Syllune` / `syllune`，并接受一次全量、无旧命令别名的破坏性切换。现在处理可避免在新 CLI 实时入口稳定后再经历第二次名称与路径迁移。

## What Changes

- **BREAKING**：将项目、CLI、Python 包、XDG 路径、Nix/Home Manager 标识、D-Bus 标识、桌面标识和文档从 Type4Me/type4me-linux 全量切换为 Syllune/syllune；旧命令和旧持久化路径不提供兼容别名或自动导入。
- 为 `syllune stream` 新增基于 Sherpa 原生在线识别器的普通话/英文术语局部文本路径。
- 保留 SenseVoice 作为默认最终文本的准确性基线；Qwen 继续是显式的最终校准策略。
- 为目标硬件建立可重复的局部文本、最终文本和准确率基准验证流程。
- 将实时采集、尾帧交付、模型解析缓存和常驻 provider 复用固化为低延迟且不弱化完整性校验的运行时契约。

## Capabilities

### New Capabilities

- `syllune-identity-migration`：以 Syllune 对外身份完成干净的破坏性名称与持久化边界切换。
- `native-cli-streaming-recognition`：在保持既有事件和一次最终注入语义的前提下，为 CLI 提供原生在线局部识别和 SenseVoice 最终文本。
- `live-recognition-latency`：保证完整的低延迟音频输入及可信、隔离的常驻实时运行时。

### Modified Capabilities

无。当前仓库不存在 OpenSpec 基线 capability；现有对外行为在新 capability 的完整契约中定义。

## Impact

受影响方包括所有 CLI 用户、Nix flake 消费者、Home Manager 用户、Sway 绑定、D-Bus 调用方、GUI 用户和依赖现有 XDG 配置、模型、历史或词汇目录的用户。旧目录保留在磁盘但不再被 Syllune 读取，用户必须按迁移文档自行处理数据和模型。实时路径新增受完整性校验保护的在线模型依赖，并增加目标硬件上的可选基准运行。
