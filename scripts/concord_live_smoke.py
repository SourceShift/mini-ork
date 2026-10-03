#!/usr/bin/env python3
"""Concord live smoke: an isolated ContextNest + real `claude -p` sessions + the
mini-ork concord CLI, across the protocol's scenarios.

Isolation: the substrate runs on its own port from a temp cwd (Config::default and
its local feature embedder, unless --embed-config) with every data path under the work dir. Hooks are installed with
a throwaway HOME into the smoke project's .claude/settings.local.json only, and
sessions run with `--setting-sources local`, so the operator's global settings
and production substrate are never touched.

Assertions are made on server state (principals, deliveries, footprints, claims,
violations, metrics, file contents); the model's text is secondary evidence.

    python3.11 scripts/concord_live_smoke.py --cn-bin <contextnest> [--port 28099] [--only 2,2b]
        [--embed-config <toml with only [services.embedding]>]  # S11 needs a real embedder

Cost: ~20 short `claude -p --model haiku` sessions per full run (about 25 minutes).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

MINI_ORK = Path(__file__).resolve().parents[1]
RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: str, evidence: dict | None = None) -> None:
    RESULTS.append({"scenario": name, "pass": ok, "detail": detail, "evidence": evidence or {}})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


class Smoke:
    def __init__(self, cn_bin: str, port: int, work: Path):
        self.cn_bin, self.port, self.work = cn_bin, port, work
        self.base = f"http://127.0.0.1:{port}"
        self.srv: subprocess.Popen | None = None
        self.project = work / "project"
        self.server_env: dict[str, str] = {}
        self.embed_config: str = ""

    # ── substrate ──────────────────────────────────────────────────────────
    def start_server(self, extra_env: dict[str, str] | None = None) -> None:
        (self.work / "srv").mkdir(exist_ok=True)
        (self.work / "data").mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("CONTEXTNEST_")}
        env.update({
            "CONTEXTNEST_WAL_PATH": str(self.work / "data" / "wal.jsonl"),
            "CONTEXTNEST_TRANSCRIPT_SWEEPER": "false",
            **(extra_env or {}),
        })
        self.server_env = extra_env or {}
        log = open(self.work / "server.log", "a")
        self.srv = subprocess.Popen([self.cn_bin, "serve", "--bind", f"127.0.0.1:{self.port}"],
                                    cwd=self.work / "srv", env=env, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        for _ in range(120):
            try:
                if self.api("GET", "/api/v1/coord/metrics")[0] == 200:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("smoke substrate did not come up; see server.log")

    def stop_server(self) -> None:
        if self.srv and self.srv.poll() is None:
            os.killpg(self.srv.pid, 15)
            self.srv.wait(timeout=20)
        self.srv = None

    def api(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            return e.code, {}

    def metrics(self) -> dict:
        return self.api("GET", "/api/v1/coord/metrics")[1]

    # ── clients ────────────────────────────────────────────────────────────
    def claude(self, prompt: str, principal: str | None, cwd: Path | None = None,
               timeout: int = 300, extra_tools: list[str] | None = None,
               env: dict[str, str] | None = None, tools: list[str] | None = None) -> tuple[int, str, float]:
        env = {**{k: v for k, v in os.environ.items() if k not in ("CONCORD_PRINCIPAL", "TMUX_PANE")},
               **(env or {})}
        if principal:
            env["CONCORD_PRINCIPAL"] = principal
        cmd = ["claude", "-p", prompt, "--model", "haiku", "--setting-sources", "local",
               "--permission-mode", "acceptEdits",
               "--allowedTools", *(tools or ["Read", "Edit", "Write", "Bash(sleep:*)", "Bash(cat:*)"]),
               *(extra_tools or [])]
        if tools is not None:  # acceptEdits auto-approves shell file writes; pin the tool surface
            cmd += ["--disallowedTools", "Bash"]
        t0 = time.time()
        p = subprocess.run(cmd, cwd=cwd or self.project, env=env, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout + p.stderr), time.time() - t0

    def concord(self, *args: str, env: dict | None = None, timeout: int = 120):
        e = {**os.environ, "CN_BASE_URL": self.base, "CN_COORD_TIMEOUT_SEC": "3", **(env or {})}
        return subprocess.run([str(MINI_ORK / "bin" / "mini-ork"), "concord", *args], cwd=self.project,
                              env=e, capture_output=True, text=True, timeout=timeout)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=smoke", "-c", "user.email=smoke@x", *args], cwd=cwd,
                   check=True, capture_output=True)


def install_hooks(s: Smoke, project: Path) -> Path:
    fake_home = s.work / "fakehome"
    fake_home.mkdir(exist_ok=True)
    env = {**os.environ, "HOME": str(fake_home)}
    subprocess.run([s.cn_bin, "ingest", "claude-code", "--install-hooks", "--substrate", s.base,
                    "--project-path", str(project)], env=env, check=True, capture_output=True, text=True)
    return project / ".claude" / "settings.local.json"


# ── scenarios ──────────────────────────────────────────────────────────────

def s0_install(s: Smoke) -> None:
    settings = install_hooks(s, s.project)
    cfg = json.loads(settings.read_text())
    hooks = cfg.get("hooks", {})
    blob = json.dumps(hooks)
    need = {"turn": "/api/v1/coord/turn" in blob, "precheck": "/api/v1/coord/precheck" in blob,
            "footprints": "/api/v1/coord/footprints" in blob,
            "PreToolUse": "PreToolUse" in hooks, "PostToolUse": "PostToolUse" in hooks}
    record("S0 install-hooks writes Concord entries to the project only", all(need.values()),
           f"{need}; user settings went to a throwaway HOME", {"settings": str(settings)})


def s1_mailbox_via_loop(s: Smoke) -> None:
    out_file = s.work / "s1_claude.txt"
    child = (f"sleep 5; claude -p 'Is there a [concord] message in your context? If yes reply with ONLY its "
             f"message body text; if no reply NONE.' --model haiku --setting-sources local "
             f"> {out_file} 2>&1")
    seen_ps = {}

    def probe_and_send():
        time.sleep(2)
        ps = s.concord("ps", "--json")
        try:
            seen_ps.update(next((p for p in json.loads(ps.stdout)["principals"]
                                 if p["principal_id"] == "loop:smoke-loop"), {}))
        except Exception:
            pass
        s.concord("send", "loop:smoke-loop", "PINEAPPLE-42 stay off config")

    t = threading.Thread(target=probe_and_send)
    t.start()
    run = s.concord("run", "--name", "smoke-loop", "--heartbeat-secs", "2", "--", "bash", "-c", child,
                    timeout=400)
    t.join()
    text = out_file.read_text() if out_file.exists() else ""
    _, msgs = s.api("GET", "/api/v1/coord/principals/loop%3Asmoke-loop/messages?unacked=false")
    m = (msgs.get("messages") or [{}])[0]
    _, princ = s.api("GET", "/api/v1/coord/principals/loop%3Asmoke-loop")
    ok = (run.returncode == 0 and bool(m.get("delivered_at")) and "PINEAPPLE-42" in text
          and seen_ps.get("pgid") and princ.get("status") == "ended")
    record("S1 loop wrapper → message delivered into the next worker's context", bool(ok),
           f"rc={run.returncode} delivered_at={m.get('delivered_at')} delivered_to={m.get('delivered_to')} "
           f"model_echo={'PINEAPPLE-42' in text} ps_pgid={seen_ps.get('pgid')} final_status={princ.get('status')}",
           {"model_text": text[-300:]})


def transcript_events(cwd: Path, needle: str) -> list[tuple[str, str, str]]:
    """(ts, kind, text) for the newest Claude transcript of `cwd` containing `needle`.

    Claude Code keys the project dir on the canonical cwd (/tmp → /private/tmp)."""
    key = re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))
    files = sorted((Path.home() / ".claude" / "projects" / key).glob("*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        raw = f.read_text(errors="replace")
        if needle not in raw:
            continue
        out = []
        for line in raw.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            content = (d.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if b.get("type") == "tool_use":
                    out.append((d.get("timestamp", ""), "use", f"{b['name']} {json.dumps(b['input'])[:160]}"))
                elif b.get("type") == "tool_result":
                    r = b.get("content")
                    out.append((d.get("timestamp", ""), "result", (r if isinstance(r, str) else json.dumps(r))[:240]))
        return out
    return []


def barrier_session(s: Smoke, fname: str, tag: str, mid_action) -> dict:
    """A real Claude session reads `fname`, then blocks on a file barrier while `mid_action()` changes
    the file, then edits it. The interleaving is the harness's, never the model's: in run-1 haiku
    backgrounded a `sleep 45` and edited before the other writer ran."""
    f = s.project / fname
    f.write_text("line one\n")
    waiting, go = s.project / f".{tag}-waiting", s.project / f".{tag}-go"
    for x in (waiting, go):
        x.unlink(missing_ok=True)
    script = s.project / f"wait_{tag}.sh"
    script.write_text(f"touch {waiting}\nwhile [ ! -f {go} ]; do sleep 1; done\necho GO\n")
    needle = f"barrier-{tag}: never run it in the background"
    before = s.metrics()
    result = {}

    def alpha():
        result["alpha"] = s.claude(
            f"Do these steps strictly in order, one tool call at a time. Step 1: Read {fname}. "
            f"Step 2: run the bash command `bash {script.name}` in the foreground and wait until it prints GO "
            f"({needle}; it blocks until another process signals). Step 3: only after GO, use Edit to append "
            f"the line 'alpha was here' to {fname}. In your final reply, quote verbatim any text starting "
            "with '[concord]' that you saw; if none, reply DONE.", "loop:alpha",
            extra_tools=["Bash(bash:*)"], env={"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"})

    th = threading.Thread(target=alpha)
    th.start()
    deadline = time.time() + 180
    while not waiting.exists() and th.is_alive() and time.time() < deadline:
        time.sleep(0.5)
    barrier = waiting.exists()
    time.sleep(3)  # PostToolUse footprints are async: let alpha's read land first
    mid = mid_action(f)
    time.sleep(3)  # past the precheck's 2 s disk grace window
    go.touch()
    th.join()
    after = s.metrics()
    events = transcript_events(s.project, needle)
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in
             ("coord_precheck_warn", "coord_precheck_total", "coord_precheck_unrecorded_total")}
    # Claude validates an Edit against its own read state BEFORE PreToolUse hooks run: a stale Edit
    # whose old_string no longer matches, or "modified since read", is refused and never reaches Concord.
    # When old_string still matches, the stale Edit lands with only a post-hoc note.
    refused = any("modified since read" in t or "String to replace not found" in t
                  for (_, k, t) in events if k == "result")
    return {"barrier": barrier, "mid": mid, "rc": result["alpha"][0], "text": result["alpha"][1],
            "events": events, "delta": delta, "refused": refused, "content": f.read_text()}


def s2_stale_premise(s: Smoke) -> None:
    """A reads, B (another Claude agent) writes through a footprinted tool, A edits."""
    def beta_writes(f: Path) -> dict:
        # Pin beta to Write: in run-2 its Edit missed and it fell back to `echo >>` (that path is S2d).
        rc, _, _ = s.claude(f"Use the Write tool to overwrite {f.name} with exactly two lines: 'line one' "
                            "then 'beta was here'. Do not use Bash. Reply DONE.", "loop:beta",
                            tools=["Read", "Write"])
        ev = transcript_events(s.project, f"Use the Write tool to overwrite {f.name}")
        return {"rc": rc, "tools": [t.split(" ")[0] for (_, k, t) in ev if k == "use" and not t.startswith("Read")]}

    r = barrier_session(s, "notes.md", "s2a", beta_writes)
    warned = r["delta"]["coord_precheck_warn"] >= 1
    caught_by = "concord-precheck" if warned else ("claude-validation" if r["refused"] else "none")
    ok = (r["barrier"] and r["mid"]["tools"] and set(r["mid"]["tools"]) == {"Write"} and caught_by != "none"
          and "beta was here" in r["content"] and "alpha was here" in r["content"])
    record("S2a stale premise in Claude Code: A read, B wrote, A's stale edit is flagged (Concord) or "
           "refused (Claude)", bool(ok),
           f"barrier={r['barrier']} caught_by={caught_by} delta={r['delta']} beta_tools={r['mid']['tools']} "
           f"alpha_rc={r['rc']} model_quoted_concord={'[concord]' in r['text']}",
           {"alpha_text": r["text"][-400:], "alpha_events": r["events"][-12:]})


def s2d_unrecorded_writer_live(s: Smoke) -> None:
    """Run-2's exact failure, live: A reads, a writer Concord never sees (a shell append) changes the
    file, A edits. Needs P1b (disk-truth)."""
    def shell_appends(f: Path) -> dict:
        p = subprocess.run(["sh", "-c", f"echo 'shell was here' >> '{f}'"], capture_output=True)
        return {"rc": p.returncode}

    r = barrier_session(s, "s2d.md", "s2d", shell_appends)
    flagged = r["delta"]["coord_precheck_unrecorded_total"] >= 1
    caught_by = "concord-disk-truth" if flagged else ("claude-validation" if r["refused"] else "none")
    ok = r["barrier"] and caught_by != "none" and "alpha was here" in r["content"] and "shell was here" in r["content"]
    record("S2d unrecorded writer, live: A read, a shell append changed the file, A's stale edit is flagged",
           bool(ok), f"barrier={r['barrier']} caught_by={caught_by} delta={r['delta']} alpha_rc={r['rc']} "
           f"model_quoted_concord={'[concord]' in r['text']}",
           {"alpha_text": r["text"][-400:], "alpha_events": r["events"][-12:]})


def s2b_stale_premise_contract(s: Smoke) -> None:
    """The same race through the harness-agnostic HTTP contract, as a codex/opencode/mini-ork-lane
    adapter without a read-state guard would drive it, against the live substrate."""
    f = s.project / "s2b.md"
    f.write_text("shared\n")
    path, cwd = str(f), str(s.project)

    def hook(endpoint: str, principal: str, session: str, tool: str, event: str) -> dict:
        body = {"session_id": session, "cwd": cwd, "hook_event_name": event, "tool_name": tool,
                "tool_input": {"file_path": path}}
        return s.api("POST", f"/api/v1/coord/{endpoint}", body, {"X-Concord-Principal": principal})[1]

    before = s.metrics()
    hook("footprints", "agent:codex-alpha", "codex-a1", "Read", "PostToolUse")
    time.sleep(0.2)
    hook("footprints", "agent:opencode-beta", "oc-b1", "Write", "PostToolUse")
    stale = hook("precheck", "agent:codex-alpha", "codex-a1", "Edit", "PreToolUse")
    hook("footprints", "agent:codex-alpha", "codex-a1", "Read", "PostToolUse")
    fresh = hook("precheck", "agent:codex-alpha", "codex-a1", "Edit", "PreToolUse")
    own = hook("precheck", "agent:opencode-beta", "oc-b1", "Edit", "PreToolUse")
    warns = s.metrics().get("coord_precheck_warn", 0) - before.get("coord_precheck_warn", 0)
    stale_txt, fresh_txt, own_txt = json.dumps(stale), json.dumps(fresh), json.dumps(own)
    ok = ("agent:opencode-beta" in stale_txt and "permissionDecision" not in stale_txt
          and "agent:opencode-beta" not in fresh_txt and "concord" not in own_txt.lower() and warns == 1)
    record("S2b stale premise via the HTTP contract (guard-less harness): warns, names the writer, "
           "clears after re-read", ok,
           f"warn_names_writer={'agent:opencode-beta' in stale_txt} no_permissionDecision="
           f"{'permissionDecision' not in stale_txt} cleared_after_reread={'agent:opencode-beta' not in fresh_txt} "
           f"writer_not_warned={'concord' not in own_txt.lower()} precheck_warn+{warns}",
           {"stale": stale, "fresh": fresh})


def s2c_unrecorded_writer(s: Smoke) -> None:
    """A writer Concord never sees (a shell `echo >>`, an editor, a formatter) changes a file after an
    agent read it. Live run-2 hit this: beta's Edit missed and it appended with Bash. Footprints carry
    the file's mtime/size at read time, so the precheck can compare against the disk."""
    f = s.project / "s2c.md"
    f.write_text("shared\n")
    path, cwd = str(f), str(s.project)

    def hook(endpoint: str, tool: str, event: str) -> dict:
        body = {"session_id": "codex-c1", "cwd": cwd, "hook_event_name": event, "tool_name": tool,
                "tool_input": {"file_path": path}}
        return s.api("POST", f"/api/v1/coord/{endpoint}", body, {"X-Concord-Principal": "agent:codex-gamma"})[1]

    hook("footprints", "Read", "PostToolUse")
    time.sleep(1.1)  # make the mtime change visible even on coarse-mtime filesystems
    with f.open("a") as fh:
        fh.write("appended by a shell\n")
    time.sleep(2.5)  # past the 2 s grace window that absorbs the caller's own async footprint lag
    stale = hook("precheck", "Edit", "PreToolUse")
    hook("footprints", "Read", "PostToolUse")
    fresh = hook("precheck", "Edit", "PreToolUse")
    ctx = (stale.get("hookSpecificOutput") or {}).get("additionalContext", "")
    fresh_ctx = (fresh.get("hookSpecificOutput") or {}).get("additionalContext", "")
    ok = ("[concord]" in ctx and "permissionDecision" not in json.dumps(stale) and "[concord]" not in fresh_ctx)
    record("S2c unrecorded writer: file changed on disk after A's read by a non-agent write → precheck warns A", ok,
           f"warned={'[concord]' in ctx} cleared_after_reread={'[concord]' not in fresh_ctx}",
           {"stale": stale, "fresh": fresh})


def s3_digest(s: Smoke) -> None:
    before = s.metrics()
    s.claude("Append the line 'beta again' to notes.md. Reply DONE.", "loop:beta")
    rc, out, _ = s.claude("Without using tools, reply with any lines from your context that start with "
                          "'[concord] changed by other agents' or with '- ' right after it. If none, reply NONE.",
                          "loop:alpha")
    after = s.metrics()
    lines = after.get("coord_digest_lines_total", 0) - before.get("coord_digest_lines_total", 0)
    record("S3 per-turn digest lists the file another agent changed", lines >= 1,
           f"digest_lines+{lines} model_mentions_notes={'notes.md' in out}", {"text": out[-300:]})


def s4_hot_claim_warn(s: Smoke) -> None:
    cfg = s.project / ".mini-ork" / "config" / "agents.yaml"
    before = s.metrics()
    s.claude("Edit .mini-ork/config/agents.yaml: change the value of `reviewer:` to `glm`. Reply DONE.",
             "loop:alpha")
    _, claims = s.api("GET", "/api/v1/coord/hot-claims")
    held = {c["path"]: c["principal_id"] for c in claims.get("claims", [])}
    rc, out, _ = s.claude("First read .mini-ork/config/agents.yaml, then change `worker:` to `minimax`. "
                          "Quote any '[concord]' text you saw, else reply DONE.", "loop:beta")
    after = s.metrics()
    conflicts = after.get("coord_hot_conflicts_total", 0) - before.get("coord_hot_conflicts_total", 0)
    cli = s.concord("claims")
    holder = next((v for k, v in held.items() if k.endswith(".mini-ork/config/agents.yaml")), None)
    ok = holder == "loop:alpha" and conflicts >= 1 and "minimax" in cfg.read_text() and "loop:alpha" in cli.stdout
    record("S4 hot file claimed by writer; another agent warned (warn mode lets it proceed)", ok,
           f"holder={holder} hot_conflicts+{conflicts} beta_edit_applied={'minimax' in cfg.read_text()} "
           f"cli_claims_shows_holder={'loop:alpha' in cli.stdout}", {"text": out[-300:]})


def make_worktree(s: Smoke) -> Path:
    origin, clone, wts = s.work / "origin.git", s.work / "clone", s.work / "worktrees"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    (clone / "README.md").write_text("smoke\n")
    git(clone, "add", "README.md"); git(clone, "commit", "-qm", "seed"); git(clone, "push", "-q", "origin", "HEAD:main")
    env = {**os.environ, "MINI_ORK_ROOT": str(clone), "MINI_ORK_WORKTREES_DIR": str(wts),
           "MINI_ORK_OWNERSHIP_FILE": str(wts / ".ownership"), "MO_CONCORD": "1", "CN_BASE_URL": s.base}
    subprocess.run([sys.executable, str(MINI_ORK / "scripts" / "mini_ork_worktree.py"), "create", "wt1",
                    "--owns", "src"], cwd=clone, env=env, check=True, capture_output=True)
    wt = wts / "wt1"
    install_hooks(s, wt)
    return wt


def s5_owns_audit(s: Smoke, wt: Path) -> None:
    _, p = s.api("GET", "/api/v1/coord/principals/agent%3Awt-wt1")
    s.claude("Create the file src/inside.py containing `x = 1` and the file lib/outside.py containing "
             "`y = 2`. Reply DONE.", "session:smoke-wt", cwd=wt)
    _, v = s.api("GET", "/api/v1/coord/owns-violations?since=0")
    paths = [r["path"] for r in v.get("violations", [])]
    cli = s.concord("violations")
    ok = (p.get("labels", {}).get("owns") == ["src"] and paths == ["lib/outside.py"]
          and (wt / "lib/outside.py").exists() and "lib/outside.py" in cli.stdout)
    record("S5 worktree registered with --owns; out-of-scope edit recorded (audit lets it through)", ok,
           f"owns={p.get('labels', {}).get('owns')} violations={paths} "
           f"outside_file_created={(wt / 'lib/outside.py').exists()} cli_lists={'lib/outside.py' in cli.stdout}")


def s6_deny_after_restart(s: Smoke, wt: Path) -> None:
    s.stop_server()
    s.start_server({"CONTEXTNEST_CONCORD_HOT_MODE": "deny", "CONTEXTNEST_CONCORD_OWNS_MODE": "deny"})
    _, claims = s.api("GET", "/api/v1/coord/hot-claims")
    _, ps = s.api("GET", "/api/v1/coord/principals?status=all")
    survived = (any(c["principal_id"] in ("loop:alpha", "loop:beta") for c in claims.get("claims", []))
                and any(p["principal_id"] == "agent:wt-wt1" for p in ps.get("principals", [])))
    record("S6a restart: principals + hot claims persist in coord.db", survived,
           f"claims={[c['principal_id'] for c in claims.get('claims', [])]} principals={len(ps.get('principals', []))}")
    cfg = s.project / ".mini-ork" / "config" / "agents.yaml"
    _, claims = s.api("GET", "/api/v1/coord/hot-claims")
    holder = next((c["principal_id"] for c in claims.get("claims", [])
                   if c["path"].endswith("agents.yaml")), "loop:alpha")
    outsider = "loop:gamma" if holder != "loop:gamma" else "loop:delta"
    before = cfg.read_text()
    rc, out, _ = s.claude("First read .mini-ork/config/agents.yaml, then change `planner:` to `opus`. "
                          "Report exactly what happened, including any tool error text.", outsider)
    record("S6b deny mode: an outsider's edit of a claimed hot file is blocked", cfg.read_text() == before,
           f"holder={holder} outsider={outsider} file_unchanged={cfg.read_text() == before}",
           {"text": out[-400:]})
    rc, out, _ = s.claude("Create the file lib/denied.py containing `z = 3`. Report exactly what happened.",
                          "session:smoke-wt", cwd=wt)
    record("S6c deny mode: an out-of-scope write in a worktree is blocked",
           not (wt / "lib/denied.py").exists(),
           f"lib/denied.py exists={(wt / 'lib/denied.py').exists()}", {"text": out[-400:]})
    rc, out, _ = s.claude("Create the file src/allowed.py containing `ok = 1`. Reply DONE.",
                          "session:smoke-wt", cwd=wt)
    record("S6d deny mode: an in-scope NEW file is still allowed (canonical-path regression)",
           (wt / "src/allowed.py").exists(), f"src/allowed.py exists={(wt / 'src/allowed.py').exists()}")


def s7_run_principal(s: Smoke) -> None:
    code = ("import os,subprocess,sys; sys.path.insert(0, %r); "
            "from mini_ork.orchestration import concord_run as c; "
            "h=c.start('smoke-run-1','code-fix','k.md'); "
            "print(subprocess.run(['sh','-c','echo $CONCORD_PRINCIPAL'],capture_output=True,text=True).stdout.strip()); "
            "c.stop(h)") % str(MINI_ORK)
    p = subprocess.run([sys.executable, "-c", code], env={**os.environ, "CN_BASE_URL": s.base,
                       "MO_CONCORD_HEARTBEAT_S": "1"}, capture_output=True, text=True, timeout=60)
    _, pr = s.api("GET", "/api/v1/coord/principals/run%3Asmoke-run-1")
    ok = p.stdout.strip() == "run:smoke-run-1" and pr.get("harness") == "mini-ork" and pr.get("status") == "ended"
    record("S7 mini-ork run principal: child inherits run:<id>; registered then ended", ok,
           f"child_saw={p.stdout.strip()!r} harness={pr.get('harness')} status={pr.get('status')}")


def s8_admission(s: Smoke) -> None:
    import sqlite3
    d = s.work / "admit"
    d.mkdir(exist_ok=True)
    (d / "a.md").write_text("## Files in scope\n- `mini_ork/foo.py` — x\n")
    (d / "b.md").write_text("## Files in scope\n- `mini_ork/foo.py`\n- `docs/x.md`\n")
    (d / "c.md").write_text("## Files in scope\n- `tests/unit/test_other.py`\n")
    db = d / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE epics (id TEXT PRIMARY KEY, status TEXT, kickoff_path TEXT, archived_at TEXT)")
    con.execute("INSERT INTO epics VALUES ('A','in progress',?,NULL)", (str(d / "a.md"),))
    con.commit(); con.close()
    r_b = s.concord("admit", "--db", str(db), env={"MO_EPIC_ID": "B", "MO_EPIC_KICKOFF": str(d / "b.md")})
    r_c = s.concord("admit", "--db", str(db), env={"MO_EPIC_ID": "C", "MO_EPIC_KICKOFF": str(d / "c.md")})
    ok = r_b.returncode == 75 and "in-progress A" in r_b.stdout and r_c.returncode == 0
    record("S8 concord admit defers an overlapping epic, admits a disjoint one", ok,
           f"B rc={r_b.returncode} ({r_b.stdout.strip()}) C rc={r_c.returncode}")


def s9_latency(s: Smoke) -> None:
    def time_it(path: str, body: dict, n: int = 50) -> tuple[float, float]:
        xs = []
        for _ in range(n):
            t0 = time.perf_counter()
            s.api("POST", path, body, {"X-Concord-Principal": "loop:alpha"})
            xs.append((time.perf_counter() - t0) * 1000)
        xs.sort()
        return statistics.median(xs), xs[int(0.95 * len(xs)) - 1]
    turn = time_it("/api/v1/coord/turn", {"session_id": "lat", "cwd": str(s.project),
                                          "hook_event_name": "UserPromptSubmit"})
    pre = time_it("/api/v1/coord/precheck", {"session_id": "lat", "cwd": str(s.project),
                                             "tool_name": "Edit",
                                             "tool_input": {"file_path": str(s.project / "notes.md")}})
    ok = turn[1] < 50 and pre[1] < 50
    record("S9 hook latency within budget (round trip incl. HTTP)", ok,
           f"turn p50={turn[0]:.1f}ms p95={turn[1]:.1f}ms; precheck p50={pre[0]:.1f}ms p95={pre[1]:.1f}ms")


def s11_topic_overlap(s: Smoke) -> None:
    """The original incident: two loops independently improving the same surface. With topic notices on,
    the second turn of one loop is told the other is working on the same thing (P3)."""
    s.stop_server()
    # The default local embedder scores any two English prompts 0.73-0.97 (run-3), so topic
    # calibration needs the production embedder: --embed-config <[services.embedding] toml>.
    s.start_server({"CONTEXTNEST_CONCORD_TOPIC": "1",
                    **({"CONTEXTNEST_CONFIG": s.embed_config} if s.embed_config else {})})
    task = ("Improve the onboarding funnel landing page: rewrite the hero headline and the signup call to "
            "action in landing/hero.md to raise trial conversion, keeping the brand voice.")
    before = s.metrics()
    s.claude(f"Context for later turns: {task} For now reply with exactly NOTED and use no tools.",
             "loop:surface-craft")
    s.claude(f"Context for later turns: {task} Focus on the funnel. For now reply with exactly NOTED and use no tools.",
             "loop:coach-funnel")
    s.claude("Context for later turns: migrate the billing database schema to add an invoices table with "
             "foreign keys and backfill. For now reply with exactly NOTED and use no tools.", "loop:billing")
    time.sleep(3)  # intent embedding is async, off the hook's critical path
    # Snapshot pairs BEFORE the probe turn: an intent is the principal's latest prompt, so the probe
    # below replaces surface-craft's work intent (run-3b measured 0.62 against the probe text).
    _, pairs = s.api("GET", "/api/v1/coord/topic-pairs?min=0.3")
    rc, out, _ = s.claude("Without using tools, reply with any line from your context that starts with "
                          "'[concord] ↔'; if none, reply NONE.", "loop:surface-craft")
    after = s.metrics()
    notices = after.get("coord_topic_notices_total", 0) - before.get("coord_topic_notices_total", 0)
    pair_keys = {tuple(sorted((p["a"], p["b"]))): round(p["similarity"], 3) for p in pairs.get("pairs", [])}
    same = pair_keys.get(("loop:coach-funnel", "loop:surface-craft"))
    billing_hi = [k for k, v in pair_keys.items() if "loop:billing" in k and v >= 0.85]
    ok = notices >= 1 and same is not None and same >= 0.85 and not billing_hi
    record("S11 topic overlap: two loops on the same surface are told about each other (P3, opt-in)", ok,
           f"embedder={'config' if s.embed_config else 'default-local'} "
           f"topic_notices+{notices} pair_similarity={same} unrelated_billing_flagged={billing_hi} "
           f"model_saw_notice={'↔' in out}", {"pairs": pair_keys and {f'{a}|{b}': v for (a, b), v in pair_keys.items()},
                                              "text": out[-300:]})


def s10_fail_open(s: Smoke) -> None:
    s.stop_server()
    r = s.concord("run", "--name", "offline", "--", "echo", "still-ran")
    rc, out, secs = s.claude("Reply with exactly: OK", "loop:alpha")
    ok = r.returncode == 0 and "still-ran" in r.stdout and rc == 0 and "OK" in out
    record("S10 substrate down: wrapper and Claude sessions fail open", ok,
           f"wrapper rc={r.returncode} warned={'unavailable' in r.stderr} claude rc={rc} secs={secs:.1f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cn-bin", required=True)
    ap.add_argument("--port", type=int, default=28099)
    ap.add_argument("--work", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--embed-config", default="",
                    help="toml with only [services.embedding] (e.g. production's) for S11 topic calibration")
    ap.add_argument("--only", default="", help="comma-separated scenario numbers, e.g. 2,3 (S0 always runs)")
    a = ap.parse_args()
    only = {f"s{n.strip()}" for n in a.only.split(",") if n.strip()}

    def want(f) -> bool:
        return not only or f.__name__.split("_")[0] in only or f.__name__.startswith("s0_")
    work = Path(a.work or tempfile.mkdtemp(prefix="concord-smoke-"))
    s = Smoke(a.cn_bin, a.port, work)
    s.embed_config = a.embed_config
    s.project.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(s.project)], check=True)
    (s.project / ".mini-ork" / "config").mkdir(parents=True)
    (s.project / ".mini-ork" / "config" / "agents.yaml").write_text(
        "lanes:\n  planner: glm\n  reviewer: opus\n  worker: deepseek\n")
    print(f"work dir: {work}", flush=True)
    s.start_server()
    try:
        steps = [s0_install, s1_mailbox_via_loop, s2_stale_premise, s2b_stale_premise_contract,
                 s2c_unrecorded_writer, s2d_unrecorded_writer_live, s3_digest,
                 s4_hot_claim_warn]
        for f in filter(want, steps):
            try:
                f(s)
            except Exception as e:
                record(f.__name__, False, f"harness error: {e!r}")
        wt = None
        if want(s5_owns_audit) or want(s6_deny_after_restart):
            try:
                wt = make_worktree(s)
                s5_owns_audit(s, wt)
            except Exception as e:
                record("s5_owns_audit", False, f"harness error: {e!r}")
        if wt is not None and want(s6_deny_after_restart):
            try:
                s6_deny_after_restart(s, wt)
            except Exception as e:
                record("s6_deny_after_restart", False, f"harness error: {e!r}")
        for f in filter(want, (s7_run_principal, s8_admission, s9_latency, s11_topic_overlap, s10_fail_open)):
            try:
                f(s)
            except Exception as e:
                record(f.__name__, False, f"harness error: {e!r}")
    finally:
        s.stop_server()
    out = Path(a.out or work / "results.json")
    out.write_text(json.dumps({"work": str(work), "results": RESULTS}, indent=2))
    passed = sum(r["pass"] for r in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} scenarios passed — {out}", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
