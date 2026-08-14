# Syllune

Syllune 是面向 NixOS 与 Wayland 的原生 Rust 实时语音输入 CLI。它用 PipeWire（`pw-record`）采集 16 kHz 单声道 PCM16 音频，默认通过 DashScope 云端实时会话在说话期间持续转写，停止后冲刷尾帧并注入最终文本；也可以选择 Sherpa-ONNX 在线 Paraformer 的本地原生流式后端。

Syllune 由 `type4me-linux` 演化而来（`migrate-syllune-native-streaming` change）：运行时不再依赖 Python，也没有 GTK/Adwaita 桌面入口。

## 快速开始

```bash
nix develop
nix run . -- doctor

# 默认云端实时流式：说话即转写，Ctrl-C 停止并注入
nix run . -- stream

# 本地后端需要先安装在线 Paraformer 模型
nix run . -- model install streaming-paraformer-bilingual-zh-en
nix run . -- stream --backend local-streaming
```

## 命令

| 命令 | 说明 |
| --- | --- |
| `syllune stream` | 实时录音识别；`--mode`、`--json`、`--no-inject`、`--backend` |
| `syllune transcribe <wav>` | 批量转写 WAV（云端 DashScope 或本地 SenseVoice） |
| `syllune record --seconds N` | 定时录音后转写 |
| `syllune model list\|install\|check\|remove` | 模型目录管理（固定 URL、字节数、SRI SHA-256、成员白名单） |
| `syllune mode list\|reload\|add\|update\|remove` | 文本处理模式 |
| `syllune history list\|delete\|export\|totals\|usage` | 识别历史 |
| `syllune daemon` | headless daemon，导出 `dev.syllune.Daemon.Controller` D-Bus 接口 |
| `syllune doctor` | 检查 `pw-record`、`wtype`、`wl-copy` 与数据目录 |

停止语义：第一次 `Ctrl-C`（或第二次快捷键激活、采集 EOF）停止采集、冲刷尾帧、发送一次完成信号并注入唯一最终文本；第二次 `Ctrl-C` 或 `SIGTERM` 强制取消，不注入任何部分文本。

## 配置

配置文件位于 `~/.config/syllune/config.toml`（严格 TOML，未知键报错）：

```toml
[asr]
streaming_backend = "cloud-realtime"   # 或 "local-streaming"

[cloud]
api_key = "sk-..."                     # 文件权限必须不宽于 0600
realtime_endpoint = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
realtime_model = "qwen3-asr-flash-realtime"

[processing]
provider = "openai-compatible"         # none | openai-compatible | ollama
base_url = "https://..."
model = "..."
api_key_env = "MY_KEY"

[history]
enabled = true
```

旧 `cloud-vad`、`sensevoice-vad`、`final_backend` 配置会被明确拒绝，不会静默改写。

`local-streaming` 默认使用 ModelManager 安装的 `streaming-paraformer-bilingual-zh-en`；每次会话前重新校验模型完整性，损坏的模型不会被使用。

## 模式与注入

`quick` 模式零外部调用直接注入识别文本；其他模式（语音润色、提示词优化、翻译为英文）把最终文本交给 `[processing]` 配置的模型处理，处理失败时保留识别原文并输出 warning。最终文本最多注入一次；历史只记录成功的权威文本。

## 模型目录

| ID | 版本与字节数 | 固定源与 SRI SHA-256 |
| --- | --- | --- |
| `streaming-paraformer-bilingual-zh-en` | `asr-models-2024-03-10`；`1047319737` | <https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-paraformer-bilingual-zh-en.tar.bz2><br>`sha256-VGKh/OQmk96uVyrx6MRocSSxKqhf5h/00xaLtSgOIF8=` |

安装使用 HTTPS 固定源，下载后校验 SRI SHA-256 与字节数、解压时拒绝白名单外成员、写入按文件哈希的 manifest，并通过 `versions/<id>/<version>-<digest12>` + `current` 符号链接原子激活。许可证状态见 `syllune model list --json` 的 `license_status` 字段；发布前需独立核验。

## Home Manager

```nix
programs.syllune = {
  enable = true;
  settings.asr.streaming_backend = "cloud-realtime";
  service.enable = true;               # 常驻 syllune daemon
  shortcuts.sway.enable = true;        # Sway 快捷键 -> daemon Activate
};
```

## 从 type4me-linux 迁移

- Syllune 只使用 `syllune` 命名的 XDG 目录（`~/.config/syllune`、`~/.local/share/syllune` 等），**不会**自动读取、移动或删除旧 `type4me-linux` 目录；旧配置、模型、词汇和历史保留在原地。
- 手动迁移前提：
  - 配置：把 `~/.config/type4me-linux/config.toml` 中的 `cloud` 段复制到新文件，并把 `streaming_backend` 改为 `cloud-realtime` 或 `local-streaming`（旧枚举已删除）。
  - 模型：Syllune 的模型目录独立；运行 `syllune model install streaming-paraformer-bilingual-zh-en` 重新安装（旧模型文件可手动移动但不会自动接管）。
  - 历史：旧 SQLite 历史不会自动导入；如需保留可用旧工具导出 CSV。
- 回滚：安装旧版 `type4me-linux` 发布物即可，两个应用的状态目录互不干扰。

## 开发

```bash
nix develop
cargo test --all-targets
cargo fmt && cargo clippy --all-targets --all-features -- -D warnings
nix flake check -L
```

真实云端质量与延迟门禁（`benchmark asr`/`benchmark latency`，CER ≤ 0.02，stop→inject p99 ≤ 1.0 s）见 `docs/low-latency-rust-plan.md` 与 OpenSpec change；缺真实凭据或 Wayland 注入环境时只报告 skip，不产生通过结论。

## 相关文档

- 设计文档：`openspec/changes/migrate-syllune-native-streaming/`
- 低延迟方案：`docs/low-latency-rust-plan.md`
- PipeWire: <https://pipewire.org/>
- Sherpa-ONNX: <https://github.com/k2-fsa/sherpa-onnx>
- XDG Desktop Portal: <https://flatpak.github.io/xdg-desktop-portal/>
