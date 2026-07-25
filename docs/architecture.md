# type4me-linux 架构

本文描述当前实现的运行时边界、持久化契约和集成接口。`type4me-linux` 只有一个已安装入口 `type4me_linux.cli:main`；CLI 批量识别、CLI 实时识别、常驻桌面应用、D-Bus 快捷键控制和本地 daemon 复用同一组配置、模型、词汇、处理和历史组件。

## 设计约束

- 音频、模型和历史属于当前用户，不写入 Nix store。
- SenseVoice、Silero VAD 和 Qwen3-ASR 都由本机 `sherpa_onnx` 执行；网络只用于目录固定的模型下载和用户明确配置的文本处理服务。
- `RecognitionSession` 是实时会话唯一的副作用边界：它拥有采集进程、最终校准、文本处理、历史写入和文本注入的顺序。
- GUI 不在 GTK 主线程运行采集、ASR、子进程或数据库查询。
- 稳定协议标识保持不变：CLI 命令与选项、TOML/JSON 字段、模型 ID、HTTP 路由、D-Bus 名称和 Nix 选项都不翻译。
- Wayland 没有通用的跨合成器焦点应用标识，因此不存在自动的按应用词汇选择。

## 模块分层

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 入口与传输 | `cli.py`、`daemon.py`、`desktop.py`、`control_bus.py` | 参数解析、JSON/HTTP/D-Bus/GTK 边界 |
| 应用编排 | `pipeline.py`、`session.py`、`controller.py`、`app_state.py` | 依赖装配、会话状态机、UI 状态归约和工作线程调度 |
| 音频与识别 | `capture.py`、`providers.py`、`events.py` | PipeWire 采集、VAD、SenseVoice、Qwen3-ASR 和事件记录 |
| 产品数据 | `modes.py`、`vocabulary.py`、`history.py`、`clipboard.py` | 模式、词汇、SQLite 历史和 Wayland 选区快照 |
| 模型供应链 | `model_catalog.py`、`model_manager.py` | 固定目录、下载验证、安全解包、清单和原子激活 |
| 系统集成 | `paths.py`、`doctor.py`、`shortcuts.py`、`inject.py` | XDG 路径、诊断、GlobalShortcuts 和文本注入 |
| 配置与部署 | `config.py`、`flake.nix`、`nix/home-manager.nix` | 严格配置、Nix 包装、桌面资源和用户服务 |

`VoiceInputPipeline.run_once()` 是批量路径的所有者，供 `transcribe`、`record` 和 daemon 的 `/transcribe` 使用。`VoiceInputPipeline.create_session()` 创建实时路径，供 `stream`、GUI 和快捷键控制使用；实时路径不会绕回批量方法。

## XDG 路径与权限

`AppPaths.from_environment()` 计算并保护以下根目录：

```text
config  ${XDG_CONFIG_HOME:-~/.config}/type4me-linux
data    ${XDG_DATA_HOME:-~/.local/share}/type4me-linux
cache   ${XDG_CACHE_HOME:-~/.cache}/type4me-linux
state   ${XDG_STATE_HOME:-~/.local/state}/type4me-linux
runtime ${XDG_RUNTIME_DIR}/type4me-linux
```

前四个目录总会按需创建；只有设置了 `XDG_RUNTIME_DIR` 才创建运行时目录。目录必须是当前用户拥有的真实目录，不能是符号链接，并被收紧到 `0700`；创建、所有权或权限处理失败会产生中文诊断并停止相关操作。

主要数据位置：

```text
config/config.toml
config/modes.json
data/models/versions/<id>/<version>-<digest-prefix>/
data/models/<id>/current
data/vocabulary/hotwords.json
data/vocabulary/snippets.json
data/history.sqlite3
cache/model-downloads/
cache/hotwords.txt
state/model-manager/
state/history/migration.lock
```

## 严格配置契约

`load_config()` 默认读取 `config/config.toml`。配置只接受 `[asr]`、`[capture]`、`[inject]`、`[processing]`、`[history]` 和 `[daemon]`；未知节或键立即失败。完整默认值如下：

```toml
[asr]
batch_backend = "hybrid"
streaming_backend = "sensevoice-vad"
final_backend = "qwen3-sherpa"
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
chunk_millis = 200

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

[history]
enabled = true

[daemon]
host = "127.0.0.1"
port = 8766
```

枚举边界：

- `asr.batch_backend`：`fake`、`sensevoice`、`qwen3-sherpa`、`hybrid`。
- `asr.streaming_backend`：仅 `sensevoice-vad`。
- `asr.final_backend`：`none`、`sensevoice`、`qwen3-sherpa`。
- `asr.language`：`auto`、`zh`、`en`、`ja`、`ko`、`yue`。
- `asr.provider`：`cpu`、`cuda`。
- `asr.qwen3_max_segment_seconds`：大于 `0` 且不大于 `12`；限制单次 Qwen3-ASR 解码的音频时长，避免当前 512 上下文窗口截断。
- 当前 Nix 闭包中的 `sherpa_onnx` 只有 CPU 和 OpenVINO Execution Provider；`asr.provider = "cuda"` 虽可通过配置校验，但会回退到 CPU，必须先打包 CUDA 运行时才能启用。
- `inject.prefer`：`wtype`、`clipboard`。
- `processing.provider`：`none`、`openai-compatible`、`ollama`。

数值、布尔值、模型 ID 和环境变量名都有类型与范围校验。三个模型只能通过 ModelManager 的 `current` 指针解析；调用方不能直接指定任意载荷路径。

## 模型目录与事务安装

`MODEL_CATALOG` 是不可变映射，条目包含 ID、版本、唯一 HTTPS URL、SRI SHA-256、精确字节数、归档类型、唯一顶层目录、允许成员、必需路径和许可证状态：

| ID | 版本 | 运行时文件 |
| --- | --- | --- |
| `sensevoice-int8` | `2024-07-17` | `model.int8.onnx`、`tokens.txt` |
| `silero-vad` | `asr-models` | `silero_vad.onnx` |
| `qwen3-asr-0.6b-int8` | `2026-03-25` | `conv_frontend.onnx`、`encoder.int8.onnx`、`decoder.int8.onnx`、三个 `tokenizer/` 文件 |

固定供应链数据：

| ID | 精确字节数 | URL | SRI SHA-256 |
| --- | ---: | --- | --- |
| `sensevoice-int8` | `163002883` | <https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2> | `sha256-fR76ITimWwtIjfN/i4nj2RpgZ25Bb1FblSNY2D39NH4=` |
| `silero-vad` | `643854` | <https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx> | `sha256-niRJ4Qh0ltjUyrqQfyPgvT942R+lUkebucI6wJy7H9Y=` |
| `qwen3-asr-0.6b-int8` | `878702423` | <https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2> | `sha256-OT+KFOL1+5Z0aqqzQpl6QGQQAfvVv5WSoICoMpF47pY=` |

操作入口：

```bash
type4me-linux model list
type4me-linux model install <id>
type4me-linux model update <id>
type4me-linux model check <id>
type4me-linux model check <id> --json
type4me-linux model remove <id>
type4me-linux model remove <id> --force
```

安装与更新执行同一个受锁事务：

1. 获得 `state/model-manager` 下的每模型进程锁。
2. 拒绝目录中缺少摘要、非 HTTPS URL 或无效大小的条目。
3. 下载到 `cache/model-downloads/<id>-<version>.partial`，边读边限制总大小并计算 SHA-256。
4. 摘要和精确长度一致后，才进入 `0700` 暂存目录。
5. 对 tar 先检查全部头：拒绝绝对路径、`..`、重复成员、多个顶层目录、链接、设备、特殊文件、`setuid`/`setgid` 和白名单外内容。
6. 只以 `0600` 写入允许的普通文件，检查全部必需文件并逐个哈希。
7. 写入并 `fsync` `manifest.json`，原子移动到 `versions/<id>/<version>-<digest-prefix>`。
8. 用同目录临时符号链接加 `os.replace()` 原子切换 `models/<id>/current`。
9. 无论成功失败都清理 `.partial` 和暂存目录；失败不会改变旧 `current`。

`model check` 不使用网络。它验证 `current` 目标范围、清单结构、模型 ID、目录版本、每个普通文件的大小与摘要，并报告 `missing`、`extra`、`corrupt`、`errors`。`remove` 不跟随符号链接；未安装时幂等返回，活动配置引用默认拒绝，只有 `--force` 可以覆盖。

许可证不是完整性校验的一部分。SenseVoice 与 Silero VAD 上游标注为 MIT；Qwen3-ASR 上游标注为 Apache-2.0，但转换后归档未附许可证，重新分发许可尚未核实。权重由最终用户直接下载到 XDG 数据目录，Nix 包不携带也不镜像权重。

## 批量识别路径

批量数据流为：

```text
WAV 或定时 pw-record
  -> ASRProvider.transcribe()
  -> VocabularyService.apply_snippets()
  -> quick 或 TextProcessor
  -> 可选 TextInjector
  -> HistoryStore
```

`SenseVoiceProvider` 通过 `OfflineRecognizer.from_sense_voice()` 构造识别器，使用 `language`、`use_itn=True`、`num_threads` 和 `provider`。`Qwen3SherpaProvider` 通过 `OfflineRecognizer.from_qwen3_asr()` 构造识别器，使用词汇热词、`max_new_tokens=512`、线程数和运行提供方；它在解码前按 `qwen3_max_segment_seconds` 切分 PCM，规避当前 Qwen3 模型的 512 上下文窗口。输入 WAV 必须是未压缩 PCM16、16 kHz、单声道。

`HybridProvider` 先得到 SenseVoice 草稿，再用 Qwen3-ASR 对同一 WAV 的安全时长分段解码。Qwen3-ASR 失败时返回草稿、`draft_text` 和 `hybrid-fallback`，不让一次成功的 SenseVoice 识别变成整体失败。

批量 CLI：

```bash
type4me-linux transcribe ./audio.wav --backend hybrid --json
type4me-linux transcribe ./audio.wav --backend sensevoice --inject
type4me-linux record --seconds 5 --backend hybrid --no-inject
```

批量 JSON 保持 `text`、`backend`、`draft_text` 和 `injection` 字段。daemon 的 `/transcribe` 使用同一批量管线，但固定不注入。

## 实时会话与模拟流式识别

`RawCaptureSession` 启动精确命令：

```text
pw-record --raw --rate 16000 --channels 1 --format s16 --latency 200ms -
```

stdout 以 6,400 字节，即 200 ms PCM16-LE 块读取，同时通过 `wave.writeframesraw()` 镜像到应用拥有的临时 WAV。所有退出路径都会关闭管道、发送 `terminate`、限时等待、必要时发送 `kill` 并回收子进程。最终校准暂时认领 WAV；会话完成或取消后释放并删除。调用方显式提供的 WAV 永不由采集器删除。

`SenseVoiceVadStreamer` 是模拟流式层：

1. `numpy.frombuffer(..., dtype="<i2") / 32768.0` 转换采集块。
2. Silero VAD 按 512 样本窗口消费，默认阈值 `0.2`、最短语音 `0.2` 秒、最短静音 `0.5` 秒、最长语音 `20.0` 秒。
3. 语音活动期间每累计 3,200 样本，即 200 ms，以新的 SenseVoice 离线流解码当前活动段；只在局部文本变化时发布。
4. VAD 释放一段后只最终解码一次，追加到 `confirmed_segments` 并清空 `partial_text`。
5. 正常停止时补齐最后 VAD 窗口、`flush()`、排空完成段，并解码剩余活动段。

因此 `sensevoice-vad` 不表示 SenseVoice 原生 token 流。它是 VAD 分段加重复离线解码，代价和延迟特征必须按这一事实理解。

停止后的权威文本规则：

- `final_backend = "qwen3-sherpa"`：从完整临时 WAV 读取音频，按 `qwen3_max_segment_seconds` 分段执行 Qwen3-ASR 校准；成功后最终后端为 `hybrid`。
- `final_backend = "none"` 或 `"sensevoice"`：SenseVoice 拼接结果直接成为权威文本。
- Qwen3-ASR 缺 WAV、报错或返回空文本：先发布 `warning`，再以 `hybrid-fallback` 发布 SenseVoice 最终文本。

片段替换和文本处理只接收最终权威文本。局部文本不会进入处理服务、历史或 `TextInjector`，最终文本最多注入一次。

## 会话状态机与事件

`RecognitionSession` 的成功状态路径为：

```text
idle -> starting -> recording -> finishing -> processing -> injecting -> idle
```

取消路径为当前活动状态到 `cancelled -> idle`。`start()`、`stop()` 和 `cancel()` 对重复调用幂等。会话是唯一可以创建采集句柄、调用终结器与校准器、调用处理器、写历史和注入文本的对象。

不可变事件记录：

```python
RecognitionTranscript(
    confirmed_segments: tuple[str, ...],
    partial_text: str,
    authoritative_text: str,
    is_final: bool,
    backend: str,
)

RecognitionEvent(
    type: Literal[
        "ready", "transcript", "warning", "error",
        "cancelled", "completed", "finalized",
    ],
    sequence: int,
    transcript: RecognitionTranscript | None,
    message: str | None,
    injection: InjectionResult | None,
)
```

`stream --json` 每行固定输出 `type`、`sequence`、`transcript`、`message`、`injection`。其中 `transcript` 对象固定包含 `confirmed_segments`、`partial_text`、`authoritative_text`、`is_final`、`backend`；`injection` 存在时固定包含 `ok`、`method`、`message`。

成功通常按 `ready -> transcript(partial/confirmed)* -> transcript(final) -> finalized -> completed`。采集、VAD 或 SenseVoice 错误只发布一个 `error`，随后 `completed`，跳过处理与注入并写入失败历史。取消发布一个终止 `cancelled`，跳过 Qwen3-ASR、处理、注入和成功历史，并清理所有自有音频。

CLI 信号契约：第一次 `SIGINT` 调用 `stop()` 并允许最终校准、处理与注入；第二次 `SIGINT` 或 `SIGTERM` 调用 `cancel()` 并退出 `130`。采集 EOF 等同正常停止。完成退出 `0`，错误退出 `1`，取消退出 `130`。非 JSON 交互终端把局部文本写 stderr，并只把一个最终权威文本写 stdout；非 TTY 普通模式只输出最终 stdout 文本。

## 模式、选区与文本处理

`ModesRepository` 原子维护 `config/modes.json`。每条记录字段固定为 `id`、`name`、`prompt`、`processing_label`、`builtin`、`sort_order`。四个内置模式 ID 为：

- `quick`：快速输入，空提示词，绕过所有外部处理。
- `voice-polish`：删除口头语并修正语病。
- `prompt-optimize`：改写为清晰、可执行的提示词。
- `translate-en`：翻译为自然英文。

用户模式使用规范 UUID，内置模式不可修改或删除。`render_template()` 只扫描一次 `{text}`、`{selected}`、`{clipboard}`；未知字段原样保留，插入值中的占位符不会再次解析。

实时会话创建时，`ClipboardSnapshotService` 立即执行：

```text
wl-paste --no-newline
wl-paste --primary --no-newline
```

两个读取彼此独立。可执行文件缺失、超时、非零退出、非 UTF-8 或主选区为空会让对应值变为 `""`，并把警告附加到会话启动事件后继续执行。

`OpenAICompatibleProcessor` 向 `<base_url>/chat/completions` POST `model` 与单条 user message；`OllamaProcessor` 向 `<base_url>/api/chat` POST 同样结构，并附加 `stream: false`。`api_key_env` 只保存环境变量名；处理器在每个请求开始时读取环境变量，存在时才发送 `Authorization: Bearer ...`。密钥不会进入 TOML、模式、历史、Nix 或应用日志。

处理状态为 `bypassed`、`success`、`missing-secret`、`http-error`、`malformed-response` 或 `timeout`。缺少密钥、网络/HTTP 错误、超时和无效响应都返回片段替换后的输入文本及中文警告；调用方取消会传播取消，不走文本后备或注入。

CLI 管理入口：

```bash
type4me-linux mode list
type4me-linux mode add <name> --prompt <prompt> [--processing-label <label>] [--sort-order <n>]
type4me-linux mode update <uuid> [--name <name>] [--prompt <prompt>] \
  [--processing-label <label>] [--sort-order <n>]
type4me-linux mode remove <uuid>
type4me-linux mode reload
```

## 词汇契约

`VocabularyService` 是唯一词汇仓库。随包安装的不可变 `hotwords.json` 和 `snippets.json` 先加载，再合并用户数据目录中的同名文件。用户变更使用同目录临时文件、`fsync` 和 `os.replace()` 原子提交。

热词以 NFKC、`casefold()` 和移除空白后的键去重，默认顺序优先。片段采用相同规范键，冲突时用户定义覆盖默认定义。匹配表达式在触发字符之间允许任意空白，忽略大小写，并对 ASCII 字母数字添加负向前后边界。片段按有效映射顺序替换，发生在外部文本处理之前。

每次加载或修改都会从有效热词重新生成 `cache/hotwords.txt`。这是可删除的派生数据，用户 JSON 才是持久化数据源。

```bash
type4me-linux vocabulary hotwords list
type4me-linux vocabulary hotwords add <word>
type4me-linux vocabulary hotwords update <word> <replacement>
type4me-linux vocabulary hotwords remove <word>
type4me-linux vocabulary snippets list
type4me-linux vocabulary snippets add <trigger> <replacement>
type4me-linux vocabulary snippets update <trigger> <replacement> [--new-trigger <trigger>]
type4me-linux vocabulary snippets remove <trigger>
type4me-linux vocabulary reload
```

## 历史契约

`HistoryStore` 使用 `data/history.sqlite3`，启用 WAL 和 5 秒 busy timeout。迁移由线程锁与 `state/history/migration.lock` 的 `flock` 双重保护，数据库文件权限为 `0600`。当前 `recognition_history` 模式固定为：

```sql
id TEXT PRIMARY KEY,
created_at TEXT NOT NULL,
duration_seconds REAL,
raw_text TEXT NOT NULL,
processing_mode TEXT,
processed_text TEXT,
final_text TEXT NOT NULL,
status TEXT NOT NULL,
character_count INTEGER,
asr_provider TEXT,
asr_model TEXT
```

历史默认无限保留。查询按 `created_at DESC, id DESC`，分页大小为 1 到 1000；游标编码最后一条记录的 RFC 3339 时间与 ID。`from_date` 包含下界，`to_date` 不包含上界。删除支持一个、多个或全部；用量汇总窗口只接受 1、7、30 天。

RFC 4180 CSV 固定使用 UTF-8、CRLF 和完整表头：

```text
id,created_at,duration_seconds,raw_text,processing_mode,processed_text,final_text,status,character_count,asr_provider,asr_model
```

CLI：

```bash
type4me-linux history list --limit 50 [--cursor <cursor>] \
  [--from-date <RFC3339>] [--to-date <RFC3339>]
type4me-linux history delete <id>...
type4me-linux history delete --all
type4me-linux history export <destination> [--from-date <RFC3339>] [--to-date <RFC3339>]
type4me-linux history totals
type4me-linux history usage --days 1|7|30
```

批量和完成的实时会话写入原始文本、模式、成功处理后的文本、最终文本、状态、字符数、ASR 后端和模型；实时记录还包含会话时长。注入失败覆盖状态为 `injection-failed`，处理失败保存对应处理状态，识别失败保存 `failed`。取消不创建成功行。GUI 历史页异步读取最近记录；删除、分页导出和汇总由 CLI 提供。

## 文本注入

`TextInjector` 按 `[inject]` 配置优先使用 `wtype`。当 `clipboard_fallback = true` 时，失败可以回退到 `wl-copy`；剪贴板后备只复制文本，不伪造按键。事件和批量结果保存 `ok`、`method`、`message`。实时会话只有最终处理文本能到达注入器。

## 常驻 GUI、控制总线与门户

`Type4MeApplication` 是 `Adw.Application(application_id="io.github.vitus.Type4Me")`。`do_startup()` 调用 `hold()`，所以关闭主窗口只隐藏它；下一次桌面文件启动由会话 D-Bus 激活并呈现同一进程。显式退出按顺序取消控制器、关闭 PortalShortcuts 和 ControlBusService、释放 `hold()`、退出应用。窗口未聚焦时，`finalized` 和错误状态通过 `Gio.Notification` 发出；GTK4 托盘不是运行依赖。

主窗口页为语音输入、模式、词汇、模型、历史和设置。`ApplicationController` 是 UI 唯一实时会话所有者。它用 `ThreadPoolExecutor` 创建与运行会话，异步检查模型与读取历史，通过 GLib 调度器把不可变 `AppState` 更新送回主线程；开始、停止、取消按钮和快捷键最终调用同一个控制器状态机。

ControlBusService 导出：

```text
bus name  io.github.vitus.Type4Me
object    /io/github/vitus/Type4Me
interface io.github.vitus.Type4Me.Controller
methods   Toggle() HoldStart() HoldStop() Cancel() ShowWindow()
```

`toggle`、`hold-start`、`hold-stop`、`cancel` CLI 是同步 D-Bus 客户端。没有名称所有者时以中文错误退出 `1`，绝不自行创建第二个采集进程。

PortalShortcuts 使用会话总线上的 `org.freedesktop.portal.GlobalShortcuts`：

1. 查询接口 `version`，版本必须至少为 1。
2. 先订阅 `Request::Response`、`Activated`、`Deactivated`、`ShortcutsChanged`、`Session::Closed` 和名称丢失，再发送请求，避免丢失快速响应。
3. 创建一次门户会话，在该会话内仅调用一次 `BindShortcuts`。
4. 请求稳定 ID `hold-to-talk` 和 `toggle-recording`，将门户返回的子集视为唯一权威集合。
5. `Activated(hold-to-talk)` 调用 `hold_start()`，对应 `Deactivated` 调用 `hold_stop()`；`Activated(toggle-recording)` 调用 `toggle()`。重复激活被去重。
6. 会话关闭、总线丢失、应用关闭或重新绑定最多强制停止一次活动按住录音。

用户取消、请求失败、空集合或门户缺失只把快捷键状态置为不可用并提供 Sway 提示，不让应用失败。NixOS 必须由用户配置支持 GlobalShortcuts 的 XDG Portal 后端；运行时能力由 `doctor` 探测。

精确 Sway 后备配置：

```text
bindsym --no-repeat XF86AudioRecord exec type4me-linux hold-start
bindsym --release XF86AudioRecord exec type4me-linux hold-stop
bindsym $mod+Shift+d exec type4me-linux toggle
```

这三条命令通过同一 D-Bus 控制器工作，要求常驻 GUI 已运行。

## daemon 协议

`ThreadingHTTPServer` 默认绑定 `127.0.0.1:8766`：

| 方法与路由 | 请求 | 响应 |
| --- | --- | --- |
| `GET /health` | 无 | `{"ok": true}` |
| `POST /inject` | `{"text": "..."}` | `ok`、`method`、`message` |
| `POST /transcribe` | `{"path": "/server/local.wav"}` | `text`、`backend`、`draft_text` |

`/transcribe` 只接受服务进程可访问的本地路径，并固定不注入。未知路由返回 404；无效请求返回 400；处理异常返回 500。JSON 字段与后端 ID 是协议，人类可读 `message` 为中文。这里没有事件流路由；实时传输契约由 CLI JSON Lines 提供。daemon 没有身份验证，部署时应保持可信本机绑定。

## Nix 与 Home Manager

flake 为 `x86_64-linux` 和 `aarch64-linux` 构建 Python 3.12 应用。运行闭包包含 NumPy、PyGObject、GTK4、libadwaita、Adwaita 图标、PipeWire、`sherpa-onnx`、`wl-clipboard`、`wtype` 和通知支持；包装器把 sherpa Python 输出加入 `PYTHONPATH`。包安装桌面文件和两个不可变空词汇默认文件，不安装模型权重。

Home Manager 接口：

```nix
programs.type4me-linux = {
  enable = true;
  package = type4me-linux.packages.${pkgs.system}.default;
  settings = { /* 严格 TOML 模式 */ };
  service.enable = true;
  shortcuts.sway = {
    enable = true;
    holdKey = "XF86AudioRecord";
    toggleKey = "$mod+Shift+d";
  };
};
```

启用服务后，systemd user unit 执行 `type4me-linux service`。该入口以 `Gio.ApplicationFlags.IS_SERVICE` 运行，确保 unit 进程必须持有应用单例，而不会作为远程激活客户端成功退出。unit 在 `graphical-session.target` 之后启动并成为其一部分，失败后 2 秒重启，`TimeoutStopSec = 20`。Sway 选项生成上述三条精确配置，但不会配置 NixOS 层门户。

`doctor` 检查 `pw-record`、`wtype`、`wl-copy`、`wl-paste`、`sherpa_onnx` 导入、所有 XDG 根的可写性、三个目录模型的离线完整性以及 GlobalShortcuts 接口版本。`--allow-missing-models` 只豁免结果严格为“尚未安装”的模型；损坏模型和任何命令、Python、XDG 或门户失败仍导致非零退出。

## 验证矩阵

实时核心与产品数据：

```bash
nix develop -c python -m pytest --no-cov \
  tests/test_session.py tests/test_capture_stream.py tests/test_model_manager.py \
  tests/test_vocabulary.py tests/test_processing.py tests/test_history.py \
  tests/test_shortcuts.py
```

CLI、daemon 与无 GI 控制器：

```bash
nix develop -c python -m pytest --no-cov \
  tests/test_cli_stream.py tests/test_daemon.py tests/test_desktop_controller.py
```

GTK/Xvfb 契约：

```bash
nix develop -c xvfb-run -a python -m pytest --no-cov tests/test_desktop_view.py
```

配置、目录模型、词汇、处理、历史、D-Bus、门户与 Home Manager 专项：

```bash
nix develop -c python -m pytest --no-cov tests/test_config.py
nix develop -c python -m pytest --no-cov tests/test_model_catalog.py tests/test_model_manager.py
nix develop -c python -m pytest --no-cov tests/test_vocabulary.py
nix develop -c python -m pytest --no-cov tests/test_processing.py tests/test_modes.py
nix develop -c python -m pytest --no-cov tests/test_history.py
nix develop -c python -m pytest --no-cov tests/test_control_bus.py tests/test_shortcuts.py tests/test_home_manager.py
```

完整测试、打包和运行时导入：

```bash
nix develop -c python -m pytest
nix flake check -L
nix build -L
nix run . -- doctor --allow-missing-models
nix develop -c python -c 'import sherpa_onnx, gi, numpy'
```

最后一条 doctor 命令允许模型未下载，但不能把缺失命令、Python 绑定、不可写 XDG 目录或缺失门户误报为成功。

## 真实 Wayland/PipeWire 冒烟路径

以下步骤只在真实麦克风、PipeWire 与 Wayland 会话中有意义：

```bash
type4me-linux model install sensevoice-int8
type4me-linux model install silero-vad
type4me-linux model install qwen3-asr-0.6b-int8
type4me-linux model check sensevoice-int8 --json
type4me-linux doctor
type4me-linux stream --mode quick --no-inject --json
```

说一句中文，第一次按 `Ctrl-C` 正常停止。必须观察到至少 `ready`、一个或多个非最终 `transcript`、恰好一个 `is_final: true` 权威转写，随后 `completed`；`--no-inject` 下不得调用 `wtype`。去掉该选项，在一次性 Wayland 文本目标重试，确认只注入一次最终处理文本。

然后启动：

```bash
type4me-linux gui
```

在门户中绑定按住说话与切换录音，确认按下/松开各触发一次，切换激活只触发一次。门户不可用时应用精确 Sway 后备绑定，确认仍控制同一 ApplicationController。关闭窗口应保持常驻；再次桌面激活应呈现原进程；显式退出应释放门户会话、D-Bus 名称和应用持有。
