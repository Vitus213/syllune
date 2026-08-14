# Cloud ASR：接入百炼云端语音识别

## 问题

本地 ASR（SenseVoice / Qwen3-ASR-0.6B）在嘈杂或口音场景下准确率受限，
英文/数字混排与标点结构依赖离线模型能力。用户希望在 CLI 中接入云端语音识别，
配置方式与 omp 的 models 配置一致（url + apiKey + model），并可与本地后端一键切换。

## 调研结论（2026-08-13 实测）

- 百炼账号（`BAILIAN_API_KEY`，`https://dashscope.aliyuncs.com`）可用模型：
  `qwen3-asr-flash-2026-02-10`（专用 ASR）、`qwen3-omni-flash`、
  `qwen3.5-omni-flash`（对话 Omni），均可通过
  `POST /api/v1/services/aigc/multimodal-generation/generation` 以
  `messages[].content[].audio` 的 **base64 data URI** 直接传入本地 WAV，无需上传。
- `qwen-audio-3.0-asr-flash`、`fun-asr-flash-2026-06-15` 仅接受 URL 输入
  （实测 base64/本地直传均报 `UNSUPPORTED_FORMAT`），需要公网托管，不作为 CLI 候选。
- 实时 WebSocket 端点（paraformer realtime 系列路径）对该 key 不可达
  （404 / “不支持 http 调用”）；流式用本地 Silero VAD 分段 + 云端按段转写实现。
- 基准（36 样本 TTS 语料，dev/test 分离）：`qwen3.5-omni-flash` 内容 CER
  0.0089、`qwen3-omni-flash` 0.0090、`qwen3-asr-flash-2026-02-10` 0.0110 /
  时延 0.64s（最快，专用 ASR 无聊天漂移）、本地 `qwen3-sherpa` 0.0229、
  本地 `sensevoice` 0.1008。Omni 系需要固定“只转写”系统提示，否则问句会触发
  聊天回复。

## 变更内容

1. 新增 `[cloud]` 配置节：`base_url`、`api_key_env`、`model`、`timeout_seconds`。
2. `asr.batch_backend` 增加 `cloud`；`asr.streaming_backend` 增加 `cloud-vad`；
   `asr.final_backend` 增加 `cloud`。
3. 新增 `CloudASRClient`（stdlib urllib，重试/退避/超时）与
   `CloudASRProvider`（实现现有 `ASRProvider` 协议，batch 转写）。
4. 新增 `CloudVadStreamer`：复用 Silero VAD 分段，逐段云端转写，
   维持现有 `RecognitionTranscript` 事件契约。
5. CLI `transcribe/record --backend cloud`，`stream --backend cloud-vad`。
6. 基准工具 `scripts/asr_benchmark/`：dev/test 分离语料 + CER/WER/时延度量，
   支持候选模型回归对比。

## 成功标准

- 配置 `[cloud]` 后 `transcribe a.wav --backend cloud` 输出云转写文本与标点。
- `stream --backend cloud-vad` 输出与本地一致的 JSON 事件序列
  （ready → transcript → finalized → completed），backend 字段为 `cloud-vad`。
- 本地后端不受影响：未配置 `[cloud]` 时全流程仍走本地模型。
- `scripts/asr_benchmark/benchmark.py --split test` 可复现排名报告。
- 全量 pytest 通过，覆盖率 ≥ 90%。

## 非目标

- 不做 GUI；不做语音上传托管服务；不接入其他云厂商；
- 不替换本地模型下载/校验体系；不改变模拟流式 vs 真流式的对外契约。

## 决策记录（ADR）

- **D1** 密钥走环境变量（`api_key_env`），不落盘进 TOML —— 与 `[processing]` 一致。
- **D2 默认模型 `qwen3-asr-flash-2026-02-10`**：专用 ASR、无聊天漂移、
  时延最低；omni 系在文档中列为更高准确率选项（需系统提示）。
- **D3 流式 = 本地 VAD + 云端逐段转写**：真流式 WebSocket 对该 key 不可达，
  且保持与现有 `sensevoice-vad` 相同的事件契约，切换无感知。
- **D4 音频以 base64 data URI 直传**：零上传依赖，隐私更好、链路更简单。