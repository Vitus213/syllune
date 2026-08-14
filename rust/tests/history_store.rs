use syllune::coordinator::HistoryEntry;
use syllune::history::HistoryStore;
use tempfile::tempdir;

fn entry(raw: &str, final_text: &str, mode: &str) -> HistoryEntry {
    HistoryEntry {
        raw_text: raw.to_owned(),
        processed_text: Some(final_text.to_owned()),
        final_text: final_text.to_owned(),
        processing_mode: mode.to_owned(),
        status: "completed".to_owned(),
        backend: "cloud-realtime".to_owned(),
    }
}

#[test]
fn insert_and_get_roundtrip_records_authoritative_text() {
    let root = tempdir().expect("temporary root");
    let store = HistoryStore::open(root.path().join("history.sqlite3")).expect("open store");

    let stored = store
        .insert(&entry("原始", "最终文本", "quick"), "cloud-realtime")
        .expect("insert");
    assert_eq!(stored.raw_text, "原始");
    assert_eq!(stored.final_text, "最终文本");
    assert_eq!(stored.character_count, 4);
    assert_eq!(stored.asr_provider.as_deref(), Some("cloud-realtime"));
    assert!(stored.created_at.ends_with('Z'));

    let fetched = store.get(&stored.id).expect("get").expect("record exists");
    assert_eq!(fetched.raw_text, "原始");
    assert_eq!(fetched.processing_mode.as_deref(), Some("quick"));
}

#[test]
fn schema_is_version_one_and_file_permissions_are_private() {
    use std::os::unix::fs::PermissionsExt;

    let root = tempdir().expect("temporary root");
    let path = root.path().join("history.sqlite3");
    HistoryStore::open(path.clone()).expect("open store");

    let mode = std::fs::metadata(&path)
        .expect("metadata")
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600, "history file must be 0600");

    let connection = rusqlite::Connection::open(&path).expect("open sqlite");
    let version: i64 = connection
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .expect("user_version");
    assert_eq!(version, 1);
}

#[test]
fn query_paginates_with_stable_cursors() {
    let root = tempdir().expect("temporary root");
    let store = HistoryStore::open(root.path().join("history.sqlite3")).expect("open store");
    for index in 0..5 {
        store
            .insert(
                &entry(&format!("raw{index}"), &format!("text{index}"), "quick"),
                "cloud-realtime",
            )
            .expect("insert");
    }

    let page = store.query(2, None).expect("first page");
    assert_eq!(page.records.len(), 2);
    let cursor = page.next_cursor.expect("more pages");

    let second = store.query(2, Some(&cursor)).expect("second page");
    assert_eq!(second.records.len(), 2);
    let first_ids: Vec<&str> = page
        .records
        .iter()
        .map(|record| record.id.as_str())
        .collect();
    assert!(
        second
            .records
            .iter()
            .all(|record| !first_ids.contains(&record.id.as_str())),
        "cursor pages must not repeat records"
    );

    let third = store
        .query(2, second.next_cursor.as_deref())
        .expect("third page");
    assert_eq!(third.records.len(), 1);
    assert!(third.next_cursor.is_none());

    assert!(store.query(0, None).is_err(), "page size must be validated");
    assert!(store.query(2, Some("not-a-cursor")).is_err());
}

#[test]
fn delete_targets_specific_ids_and_delete_all_clears() {
    let root = tempdir().expect("temporary root");
    let store = HistoryStore::open(root.path().join("history.sqlite3")).expect("open store");
    let first = store
        .insert(&entry("a", "a", "quick"), "cloud-realtime")
        .unwrap();
    let second = store
        .insert(&entry("b", "b", "quick"), "cloud-realtime")
        .unwrap();

    assert_eq!(
        store
            .delete(std::slice::from_ref(&first.id))
            .expect("delete"),
        1
    );
    assert!(store.get(&first.id).expect("get").is_none());
    assert!(store.get(&second.id).expect("get").is_some());

    assert_eq!(store.delete_all().expect("delete all"), 1);
    assert_eq!(store.totals().expect("totals").records, 0);
}

#[test]
fn totals_and_usage_aggregate_character_counts() {
    let root = tempdir().expect("temporary root");
    let store = HistoryStore::open(root.path().join("history.sqlite3")).expect("open store");
    store
        .insert(&entry("一", "一二三", "quick"), "cloud-realtime")
        .unwrap();
    store
        .insert(&entry("二", "四五", "quick"), "local-streaming")
        .unwrap();

    let totals = store.totals().expect("totals");
    assert_eq!(totals.records, 2);
    assert_eq!(totals.characters, 5);

    let usage = store.usage(7).expect("usage");
    let providers = usage["providers"].as_object().expect("providers");
    assert_eq!(providers.get("cloud-realtime"), Some(&serde_json::json!(1)));
    assert_eq!(
        providers.get("local-streaming"),
        Some(&serde_json::json!(1))
    );
    assert!(store.usage(3).is_err(), "window must be 1, 7 or 30 days");
}

#[test]
fn export_csv_contains_header_and_escaped_records() {
    let root = tempdir().expect("temporary root");
    let store = HistoryStore::open(root.path().join("history.sqlite3")).expect("open store");
    store
        .insert(&entry("带,逗号", "含\"引号\"", "quick"), "cloud-realtime")
        .expect("insert");

    let destination = root.path().join("export.csv");
    let count = store.export_csv(&destination).expect("export");
    assert_eq!(count, 1);
    let content = std::fs::read_to_string(&destination).expect("read csv");
    assert!(content.starts_with("id,created_at,duration_seconds,raw_text,"));
    assert!(content.contains("\"带,逗号\""));
    assert!(content.contains("\"含\"\"引号\"\"\""));
}

#[test]
fn reopening_store_migrates_idempotently_and_keeps_records() {
    let root = tempdir().expect("temporary root");
    let path = root.path().join("history.sqlite3");
    let store = HistoryStore::open(path.clone()).expect("open store");
    let record = store
        .insert(&entry("持久", "持久", "quick"), "cloud-realtime")
        .unwrap();
    drop(store);

    let store = HistoryStore::open(path).expect("reopen store");
    assert!(store.get(&record.id).expect("get").is_some());
}
