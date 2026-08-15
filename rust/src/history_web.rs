//! `syllune history serve`: local web console over the SQLite history
//! store. Hand-rolled HTTP/1.1 over tokio TCP — GET only, one response
//! per connection, no external dependencies. Binds loopback by default:
//! history rows and recordings are private data.

use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

use crate::history::HistoryStore;

/// Embedded single-file console (HTML + CSS + JS).
pub const CONSOLE_PAGE: &str = include_str!("../assets/history-console.html");

const MAX_REQUEST_HEAD: usize = 16_384;
const DEFAULT_PAGE_LIMIT: i64 = 200;

#[derive(Debug, Clone)]
pub struct ServeOptions {
    pub host: String,
    pub port: u16,
}

impl Default for ServeOptions {
    fn default() -> Self {
        Self {
            host: "127.0.0.1".to_owned(),
            port: 8790,
        }
    }
}

pub async fn serve(store_path: PathBuf, options: ServeOptions) -> Result<i32, String> {
    let store = Arc::new(
        HistoryStore::open(store_path).map_err(|error| format!("history store: {error}"))?,
    );
    let address: SocketAddr = format!("{}:{}", options.host, options.port)
        .parse()
        .map_err(|error| format!("invalid listen address: {error}"))?;
    let listener = TcpListener::bind(address)
        .await
        .map_err(|error| format!("cannot bind {address}: {error}"))?;
    println!("Syllune history console: http://{address}/");
    serve_listener(listener, store).await
}

/// Serve until the accept loop fails; separated from `serve` so tests can
/// bind an ephemeral port themselves.
pub async fn serve_listener(
    listener: TcpListener,
    store: Arc<HistoryStore>,
) -> Result<i32, String> {
    loop {
        let (stream, _) = listener
            .accept()
            .await
            .map_err(|error| format!("accept failed: {error}"))?;
        let store = Arc::clone(&store);
        tokio::spawn(async move {
            handle_connection(stream, &store).await;
        });
    }
}

struct Request {
    method: String,
    path: String,
    query: String,
    range: Option<(u64, Option<u64>)>,
}

async fn handle_connection(mut stream: TcpStream, store: &HistoryStore) {
    let request = match read_request(&mut stream).await {
        Ok(Some(request)) => request,
        Ok(None) => return,
        Err(error) => {
            let reply = respond(error.status, "text/plain", error.message.as_bytes());
            let _ = stream.write_all(&reply).await;
            return;
        }
    };
    if request.method != "GET" {
        let reply = respond(405, "text/plain", b"only GET is supported");
        let _ = stream.write_all(&reply).await;
        return;
    }
    let reply = route(&request, store);
    let _ = stream.write_all(&reply).await;
}

struct RequestError {
    status: u16,
    message: String,
}

async fn read_request(stream: &mut TcpStream) -> Result<Option<Request>, RequestError> {
    let mut buffer: Vec<u8> = Vec::new();
    let mut chunk = [0_u8; 2048];
    loop {
        let read = stream
            .read(&mut chunk)
            .await
            .map_err(|error| RequestError {
                status: 400,
                message: format!("read failed: {error}"),
            })?;
        if read == 0 {
            return Ok(None);
        }
        buffer.extend_from_slice(&chunk[..read]);
        let Some(end) = find_header_end(&buffer) else {
            if buffer.len() > MAX_REQUEST_HEAD {
                return Err(RequestError {
                    status: 431,
                    message: "request head too large".to_owned(),
                });
            }
            continue;
        };
        let head = String::from_utf8_lossy(&buffer[..end]);
        let mut lines = head.lines();
        let Some(first_line) = lines.next() else {
            return Err(RequestError {
                status: 400,
                message: "missing request line".to_owned(),
            });
        };
        let mut parts = first_line.split_whitespace();
        let (Some(method), Some(target)) = (parts.next(), parts.next()) else {
            return Err(RequestError {
                status: 400,
                message: "malformed request line".to_owned(),
            });
        };
        let (path, query) = match target.split_once('?') {
            Some((path, query)) => (path.to_owned(), query.to_owned()),
            None => (target.to_owned(), String::new()),
        };
        let range = lines
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                (name.eq_ignore_ascii_case("range")).then(|| parse_range(value.trim()))
            })
            .flatten();
        return Ok(Some(Request {
            method: method.to_owned(),
            path,
            query,
            range,
        }));
    }
}

fn find_header_end(buffer: &[u8]) -> Option<usize> {
    buffer.windows(4).position(|window| window == b"\r\n\r\n")
}

/// `bytes=a-b` / `bytes=a-` / `bytes=-n` (suffix, resolved by callers).
fn parse_range(value: &str) -> Option<(u64, Option<u64>)> {
    let spec = value.strip_prefix("bytes=")?;
    let (start, end) = spec.split_once('-')?;
    if start.is_empty() {
        let suffix = end.parse::<u64>().ok()?;
        (suffix > 0).then_some((suffix, None))
    } else {
        let start = start.parse::<u64>().ok()?;
        let end = if end.is_empty() {
            None
        } else {
            Some(end.parse::<u64>().ok()?)
        };
        Some((start, end))
    }
}

fn route(request: &Request, store: &HistoryStore) -> Vec<u8> {
    match request.path.as_str() {
        "/" | "/index.html" => respond(200, "text/html; charset=utf-8", CONSOLE_PAGE.as_bytes()),
        "/api/records" => {
            let params = parse_query(&request.query);
            let limit = params
                .get("limit")
                .and_then(|value| value.parse::<i64>().ok())
                .unwrap_or(DEFAULT_PAGE_LIMIT);
            let cursor = params.get("cursor").map(String::as_str);
            match store.query(limit, cursor) {
                Ok(page) => json_response(serde_json::json!({
                    "records": page.records,
                    "next_cursor": page.next_cursor,
                })),
                Err(error) => json_response(serde_json::json!({ "error": error.to_string() })),
            }
        }
        "/api/totals" => match store.totals() {
            Ok(totals) => match serde_json::to_vec(&totals) {
                Ok(body) => respond(200, "application/json; charset=utf-8", &body),
                Err(_) => respond(500, "application/json", b"{\"error\":\"serialize\"}"),
            },
            Err(error) => json_response(serde_json::json!({ "error": error.to_string() })),
        },
        path if path.starts_with("/api/audio/") => audio_response(request, store),
        _ => respond(404, "text/plain", b"not found"),
    }
}

/// Serve the retained WAV for one history record. The record id is
/// validated as a uuid; the file path comes from the database row, never
/// from the URL. Supports `Range` so the browser can seek while playing.
fn audio_response(request: &Request, store: &HistoryStore) -> Vec<u8> {
    let Some(id) = request
        .path
        .strip_prefix("/api/audio/")
        .and_then(|rest| rest.strip_suffix(".wav"))
    else {
        return respond(404, "text/plain", b"no such recording");
    };
    if !is_record_id(id) {
        return respond(404, "text/plain", b"no such recording");
    }
    let record = match store.get(id) {
        Ok(Some(record)) => record,
        Ok(None) => return respond(404, "text/plain", b"no such recording"),
        Err(_) => return respond(500, "text/plain", b"history store error"),
    };
    let Some(audio_path) = record.audio_path.as_deref() else {
        return respond(404, "text/plain", b"record has no saved audio");
    };
    let bytes = match std::fs::read(audio_path) {
        Ok(bytes) => bytes,
        Err(_) => return respond(410, "text/plain", b"audio file is gone"),
    };
    match request.range {
        Some((start, end)) if start < bytes.len() as u64 => {
            let end = match end {
                Some(end) => end.min(bytes.len() as u64 - 1),
                None => bytes.len() as u64 - 1,
            };
            let slice = &bytes[start as usize..=end as usize];
            partial_response(slice, start, end, bytes.len() as u64)
        }
        Some(_) => {
            let mut headers = vec![(
                "Content-Range".to_owned(),
                format!("bytes */{}", bytes.len()),
            )];
            respond_with(416, "text/plain", b"", &mut headers)
        }
        None => {
            let mut headers = vec![("Accept-Ranges".to_owned(), "bytes".to_owned())];
            respond_with(200, "audio/wav", &bytes, &mut headers)
        }
    }
}

fn is_record_id(id: &str) -> bool {
    id.len() == 36
        && id.as_bytes().iter().enumerate().all(|(index, byte)| {
            byte.is_ascii_hexdigit() || (matches!(index, 8 | 13 | 18 | 23) && *byte == b'-')
        })
}

fn json_response(value: serde_json::Value) -> Vec<u8> {
    match serde_json::to_vec(&value) {
        Ok(body) => respond(200, "application/json; charset=utf-8", &body),
        Err(_) => respond(500, "application/json", b"{\"error\":\"serialize\"}"),
    }
}

fn respond(status: u16, content_type: &str, body: &[u8]) -> Vec<u8> {
    respond_with(status, content_type, body, &mut [])
}

fn respond_with(
    status: u16,
    content_type: &str,
    body: &[u8],
    headers: &mut [(String, String)],
) -> Vec<u8> {
    let mut reply = format!(
        "HTTP/1.1 {status} {}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n",
        reason_phrase(status),
        body.len()
    )
    .into_bytes();
    for (name, value) in headers.iter() {
        reply.extend_from_slice(format!("{name}: {value}\r\n").as_bytes());
    }
    reply.extend_from_slice(b"\r\n");
    reply.extend_from_slice(body);
    reply
}

fn partial_response(slice: &[u8], start: u64, end: u64, total: u64) -> Vec<u8> {
    let mut headers = vec![
        (
            "Content-Range".to_owned(),
            format!("bytes {start}-{end}/{total}"),
        ),
        ("Accept-Ranges".to_owned(), "bytes".to_owned()),
    ];
    respond_with(206, "audio/wav", slice, &mut headers)
}

fn reason_phrase(status: u16) -> &'static str {
    match status {
        200 => "OK",
        206 => "Partial Content",
        400 => "Bad Request",
        404 => "Not Found",
        405 => "Method Not Allowed",
        410 => "Gone",
        416 => "Range Not Satisfiable",
        431 => "Request Header Fields Too Large",
        500 => "Internal Server Error",
        _ => "OK",
    }
}

/// Parse `a=b&c=d` with percent-decoding; later duplicates win.
fn parse_query(query: &str) -> BTreeMap<String, String> {
    let mut params = BTreeMap::new();
    for pair in query.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        params.insert(percent_decode(key), percent_decode(value));
    }
    params
}

fn percent_decode(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut output = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'%' if index + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).unwrap_or("");
                match u8::from_str_radix(hex, 16) {
                    Ok(decoded) => {
                        output.push(decoded);
                        index += 3;
                    }
                    Err(_) => {
                        output.push(bytes[index]);
                        index += 1;
                    }
                }
            }
            b'+' => {
                output.push(b' ');
                index += 1;
            }
            byte => {
                output.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&output).into_owned()
}
