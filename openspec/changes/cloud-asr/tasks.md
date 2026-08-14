# 任务：cloud-asr

按依赖顺序执行；每个任务 TDD（先写失败测试 → 最小实现 → 重构）。

## T1 配置节与枚举

- [x] RED：`tests/test_config.py` 新增 `[cloud]` 节用例：
      默认值、完整配置、未知键、空 model/base_url、非法 env 名、timeout 越界。
- [x] GREEN：`config.py` 增加 `CloudConfig` 与 `Config.cloud`，
      `_validate_config` 校验；`model` 枚举
      `{qwen3-asr-flash-2026-02-10, qwen3-omni-flash, qwen3.5-omni-flash}`。
- [x] 重构：`_build_section` 复用，无重复校验逻辑。

## T2 云端客户端

- [x] RED：`tests/test_cloud_asr.py`：请求形状（URL、Authorization、
      Content-Type、base64 data URI）、成功解析、空白文本报错、
      429/5xx 重试与退避、超时、401/403 鉴权错误、非预期 JSON。
- [x] GREEN：`src/type4me_linux/cloud_asr.py`：
      `CloudConfig` 迁移（或引用 config）、`CloudASRClient`（urlopen 注入
      以便测试）、异常层级。
- [x] 重构：重试逻辑独立函数，超时/尝试次数可注入。

## T3 批量 Provider 与后端枚举装配

- [x] RED：`create_provider` 支持 `batch_backend == "cloud"` 返回
      `CloudASRProvider`；`transcribe` 文本与 `backend=="cloud"`；
      密钥缺失抛 `CloudASRAuthenticationError`；`transcribe_samples`
      PCM16 往返。
- [x] GREEN：`providers.py` 新增 `CloudASRProvider`，`create_provider`
      分支；`ASRConfig.batch_backend` 枚举加 `cloud`。
- [x] 重构：与 `SenseVoiceProvider` 保持同一注入风格（client 可注入）。

## T4 流式 CloudVadStreamer

- [x] RED：`tests/test_providers.py` 或新文件：`cloud-vad` 事件序列、
      确认片段、partial 去重、flush 最终文本、单段失败发布 warning
      且不中断、（成功段才计入 authoritative）。
- [x] GREEN：`cloud_asr.py` 或 `providers.py` 新增 `CloudVadStreamer`
      （复用 VAD 工厂）；`streaming_backend` 枚举加 `cloud-vad`。
- [x] 重构：与 `SenseVoiceVadStreamer` 共享分段骨架（若代价合理）。

## T5 最终校准与 pipeline 装配

- [x] RED：`final_backend == "cloud"` 时 `_get_live_calibrator` 返回
      `CloudASRProvider`；`streaming_backend == "cloud-vad"` 时 session
      使用 CloudVadStreamer 且不初始化 SenseVoice。
- [x] GREEN：`pipeline.py` 装配分支；`local` 路径回归不变。
- [x] 重构：cloud 装配收敛到工厂函数，避免 pipeline 内 if 扩散。

## T6 CLI

- [x] RED：`tests/test_cli.py`：`--backend cloud`（transcribe/record）
      与 `--backend cloud-vad`（stream）可用；错误枚举仍报错。
- [x] GREEN：`cli.py` `BATCH_BACKENDS`/`STREAM_BACKENDS` 扩展。
- [x] 重构：无。

## T7 保质与文档

- [x] `README.md` 增加云端配置示例与后端切换说明；`model_catalog` 不涉及。
- [x] `doctor` 检查不因 cloud 后端误报模型缺失（`active_model_ids`
      保持本地模型集合即可）。
- [x] 全量 `pytest` 通过（覆盖率 ≥ 90%）；ruff 干净。
- [x] 真实冒烟：`transcribe --backend cloud` 与
      `stream --backend cloud-vad` 各跑一次；`benchmark.py --split test`
      复现报告。

## 验证门禁

- `nix develop --command pytest -q` 全绿。
- `nix develop --command ruff check .` 干净。
- 真实 API 冒烟成功（见 T7）。