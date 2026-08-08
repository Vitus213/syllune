## Context

当前 `RawCaptureSession` 已以 32 ms PCM16 块读取 PipeWire 音频，但 `SenseVoiceVadStreamer` 为每个局部阈值创建新的离线 SenseVoice 流并重解码不断增长的 VAD 段。`VoiceInputPipeline` 已在常驻进程中复用 SenseVoice 和可选 Qwen 提供方，但每个会话仍只拥有模拟流式 VAD 状态。现有事件协议已有 `partial_text`、追加式 `confirmed_segments`、最终文本和一次注入边界。

用户已批准两个行为 capability：全量 Syllune 身份切换与 CLI 原生在线局部识别。目标为普通话加英文术语、本机运行、CPU 基线与可选 CUDA 加速；默认最终准确性继续以 SenseVoice 为基线。

## Goals / Non-Goals

**Goals:**

- 满足 `syllune-identity-migration` 的全量、无旧入口身份切换。
- 满足 `native-cli-streaming-recognition` 的原生在线局部事件、SenseVoice 默认最终文本和一次注入。
- 保持现有 JSON 字段和事件顺序，支持 CPU 与可选 CUDA。
- 以可选真实模型基准验证已批准的 p95 和准确率目标。

**Non-Goals:**

- 云端识别、远端 API、自动导入旧 XDG 数据或旧命令兼容别名。
- 在本轮新增 GUI 流式展示、时间戳、置信度或多说话人协议。
- 将 Fun-ASR/vLLM 服务作为默认运行时，或用 Qwen 取代默认最终路径。

## Decisions

### 1. 采用全量 Syllune clean cutover

源包、导入、脚本入口、`pname`、flake 属性、overlay、Home Manager 选项、systemd 服务、XDG 子目录、桌面 ID、D-Bus 名称、资源安装路径、文档和测试全部改为 Syllune/syllune。旧名称不保留 re-export、命令别名、XDG 回退或自动迁移。旧目录只由用户自行备份或迁移，因此回滚到旧发布物不会损坏其原有状态。

选择理由：用户明确要求破坏性全量迁移；双入口会制造第二个公共契约和持续维护成本。拒绝只改 CLI 的过渡方案，因为它会留下品牌、路径和系统集成分裂。

### 2. 使用 Sherpa 原生 OnlineRecognizer 作为局部路径

新增在线 Paraformer 中英 int8 模型的受控目录条目，并在常驻在线提供方中使用 Sherpa `OnlineRecognizer` 与每会话独立的 `OnlineStream`。采集块写入在线流，只有识别器就绪时才解码；变化的结果投影为 `partial_text`，识别器端点将稳定文本追加到 `confirmed_segments` 后重置在线流。

选择理由：当前打包运行时已包含 `OnlineRecognizer` 和 `OnlineStream`，官方模型支持普通话和英文，且该路径保留现有 Sherpa、模型校验和 CPU/CUDA 提供方边界。拒绝继续调低 SenseVoice 重解码间隔，因为它不会创建真正的增量状态。拒绝默认 Fun-ASR/vLLM 服务，因为官方实时路径依赖 CUDA/vLLM，无法单独满足 CPU 基线。

在线模型的 URL、精确大小、SRI SHA-256、允许成员、必需成员和许可证状态必须先从上游发布物获得并纳入 `ModelSpec`；缺少任一供应链字段时不实现或安装该模型。

### 3. 双路径会话：在线局部、SenseVoice 默认最终

新的流式会话组件同时保存在线 Paraformer 状态和每会话的 VAD/SenseVoice 最终状态。局部事件只来自在线结果；普通停止时，VAD/SenseVoice 完整消费会话音频并生成默认最终权威文本。现有显式 Qwen 校准保持在最终文本之后，失败继续遵循当前 `hybrid-fallback` 警告行为。

选择理由：局部反馈不再重复离线解码，最终文本保留已验证的 SenseVoice 质量基线。代价是会话期间维护两套模型状态和更高内存占用；必须通过真实硬件基准验证。

### 4. 完整低延迟采集与可信运行时复用

实时采集继续以 16 kHz、单声道 PCM16 和默认 32 ms 块运行，并由配置同时驱动 PipeWire 参数、WAV header 与 chunk 大小。每个非空且 PCM16 对齐的 EOF 尾帧必须在 flush 前同时送入在线局部与最终识别路径；奇数字节尾帧是可诊断错误，不能静默丢失或注入部分文本。

`ModelManager.resolve()` 只能复用与当前安全 `current` 指针和有效载荷相同的成功校验结果；显式 `check()` 始终完整扫描并哈希，检查失败、指针变化、安装、更新或移除必须使缓存失效。常驻桌面进程懒加载并复用 SenseVoice、可选 Qwen 和在线 recognizer；每个会话仍独立拥有 OnlineStream、VAD、去重、确认段和终止状态。拒绝 eager warm-up，以保持启动非阻塞。

选择理由：32 ms 输入与尾帧交付缩短可见与最终边界的等待，缓存和 provider 复用避免在连续会话中重复完整性扫描或模型装载，同时不把完整性检查降级为缓存命中。旧 SenseVoice 模拟 partial cadence 不再保留，因为局部文本只能来自 OnlineRecognizer。

### 5. 严格模型和错误边界

配置默认流式后端切换为在线模型。`doctor` 和模型管理器必须检查在线模型；流式会话若在线模型缺失、损坏或配置 provider 不可用，只发布现有错误/完成协议，不使用旧的模拟流式路径。`asr.final_backend` 只接受 `sensevoice` 或显式 `qwen3-sherpa`；旧 `none`、空值或未知值必须在配置加载时被拒绝，不提供兼容别名或自动重写。

### 6. 可选硬件基准而不改变事件 JSON

基准由独立的 opt-in 测试/脚本收集事件时间，不向稳定 JSON 事件添加字段。它使用版本化的普通话/英文术语语料清单和参考转写，在目标硬件上记录音频起点到首个非空局部事件、停止到最终事件、p50/p95、模型、提供方与准确率。CI 没有真实硬件或模型时显式 skip；确定性 fake 测试继续保护协议。独立 real-ASR smoke 只证明已安装模型可推理，不能替代性能或准确率基准。

## Risks / Trade-offs

- 在线 Paraformer 与 SenseVoice 的文本可能暂时不一致 → 局部文本明确可修订，最终文本唯一且只注入一次。
- 两套模型增加 CPU/内存压力 → int8 模型、单例识别器和目标硬件 p95 门禁；CUDA 只作加速，不替代 CPU 验收。
- 上游在线模型供应链信息不完整 → 未取得精确 SRI、大小、成员和许可证状态时停止模型目录变更。
- 全量更名使用户失去旧配置和模型发现 → 不自动删除旧目录，文档提供人工备份/迁移说明，发布说明突出 breaking change。
- Syllune 仅完成技术命名筛查 → 发布前必须完成商标和域名法律/运营检查；这不阻塞本地实现，但阻塞公开发布。

## Migration / Rollback

1. 在变更中先完成 Syllune 包、CLI 和系统集成的完整切换，再添加在线模型与原生流式路径。
2. 首次 Syllune 启动创建新的 XDG 根；它不读取或修改旧 Type4Me 根。用户按文档手动备份、复制或重新安装模型。
3. 发布前在目标 CPU 与 CUDA 机器运行模型完整性、事件协议和 opt-in 性能/准确率基准。
4. 回滚时安装前一版 Type4Me 发布物并继续使用尚未删除的旧目录；Syllune 新目录不被旧版读取。由于无兼容别名，回滚不会混合状态。

## Open Questions

- 负责人：维护者。实现在线模型目录前，取得并复核官方中英 Paraformer 发布物的精确供应链元数据；未完成时不得添加下载条目。
- 负责人：维护者。公开发布前完成 Syllune 商标和域名检查；未完成时可做本地开发验证，但不得宣称品牌法律可用。
- 负责人：维护者。建立版本化普通话/英文术语基准语料的授权和存储方式；未完成时不得宣称 p95 或准确率门禁已通过。
