//! Recognition history persisted in SQLite with the Python-compatible
//! `recognition_history` schema, WAL journaling and 0600 permissions.

use std::path::{Path, PathBuf};

use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;
use time::format_description::well_known::Rfc3339;
use time::{Duration, OffsetDateTime};

use crate::coordinator::HistoryEntry;

const SCHEMA_VERSION: i64 = 1;
const BUSY_TIMEOUT_MS: i64 = 5_000;

#[derive(Debug, thiserror::Error)]
pub enum HistoryError {
    #[error("history database error: {0}")]
    Database(String),
    #[error("history timestamp invalid: {0}")]
    Timestamp(String),
    #[error("history cursor invalid")]
    Cursor,
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct HistoryRecord {
    pub id: String,
    pub created_at: String,
    pub duration_seconds: Option<f64>,
    pub raw_text: String,
    pub processing_mode: Option<String>,
    pub processed_text: Option<String>,
    pub final_text: String,
    pub status: String,
    pub character_count: i64,
    pub asr_provider: Option<String>,
    pub asr_model: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct HistoryPage {
    pub records: Vec<HistoryRecord>,
    pub next_cursor: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct HistoryTotals {
    pub records: i64,
    pub characters: i64,
    pub duration_seconds: f64,
}

pub struct HistoryStore {
    path: PathBuf,
}

impl HistoryStore {
    pub fn open(path: PathBuf) -> Result<Self, HistoryError> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let store = Self { path };
        store.migrate()?;
        Ok(store)
    }

    fn connect(&self) -> Result<Connection, HistoryError> {
        let connection = Connection::open(&self.path)
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        connection
            .pragma_update(None, "busy_timeout", BUSY_TIMEOUT_MS)
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        connection
            .pragma_update(None, "journal_mode", "WAL")
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        Ok(connection)
    }

    fn migrate(&self) -> Result<(), HistoryError> {
        let connection = self.connect()?;
        connection
            .execute_batch("BEGIN IMMEDIATE")
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        let version: i64 = connection
            .pragma_query_value(None, "user_version", |row| row.get(0))
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        if version > SCHEMA_VERSION {
            return Err(HistoryError::Database(format!(
                "history database version too new: {version}"
            )));
        }
        if version < 1 {
            connection
                .execute_batch(
                    "CREATE TABLE IF NOT EXISTS recognition_history (
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
                    );
                    PRAGMA user_version = 1;",
                )
                .map_err(|error| HistoryError::Database(error.to_string()))?;
        }
        connection
            .execute_batch("COMMIT")
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let permissions = std::fs::Permissions::from_mode(0o600);
            std::fs::set_permissions(&self.path, permissions)?;
        }
        Ok(())
    }

    pub fn insert(&self, entry: &HistoryEntry, backend: &str) -> Result<HistoryRecord, HistoryError> {
        let id = new_record_id();
        let created_at = timestamp_now()?;
        let connection = self.connect()?;
        connection
            .execute(
                "INSERT INTO recognition_history (
                    id, created_at, duration_seconds, raw_text, processing_mode,
                    processed_text, final_text, status, character_count,
                    asr_provider, asr_model
                ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
                params![
                    id,
                    created_at,
                    None::<f64>,
                    entry.raw_text,
                    entry.processing_mode,
                    entry.processed_text,
                    entry.final_text,
                    entry.status,
                    entry.final_text.chars().count() as i64,
                    backend,
                    None::<String>,
                ],
            )
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        Ok(HistoryRecord {
            id,
            created_at,
            duration_seconds: None,
            raw_text: entry.raw_text.clone(),
            processing_mode: Some(entry.processing_mode.clone()),
            processed_text: entry.processed_text.clone(),
            final_text: entry.final_text.clone(),
            status: entry.status.clone(),
            character_count: entry.final_text.chars().count() as i64,
            asr_provider: Some(backend.to_owned()),
            asr_model: None,
        })
    }

    pub fn query(
        &self,
        limit: i64,
        cursor: Option<&str>,
    ) -> Result<HistoryPage, HistoryError> {
        if !(1..=1_000).contains(&limit) {
            return Err(HistoryError::Database(format!(
                "page size must be between 1 and 1000, got {limit}"
            )));
        }
        let connection = self.connect()?;
        let mut statement;
        let rows = if let Some(cursor) = cursor {
            let (created_at, record_id) = decode_cursor(cursor)?;
            statement = connection
                .prepare(
                    "SELECT id, created_at, duration_seconds, raw_text, processing_mode,
                            processed_text, final_text, status, character_count,
                            asr_provider, asr_model
                     FROM recognition_history
                     WHERE (created_at < ?1 OR (created_at = ?1 AND id < ?2))
                     ORDER BY created_at DESC, id DESC LIMIT ?3",
                )
                .map_err(|error| HistoryError::Database(error.to_string()))?;
            statement
                .query_map(params![created_at, record_id, limit + 1], record_from_row)
                .map_err(|error| HistoryError::Database(error.to_string()))?
        } else {
            statement = connection
                .prepare(
                    "SELECT id, created_at, duration_seconds, raw_text, processing_mode,
                            processed_text, final_text, status, character_count,
                            asr_provider, asr_model
                     FROM recognition_history
                     ORDER BY created_at DESC, id DESC LIMIT ?1",
                )
                .map_err(|error| HistoryError::Database(error.to_string()))?;
            statement
                .query_map(params![limit + 1], record_from_row)
                .map_err(|error| HistoryError::Database(error.to_string()))?
        };
        let mut records = Vec::new();
        for row in rows {
            records.push(row.map_err(|error| HistoryError::Database(error.to_string()))?);
        }
        let has_more = records.len() as i64 > limit;
        records.truncate(limit as usize);
        let next_cursor = if has_more {
            records.last().map(|last| encode_cursor(&last.created_at, &last.id))
        } else {
            None
        };
        Ok(HistoryPage {
            records,
            next_cursor,
        })
    }

    pub fn delete(&self, ids: &[String]) -> Result<i64, HistoryError> {
        if ids.is_empty() {
            return Ok(0);
        }
        let connection = self.connect()?;
        let placeholders = vec!["?"; ids.len()].join(", ");
        let deleted = connection
            .execute(
                &format!("DELETE FROM recognition_history WHERE id IN ({placeholders})"),
                rusqlite::params_from_iter(ids),
            )
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        Ok(deleted as i64)
    }

    pub fn delete_all(&self) -> Result<i64, HistoryError> {
        let connection = self.connect()?;
        let deleted = connection
            .execute("DELETE FROM recognition_history", [])
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        Ok(deleted as i64)
    }

    pub fn totals(&self) -> Result<HistoryTotals, HistoryError> {
        let connection = self.connect()?;
        connection
            .query_row(
                "SELECT COUNT(*), COALESCE(SUM(character_count), 0),
                        COALESCE(SUM(duration_seconds), 0.0)
                 FROM recognition_history",
                [],
                |row| {
                    Ok(HistoryTotals {
                        records: row.get(0)?,
                        characters: row.get(1)?,
                        duration_seconds: row.get(2)?,
                    })
                },
            )
            .map_err(|error| HistoryError::Database(error.to_string()))
    }

    pub fn usage(&self, days: i64) -> Result<serde_json::Value, HistoryError> {
        if !matches!(days, 1 | 7 | 30) {
            return Err(HistoryError::Database(format!(
                "usage window must be 1, 7 or 30 days, got {days}"
            )));
        }
        let since = (OffsetDateTime::now_utc() - Duration::days(days))
            .format(&Rfc3339)
            .map_err(|error| HistoryError::Timestamp(error.to_string()))?;
        let connection = self.connect()?;
        let mut providers: std::collections::BTreeMap<String, i64> =
            std::collections::BTreeMap::new();
        let mut models: std::collections::BTreeMap<String, i64> =
            std::collections::BTreeMap::new();
        let mut statement = connection
            .prepare(
                "SELECT COALESCE(asr_provider, ''), COUNT(*) FROM recognition_history
                 WHERE created_at >= ?1 GROUP BY 1",
            )
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        let rows = statement
            .query_map(params![since], |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)))
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        for row in rows {
            let (key, count) = row.map_err(|error| HistoryError::Database(error.to_string()))?;
            if !key.is_empty() {
                providers.insert(key, count);
            }
        }
        let mut statement = connection
            .prepare(
                "SELECT COALESCE(asr_model, ''), COUNT(*) FROM recognition_history
                 WHERE created_at >= ?1 GROUP BY 1",
            )
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        let rows = statement
            .query_map(params![since], |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)))
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        for row in rows {
            let (key, count) = row.map_err(|error| HistoryError::Database(error.to_string()))?;
            if !key.is_empty() {
                models.insert(key, count);
            }
        }
        Ok(serde_json::json!({
            "days": days,
            "since": since,
            "providers": providers,
            "models": models,
        }))
    }

    pub fn export_csv(&self, destination: &Path) -> Result<i64, HistoryError> {
        let connection = self.connect()?;
        let mut statement = connection
            .prepare(
                "SELECT id, created_at, duration_seconds, raw_text, processing_mode,
                        processed_text, final_text, status, character_count,
                        asr_provider, asr_model
                 FROM recognition_history ORDER BY created_at DESC, id DESC",
            )
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        let records: Vec<HistoryRecord> = statement
            .query_map([], record_from_row)
            .map_err(|error| HistoryError::Database(error.to_string()))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        let mut csv = String::from(
            "id,created_at,duration_seconds,raw_text,processing_mode,processed_text,final_text,status,character_count,asr_provider,asr_model\n",
        );
        for record in &records {
            let fields = [
                &record.id,
                &record.created_at,
                &record
                    .duration_seconds
                    .map(|value| value.to_string())
                    .unwrap_or_default(),
                &record.raw_text,
                record.processing_mode.as_deref().unwrap_or_default(),
                record.processed_text.as_deref().unwrap_or_default(),
                &record.final_text,
                &record.status,
                &record.character_count.to_string(),
                record.asr_provider.as_deref().unwrap_or_default(),
                record.asr_model.as_deref().unwrap_or_default(),
            ];
            let escaped: Vec<String> = fields
                .iter()
                .map(|field| {
                    if field.contains(',') || field.contains('"') || field.contains('\n') {
                        format!("\"{}\"", field.replace('"', "\"\""))
                    } else {
                        (*field).to_string()
                    }
                })
                .collect();
            csv.push_str(&escaped.join(","));
            csv.push('\n');
        }
        if let Some(parent) = destination.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(destination, csv)?;
        Ok(records.len() as i64)
    }

    pub fn get(&self, record_id: &str) -> Result<Option<HistoryRecord>, HistoryError> {
        let connection = self.connect()?;
        let record = connection
            .query_row(
                "SELECT id, created_at, duration_seconds, raw_text, processing_mode,
                        processed_text, final_text, status, character_count,
                        asr_provider, asr_model
                 FROM recognition_history WHERE id = ?1",
                params![record_id],
                record_from_row,
            )
            .optional()
            .map_err(|error| HistoryError::Database(error.to_string()))?;
        Ok(record)
    }
}

fn record_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<HistoryRecord> {
    Ok(HistoryRecord {
        id: row.get(0)?,
        created_at: row.get(1)?,
        duration_seconds: row.get(2)?,
        raw_text: row.get(3)?,
        processing_mode: row.get(4)?,
        processed_text: row.get(5)?,
        final_text: row.get(6)?,
        status: row.get(7)?,
        character_count: row.get(8)?,
        asr_provider: row.get(9)?,
        asr_model: row.get(10)?,
    })
}

fn timestamp_now() -> Result<String, HistoryError> {
    OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .map_err(|error| HistoryError::Timestamp(error.to_string()))
}

fn new_record_id() -> String {
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

fn encode_cursor(created_at: &str, record_id: &str) -> String {
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use base64::Engine;
    let payload = serde_json::json!([created_at, record_id]).to_string();
    URL_SAFE_NO_PAD.encode(payload.as_bytes())
}

fn decode_cursor(cursor: &str) -> Result<(String, String), HistoryError> {
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use base64::Engine;
    let bytes = URL_SAFE_NO_PAD
        .decode(cursor.as_bytes())
        .map_err(|_| HistoryError::Cursor)?;
    let value: serde_json::Value =
        serde_json::from_slice(&bytes).map_err(|_| HistoryError::Cursor)?;
    let created_at = value
        .get(0)
        .and_then(serde_json::Value::as_str)
        .ok_or(HistoryError::Cursor)?
        .to_owned();
    let record_id = value
        .get(1)
        .and_then(serde_json::Value::as_str)
        .ok_or(HistoryError::Cursor)?
        .to_owned();
    Ok((created_at, record_id))
}
