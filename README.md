# type4me-linux

Linux 本地语音输入工具骨架，参考 Type4Me 的 SenseVoice + Qwen3-ASR 双引擎设计，但面向 NixOS/Wayland。

第一版目标很明确：

- 通过 Nix 安装一个 `type4me-linux` 命令。
- 用 `pw-record` 录音，默认 16 kHz mono PCM wav。
- 用 `sherpa-onnx-offline` 跑 SenseVoice ONNX 模型做快速识别。
- 可选调用本机 Qwen3-ASR HTTP 服务做最终校准。
- 用 `wtype` 注入文字，失败时回退到 `wl-copy`。
- 没有模型时也能用 fake 后端跑完测试和状态机。

## 当前边界

这不是 Type4Me 的 Swift/macOS 移植。当前仓库只实现 Linux 可安装的核心管线和服务接口：录音、识别、文本处理、注入、daemon HTTP API。GUI、托盘、热键和模型下载器留到后续迭代。

## 运行

```bash
nix run . -- doctor
nix run . -- doctor --allow-missing-models
nix run . -- transcribe /path/to/audio.wav --backend fake
nix run . -- inject "你好"
```

真实 SenseVoice 后端需要模型目录包含：

```text
model.onnx
tokens.txt
```

默认模型目录是：

```text
$XDG_DATA_HOME/type4me-linux/models/sensevoice-small
```

也可以在配置中覆盖。

## 配置

默认读取：

```text
$XDG_CONFIG_HOME/type4me-linux/config.toml
```

示例：

```toml
[asr]
backend = "hybrid"
language = "zh"
model_dir = "~/.local/share/type4me-linux/models/sensevoice-small"
use_qwen_final = true
qwen_endpoint = "http://127.0.0.1:8765/transcribe"
hotwords = ["Qwen3-ASR", "SenseVoice", "NixOS"]

[inject]
prefer = "wtype"
clipboard_fallback = true

[daemon]
host = "127.0.0.1"
port = 8766
```

## Nix 安装

开发验证：

```bash
nix flake check
nix build
```

临时使用：

```bash
nix run github:YOUR_NAME/type4me-linux -- doctor
```

Home Manager 可使用仓库提供的模块：

```nix
{
  inputs.type4me-linux.url = "github:YOUR_NAME/type4me-linux";

  outputs = { type4me-linux, ... }: {
    homeConfigurations.vitus = home-manager.lib.homeManagerConfiguration {
      modules = [
        type4me-linux.homeManagerModules.default
        {
          programs.type4me-linux = {
            enable = true;
            service.enable = true;
            settings.asr = {
              backend = "hybrid";
              language = "zh";
              use_qwen_final = true;
              qwen_endpoint = "http://127.0.0.1:8765/transcribe";
            };
          };
        }
      ];
    };
  };
}
```

## 参考

- Type4Me: https://github.com/joewongjc/type4me
- SenseVoice: https://github.com/FunAudioLLM/SenseVoice
- sherpa-onnx: https://github.com/k2-fsa/sherpa-onnx
- Qwen3-ASR: https://github.com/QwenLM/Qwen3-ASR
- wtype: https://github.com/atx/wtype
