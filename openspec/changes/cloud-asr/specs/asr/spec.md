# capabilities/asr/spec.md

## ADDED Requirements

### Requirement: 云端批量识别后端 `cloud`

批量转写后端 `cloud` MUST 通过云端模型返回文本。


支持通过配置把批量转写委托给云端模型。


#### Scenario: 配置启用后批量转写走云端

Given 配置 `asr.batch_backend = "cloud"` 且 `[cloud]` 节包含
`model = "qwen3-asr-flash-2026-02-10"`、`api_key_env = "BAILIAN_API_KEY"`，
以及环境变量 `BAILIAN_API_KEY`；
When 调用 `transcribe <wav>`；
Then 返回的 `RecognitionResult.text` 为云端模型转写文本，`backend == "cloud"`。

#### Scenario: 缺少 API 密钥时报错而非静默回退

Given `api_key_env` 指向的环境变量未设置；
When 执行云端转写；
Then 抛出带密钥提示的异常，且不执行任何本地识别。

#### Scenario: 网络或 HTTP 错误透传为异常

Given 云端接口返回 HTTP 4xx/5xx、连接超时或响应体非预期结构；
When 执行云端转写；
Then 抛出 `CloudASRClientError` 及其子类，消息包含请求上下文。

#### Scenario: 短时失败自动重试

Given 云端接口连续返回 429/5xx；
When 执行云端转写；
Then 客户端按退避策略重试（默认最多 3 次），超过次数后抛错。

### Requirement: 云端流式识别后端 `cloud-vad`

流式后端 `cloud-vad` MUST 以本地 VAD 分段并逐段云端转写。


支持实时语音经本地 VAD 分段后的云端逐段转写。


#### Scenario: 实时语音逐段云端转写

Given 配置 `asr.streaming_backend = "cloud-vad"` 且云端凭据有效；
When `stream` 会话接收音频块并完成 VAD 分段；
Then 每个确认片段经云端转写后进入 `confirmed_segments`，
事件中的 `backend == "cloud-vad"`。

#### Scenario: 局部转写去重更新

Given 实时会话持续输出 partial；
When 当前语音段的云端转写结果与上一轮一致；
Then 不重复发布相同 partial 的 transcript 事件。

#### Scenario: 会话结束返回最终文本

Given 会话 `flush()`；
When 尾部非空语音段转写完成；
Then 返回 `is_final: true` 的 `RecognitionTranscript`，`authoritative_text`
为全部确认片段。

#### Scenario: 云端失败发布 warning 不中断会话

Given 单个 VAD 片段云端转写失败；
When 会话继续；
Then 该片段标为失败并发布 `warning` 事件，后续片段继续处理，
`authoritative_text` 仅包含成功的片段。

### Requirement: `[cloud]` 配置节

`[cloud]` 配置节 MUST 支持按 url、apiKey 环境变量、model 三字段配置。


新增 `[cloud]` 配置节，按 url / apiKey 环境变量 / model 三个字段配置云端服务。


#### Scenario: 完整配置通过校验

Given TOML 包含 `[cloud]` 节：`base_url = "https://dashscope.aliyuncs.com"`、
`api_key_env = "BAILIAN_API_KEY"`、`model = "qwen3-asr-flash-2026-02-10"`、
`timeout_seconds = 60.0`；
When `load_config`；
Then 返回带 `cloud` 配置的 `Config`，默认值与上述一致。

#### Scenario: 未知键与非法值被拒绝

Given `[cloud]` 节包含未知键，或 `api_key_env` 不是合法环境变量名 /
`model` 为空 / `base_url` 为空 / `timeout_seconds` 非正数；
When `load_config`；
Then 抛出与现有配置一致的校验错误。

### Requirement: CLI 后端切换

CLI MUST 支持在本地与云端后端之间切换。


CLI 的批量与流式命令可在本地与云端后端间切换。


#### Scenario: 批量命令支持 cloud 后端

Given CLI 可执行文件；
When 运行 `transcribe a.wav --backend cloud` 或 `record --backend cloud`；
Then 命令成功并输出云端转写文本。

#### Scenario: 流式命令支持 cloud-vad 后端

When 运行 `stream --backend cloud-vad --mode quick --no-inject --json`；
Then 输出 `ready`、`transcript`、`finalized`、`completed` 事件流。

#### Scenario: 本地后端保持可用

Given 未配置 `[cloud]`；
When 运行 `transcribe a.wav --backend sensevoice` 与
`stream --backend sensevoice-vad`；
Then 行为与现状完全一致（本地模型路径、事件契约不变）。

### Requirement: 回归基准语料与度量

仓库 MUST 内置 dev/test 分离语料与可复现度量命令。


仓库内置 dev/test 分离的语音语料与可复现的度量命令。


#### Scenario: 开发集与测试集分离

Given 仓库 `scripts/asr_benchmark/corpus/` 下存在 `dev.jsonl` 与
`test.jsonl`，均含 `id`、`reference`、`voice` 字段；
When 运行 `generate.py`；
Then 为每个条目在 `audio/` 生成 16 kHz 单声道 PCM16 WAV，
重复运行不重复生成（缓存命中）。

#### Scenario: 基准命令可复现排名

When 运行 `benchmark.py --split test --models <候选> --out <报告>`；
Then 输出 JSON 报告，含每模型内容 CER、格式 CER、英文 WER、平均时延与
失败样本数；同一语料重复运行结果可比。

#### Scenario: 候选模型按优先级配置

Given `[cloud]` 的 `model` 可选以下键：
`qwen3-asr-flash-2026-02-10`（默认）、`qwen3-omni-flash`、
`qwen3.5-omni-flash`；
When `load_config` 校验 `cloud.model`；
Then 仅接受上述枚举，其它值报错。

## MODIFIED Requirements

### Requirement: 现有批量后端枚举扩展

`asr.batch_backend` 枚举 MUST 新增 `cloud` 并保留全部原值。


`asr.batch_backend` 枚举新增 `cloud`，保留全部原值。


#### Scenario: 枚举包含 cloud

Given `asr.batch_backend` 校验；
Then 可选值集合为
`{"fake", "sensevoice", "qwen3-sherpa", "hybrid", "cloud"}`
（保持向后兼容，原值不动）。

### Requirement: 现有流式后端枚举扩展

`asr.streaming_backend` 枚举 MUST 新增 `cloud-vad` 并保留原值。


`asr.streaming_backend` 枚举新增 `cloud-vad`，保留原值。


#### Scenario: 枚举包含 cloud-vad

Given `asr.streaming_backend` 校验；
Then 可选值集合为 `{"sensevoice-vad", "cloud-vad"}`。

### Requirement: 现有最终校准后端枚举扩展

`asr.final_backend` 枚举 MUST 新增 `cloud` 并保留原值。


`asr.final_backend` 枚举新增 `cloud`，保留原值。


#### Scenario: 枚举包含 cloud

Given `asr.final_backend` 校验；
Then 可选值集合为 `{"sensevoice", "qwen3-sherpa", "cloud"}`。
