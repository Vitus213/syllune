# Spec Delta

## ADDED Requirements

### Requirement: 完整的低延迟音频输入

Syllune SHALL 默认以 16 kHz、单声道 PCM16 和 32 ms 采集块处理实时语音，并将有效的实时采集配置一致地传递给 PipeWire 采集命令和会话 WAV。实时识别 MUST 拒绝不满足 PCM16、16 kHz 或单声道契约的配置。非空且 PCM16 对齐的 EOF 尾帧 MUST 在最终识别前恰好交付一次；不完整的 PCM16 帧 MUST 产生可诊断错误且不得注入文本。

#### Scenario: 默认实时采集块

- **WHEN** 用户使用未覆盖 capture 配置启动 `syllune stream`
- **THEN** 采集命令和 WAV MUST 使用 16 kHz、单声道 PCM16 及 32 ms 块

#### Scenario: 对齐尾帧到达最终识别

- **WHEN** 实时采集以非空且 PCM16 对齐的尾帧结束
- **THEN** 该尾帧 MUST 在会话 flush 前恰好交付给在线局部路径和最终识别路径

#### Scenario: 不完整 PCM16 尾帧

- **WHEN** 实时采集以奇数字节数的尾帧结束
- **THEN** 会话 MUST 报告可诊断错误，且不得发布最终文本或执行文本注入

### Requirement: 可信的常驻实时运行时

在模型 current 指针和完整性保持不变时，Syllune SHALL 在同一常驻进程的连续实时会话间复用已验证的模型解析结果和已加载 provider；每个会话 MUST 保持独立的在线流、VAD、去重、确认段和终止状态。显式模型完整性检查 MUST 始终重新验证有效载荷；检查失败、模型指针变化或模型移除后，后续解析 MUST 不返回过时的已验证路径。

#### Scenario: 连续会话复用运行时但隔离状态

- **WHEN** 用户在同一常驻 Syllune 进程中顺序完成两个实时会话，且模型指针未变化
- **THEN** 两个会话 MUST 复用已验证的模型和 provider，并拥有互不共享的实时会话状态

#### Scenario: 显式检查发现缓存后的损坏

- **WHEN** 已解析模型在后续显式完整性检查中被发现损坏
- **THEN** 检查 MUST 报告损坏，且后续解析或会话 MUST 不返回此前缓存的路径

## MODIFIED Requirements

无。

## REMOVED Requirements

无。

## RENAMED Requirements

无。
