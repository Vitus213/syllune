# type4me-linux

`type4me-linux` 是面向 NixOS 与 Wayland 的本地语音输入应用。它用 PipeWire 采集音频，以 SenseVoice、Silero VAD 和 Qwen3-ASR 的 `sherpa_onnx` ONNX 运行时完成识别，再按需执行片段替换、文本处理、历史记录和 Wayland 文本注入。仓库同时提供 CLI、常驻 Adwaita 桌面应用、本地 HTTP daemon、D-Bus 控制接口和 Home Manager 模块。

模型权重不进入 Nix store，也不随本仓库重新分发；应用从固定 HTTPS 目录下载经过固定 SHA-256 校验的上游产物，并保存在当前用户的 XDG 数据目录。

## 快速开始

```bash
nix develop
nix run . -- doctor --allow-missing-models

nix run . -- model install sensevoice-int8
nix run . -- model install silero-vad
nix run . -- model install qwen3-asr-0.6b-int8
nix run . -- model check sensevoice-int8 --json

nix run . -- stream --mode quick --no-inject --json
```

真实录音需要可用的 PipeWire 会话；文本注入需要 Wayland、`wtype`，或允许使用 `wl-copy` 的剪贴板后备路径。`doctor --allow-missing-models` 只允许三个模型尚未安装，`pw-record`、`wtype`、`wl-copy`、`wl-paste`、`sherpa_onnx`、XDG 目录和 GlobalShortcuts 门户检查仍须通过。

## 原生 Syllune 流式 CLI

Rust 原生 CLI 是独立的 `syllune` 包，当前提供 `stream` 和 `doctor` tracer；默认云端后端在说话期间持续发送 PCM，`local-streaming` 后端使用 Sherpa-ONNX 在线 Paraformer。它不依赖 Python 运行时，也不把模型权重打入 Nix store。

```bash
nix build .#syllune
nix run .#syllune -- --help
nix run .#syllune -- doctor
```

Rust 包当前没有自己的模型下载器。使用既有、带固定 URL/SHA-256、成员白名单和原子激活的 ModelManager 安装本地在线模型，再把激活目录交给 `syllune`：

```bash
nix run . -- model install streaming-paraformer-bilingual-zh-en
MODEL_DIR="$(readlink -f "${XDG_DATA_HOME:-$HOME/.local/share}/type4me-linux/models/streaming-paraformer-bilingual-zh-en/current")"
```

在配置文件中指定该目录：

```toml
[asr]
streaming_backend = "local-streaming"
local_model_dir = "/home/user/.local/share/type4me-linux/models/streaming-paraformer-bilingual-zh-en/current"
```

然后运行本地路径；第一次 `Ctrl-C` 冲刷并完成当前会话，第二次才取消：

```bash
nix run .#syllune -- --config ~/.config/type4me-linux/syllune.toml stream --json --no-inject
```

`local-streaming` 不建立云端连接。`cloud-realtime` 需要配置文件中的 `cloud.api_key`，且包含密钥的文件权限不得宽于 `0600`。

## 模型目录

内置模型目录包含四个稳定 ID：

| ID | 用途 | 必需运行时文件 |
| --- | --- | --- |
| `sensevoice-int8` | 批量识别和模拟流式草稿 | `model.int8.onnx`、`tokens.txt` |
| `silero-vad` | 语音活动检测 | `silero_vad.onnx` |
| `qwen3-asr-0.6b-int8` | 批量识别或流式会话最终校准 | `conv_frontend.onnx`、`encoder.int8.onnx`、`decoder.int8.onnx`、`tokenizer/merges.txt`、`tokenizer/tokenizer_config.json`、`tokenizer/vocab.json` |
| `streaming-paraformer-bilingual-zh-en` | Rust 本地原生流式识别（中英） | `encoder.int8.onnx`、`decoder.int8.onnx`、`tokens.txt` |

目录源数据如下；大小是安装时的硬上限和完整长度要求，不是下载进度估算：

| ID | 版本与字节数 | 固定源与 SRI SHA-256 |
| --- | --- | --- |
| `sensevoice-int8` | `2024-07-17`；`163002883` | <https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2><br>`sha256-fR76ITimWwtIjfN/i4nj2RpgZ25Bb1FblSNY2D39NH4=` |
| `silero-vad` | `asr-models`；`643854` | <https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx><br>`sha256-niRJ4Qh0ltjUyrqQfyPgvT942R+lUkebucI6wJy7H9Y=` |
| `qwen3-asr-0.6b-int8` | `2026-03-25`；`878702423` | <https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2><br>`sha256-OT+KFOL1+5Z0aqqzQpl6QGQQAfvVv5WSoICoMpF47pY=` |
| `streaming-paraformer-bilingual-zh-en` | `asr-models-2024-03-10`；`1047319737` | <https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-paraformer-bilingual-zh-en.tar.bz2><br>`sha256-VGKh/OQmk96uVyrx6MRocSSxKqhf5h/00xaLtSgOIF8=` |

常用命令：

```bash
type4me-linux model list
type4me-linux model install sensevoice-int8
type4me-linux model update sensevoice-int8
type4me-linux model check sensevoice-int8
type4me-linux model check sensevoice-int8 --json
type4me-linux model remove sensevoice-int8
type4me-linux model remove sensevoice-int8 --force
```

`model check` 完全离线：它读取已激活版本的 `manifest.json`，重新检查缺失、多余和损坏文件，不访问网络。`ModelManager.resolve()` 每次仍安全读取 `current` 指针，但只在目标名和载荷路径匹配已成功校验的缓存时复用该路径；显式 `check()` 始终重新扫描并哈希全部清单文件，失败会逐出缓存。`install` 与 `update` 使用每模型锁、有限大小的 `.partial` 下载、固定 SRI SHA-256、严格归档成员白名单、私有暂存目录和原子 `current` 指针；失败不会替换原有可用版本。配置正在引用的模型默认不能删除，明确使用 `--force` 才会删除；重复删除未安装模型会报告“模型未安装”。

默认布局如下：

```text
${XDG_DATA_HOME:-~/.local/share}/type4me-linux/models/
├── versions/<model-id>/<version>-<digest-prefix>/
└── <model-id>/current -> ../versions/<model-id>/<version>-<digest-prefix>

${XDG_CACHE_HOME:-~/.cache}/type4me-linux/model-downloads/
${XDG_STATE_HOME:-~/.local/state}/type4me-linux/model-manager/
```

下载只允许目录中声明的 HTTPS URL。归档安装拒绝绝对路径、路径穿越、重复成员、链接、设备、特殊文件、`setuid`/`setgid` 和未列出的内容；目录权限为 `0700`，模型文件和清单为 `0600`。这能保护安装完整性，但不能替代对模型内容、训练数据或许可证的独立审查。

许可证状态以目录记录为准：SenseVoice 与 Silero VAD 上游标注为 MIT；Qwen3-ASR 上游标注为 Apache-2.0，但当前转换后归档没有附带许可证，重新分发许可尚未核实。因此 Qwen3-ASR 产物仅由用户从上游下载到自己的 XDG 数据目录，不打包、不镜像。

## 识别方式

批量命令支持 `fake`、`sensevoice`、`qwen3-sherpa`、`hybrid` 和 `cloud`：

```bash
type4me-linux transcribe ./audio.wav --backend sensevoice
type4me-linux transcribe ./audio.wav --backend hybrid --json
type4me-linux transcribe ./audio.wav --backend fake --inject
type4me-linux transcribe ./audio.wav --backend cloud --json
type4me-linux record --seconds 5 --backend hybrid --no-inject
```

`hybrid` 先用 SenseVoice 识别完整 WAV，再用 Qwen3-ASR 识别同一音频；Qwen3-ASR 失败时保留 SenseVoice 文本并把后端标记为 `hybrid-fallback`。Qwen 输入会按 `asr.qwen3_max_segment_seconds` 分段，避免当前模型的 512 上下文窗口截断长音频。

实时命令支持 `sensevoice-vad` 与 `cloud-vad`：

```bash
type4me-linux stream --backend sensevoice-vad --mode quick
type4me-linux stream --backend cloud-vad --mode quick --json
type4me-linux stream --mode 语音润色 --no-inject
type4me-linux stream --mode quick --json
```

这里的“流式”是模拟流式，而不是模型的原生逐 token 流。实时采集严格要求 PCM16-LE、16 kHz、单声道；`pw-record` 默认每 32 ms 读取一个 1,024 字节块并写入临时 WAV。Silero VAD 以 512 样本窗口分段，活动语音期间默认每 200 ms 用新的 SenseVoice 离线流解码当前片段，两个间隔分别由 `capture.chunk_millis` 和 `asr.partial_interval_millis` 控制。VAD 释放片段后，文本进入 `confirmed_segments`；EOF 的非空对齐尾部会先交给 SenseVoice，再执行 `flush()`。正常停止默认直接采用 SenseVoice 的 `sensevoice-vad` 最终文本；`qwen3-sherpa` 是显式的准确性优先校准策略，成功后后端为 `hybrid`，失败会产生 `warning` 并发布 `hybrid-fallback` 的 SenseVoice 文本。旧配置的 `final_backend = "none"` 必须改为 `"sensevoice"`。

普通模式只把唯一最终权威文本写到 stdout；交互终端上的局部文本显示在 stderr。任何局部文本都不会注入目标应用，最终文本最多注入一次。

### 云端语音识别

`cloud`（批量）与 `cloud-vad`（实时）把识别委托给百炼（DashScope）云端模型。
音频以 base64 data URI 直传，不经过第三方上传。配置方式与 omp 的 models 配置
一致：url + apiKey（环境变量）+ model。

```toml
[cloud]
base_url = "https://dashscope.aliyuncs.com"
api_key = "sk-..."          # 与 omp models.yml 的 apiKey 字段一致的明文密钥
model = "qwen3-asr-flash-2026-02-10"
timeout_seconds = 60.0

[asr]
batch_backend = "cloud"
streaming_backend = "cloud-vad"
final_backend = "cloud"
```

```bash
type4me-linux transcribe ./audio.wav --backend cloud
type4me-linux stream --backend cloud-vad --mode quick
```

密钥直接写在 `[cloud].api_key`（不依赖环境变量）；请确保配置文件权限为
`0600`（`chmod 600 ~/.config/type4me-linux/config.toml`）。缺少密钥、HTTP
错误、超时或响应格式无效时批量命令直接报错；`cloud-vad` 实时会话对单个
语音段的失败会跳过该段并在结束时发布 `warning`，不中断整段录音。

候选模型按 `cloud.model` 枚举约束，实测优先级（`scripts/asr_benchmark/`，
TTS 语料 dev/test 分离，36 样本）：

| 优先级 | 模型 | 内容 CER | 平均时延 | 说明 |
| --- | --- | --- | --- | --- |
| 1（默认） | `qwen3-asr-flash-2026-02-10` | 0.0110 | 0.64s | 专用 ASR，无聊天漂移，最快 |
| 2 | `qwen3.5-omni-flash` | 0.0089 | 0.88s | 更准；需内置“只转写”系统提示 |
| 3 | `qwen3-omni-flash` | 0.0090 | 0.86s | 同上 |
| 本地基线 | `qwen3-sherpa` | 0.0229 | 1.04s | 完全离线 |
| 本地基线 | `sensevoice` | 0.1008 | 0.14s | 草稿/流式 |

回归测试：`nix develop --command python scripts/asr_benchmark/benchmark.py
--split test`，内容 CER 相对上一轮劣化超过 0.005 视为回归（dev/test 分离、
语料与报告均入库于 `scripts/asr_benchmark/`，音频按需由 TTS 生成并缓存）。

### JSON 事件与信号

`stream --json` 每行输出一个 `RecognitionEvent` JSON 对象，顶层键始终为：

```json
{
  "type": "transcript",
  "sequence": 2,
  "transcript": {
    "confirmed_segments": ["你好"],
    "partial_text": "世界",
    "authoritative_text": "你好",
    "is_final": false,
    "backend": "sensevoice-vad"
  },
  "message": null,
  "injection": null
}
```

`type` 只会是 `ready`、`transcript`、`warning`、`error`、`cancelled`、`finalized` 或 `completed`；`sequence` 在单次会话内严格递增。`transcript` 不适用时为 `null`。注入结果存在时，`injection` 为 `{"ok": ..., "method": ..., "message": ...}`。

正常顺序通常为 `ready`、零个或多个非最终 `transcript`、一个 `is_final: true` 的 `transcript`、`finalized`、`completed`。采集、VAD 或 SenseVoice 失败产生一个 `error` 后接 `completed`，不处理也不注入。取消产生 `cancelled`，跳过校准、处理和注入，并清理应用拥有的临时 WAV。

信号规则：

- 第一次 `SIGINT` 等同正常停止，仍会完成校准、文本处理、历史写入和可选注入。
- 第二次 `SIGINT` 或任何 `SIGTERM` 取消会话，退出码为 `130`。
- 采集 EOF 按正常停止处理；成功退出码为 `0`，错误退出码为 `1`。

## 模式与文本处理

内置模式为 `quick`（快速输入）、`voice-polish`（语音润色）、`prompt-optimize`（提示词优化）和 `translate-en`（翻译为英文）。`quick` 总是绕过外部文本处理。用户模式保存在 `config/modes.json`，ID 为 UUID；`stream --mode` 接受 UUID 或模式名称。

```bash
type4me-linux mode list
type4me-linux mode add "会议纪要" \
  --prompt '将{text}整理为要点。参考选区：{selected}；参考剪贴板：{clipboard}' \
  --processing-label "整理中" \
  --sort-order 10
type4me-linux mode update <uuid> --name "简洁会议纪要"
type4me-linux mode remove <uuid>
type4me-linux mode reload
```

只有 `{text}`、`{selected}` 和 `{clipboard}` 会被单次展开，插入内容中的模板字样不会递归展开。实时会话创建时立即执行 `wl-paste --no-newline` 和 `wl-paste --primary --no-newline`；命令缺失、读取失败或主选区为空时，对应值变为 `""`，同时发布警告并继续识别。Wayland 没有通用的跨合成器焦点应用标识，因此没有自动的按应用词汇规则。

文本处理配置支持：

- `openai-compatible`：POST 到 `<base_url>/chat/completions`。
- `ollama`：POST 到 `<base_url>/api/chat`，并发送 `"stream": false`。
- `none`：不调用外部处理服务。

凭据只在每次请求时从 `processing.api_key_env` 指定的环境变量读取。不要把密钥写入 TOML、模式、历史、Nix 表达式或日志。例如：

```bash
export TYPE4ME_API_KEY='...'
type4me-linux --config ~/.config/type4me-linux/config.toml stream --mode voice-polish
```

缺少密钥、HTTP 错误、超时或响应格式无效时，会警告并保留片段替换后的文本；处理失败状态进入历史，识别文本不会丢失。

## 完整配置

默认读取 `${XDG_CONFIG_HOME:-~/.config}/type4me-linux/config.toml`。配置是严格模式：未知节、未知键、无效枚举和越界数值会直接报错。以下内容列出当前完整模式及全部默认值：

```toml
[asr]
batch_backend = "hybrid"
streaming_backend = "sensevoice-vad"
final_backend = "sensevoice"
partial_interval_millis = 200
sensevoice_model_id = "sensevoice-int8"
vad_model_id = "silero-vad"
qwen3_model_id = "qwen3-asr-0.6b-int8"
language = "zh"
provider = "cpu"
num_threads = 4
vad_threshold = 0.2
vad_min_speech_seconds = 0.2
vad_min_silence_seconds = 0.5
vad_max_speech_seconds = 20.0
qwen3_max_segment_seconds = 12.0

[capture]
command = "pw-record"
sample_rate = 16000
channels = 1
format = "s16"
chunk_millis = 32

[inject]
prefer = "wtype"
wtype_command = "wtype"
wl_copy_command = "wl-copy"
clipboard_fallback = true
timeout_seconds = 10.0

[processing]
provider = "none"
base_url = ""
model = ""
api_key_env = ""
timeout_seconds = 30.0

[cloud]
base_url = "https://dashscope.aliyuncs.com"
api_key = ""
model = "qwen3-asr-flash-2026-02-10"
timeout_seconds = 60.0

[history]
enabled = true

[daemon]
host = "127.0.0.1"
port = 8766
```

`asr.language` 可为 `auto`、`zh`、`en`、`ja`、`ko`、`yue`；`asr.provider` 可为 `cpu` 或 `cuda`；`asr.final_backend` 只可为 `sensevoice` 或 `qwen3-sherpa`；`asr.partial_interval_millis` 必须在 32 到 5000 之间。实时识别只接受 `capture.sample_rate = 16000`、`capture.channels = 1` 和 `capture.format = "s16"`。每个模型 ID 只通过 ModelManager 的已校验 `current` 指针解析。

### CUDA 推理

`x86_64-linux` 包会构建带 CUDA 的 ONNX Runtime 与 Sherpa；在配置中设置 `provider = "cuda"` 即可启用。它需要兼容的 NVIDIA 驱动，且 CUDA 依赖受 NVIDIA EULA 约束，因此 flake 只为该目标允许这些非自由依赖。CUDA 提供者不可用时会报告识别错误，不会静默回退到 CPU；默认值仍为 `cpu`，以保持非 NVIDIA 系统可移植。

OpenAI 兼容示例：

```toml
[processing]
provider = "openai-compatible"
base_url = "https://api.openai.com/v1"
model = "gpt-4.1-mini"
api_key_env = "TYPE4ME_API_KEY"
timeout_seconds = 30.0
```

Ollama 示例：

```toml
[processing]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen3:8b"
api_key_env = ""
timeout_seconds = 30.0
```

## 词汇

随包安装的只读默认文件与用户文件分别合并；当前随包默认值为空：

```text
$prefix/share/type4me-linux/vocabulary/hotwords.json
$prefix/share/type4me-linux/vocabulary/snippets.json
${XDG_DATA_HOME:-~/.local/share}/type4me-linux/vocabulary/hotwords.json
${XDG_DATA_HOME:-~/.local/share}/type4me-linux/vocabulary/snippets.json
${XDG_CACHE_HOME:-~/.cache}/type4me-linux/hotwords.txt
```

热词按 NFKC、忽略大小写和空白去重，默认值先于用户值。片段冲突也按规范化触发词判断，但用户片段覆盖默认片段。片段匹配忽略大小写，允许触发字符之间出现空白，并对 ASCII 字母数字使用前后边界；替换发生在 LLM 处理之前。`hotwords.txt` 是每次变更后重新生成的派生缓存，不是数据源。

```bash
type4me-linux vocabulary hotwords list
type4me-linux vocabulary hotwords add NixOS
type4me-linux vocabulary hotwords update NixOS "Nix OS"
type4me-linux vocabulary hotwords remove "Nix OS"

type4me-linux vocabulary snippets list
type4me-linux vocabulary snippets add "我的邮箱" "me@example.com"
type4me-linux vocabulary snippets update "我的邮箱" "new@example.com" --new-trigger "工作邮箱"
type4me-linux vocabulary snippets remove "工作邮箱"
type4me-linux vocabulary correct "type for me" "Type4Me"

type4me-linux vocabulary reload
```

CLI 的添加、更新、删除与修正只修改用户文件；`correct` 同时将正确词加入热词并建立“误识别文本→正确词”片段映射。每次文件写入采用原子替换，内置默认文件保持不可变。

## 历史

历史默认启用并保存在 `${XDG_DATA_HOME:-~/.local/share}/type4me-linux/history.sqlite3`。数据库使用 WAL、busy timeout、迁移锁和参数化 SQL；保留期无限，直到用户明确删除。完成记录保存原始 ASR 文本、处理模式、成功时的处理后文本、最终文本、状态、时长、字符数、后端和模型。识别失败会写入 `failed` 状态；取消不会创建成功记录。

```bash
type4me-linux history list --limit 50
type4me-linux history list --limit 50 --cursor '<next_cursor>'
type4me-linux history list --from-date 2026-07-01T00:00:00Z --to-date 2026-08-01T00:00:00Z
type4me-linux history totals
type4me-linux history usage --days 1
type4me-linux history usage --days 7
type4me-linux history usage --days 30

type4me-linux history delete <id-1> <id-2>
type4me-linux history delete --all
type4me-linux history export ./type4me-history.csv
type4me-linux history export ./july.csv \
  --from-date 2026-07-01T00:00:00Z \
  --to-date 2026-08-01T00:00:00Z
```

列表按 `created_at`、`id` 倒序分页，`to-date` 是不包含的上界。CSV 使用 UTF-8、RFC 4180 和固定完整表头：`id,created_at,duration_seconds,raw_text,processing_mode,processed_text,final_text,status,character_count,asr_provider,asr_model`。

## 常驻桌面应用与快捷键

```bash
type4me-linux gui
type4me-linux gui --background
```

Adwaita 应用 ID 为 `io.github.vitus.Type4Me`。启动后应用调用 `hold()` 保持常驻；关闭窗口只隐藏窗口，后续桌面文件启动会激活并呈现同一实例。显式“退出 Type4Me”才会取消活动会话、关闭快捷键与 D-Bus 服务、释放持有并退出。音频、ASR、模型检查和历史读取在工作线程运行，GTK 主线程只接收不可变状态更新；录音开始、停止后最终识别、识别完成和错误状态都会通过 `Gio.Notification` 通知，窗口是否聚焦不影响发送。

常驻进程导出 D-Bus 名称 `io.github.vitus.Type4Me`、对象 `/io/github/vitus/Type4Me`、接口 `io.github.vitus.Type4Me.Controller`，方法为 `Toggle()`、`HoldStart()`、`HoldStop()`、`Cancel()` 和 `ShowWindow()`。这些 CLI 命令只控制现有常驻进程，不会启动第二个录音器：

```bash
type4me-linux toggle
type4me-linux hold-start
type4me-linux hold-stop
type4me-linux cancel
```

GUI 通过 `org.freedesktop.portal.GlobalShortcuts` 请求稳定 ID `hold-to-talk` 和 `toggle-recording`。门户返回的绑定子集是唯一有效集合：按下/松开 `hold-to-talk` 对应 `HoldStart`/`HoldStop`，按下 `toggle-recording` 对应 `Toggle`。用户取消、门户缺失、空绑定、会话关闭或总线断开不会使应用退出；活动的按住说话会被停止，并显示备用提示。

必须在 NixOS 层启用一个实际支持 GlobalShortcuts 的 XDG Portal 后端；Home Manager 模块不会猜测合成器，也不会替用户配置门户。用以下命令检查运行时能力：

```bash
type4me-linux doctor
type4me-linux doctor --allow-missing-models
```

Sway 可使用这三条精确后备绑定：

```text
bindsym --no-repeat XF86AudioRecord exec type4me-linux hold-start
bindsym --release XF86AudioRecord exec type4me-linux hold-stop
bindsym $mod+Shift+d exec type4me-linux toggle
```

`hold-start` 与 `hold-stop` 必须成对；备用命令依赖已经运行并持有 D-Bus 名称的 GUI 服务。

## 本地 daemon

```bash
type4me-linux daemon
```

服务默认监听 `127.0.0.1:8766`，提供三个现有路由，不提供流式 HTTP 传输：

```bash
curl http://127.0.0.1:8766/health
curl -X POST http://127.0.0.1:8766/inject \
  -H 'Content-Type: application/json' \
  -d '{"text":"你好"}'
curl -X POST http://127.0.0.1:8766/transcribe \
  -H 'Content-Type: application/json' \
  -d '{"path":"/absolute/server-local/audio.wav"}'
```

- `GET /health` 返回 `{"ok": true}`。
- `POST /inject` 接受 `text`，返回 `ok`、`method`、`message`。
- `POST /transcribe` 接受服务器本地 WAV 的 `path`，返回 `text`、`backend`、`draft_text`，且不注入。

JSON 字段名和后端 ID 保持稳定；人类可读的消息值为简体中文。daemon 未实现身份验证，只应绑定可信本机地址。

## XDG 数据

应用只创建用户拥有的目录，并将权限收紧为 `0700`：

| 根目录 | 默认值 | 内容 |
| --- | --- | --- |
| 配置 | `${XDG_CONFIG_HOME:-~/.config}/type4me-linux` | `config.toml`、`modes.json` |
| 数据 | `${XDG_DATA_HOME:-~/.local/share}/type4me-linux` | 模型、用户词汇、`history.sqlite3` |
| 缓存 | `${XDG_CACHE_HOME:-~/.cache}/type4me-linux` | 模型下载暂存、`hotwords.txt` |
| 状态 | `${XDG_STATE_HOME:-~/.local/state}/type4me-linux` | 模型锁、历史迁移锁 |
| 运行时 | `${XDG_RUNTIME_DIR}/type4me-linux` | 当前会话的私有运行时目录；未设置 `XDG_RUNTIME_DIR` 时不创建 |

## Nix 与 Home Manager

构建和临时运行：

```bash
nix build -L
nix run . -- doctor --allow-missing-models
```

Home Manager 示例：

```nix
{
  inputs.type4me-linux.url = "github:vitus/type4me-linux";

  outputs = { type4me-linux, home-manager, nixpkgs, ... }: {
    homeConfigurations.vitus = home-manager.lib.homeManagerConfiguration {
      pkgs = nixpkgs.legacyPackages.x86_64-linux;
      modules = [
        type4me-linux.homeManagerModules.default
        {
          programs.type4me-linux = {
            enable = true;
            service.enable = true;
            settings = {
              asr = {
                batch_backend = "hybrid";
                streaming_backend = "sensevoice-vad";
                final_backend = "sensevoice";
                language = "zh";
              };
              processing.provider = "none";
            };
            shortcuts.sway = {
              enable = true;
              holdKey = "XF86AudioRecord";
              toggleKey = "$mod+Shift+d";
            };
          };
        }
      ];
    };
  };
}
```

`service.enable` 启动 `${lib.getExe cfg.package} service`；该专用入口使用 Gio service 模式，避免单例 GUI 的远程激活进程退出后脱离 systemd 监管。unit 绑定 `graphical-session.target`，`TimeoutStopSec = 20`。`shortcuts.sway.enable` 生成上一节的三条 Sway 绑定；它不配置 NixOS 的 XDG Portal 后端。修改后执行：

```bash
home-manager switch
systemctl --user status type4me-linux.service
```

## 验证

开发中的无覆盖率专项测试：

```bash
nix develop -c python -m pytest --no-cov \
  tests/test_config.py tests/test_capture_stream.py tests/test_providers.py \
  tests/test_model_manager.py tests/test_pipeline.py

nix develop -c python -m pytest --no-cov \
  tests/test_cli_stream.py tests/test_daemon.py tests/test_desktop_controller.py

nix develop -c xvfb-run -a python -m pytest --no-cov tests/test_desktop_view.py
```

安装 SenseVoice 模型后的可选真实推理冒烟：

```bash
TYPE4ME_REAL_ASR=1 nix develop -c python -m pytest --no-cov \
  -m real_asr tests/test_real_asr_smoke.py
```

完整测试和打包检查：

```bash
nix develop -c python -m pytest
nix flake check -L
nix build -L
nix run . -- doctor --allow-missing-models
nix develop -c python -c 'import sherpa_onnx, gi, numpy'
```

模型、产品数据和桌面集成的进一步专项命令：

```bash
nix develop -c python -m pytest --no-cov tests/test_model_catalog.py
nix develop -c python -m pytest --no-cov tests/test_vocabulary.py
nix develop -c python -m pytest --no-cov tests/test_processing.py tests/test_modes.py
nix develop -c python -m pytest --no-cov tests/test_history.py
nix develop -c python -m pytest --no-cov tests/test_control_bus.py tests/test_shortcuts.py tests/test_home_manager.py
```

### 真实 Wayland/PipeWire 冒烟测试

此部分必须在有麦克风、PipeWire、Wayland 和已安装模型的真实桌面会话中执行：

```bash
type4me-linux model install sensevoice-int8
type4me-linux model install silero-vad
type4me-linux model install qwen3-asr-0.6b-int8
type4me-linux model check sensevoice-int8 --json
type4me-linux doctor
type4me-linux stream --mode quick --no-inject --json
```

说一句中文，然后第一次按 `Ctrl-C` 正常停止。默认快速模式必须出现 `ready`、零个或多个非最终 `transcript`、恰好一个最终 `backend: "sensevoice-vad"` 的转写和 `completed`；不得出现 Qwen 回退警告，也不得执行 `wtype`。在一次性 Wayland 文本目标中去掉 `--no-inject` 重试，确认只注入一次最终文本。

再验证显式准确性优先策略：

```bash
printf '[asr]\nfinal_backend = "qwen3-sherpa"\n' > /tmp/type4me-qwen.toml
type4me-linux --config /tmp/type4me-qwen.toml stream --mode quick --no-inject --json
```

其成功最终事件必须保留 `backend: "hybrid"`。然后运行：

```bash
type4me-linux gui
```

在同一个一次性 Wayland 文本目标中不重启 GUI，连续完成两次按住说话或切换录音；两次都必须回到空闲、无 Qwen 回退警告，并且各自只注入一次最终文本。门户不可用时应用上面的三条 Sway 绑定，并确认它们通过同一个常驻控制器工作；关闭窗口后服务应继续运行，显式退出后服务应停止。

## 上游项目

- Type4Me: <https://github.com/joewongjc/type4me>
- SenseVoice: <https://github.com/FunAudioLLM/SenseVoice>
- sherpa-onnx: <https://github.com/k2-fsa/sherpa-onnx>
- Silero VAD: <https://github.com/snakers4/silero-vad>
- Qwen3-ASR: <https://github.com/QwenLM/Qwen3-ASR>
- XDG Desktop Portal: <https://flatpak.github.io/xdg-desktop-portal/>
