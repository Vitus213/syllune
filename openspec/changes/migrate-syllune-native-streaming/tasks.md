## 1. Syllune 全量身份切换

- [ ] 1.1 [Requirement: Syllune 对外身份；Scenario: 新 CLI 和系统集成可识别] 先为 `syllune` CLI、Python 包、Nix/Home Manager、桌面和 D-Bus 标识写失败测试；完成一次无旧别名的全量重命名并使新入口可运行。验证：`nix develop -c python -m pytest --no-cov tests/test_cli.py tests/test_paths.py tests/test_control_bus.py tests/test_desktop_view.py tests/test_home_manager.py`。
- [ ] 1.2 [Requirement: 干净的持久化状态边界；Scenario: 仅存在旧状态目录时首次启动] 先写旧 XDG 根不被读取或修改的失败测试，以及迁移文档断言；完成新的 Syllune 路径和手动迁移说明。验证：`nix develop -c python -m pytest --no-cov tests/test_paths.py tests/test_config.py tests/test_vocabulary.py tests/test_history.py`。

## 2. 低延迟输入与常驻运行时

- [ ] 2.1 [Requirement: 完整的低延迟音频输入；Scenario: 默认实时采集块；Scenario: 对齐尾帧到达最终识别；Scenario: 不完整 PCM16 尾帧] 先补严格 16 kHz/单声道/PCM16 配置、默认 32 ms 捕获、配置驱动 argv/WAV 和尾帧传播的行为测试；完成缺失的配置与采集实现，确保在线和最终路径都接收对齐尾帧。验证：`nix develop -c python -m pytest --no-cov tests/test_config.py tests/test_capture_stream.py tests/test_session.py tests/test_pipeline.py`。
- [ ] 2.2 [Requirement: 可信的常驻实时运行时；Scenario: 连续会话复用运行时但隔离状态；Scenario: 显式检查发现缓存后的损坏] 先补模型解析缓存失效、显式完整检查、懒加载 provider 复用和每会话状态隔离的行为测试；完成缺失的锁定、失效与 desktop ownership 实现。验证：`nix develop -c python -m pytest --no-cov tests/test_model_manager.py tests/test_pipeline.py tests/test_desktop_view.py`。

## 3. 受控在线模型与原生局部识别

- [ ] 3.1 [Requirement: 原生在线 CLI 局部文本；Scenario: 语音输入期间发布变化的局部文本] 先在取得并复核在线 Paraformer 的 URL、SRI、大小、成员和许可证状态后，为模型目录、配置、provider/doctor 不可用路径写失败测试；最小实现受完整性校验保护的在线模型与 CPU/CUDA 配置。验证：`nix develop -c python -m pytest --no-cov tests/test_model_catalog.py tests/test_model_manager.py tests/test_config.py tests/test_doctor.py`。
- [ ] 3.2 [Requirement: 原生在线 CLI 局部文本；Scenario: 语音输入期间发布变化的局部文本；Scenario: 在线段落结束后确认文本] 先以记录型 Sherpa 在线假体写 ready/decode、局部去重、端点确认、每会话 OnlineStream 隔离和常驻 online recognizer 复用的失败测试；最小实现原生在线流生命周期，不得保留 SenseVoice 模拟 partial cadence。验证：`nix develop -c python -m pytest --no-cov tests/test_providers.py tests/test_session.py tests/test_pipeline.py tests/test_cli_stream.py`。

## 4. 最终准确性、错误语义和真实基准

- [ ] 4.1 [Requirement: 默认准确最终文本与一次注入；Scenario: 默认策略在停止后完成；Scenario: 显式 Qwen 校准成功；Scenario: 旧或未知最终后端被拒绝；Scenario: 在线模型不可用] 先写默认 SenseVoice、显式 Qwen、无效最终后端、缺模型/provider 错误和一次注入的失败测试；完成双路径会话并保留 JSON 事件顺序。验证：`nix develop -c python -m pytest --no-cov tests/test_config.py tests/test_session.py tests/test_pipeline.py tests/test_cli_stream.py tests/test_integration_cli.py tests/test_e2e_cli.py`。
- [ ] 4.2 [Requirement: 目标硬件上的性能和准确率验证；Scenario: 目标硬件基准通过；Scenario: 非目标硬件持续集成] 先定义可选基准语料清单、real-ASR health smoke、事件计时报告和 skip 条件的失败测试；实现 Syllune 文档化验证流程，在目标 CPU 与 CUDA 环境记录 p50/p95 和相对 SenseVoice 基线准确率。验证：`SYLLUNE_REAL_ASR=1 nix develop -c python -m pytest --no-cov -m real_asr tests/test_real_asr_smoke.py`；`SYLLUNE_STREAM_BENCHMARK=1 nix develop -c python -m pytest --no-cov -m stream_benchmark tests/test_stream_benchmark.py`；完整回归：`nix develop -c python -m pytest && nix flake check -L`。
