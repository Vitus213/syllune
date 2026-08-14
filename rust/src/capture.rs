use std::io;
use std::process::Stdio;

use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::{Child, ChildStdout, Command};

pub const SAMPLE_RATE: u32 = 16_000;
pub const CHUNK_BYTES: usize = 1_024;

pub struct RawCapture {
    child: Child,
    stdout: ChildStdout,
    pending: Vec<u8>,
}

impl RawCapture {
    pub fn start() -> io::Result<Self> {
        let mut child = Command::new("pw-record")
            .args([
                "--rate",
                "16000",
                "--channels",
                "1",
                "--format",
                "s16",
                "--raw",
                "--latency",
                "32ms",
                "-",
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .kill_on_drop(true)
            .spawn()?;
        let stdout = child.stdout.take().ok_or_else(|| {
            io::Error::new(io::ErrorKind::BrokenPipe, "pw-record stdout unavailable")
        })?;
        Ok(Self {
            child,
            stdout,
            pending: Vec::with_capacity(CHUNK_BYTES),
        })
    }

    pub async fn next_chunk(&mut self) -> io::Result<Option<Vec<u8>>> {
        let mut buffer = [0_u8; CHUNK_BYTES];
        loop {
            let read = self.stdout.read(&mut buffer).await?;
            if read == 0 {
                if self.pending.is_empty() {
                    return Ok(None);
                }
                if self.pending.len() % 2 != 0 {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "capture ended with an incomplete PCM16 sample",
                    ));
                }
                return Ok(Some(std::mem::take(&mut self.pending)));
            }
            self.pending.extend_from_slice(&buffer[..read]);
            if self.pending.len() >= CHUNK_BYTES {
                return Ok(Some(self.pending.drain(..CHUNK_BYTES).collect()));
            }
        }
    }

    pub async fn stop(&mut self) -> io::Result<Option<Vec<u8>>> {
        if let Some(pid) = self.child.id() {
            #[cfg(unix)]
            unsafe {
                libc::kill(pid as i32, libc::SIGINT);
            }
            #[cfg(not(unix))]
            self.child.kill().await?;
        }
        let mut tail = Vec::new();
        self.stdout.read_to_end(&mut tail).await?;
        self.pending.extend_from_slice(&tail);
        let status = self.child.wait().await?;
        if !status.success() && self.pending.is_empty() {
            return Ok(None);
        }
        if self.pending.len() % 2 != 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "capture ended with an incomplete PCM16 sample",
            ));
        }
        if self.pending.is_empty() {
            Ok(None)
        } else {
            Ok(Some(std::mem::take(&mut self.pending)))
        }
    }
}

pub async fn read_fixed_chunks<R>(mut reader: R) -> io::Result<Vec<Vec<u8>>>
where
    R: AsyncRead + Unpin,
{
    let mut data = Vec::new();
    reader.read_to_end(&mut data).await?;
    if data.len() % 2 != 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "capture ended with an incomplete PCM16 sample",
        ));
    }
    Ok(data
        .chunks(CHUNK_BYTES)
        .filter(|chunk| !chunk.is_empty())
        .map(ToOwned::to_owned)
        .collect())
}
