//! Text processing modes: builtin catalog plus a JSON-persisted repository
//! compatible with the Python `modes.json` layout.

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Mode {
    pub id: String,
    pub name: String,
    pub prompt: String,
    pub processing_label: String,
    pub builtin: bool,
    pub sort_order: i64,
}

#[derive(Debug, thiserror::Error)]
pub enum ModesError {
    #[error("mode not found: {0}")]
    NotFound(String),
    #[error("builtin mode cannot be modified: {0}")]
    BuiltinImmutable(String),
    #[error("mode name already exists: {0}")]
    DuplicateName(String),
    #[error("mode name must not be empty")]
    EmptyName,
    #[error("cannot read modes file {0}: {1}")]
    Read(String, String),
    #[error("cannot write modes file {0}: {1}")]
    Write(String, String),
}

pub fn builtin_modes() -> Vec<Mode> {
    vec![
        Mode {
            id: "quick".to_owned(),
            name: "快速输入".to_owned(),
            prompt: String::new(),
            processing_label: "处理中".to_owned(),
            builtin: true,
            sort_order: 0,
        },
        Mode {
            id: "voice-polish".to_owned(),
            name: "语音润色".to_owned(),
            prompt: "在不改变原意、不编造事实的前提下，删除口头语并修正语病，输出简体中文；英文与代码原样保留，数字一律使用阿拉伯数字。只输出处理后的文本，不要解释。原文：{text}".to_owned(),
            processing_label: "润色中".to_owned(),
            builtin: true,
            sort_order: 1,
        },
        Mode {
            id: "prompt-optimize".to_owned(),
            name: "提示词优化".to_owned(),
            prompt: "将以下需求改写为清晰、可执行的提示词。保留所有事实和约束；只输出提示词，不要解释。原文：{text}".to_owned(),
            processing_label: "优化中".to_owned(),
            builtin: true,
            sort_order: 2,
        },
        Mode {
            id: "translate-en".to_owned(),
            name: "翻译为英文".to_owned(),
            prompt: "将以下文本准确翻译为自然英文。只输出译文，不要解释。原文：{text}".to_owned(),
            processing_label: "翻译中".to_owned(),
            builtin: true,
            sort_order: 3,
        },
    ]
}

/// One-pass template expansion; only known placeholders are substituted and
/// inserted content is never re-parsed.
pub fn render_template(template: &str, text: &str, selected: &str, clipboard: &str) -> String {
    let mut output = String::with_capacity(template.len());
    let mut cursor = 0;
    let bytes = template.as_bytes();
    while cursor < bytes.len() {
        let Some(opening) = template[cursor..].find('{') else {
            output.push_str(&template[cursor..]);
            break;
        };
        let opening = cursor + opening;
        output.push_str(&template[cursor..opening]);
        let Some(closing) = template[opening + 1..].find('}') else {
            output.push_str(&template[opening..]);
            break;
        };
        let closing = opening + 1 + closing;
        let field = &template[opening + 1..closing];
        match field {
            "text" => output.push_str(text),
            "selected" => output.push_str(selected),
            "clipboard" => output.push_str(clipboard),
            _ => output.push_str(&template[opening..closing + 1]),
        }
        cursor = closing + 1;
    }
    output
}

pub struct ModesRepository {
    path: PathBuf,
    modes: Vec<Mode>,
}

impl ModesRepository {
    /// Open the repository. A missing file loads the builtin modes into
    /// memory without touching disk; the file is only created on the first
    /// mutation so read-only commands work anywhere.
    pub fn open(path: PathBuf) -> Result<Self, ModesError> {
        let modes = if path.exists() {
            Self::load(&path)?
        } else {
            sort_modes(builtin_modes())
        };
        Ok(Self { path, modes })
    }

    fn load(path: &Path) -> Result<Vec<Mode>, ModesError> {
        let raw = fs::read_to_string(path).map_err(|error| {
            ModesError::Read(path.display().to_string(), error.to_string())
        })?;
        let modes: Vec<Mode> = serde_json::from_str(&raw).map_err(|error| {
            ModesError::Read(path.display().to_string(), error.to_string())
        })?;
        validate(&modes).map_err(|message| ModesError::Read(path.display().to_string(), message))?;
        Ok(sort_modes(modes))
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn list(&self) -> &[Mode] {
        &self.modes
    }

    pub fn reload(&mut self) -> Result<&[Mode], ModesError> {
        self.modes = Self::load(&self.path)?;
        Ok(&self.modes)
    }

    pub fn get(&self, mode_id: &str) -> Result<&Mode, ModesError> {
        self.modes
            .iter()
            .find(|mode| mode.id == mode_id)
            .ok_or_else(|| ModesError::NotFound(mode_id.to_owned()))
    }

    /// Resolve by id first, then by case-insensitive name; empty input maps
    /// to quick mode.
    pub fn resolve(&self, identifier: Option<&str>) -> Result<&Mode, ModesError> {
        match identifier {
            None => self.get("quick"),
            Some(identifier) if identifier.trim().is_empty() => self.get("quick"),
            Some(identifier) => {
                if let Some(mode) = self.modes.iter().find(|mode| mode.id == identifier) {
                    return Ok(mode);
                }
                let key = normalized_name(identifier);
                self.modes
                    .iter()
                    .find(|mode| normalized_name(&mode.name) == key)
                    .ok_or_else(|| ModesError::NotFound(identifier.to_owned()))
            }
        }
    }

    pub fn add(
        &mut self,
        name: &str,
        prompt: &str,
        processing_label: &str,
    ) -> Result<Mode, ModesError> {
        let name = clean_name(name)?;
        self.ensure_unique_name(&name, None)?;
        let sort_order = self.next_sort_order();
        let mode = Mode {
            id: new_mode_id(),
            name,
            prompt: prompt.to_owned(),
            processing_label: processing_label.to_owned(),
            builtin: false,
            sort_order,
        };
        self.modes.push(mode.clone());
        self.persist()?;
        Ok(mode)
    }

    pub fn update(
        &mut self,
        mode_id: &str,
        name: Option<&str>,
        prompt: Option<&str>,
        processing_label: Option<&str>,
    ) -> Result<Mode, ModesError> {
        let index = self
            .modes
            .iter()
            .position(|mode| mode.id == mode_id)
            .ok_or_else(|| ModesError::NotFound(mode_id.to_owned()))?;
        if self.modes[index].builtin {
            return Err(ModesError::BuiltinImmutable(mode_id.to_owned()));
        }
        if let Some(name) = name {
            let cleaned = clean_name(name)?;
            self.ensure_unique_name(&cleaned, Some(mode_id))?;
            self.modes[index].name = cleaned;
        }
        if let Some(prompt) = prompt {
            self.modes[index].prompt = prompt.to_owned();
        }
        if let Some(label) = processing_label {
            self.modes[index].processing_label = label.to_owned();
        }
        let mode = self.modes[index].clone();
        self.persist()?;
        Ok(mode)
    }

    pub fn remove(&mut self, mode_id: &str) -> Result<Mode, ModesError> {
        let index = self
            .modes
            .iter()
            .position(|mode| mode.id == mode_id)
            .ok_or_else(|| ModesError::NotFound(mode_id.to_owned()))?;
        if self.modes[index].builtin {
            return Err(ModesError::BuiltinImmutable(mode_id.to_owned()));
        }
        let mode = self.modes.remove(index);
        self.persist()?;
        Ok(mode)
    }

    fn persist(&mut self) -> Result<(), ModesError> {
        self.modes = sort_modes(std::mem::take(&mut self.modes));
        validate(&self.modes)
            .map_err(|message| ModesError::Write(self.path.display().to_string(), message))?;
        store(&self.path, &self.modes)
    }

    fn next_sort_order(&self) -> i64 {
        self.modes
            .iter()
            .map(|mode| mode.sort_order)
            .max()
            .unwrap_or(-1)
            + 1
    }

    fn ensure_unique_name(&self, name: &str, excluding: Option<&str>) -> Result<(), ModesError> {
        let key = normalized_name(name);
        if self
            .modes
            .iter()
            .any(|mode| Some(mode.id.as_str()) != excluding && normalized_name(&mode.name) == key)
        {
            return Err(ModesError::DuplicateName(name.to_owned()));
        }
        Ok(())
    }
}

fn store(path: &Path, modes: &[Mode]) -> Result<(), ModesError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            ModesError::Write(path.display().to_string(), error.to_string())
        })?;
    }
    let content = serde_json::to_string_pretty(modes)
        .map_err(|error| ModesError::Write(path.display().to_string(), error.to_string()))?
        + "\n";
    let temporary = path.with_extension("tmp");
    fs::write(&temporary, content)
        .map_err(|error| ModesError::Write(path.display().to_string(), error.to_string()))?;
    fs::rename(&temporary, path)
        .map_err(|error| ModesError::Write(path.display().to_string(), error.to_string()))?;
    Ok(())
}

fn sort_modes(mut modes: Vec<Mode>) -> Vec<Mode> {
    modes.sort_by(|left, right| {
        left.sort_order
            .cmp(&right.sort_order)
            .then_with(|| left.name.cmp(&right.name))
            .then_with(|| left.id.cmp(&right.id))
    });
    modes
}

fn validate(modes: &[Mode]) -> Result<(), String> {
    let mut ids = std::collections::HashSet::new();
    for mode in modes {
        if mode.id.trim().is_empty() {
            return Err("mode id must not be empty".to_owned());
        }
        if !ids.insert(mode.id.clone()) {
            return Err(format!("duplicate mode id: {}", mode.id));
        }
    }
    for builtin in builtin_modes() {
        if !modes.iter().any(|mode| mode.id == builtin.id) {
            return Err(format!("missing builtin mode: {}", builtin.id));
        }
    }
    Ok(())
}

fn clean_name(name: &str) -> Result<String, ModesError> {
    let cleaned = name.trim();
    if cleaned.is_empty() {
        return Err(ModesError::EmptyName);
    }
    Ok(cleaned.to_owned())
}

fn normalized_name(value: &str) -> String {
    value.trim().to_lowercase()
}

/// UUID-v4 style identifier sourced from the OS CSPRNG, formatted with
/// hyphens for compatibility with the Python repository.
fn new_mode_id() -> String {
    let mut bytes = [0_u8; 16];
    getrandom::getrandom(&mut bytes).expect("OS random source available");
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}
