# asr Specification

## Purpose
TBD - created by archiving change cloud-asr. Update Purpose after archive.
## Requirements
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

