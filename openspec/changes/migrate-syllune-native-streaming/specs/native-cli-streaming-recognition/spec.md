# Spec Delta

## ADDED Requirements

### Requirement: 原生在线 CLI 局部文本

在配置的在线模型、SenseVoice 和 VAD 均通过完整性校验后，`syllune stream --json` SHALL 以原生在线识别状态生成局部文本。局部文本 MUST 在语音仍在输入时更新，MUST 使用既有 `RecognitionEvent` 和 `RecognitionTranscript` 字段，且相同文本不得重复发布。在线端点已经确认的文本 MUST 仅以追加方式出现在 `confirmed_segments`。

#### Scenario: 语音输入期间发布变化的局部文本

- **WHEN** 用户在普通话夹英文术语的语音会话中持续输入，且在线识别器产生新的非最终文本
- **THEN** CLI MUST 在会话结束前输出一个或多个非最终 `transcript` JSON 事件，并在文本未变化时不额外输出重复事件

#### Scenario: 在线段落结束后确认文本

- **WHEN** 在线识别器报告一个语音端点且该段包含文本
- **THEN** CLI MUST 将该文本追加到 `confirmed_segments`，并继续允许后续语音产生新的局部文本

### Requirement: 默认准确最终文本与一次注入

普通停止时，Syllune SHALL 使用同一会话音频的 SenseVoice 最终文本作为默认权威文本；`asr.final_backend = "qwen3-sherpa"` SHALL 保持为显式最终校准选项。无论局部文本更新次数，CLI MUST 输出恰好一个最终 `transcript`、随后输出 `finalized` 和 `completed`，并且启用注入时最多注入一次最终权威文本。

#### Scenario: 默认策略在停止后完成

- **WHEN** 用户以第一次 `SIGINT` 正常停止默认 `syllune stream --json` 会话
- **THEN** 输出 MUST 包含恰好一个 `is_final: true` 且 `backend: "sensevoice-vad"` 的最终转写，随后依次出现 `finalized` 和 `completed`

#### Scenario: 显式 Qwen 校准成功

- **WHEN** 用户配置 `asr.final_backend = "qwen3-sherpa"` 并正常停止会话
- **THEN** 成功的最终转写 MUST 使用 `backend: "hybrid"`，且局部事件和最终注入次数保持不变

#### Scenario: 旧或未知最终后端被拒绝

- **WHEN** 用户配置 `asr.final_backend` 为 `none`、空值或其他未知值
- **THEN** 配置加载 MUST 以可诊断错误失败，且不得将该值解释为 SenseVoice 或 Qwen 策略

#### Scenario: 在线模型不可用

- **WHEN** 用户启动需要在线模型但该模型未安装、未通过完整性校验或配置 provider 不可用的流式会话
- **THEN** CLI MUST 发布一个可诊断的 `error`，随后发布 `completed`，且不得注入文本或隐式退回到旧的重复离线局部解码路径

### Requirement: 目标硬件上的性能和准确率验证

仓库 SHALL 提供可选的、可重复执行的本地基准流程，在普通话夹英文术语的带标注语料上记录首个非空局部文本和最终文本的延迟，并与变更前 SenseVoice 最终文本基线比较准确率。默认在线路径的验收 MUST 要求首个局部文本 p95 不超过 800 ms、停止到最终文本 p95 不超过 1.5 s，且准确率不低于基线；不具备模型或目标硬件的 CI MUST 明确跳过该基准而不宣称通过。

#### Scenario: 目标硬件基准通过

- **WHEN** 维护者在具备所需模型和目标硬件的环境中执行已记录的基准流程
- **THEN** 报告 MUST 包含每个样本的局部和最终延迟、聚合 p50/p95、运行模型和提供方，以及相对 SenseVoice 基线的准确率结果

#### Scenario: 非目标硬件持续集成

- **WHEN** 持续集成环境没有真实模型或目标硬件
- **THEN** 基准 MUST 被显式跳过，且确定性事件、错误和生命周期测试仍 MUST 运行

## MODIFIED Requirements

无。

## REMOVED Requirements

无。

## RENAMED Requirements

无。
