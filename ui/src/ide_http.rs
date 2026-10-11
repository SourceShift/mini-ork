//! The IDE's fast path to mini-ork: a `mini-ork serve` spoken to over loopback.
//!
//! Reading the board otherwise means running `mini-ork board --json` as a
//! subprocess — a `/bin/sh` launcher, the dispatcher Python, and a second
//! `python -m` for the module, ~0.25 s of interpreter startup before any of
//! mini-ork's own work, paid again on every poll and every click. The same
//! payloads are served from a process that is already warm (`web/routes/ide.py`
//! runs the identical `board_cmd` code), so this module asks that server first
//! and the subprocess stays the fallback.
//!
//! The server is started lazily by the first read that finds none, and left
//! running for the session — like a language server, and because the state that
//! makes it worth having (mini-ork's imports, its DB handles) is exactly the
//! state that a per-read process would have to rebuild. If it cannot start —
//! no `fastapi`/`uvicorn` in that checkout, the port taken, no `state.db` —
//! `get` returns `None` and every read falls back, so a missing server costs
//! the IDE the speedup and nothing else.

use std::{
    path::Path,
    sync::{
        Arc, OnceLock,
        atomic::{AtomicU8, Ordering},
    },
    time::{Duration, Instant},
};

use futures::AsyncReadExt as _;
use http_client::{AsyncBody, HttpClient, HttpRequestExt as _, Request, StatusCode, Url};
use serde_json::Value;
use util::command::Stdio;

/// The port `mini-ork serve` binds by default. Fixed rather than negotiated
/// because a server the user already started by hand is worth reusing, and
/// because a fixed port is what the first probe can look at.
const PORT: u16 = 7090;
/// How long to keep probing a server we started before giving up on it for the
/// session. `mini-ork serve` imports fastapi and uvicorn, which is the slow part.
const START_TIMEOUT: Duration = Duration::from_secs(15);
/// A read that hangs would be worse than the subprocess it replaced.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);

/// Never tried; a read may start a server.
const UNTRIED: u8 = 0;
/// A server answered.
const UP: u8 = 1;
/// A server was started and has not answered yet.
const STARTING: u8 = 2;
/// No server, and none is coming — every read takes the subprocess path.
const DOWN: u8 = 3;

static STATE: AtomicU8 = AtomicU8::new(UNTRIED);
/// When the server was started, for `START_TIMEOUT`. Set once: `STATE`'s
/// transition out of `UNTRIED` is what guards the single spawn.
static STARTED_AT: OnceLock<Instant> = OnceLock::new();
/// The app's HTTP client, handed over once by `store::init`. Until then no
/// request is made and every read takes the subprocess path.
static HTTP: OnceLock<Arc<dyn HttpClient>> = OnceLock::new();

pub fn init(client: Arc<dyn HttpClient>) {
    HTTP.set(client).ok();
}

/// What one probe of the server found.
enum Probe {
    /// A payload this read can use.
    Payload(Value),
    /// The server answered, but not with this read — a 4xx for a home with no
    /// `state.db` yet, or a body that is not JSON. It is plainly up, so it must
    /// not be mistaken for a server that is missing and restarted.
    Rejected,
    /// Nothing is listening.
    Offline,
}

/// One read over loopback, or `None` to mean "take the subprocess path".
///
/// `bin` is the mini-ork launcher (started if nothing answers) and `home` the
/// project to read — sent as the `home` query parameter, which is how one
/// server serves whichever project the IDE has open.
pub async fn get(bin: &Path, home: &Path, path: &str, query: &[(&str, String)]) -> Option<Value> {
    if STATE.load(Ordering::Relaxed) == DOWN {
        return None;
    }
    match probe(home, path, query).await {
        Probe::Payload(value) => {
            STATE.store(UP, Ordering::Relaxed);
            Some(value)
        }
        Probe::Rejected => {
            STATE.store(UP, Ordering::Relaxed);
            None
        }
        Probe::Offline => {
            match STATE.load(Ordering::Relaxed) {
                // It answered before and does not now: the server is gone.
                // Stop paying a refused connection on every read.
                UP => STATE.store(DOWN, Ordering::Relaxed),
                UNTRIED => {
                    // One spawn, whichever read gets here first.
                    if STATE
                        .compare_exchange(UNTRIED, STARTING, Ordering::Relaxed, Ordering::Relaxed)
                        .is_ok()
                    {
                        STARTED_AT.set(Instant::now()).ok();
                        start(bin, home);
                    }
                }
                STARTING => {
                    if STARTED_AT.get().is_some_and(|at| at.elapsed() > START_TIMEOUT) {
                        STATE.store(DOWN, Ordering::Relaxed);
                    }
                }
                _ => {}
            }
            None
        }
    }
}

async fn probe(home: &Path, path: &str, query: &[(&str, String)]) -> Probe {
    let Some(client) = HTTP.get() else {
        return Probe::Offline;
    };
    let Ok(mut url) = Url::parse(&format!("http://127.0.0.1:{PORT}{path}")) else {
        return Probe::Rejected;
    };
    {
        let mut pairs = url.query_pairs_mut();
        pairs.append_pair("home", &home.display().to_string());
        for (name, value) in query {
            pairs.append_pair(name, value);
        }
    }
    let Ok(request) = Request::get(url.as_str())
        .timeout(REQUEST_TIMEOUT)
        .body(AsyncBody::default())
    else {
        return Probe::Rejected;
    };
    // A refused connection is the one outcome that means "no server"; a reply
    // of any kind means there is one.
    let Ok(mut response) = client.send(request).await else {
        return Probe::Offline;
    };
    if response.status() != StatusCode::OK {
        return Probe::Rejected;
    }
    let mut body = Vec::new();
    if response.body_mut().read_to_end(&mut body).await.is_err() {
        return Probe::Rejected;
    }
    match serde_json::from_slice(&body) {
        Ok(value) => Probe::Payload(value),
        Err(_) => Probe::Rejected,
    }
}

/// Start `mini-ork serve` for this home and let it outlive the IDE. Detached: no
/// pipes to inherit, and no kill when the handle drops, since the whole point is
/// that it stays up between reads.
fn start(bin: &Path, home: &Path) {
    let mut command = util::command::new_command(bin);
    command
        .args([
            "serve".to_string(),
            "--port".to_string(),
            PORT.to_string(),
            "--home".to_string(),
        ])
        .arg(home)
        .env("MINI_ORK_HOME", home)
        // Let the launcher re-exec into mini-ork's own Python; an inherited
        // flag would stop it.
        .env_remove("MINI_ORK_VENV_ACTIVE")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(false);
    if let Some(project) = home.parent() {
        command.current_dir(project);
    }
    if let Err(error) = command.spawn() {
        log::warn!("mini-ork: could not start the IDE server: {error:#}");
    }
}
