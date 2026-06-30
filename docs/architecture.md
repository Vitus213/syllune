# 架构

`type4me-linux` 按 Type4Me 的本地双引擎思路拆分，但各层都保持 Linux/Nix 可替换：

- `Recorder`：调用 `pw-record` 生成 wav。
- `ASRProvider`：统一识别接口。
- `SenseVoiceProvider`：调用 `sherpa-onnx-offline`，读取 SenseVoice ONNX 模型。
- `Qwen3ASRClient`：调用本机 Qwen3-ASR HTTP 服务，用完整音频和 SenseVoice draft 做最终校准。
- `HybridProvider`：先跑 SenseVoice，再尝试 Qwen3-ASR；Qwen 不可用时返回 SenseVoice 结果。
- `TextInjector`：优先 `wtype`，失败回退 `wl-copy`。
- `VoiceInputPipeline`：组织 `音频 -> ASR -> 片段替换 -> 注入`。

## 为什么 Qwen3-ASR 先做 HTTP 客户端

Type4Me 的本地版本在 macOS Apple Silicon 上用 MLX 管理 Qwen3-ASR 服务。Linux + RTX 3070 更适合用 CUDA/Transformers/vLLM 或 sherpa-onnx 的 Qwen3-ASR ONNX 路径。第一版先固定协议边界，避免把重型模型运行时绑死在 CLI 内部。

## 模型管理

Nix 包不在构建期下载模型。模型属于运行时数据，默认放在：

```text
~/.local/share/type4me-linux/models/
```

这样可以保持 Nix 构建可复现，也方便在不同机器上复用同一个二进制包。

