# Syllune

Realtime voice input for NixOS / Wayland, written in Rust. Syllune captures 16 kHz mono PCM16 through PipeWire (`pw-record`), transcribes while you speak (DashScope cloud realtime by default, or a local Sherpa-ONNX streaming Paraformer), then types the final text into the focused window with `wtype`. No Python runtime, no desktop shell — one binary, one hotkey.

> 中文文档见 [README.zh.md](README.zh.md).

**30-second version**

```bash
nix run github:Vitus213/syllune -- doctor   # verify pw-record / wtype / wl-copy
nix run github:Vitus213/syllune -- stream   # speak, Ctrl-C, text is typed
```

Contents: [Compatibility](#compatibility) · [Install](#install) · [First run](#first-run) · [Commands](#commands) · [Configuration](#configuration) · [Modes & injection](#modes--injection) · [History, recordings & web console](#history-recordings--web-console) · [Model catalog](#model-catalog) · [Migration from type4me-linux](#migration-from-type4me-linux) · [Development](#development)

## Compatibility

| Layer | Requirement | Notes |
| --- | --- | --- |
| OS | NixOS / Linux, `x86_64` or `aarch64` | flake systems are pinned to these two |
| Display server | Wayland | injection uses `wtype`; clipboard fallback uses `wl-copy` |
| Audio | PipeWire | capture spawns `pw-record` (16 kHz mono PCM16) |
| Compositor shortcuts | XDG Desktop Portal `GlobalShortcuts` (best effort) | falls back to the `dev.syllune.Daemon` D-Bus control bus; a ready-made Sway binding ships with the Home Manager module |
| Cloud ASR | DashScope realtime endpoint (`qwen3-asr-flash-realtime`) | API key required |
| Local ASR | Sherpa-ONNX streaming Paraformer (`streaming-paraformer-bilingual-zh-en`) | installed via `syllune model install`, integrity re-verified per session |
| Batch ASR | DashScope multimodal or local SenseVoice | used by `transcribe` / `record` |

Not supported: X11, macOS, Windows.

## Install

### Nix flake (recommended)

One-off run without installing anything:

```bash
nix run github:Vitus213/syllune -- doctor
```

Or from a local checkout:

```bash
nix run . -- doctor
```

Add it to your NixOS / home configuration through the flake overlay:

```nix
{
  inputs.syllune.url = "github:Vitus213/syllune";

  outputs = { nixpkgs, syllune, ... }: {
    # e.g. environment.systemPackages, or home.packages:
    home.packages = [ syllune.packages.${system}.syllune ];
    # or: nixpkgs.overlays = [ syllune.overlays.default ]; then pkgs.syllune
  };
}
```

### Home Manager

The flake ships `homeManagerModules.default`. It installs the package, manages `~/.config/syllune/config.toml`, and can run the headless daemon plus a Sway hotkey:

```nix
{
  inputs.syllune.url = "github:Vitus213/syllune";

  imports = [ inputs.syllune.homeManagerModules.default ];

  programs.syllune = {
    enable = true;
    settings.asr.streaming_backend = "cloud-realtime"; # or "local-streaming"
    settings.cloud.api_key = "sk-...";                 # file is written 0600
    service.enable = true;                             # persistent `syllune daemon`
    shortcuts.sway.enable = true;                      # $mod+Shift+d toggles
  };
}
```

`shortcuts.sway` only adds the Sway binding; the GlobalShortcuts portal backend itself is configured at NixOS level, and the daemon keeps working through D-Bus without it.

### From source (development)

```bash
nix develop
cargo test --all-targets
cargo fmt && cargo clippy --all-targets --all-features -- -D warnings
nix flake check -L
```

The dev shell provides `cargo`, `pipewire`, `wtype`, `wl-clipboard` and the Sherpa-ONNX/ONNX runtime libraries (`SHERPA_ONNX_LIB_DIR` is preset).

## First run

```bash
syllune doctor          # checks pw-record, wtype, wl-copy and the data directory
syllune stream          # speak; first Ctrl-C stops and injects; second Ctrl-C cancels
```

Stop semantics: the first `Ctrl-C` (or second hotkey activation, or capture EOF) stops capture, flushes the tail frame, sends exactly one finish and injects the single final text; a second `Ctrl-C` or `SIGTERM` force-cancels and never injects partial text.

Configuration lives at `~/.config/syllune/config.toml` (strict TOML — unknown keys are errors; files containing `api_key` must be `0600` or stricter):

```toml
[asr]
streaming_backend = "cloud-realtime"   # or "local-streaming"

[cloud]
api_key = "sk-..."
realtime_endpoint = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
realtime_model = "qwen3-asr-flash-realtime"
```

For the local backend, install the managed model once:

```bash
syllune model install streaming-paraformer-bilingual-zh-en
syllune stream --backend local-streaming
```

## Commands

| Command | Purpose |
| --- | --- |
| `syllune stream` | realtime capture + recognition; `--backend`, `--mode`, `--json`, `--no-inject` |
| `syllune transcribe <wav>` | batch-transcribe a WAV (cloud DashScope or local SenseVoice) |
| `syllune record --seconds N` | timed capture, then transcribe |
| `syllune model list\|install\|check\|remove` | model catalog with pinned URLs, byte counts, SRI SHA-256 and member allowlists |
| `syllune mode list\|reload\|add\|update\|remove` | text-processing modes |
| `syllune history list\|delete\|export\|totals\|usage` | recognition history (SQLite) |
| `syllune history serve` | local web console for history + recordings (`--host`, `--port`; default `http://127.0.0.1:8790`) |
| `syllune daemon` | headless daemon exporting `dev.syllune.Daemon.Controller` over D-Bus |
| `syllune doctor` | dependency and data-directory checks |
| `syllune benchmark asr\|latency` | CER / stop→inject latency gates (skip, never fake-pass, without credentials or a Wayland injector) |

## Configuration

Full key reference (all optional; defaults shown):

```toml
[asr]
streaming_backend = "cloud-realtime"   # cloud-realtime | local-streaming
batch_backend = "cloud"                # cloud | local (SenseVoice)
# local_model_dir / batch_model_dir: override managed model locations

[cloud]
api_key = ""                           # required for cloud backends; file must be 0600
base_url = "https://dashscope.aliyuncs.com"
model = "qwen3-asr-flash-2026-02-10"
timeout_seconds = 60.0
realtime_endpoint = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
realtime_model = "qwen3-asr-flash-realtime"

[inject]
prefer = "wtype"                       # wtype | clipboard
wtype_command = "wtype"
wl_copy_command = "wl-copy"
clipboard_fallback = true
timeout_seconds = 10.0

[processing]
provider = "none"                      # none | openai-compatible | ollama
base_url = ""
model = ""
api_key_env = ""                       # env var name holding the key
timeout_seconds = 30.0

[history]
enabled = true
save_audio = true                      # keep a WAV per successful session
```

Legacy `cloud-vad`, `sensevoice-vad` and `final_backend` values are rejected outright, never silently rewritten.

## Modes & injection

`quick` mode injects the recognized text with zero external calls. Other modes (polish, prompt optimization, translate-to-English) send the final text through the `[processing]` provider; on processing failure the raw transcript is kept and a warning is emitted. The final text is injected at most once; history only stores successful authoritative texts.

## History, recordings & web console

Every successful streaming session mirrors its captured PCM to a canonical 16 kHz mono WAV under `~/.local/share/syllune/audio/` (≈115 KB/minute) and stores `audio_path` + `duration_seconds` in the history database (schema v2; v1 databases migrate on open). Cancelled, failed and empty sessions leave no file; `history delete` / `delete --all` remove the recordings together with their rows; `[history] save_audio = false` disables retention.

```bash
syllune history serve            # http://127.0.0.1:8790/
syllune history serve --port 9000
```

The console is a single embedded page, bound to loopback only: records grouped by day, real waveforms rendered from the saved WAVs, click-to-play with seekable progress, totals for records/characters/audio time, and cursor pagination. Audio URLs carry only the record id; the file path is resolved from the database and served with `Range` support so browsers stream while playing.

## Model catalog

| ID | Version / bytes | Pinned source & SRI SHA-256 |
| --- | --- | --- |
| `streaming-paraformer-bilingual-zh-en` | `asr-models-2024-03-10`; `1047319737` | <https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-paraformer-bilingual-zh-en.tar.bz2><br>`sha256-VGKh/OQmk96uVyrx6MRocSSxKqhf5h/00xaLtSgOIF8=` |

Installs verify the SRI hash and byte count over HTTPS, reject archive members outside the allowlist, write a per-file-hash manifest and activate atomically via `versions/<id>/<version>-<digest12>` + `current` symlink. Model integrity is re-verified before every session; a corrupted install is never used. License status is reported by `syllune model list --json` (`license_status`) and needs independent verification before redistribution.

## Migration from type4me-linux

Syllune only uses `syllune`-named XDG directories and never reads, moves or deletes legacy `type4me-linux` state. Manual migration, if desired:

- Config: copy the `[cloud]` section from `~/.config/type4me-linux/config.toml` and set `streaming_backend` to `cloud-realtime` or `local-streaming` (old enum values were removed).
- Models: run `syllune model install streaming-paraformer-bilingual-zh-en`; the catalog is independent and old files are not adopted automatically.
- History: the old SQLite history is not imported; export CSV with the old tool if needed.
- Rollback: reinstall the old `type4me-linux` release; the two apps never touch each other's state.

## Development

```bash
just test    # nix develop -c cargo test --all-targets
just lint    # cargo fmt --check + clippy -D warnings
just check   # lint + test + nix flake check -L
just run …   # nix run . -- …
```

Real quality/latency gates (`benchmark asr` CER ≤ 0.02, `benchmark latency` stop→inject p99 ≤ 1.0 s) are documented in `docs/low-latency-rust-plan.md` and the OpenSpec change; without real credentials or a Wayland injection environment they report skip and never produce a passing verdict.

Design docs live in `openspec/changes/migrate-syllune-native-streaming/`; the low-latency plan in `docs/low-latency-rust-plan.md`.

## Related

- PipeWire: <https://pipewire.org/>
- Sherpa-ONNX: <https://github.com/k2-fsa/sherpa-onnx>
- XDG Desktop Portal: <https://flatpak.github.io/xdg-desktop-portal/>
