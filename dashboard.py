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
import concurrent.futures as cf
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
# Strip ALL terminal escape sequences (color, cursor moves, clear-line, etc.),
# not just color — ffuf/others emit these and they render as '?' in the browser.
ANSI = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
# Remaining control chars (incl. carriage returns) that also render as junk.
CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
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
        self.batch = {}            # host -> status (batch runs only)

    def log(self, text, step=None):
        with self.lock:
            for ln in str(text).replace("\r", "\n").split("\n"):
                clean = CTRL.sub("", ANSI.sub("", ln))
                if clean.strip() == "" and ln != "":
                    continue  # drop lines that were pure control/escape noise
                self.lines.append({"i": len(self.lines), "step": step, "text": clean})

    def set(self, step_id, status):
        with self.lock:
            self.results[step_id] = status

    def set_batch(self, host, status):
        with self.lock:
            self.batch[host] = status

    def snapshot(self, since):
        with self.lock:
            return {"running": self.running, "current": self.current,
                    "results": dict(self.results), "batch": dict(self.batch),
                    "total": len(self.lines), "lines": self.lines[since:]}

    def reset(self):
        with self.lock:
            self.lines, self.results, self.batch, self.stop_flag = [], {}, {}, False


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
        host, target_url = bbpipe.parse_target(target)   # honor http/https + path
        if not scope.is_allowed(host):
            runner.log(f"REFUSING: {host} is not in scope ({cfg.args.scope}).")
            return
        out_dir = os.path.join(cfg.args.output, host)
        os.makedirs(out_dir, exist_ok=True)
        ctx = {"target": host, "target_url": target_url,
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
# Batch run — many targets, N at a time (used by /api/run-batch)
# ---------------------------------------------------------------------------
def _exec_shell_tagged(cmd, host, out_dir, sid):
    """Like _exec_shell but prefixes every output line with [host] for batch runs."""
    logdir = os.path.join(out_dir, "logs")
    os.makedirs(logdir, exist_ok=True)
    env = bbpipe.tool_env()
    pfx = f"[{host}] "
    runner.log(pfx + f"$ {cmd}")
    try:
        proc = subprocess.Popen(["bash", "-c", f"set -o pipefail; {cmd}"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace", bufsize=1, env=env)
    except Exception as e:
        runner.log(pfx + f"failed to start: {e}")
        return
    with open(os.path.join(logdir, f"{sid}.log"), "w") as lf:
        for line in proc.stdout:
            if runner.stop_flag:
                proc.terminate()
                break
            runner.log(pfx + line.rstrip("\n"))
            lf.write(line)
    proc.wait()


def _run_one_target(spec, dry_run, allow_dangerous):
    scope = bbpipe.Scope.load(str(cfg.scope_path))
    host, target_url = bbpipe.parse_target(spec)
    runner.set_batch(host, "running")
    runner.log(f"══════ START {host} ({target_url}) ══════")
    out_dir = os.path.join(cfg.args.output, host)
    os.makedirs(out_dir, exist_ok=True)
    ctx = {"target": host, "target_url": target_url, "out": out_dir,
           "wordlist": getattr(cfg, "wordlist", None) or cfg.args.wordlist}
    pfx = f"[{host}] "
    for st in cfg.all_steps():
        if runner.stop_flag:
            break
        if not st.get("enabled", True):
            continue
        if st.get("dangerous") and not allow_dangerous:
            continue
        runner.log(pfx + f"── {st.get('name')} ──")
        try:
            if st.get("builtin"):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    bbpipe.BUILTINS[st["builtin"]](ctx, scope, st.get("params", {}))
                for ln in buf.getvalue().split("\n"):
                    if ln.strip():
                        runner.log(pfx + ln)
            else:
                cmd = bbpipe.resolve(st.get("command", ""), ctx)
                if dry_run:
                    runner.log(pfx + f"[dry-run] {cmd}")
                else:
                    _exec_shell_tagged(cmd, host, out_dir, st.get("id"))
        except Exception as e:
            runner.log(pfx + f"error: {e}")
    runner.set_batch(host, "done")
    runner.log(f"══════ DONE {host} ══════")


def run_batch(targets, concurrency, dry_run, allow_dangerous):
    runner.reset()
    runner.running = True
    try:
        scope = bbpipe.Scope.load(str(cfg.scope_path))
        specs, seen = [], set()
        for t in targets:
            host, _ = bbpipe.parse_target(t)
            if not host or host in seen:
                continue
            seen.add(host)
            specs.append(t)
        allowed = [s for s in specs if scope.is_allowed(bbpipe.parse_target(s)[0])]
        for s in specs:
            h = bbpipe.parse_target(s)[0]
            runner.set_batch(h, "queued" if s in allowed else "out-of-scope")
        runner.log(f"batch: {len(allowed)} in-scope / {len(specs)} total, "
                   f"{concurrency} at a time"
                   + ("  [DRY RUN]" if dry_run else ""))
        if not allowed:
            runner.log("no in-scope targets — check scope.yaml.")
            return
        with cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
            futs = [ex.submit(_run_one_target, s, dry_run, allow_dangerous) for s in allowed]
            for _ in cf.as_completed(futs):
                pass
        runner.log("══════ BATCH FINISHED ══════")
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


def analyze_ffuf(path):
    """Group ffuf.json results by status and size to separate real hits from
    catch-all/false-positive noise (the majority status or a repeated size)."""
    from collections import Counter
    data = json.load(open(path))
    results = data.get("results", []) or []
    total = len(results)
    status_c = Counter(r.get("status") for r in results)
    size_c = Counter(r.get("length") for r in results)

    # A size that repeats a lot is almost certainly a catch-all/false positive.
    cutoff = max(3, int(total * 0.02))
    noise_sizes = {s for s, c in size_c.items() if c > cutoff}
    # The dominant status is suspect when it's a 2xx that covers most results.
    dominant = status_c.most_common(1)[0] if status_c else (None, 0)
    dominant_is_catchall = bool(total) and dominant[1] / total > 0.5 and 200 <= (dominant[0] or 0) < 400

    def fuzz(r):
        return (r.get("input") or {}).get("FUZZ") or r.get("url", "")

    likely = [r for r in results if r.get("length") not in noise_sizes]
    likely.sort(key=lambda r: (r.get("status") or 0, r.get("length") or 0))

    return {
        "total": total,
        "by_status": [{"code": k, "count": v} for k, v in status_c.most_common()],
        "by_size": [{"size": k, "count": v} for k, v in size_c.most_common(25)],
        "noise_sizes": sorted(x for x in noise_sizes if x is not None),
        "dominant_status": {"code": dominant[0], "count": dominant[1],
                            "is_catchall": dominant_is_catchall},
        "likely_count": len(likely),
        "likely_real": [{"word": fuzz(r), "status": r.get("status"),
                         "size": r.get("length"), "url": r.get("url")} for r in likely[:300]],
    }


@app.route("/api/analyze")
def api_analyze():
    host, _ = bbpipe.parse_target(request.args.get("target", ""))
    if not host:
        return jsonify({"error": "no target"}), 400
    path = os.path.join(cfg.args.output, host, "ffuf.json")
    if not os.path.exists(path):
        return jsonify({"error": f"no ffuf.json for {host} yet — run the ffuf step first"}), 404
    try:
        return jsonify(analyze_ffuf(path))
    except Exception as e:
        return jsonify({"error": f"could not parse ffuf.json: {e}"}), 500


SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unknown": 5}


def analyze_nuclei(path):
    """Group nuclei JSONL findings by severity."""
    from collections import Counter
    sev = Counter()
    findings = []
    for line in open(path, encoding="utf-8", errors="ignore"):
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        info = j.get("info", {}) or {}
        s = (info.get("severity") or "unknown").lower()
        sev[s] += 1
        findings.append({"severity": s,
                         "template": j.get("template-id") or j.get("templateID") or "",
                         "name": info.get("name") or "",
                         "matched_at": j.get("matched-at") or j.get("matched_at") or j.get("host") or ""})
    findings.sort(key=lambda f: SEV_ORDER.get(f["severity"], 9))
    return {"by_severity": {k: sev.get(k, 0) for k in ["critical", "high", "medium", "low", "info"]},
            "total": int(sum(sev.values())), "findings": findings[:300]}


def _count_lines(path):
    try:
        return sum(1 for l in open(path, encoding="utf-8", errors="ignore") if l.strip())
    except Exception:
        return 0


def _host_metrics(host):
    d = os.path.join(cfg.args.output, host)
    m = {"host": host, "live": False, "ffuf_real": 0, "idor": 0,
         "nuclei": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0, "total": 0}}
    if os.path.exists(os.path.join(d, "live_hosts.txt")):
        m["live"] = _count_lines(os.path.join(d, "live_hosts.txt")) > 0
    ff = os.path.join(d, "ffuf.json")
    if os.path.exists(ff):
        try:
            m["ffuf_real"] = analyze_ffuf(ff)["likely_count"]
        except Exception:
            pass
    nu = os.path.join(d, "nuclei.jsonl")
    if os.path.exists(nu):
        try:
            a = analyze_nuclei(nu)
            m["nuclei"] = {**a["by_severity"], "total": a["total"]}
        except Exception:
            pass
    m["idor"] = _count_lines(os.path.join(d, "idor_candidates.txt"))
    n = m["nuclei"]
    if n.get("critical") or n.get("high"):
        m["risk"] = "high"
    elif n.get("medium") or m["ffuf_real"] >= 5:
        m["risk"] = "med"
    else:
        m["risk"] = "low"
    return m


@app.route("/api/results")
def api_results():
    base = cfg.args.output
    hosts = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if os.path.isdir(os.path.join(base, name)):
                hosts.append(_host_metrics(name))
    rank = {"high": 0, "med": 1, "low": 2}
    hosts.sort(key=lambda h: (rank.get(h.get("risk"), 3), -h["nuclei"]["total"], -h["ffuf_real"]))
    return jsonify({"hosts": hosts})


@app.route("/api/results-detail")
def api_results_detail():
    host = request.args.get("host", "")
    d = os.path.join(cfg.args.output, host)
    if not host or not os.path.isdir(d):
        return jsonify({"error": "no results for that host"}), 404
    out = {"host": host}
    ff = os.path.join(d, "ffuf.json")
    out["ffuf"] = analyze_ffuf(ff) if os.path.exists(ff) else None
    nu = os.path.join(d, "nuclei.jsonl")
    out["nuclei"] = analyze_nuclei(nu) if os.path.exists(nu) else None
    idr = os.path.join(d, "idor_candidates.txt")
    out["idor"] = ([l.strip() for l in open(idr, encoding="utf-8", errors="ignore") if l.strip()][:200]
                   if os.path.exists(idr) else [])
    lh = os.path.join(d, "live_hosts.txt")
    out["live_hosts"] = ([l.strip() for l in open(lh, encoding="utf-8", errors="ignore") if l.strip()][:200]
                         if os.path.exists(lh) else [])
    return jsonify(out)


@app.route("/api/scope-check")
def api_scope_check():
    scope = bbpipe.Scope.load(str(cfg.scope_path))
    t = request.args.get("target", "")
    host, url = bbpipe.parse_target(t)
    return jsonify({"target": t, "host": host, "url": url,
                    "allowed": bool(host) and scope.is_allowed(host)})


@app.route("/api/run", methods=["POST"])
def api_run():
    if runner.running:
        return jsonify({"error": "a run is already in progress"}), 409
    d = request.get_json(force=True)
    target = (d.get("target") or "").strip()
    if not target:
        return jsonify({"error": "target required"}), 400
    scope = bbpipe.Scope.load(str(cfg.scope_path))
    host, _ = bbpipe.parse_target(target)
    if not scope.is_allowed(host):
        return jsonify({"error": f"{host} is not in scope"}), 403
    t = threading.Thread(target=run_pipeline, args=(
        cfg, target, d.get("step_ids"), bool(d.get("dry_run")),
        bool(d.get("allow_dangerous"))), daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/run-batch", methods=["POST"])
def api_run_batch():
    if runner.running:
        return jsonify({"error": "a run is already in progress"}), 409
    d = request.get_json(force=True)
    raw = d.get("targets") or []
    if isinstance(raw, str):
        raw = raw.splitlines()
    targets = [x.split(",")[0].strip() for x in raw
               if x and x.strip() and not x.strip().startswith("#")]
    if not targets:
        return jsonify({"error": "no targets provided"}), 400
    try:
        conc = max(1, int(d.get("concurrency") or 3))
    except (TypeError, ValueError):
        conc = 3
    th = threading.Thread(target=run_batch, args=(
        targets, conc, bool(d.get("dry_run")), bool(d.get("allow_dangerous"))),
        daemon=True)
    th.start()
    return jsonify({"ok": True, "count": len(targets)})


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
