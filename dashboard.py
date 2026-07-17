#!/usr/bin/env python3
"""
bbpipe dashboard — a LOCAL web UI to control the pipeline from your browser.

It reuses the exact same pipeline.yaml, scope.yaml and bbpipe.py logic as the CLI,
so both stay in sync. From the dashboard you can:
  - pick a target (scope-checked)
  - toggle any step on/off, edit its command, or switch tools
  - run one step, run all enabled steps, or dry-run
  - watch live output stream in

UI edits are saved to .bbpipe_state.json (git-ignored) so your nicely-commented
pipeline.yaml is never overwritten.

Run inside your Kali WSL shell:
    pip install flask pyyaml
    python3 dashboard.py               # then open http://127.0.0.1:8777

SECURITY: this server EXECUTES shell commands. It binds to 127.0.0.1 (localhost)
only — never expose it to a network. Authorized targets only.
"""

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

try:
    from flask import Flask, request, jsonify, send_file
except ImportError:
    raise SystemExit("Missing dependency. Run:  pip install flask pyyaml")

import yaml
import bbpipe  # reuse Scope, resolve, BUILTINS from the CLI

HERE = Path(__file__).resolve().parent
ANSI = re.compile(r"\033\[[0-9;]*m")
app = Flask(__name__)


# ---------------------------------------------------------------------------
# Config + UI-override state (never clobbers the commented pipeline.yaml)
# ---------------------------------------------------------------------------
class Config:
    def __init__(self, args):
        self.args = args
        self.config_path = HERE / args.config
        self.scope_path = HERE / args.scope
        self.state_path = HERE / ".bbpipe_state.json"
        self.reload()

    def reload(self):
        self.raw = yaml.safe_load(open(self.config_path)) or {}
        self.overrides = json.load(open(self.state_path)) if self.state_path.exists() else {}

    def _save(self):
        json.dump(self.overrides, open(self.state_path, "w"), indent=2)

    def _custom(self):
        return self.overrides.setdefault("_custom", [])

    def save_override(self, step_id, fields):
        # A custom step edits its own record; a pipeline.yaml step gets an override entry.
        for c in self._custom():
            if c.get("id") == step_id:
                c.update(fields)
                self._save()
                return
        self.overrides[step_id] = {**self.overrides.get(step_id, {}), **fields}
        self._save()

    def add_custom(self, name, command, tool="custom"):
        ids = [c["id"] for c in self._custom()]
        n = 1
        while f"custom_{n}" in ids:
            n += 1
        step = {"id": f"custom_{n}", "name": name or f"Custom step {n}",
                "tool": tool or "custom", "command": command,
                "enabled": True, "custom": True,
                "info": "Custom step you added from the dashboard."}
        self._custom().append(step)
        self._save()
        return step

    def delete_custom(self, step_id):
        before = len(self._custom())
        self.overrides["_custom"] = [c for c in self._custom() if c.get("id") != step_id]
        self.overrides.pop(step_id, None)
        self._save()
        return len(self.overrides["_custom"]) < before

    def phases(self):
        """Phases with per-step overrides applied, plus a phase of user-added steps."""
        out = []
        for ph in self.raw.get("phases", []):
            steps = []
            for st in ph.get("steps", []):
                merged = dict(st)
                ov = self.overrides.get(st.get("id"))
                if isinstance(ov, dict):
                    merged.update(ov)
                steps.append(merged)
            out.append({"name": ph.get("name"), "steps": steps})
        custom = self._custom()
        if custom:
            out.append({"name": "★ Custom steps (yours)", "steps": [dict(c) for c in custom]})
        return out

    def all_steps(self):
        for ph in self.phases():
            for st in ph["steps"]:
                yield st

    def find(self, step_id):
        return next((s for s in self.all_steps() if s.get("id") == step_id), None)


# ---------------------------------------------------------------------------
# Run engine — background thread, streams output the UI polls for
# ---------------------------------------------------------------------------
class Runner:
    def __init__(self):
        self.lock = threading.Lock()
        self.lines = []            # [{i, step, text}]
        self.results = {}          # step_id -> status
        self.running = False
        self.current = None
        self.proc = None
        self.stop_flag = False

    def log(self, text, step=None):
        with self.lock:
            for ln in str(text).split("\n"):
                self.lines.append({"i": len(self.lines), "step": step, "text": ANSI.sub("", ln)})

    def set(self, step_id, status):
        with self.lock:
            self.results[step_id] = status

    def snapshot(self, since):
        with self.lock:
            return {"running": self.running, "current": self.current,
                    "results": dict(self.results), "total": len(self.lines),
                    "lines": self.lines[since:]}

    def reset(self):
        with self.lock:
            self.lines, self.results, self.stop_flag = [], {}, False


runner = Runner()


def _exec_shell(cmd, step_id, out_dir):
    """Run a shell command, streaming each line into the runner + a log file."""
    logdir = os.path.join(out_dir, "logs")
    os.makedirs(logdir, exist_ok=True)
    logfile = os.path.join(logdir, f"{step_id}.log")
    env = bbpipe.tool_env()   # UTF-8 locale + Go bin on PATH
    runner.log(f"$ {cmd}", step_id)
    try:
        proc = subprocess.Popen(["bash", "-c", f"set -o pipefail; {cmd}"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace", bufsize=1, env=env)
    except Exception as e:
        runner.log(f"failed to start: {e}", step_id)
        return 1
    runner.proc = proc
    with open(logfile, "w") as lf:
        for line in proc.stdout:
            if runner.stop_flag:
                proc.terminate()
                runner.log("[stopped by user]", step_id)
                break
            runner.log(line.rstrip("\n"), step_id)
            lf.write(line)
    proc.wait()
    runner.proc = None
    return proc.returncode


def _exec_builtin(step, ctx, scope):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bbpipe.BUILTINS[step["builtin"]](ctx, scope, step.get("params", {}))
    runner.log(buf.getvalue().rstrip("\n"), step.get("id"))


def run_pipeline(cfg, target, step_ids, dry_run, allow_dangerous):
    runner.reset()
    runner.running = True
    try:
        scope = bbpipe.Scope.load(str(cfg.scope_path))
        if not scope.is_allowed(target):
            runner.log(f"REFUSING: {target} is not in scope ({cfg.args.scope}).")
            return
        out_dir = os.path.join(cfg.args.output, target)
        os.makedirs(out_dir, exist_ok=True)
        ctx = {"target": target, "target_url": f"https://{target}",
               "out": out_dir, "wordlist": getattr(cfg, "wordlist", None) or cfg.args.wordlist}

        steps = list(cfg.all_steps())
        if step_ids:
            steps = [s for s in steps if s.get("id") in step_ids]

        for st in steps:
            sid = st.get("id")
            if runner.stop_flag:
                break
            if not st.get("enabled", True) and (not step_ids or sid not in step_ids):
                runner.set(sid, "disabled")
                continue
            if st.get("dangerous") and not allow_dangerous:
                runner.set(sid, "blocked")
                runner.log(f"[blocked] '{st.get('name')}' is dangerous — tick "
                           f"'Allow dangerous steps' to run it.", sid)
                continue

            runner.current = sid
            runner.set(sid, "running")
            runner.log(f"── {st.get('name')} ──", sid)
            try:
                if st.get("builtin"):
                    _exec_builtin(st, ctx, scope)
                    runner.set(sid, "done")
                else:
                    cmd = bbpipe.resolve(st.get("command", ""), ctx)
                    if dry_run:
                        runner.log(f"[dry-run] would run: {cmd}", sid)
                        runner.set(sid, "dry-run")
                    else:
                        rc = _exec_shell(cmd, sid, out_dir)
                        runner.set(sid, "done" if rc == 0 else "failed")
            except Exception as e:
                runner.log(f"error: {e}", sid)
                runner.set(sid, "failed")
        runner.log("── pipeline finished ──")
    finally:
        runner.running = False
        runner.current = None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_file(HERE / "dashboard.html")


@app.route("/api/config")
def api_config():
    scope = bbpipe.Scope.load(str(cfg.scope_path))
    return jsonify({
        "target": cfg.args.target or "",
        "scope": {"in_scope": scope.in_scope, "out_of_scope": scope.out_of_scope,
                  "path": cfg.args.scope, "exists": cfg.scope_path.exists()},
        "phases": cfg.phases(),
    })


@app.route("/api/tools")
def api_tools():
    bins = set()
    for st in cfg.all_steps():
        if st.get("builtin"):
            continue
        for t in [st.get("tool", "")] + [a["tool"] for a in st.get("alternatives", [])]:
            b = t.split()[0] if t else ""
            if b:
                bins.add(b)
    return jsonify({b: bool(shutil.which(b)) for b in sorted(bins)})


@app.route("/api/step", methods=["POST"])
def api_step():
    d = request.get_json(force=True)
    sid = d.get("id")
    if not cfg.find(sid):
        return jsonify({"error": "unknown step"}), 404
    fields = {k: d[k] for k in ("tool", "command", "enabled") if k in d}
    cfg.save_override(sid, fields)
    return jsonify({"ok": True})


@app.route("/api/add-step", methods=["POST"])
def api_add_step():
    d = request.get_json(force=True)
    command = (d.get("command") or "").strip()
    if not command:
        return jsonify({"error": "command required"}), 400
    step = cfg.add_custom(d.get("name", ""), command, d.get("tool", "custom"))
    return jsonify({"ok": True, "step": step})


@app.route("/api/delete-step", methods=["POST"])
def api_delete_step():
    d = request.get_json(force=True)
    if cfg.delete_custom(d.get("id")):
        return jsonify({"ok": True})
    return jsonify({"error": "only custom steps can be deleted"}), 400


def list_wordlists():
    """All wordlists we can offer: the auto-detect candidates + everything in wordlists/."""
    import glob
    paths = list(bbpipe.WORDLIST_CANDIDATES)
    paths += sorted(glob.glob(str(HERE / "wordlists" / "*.txt")))
    cur = getattr(cfg, "wordlist", None)
    if cur:
        paths.insert(0, cur)
    seen, out = set(), []
    for p in paths:
        ap = os.path.abspath(p)
        if ap in seen or not os.path.exists(ap):
            continue
        seen.add(ap)
        try:
            n = sum(1 for _ in open(ap, encoding="utf-8", errors="ignore"))
        except Exception:
            n = 0
        out.append({"path": p, "label": f"{os.path.basename(p)} ({n:,} words)", "count": n})
    return out


@app.route("/api/wordlists")
def api_wordlists():
    return jsonify({"current": getattr(cfg, "wordlist", None), "options": list_wordlists()})


@app.route("/api/wordlist", methods=["POST"])
def api_set_wordlist():
    d = request.get_json(force=True)
    p = d.get("path", "")
    if not p or not os.path.exists(p):
        return jsonify({"error": "wordlist not found"}), 400
    cfg.wordlist = p
    return jsonify({"ok": True, "current": p})


@app.route("/api/scope-check")
def api_scope_check():
    scope = bbpipe.Scope.load(str(cfg.scope_path))
    t = request.args.get("target", "")
    return jsonify({"target": t, "allowed": bool(t) and scope.is_allowed(t)})


@app.route("/api/run", methods=["POST"])
def api_run():
    if runner.running:
        return jsonify({"error": "a run is already in progress"}), 409
    d = request.get_json(force=True)
    target = (d.get("target") or "").strip()
    if not target:
        return jsonify({"error": "target required"}), 400
    scope = bbpipe.Scope.load(str(cfg.scope_path))
    if not scope.is_allowed(target):
        return jsonify({"error": f"{target} is not in scope"}), 403
    t = threading.Thread(target=run_pipeline, args=(
        cfg, target, d.get("step_ids"), bool(d.get("dry_run")),
        bool(d.get("allow_dangerous"))), daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    runner.stop_flag = True
    if runner.proc:
        try:
            runner.proc.terminate()
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    since = int(request.args.get("since", 0))
    return jsonify(runner.snapshot(since))


@app.route("/api/reload", methods=["POST"])
def api_reload():
    cfg.reload()
    return jsonify({"ok": True})


def main():
    ap = argparse.ArgumentParser(description="bbpipe local dashboard")
    ap.add_argument("--target", default="", help="pre-fill a target")
    ap.add_argument("--scope", default="scope.yaml")
    ap.add_argument("--config", default="pipeline.yaml")
    ap.add_argument("--output", default="output")
    ap.add_argument("--wordlist", default=None,
                    help="wordlist for content discovery (default: auto-detect an installed one)")
    ap.add_argument("--port", type=int, default=8777)
    args = ap.parse_args()
    if not args.wordlist:
        args.wordlist = bbpipe.default_wordlist()

    global cfg
    cfg = Config(args)
    cfg.wordlist = args.wordlist   # currently-selected wordlist (changeable from the UI)
    print(f"\n  bbpipe dashboard →  http://127.0.0.1:{args.port}\n")
    print("  Bound to localhost only. Ctrl+C to stop.\n")
    # 127.0.0.1 ONLY — this executes shell commands; never expose it.
    app.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
