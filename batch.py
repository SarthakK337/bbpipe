#!/usr/bin/env python3
"""
bbpipe batch — run the pipeline against MANY targets, in parallel batches.

Feed it a CSV / txt of links (one per line, or the first column of a CSV). It
normalizes each to a bare domain, checks scope, and runs up to --concurrency full
pipelines at the same time. Unattended by design (no per-step prompts).

Each target gets its own output/<target>/ folder and logs. Dangerous steps run
only with --allow-dangerous.

Examples:
  python3 batch.py --targets targets.csv                       # 3 at a time (default)
  python3 batch.py --targets targets.csv --concurrency 5       # 5 at a time
  python3 batch.py --targets targets.csv --concurrency 999     # effectively "all at once"
  python3 batch.py --targets targets.csv --dry-run             # show commands, run nothing
  python3 batch.py --targets targets.csv --allow-dangerous --yes-authorized

Only ever run this against targets you are explicitly authorized to test.
"""

import argparse
import concurrent.futures as cf
import contextlib
import io
import os
import re
import subprocess
import sys
import threading

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency. Run: pip install pyyaml  (or: sudo apt install python3-yaml)")

import bbpipe  # reuse Scope, resolve, BUILTINS

ANSI = re.compile(r"\033\[[0-9;]*m")
PRINT_LOCK = threading.Lock()


def out(msg):
    with PRINT_LOCK:
        print(msg, flush=True)


def load_targets(path):
    """Read a CSV/txt of targets. Keeps each entry's scheme/path (parse_target
    honors http/https), dedups by bare host."""
    if not os.path.exists(path):
        sys.exit(f"targets file not found: {path}")
    lines = open(path).read().splitlines()
    specs, seen = [], set()
    for i, line in enumerate(lines):
        entry = line.split(",")[0].strip().strip('"').strip("'")
        if not entry or entry.startswith("#"):
            continue
        host, url = bbpipe.parse_target(entry)
        if not host:
            continue
        if i == 0 and any(k in host for k in ("target", "domain", "url", "host")):
            continue                        # skip a header row
        if host not in seen:
            seen.add(host)
            specs.append(url)               # full url, scheme preserved
    return specs


def run_cmd(cmd, out_dir, sid):
    logdir = os.path.join(out_dir, "logs")
    os.makedirs(logdir, exist_ok=True)
    env = bbpipe.tool_env()   # UTF-8 locale + Go bin on PATH
    with open(os.path.join(logdir, f"{sid}.log"), "w") as lf:
        return subprocess.call(["bash", "-c", f"set -o pipefail; {cmd}"],
                               stdout=lf, stderr=subprocess.STDOUT, env=env)


def run_target(cfg, spec, scope, args):
    host, url = bbpipe.parse_target(spec)   # honor http/https + path
    out(f"  ▶ start  {host}")
    out_dir = os.path.join(args.output, host)
    os.makedirs(out_dir, exist_ok=True)
    ctx = {"target": host, "target_url": url,
           "out": out_dir, "wordlist": args.wordlist}
    s = {"target": host, "done": 0, "failed": 0, "skipped": 0, "blocked": 0}

    with open(os.path.join(out_dir, "batch_run.log"), "w") as master:
        def w(t):
            master.write(t + "\n")
            master.flush()

        for phase in cfg.get("phases", []):
            for st in phase.get("steps", []):
                sid = st.get("id")
                if not st.get("enabled", True):
                    s["skipped"] += 1
                    continue
                if st.get("dangerous") and not args.allow_dangerous:
                    s["blocked"] += 1
                    w(f"[blocked] {sid} (dangerous; use --allow-dangerous)")
                    continue
                w(f"── {st.get('name')} ──")
                try:
                    if st.get("builtin"):
                        buf = io.StringIO()
                        with contextlib.redirect_stdout(buf):
                            bbpipe.BUILTINS[st["builtin"]](ctx, scope, st.get("params", {}))
                        w(ANSI.sub("", buf.getvalue()))
                        s["done"] += 1
                    else:
                        cmd = bbpipe.resolve(st.get("command", ""), ctx)
                        if args.dry_run:
                            w(f"[dry-run] {cmd}")
                            s["done"] += 1
                        else:
                            w(f"$ {cmd}")
                            rc = run_cmd(cmd, out_dir, sid)
                            s["done" if rc == 0 else "failed"] += 1
                except Exception as e:
                    w(f"error in {sid}: {e}")
                    s["failed"] += 1
    return s


def main():
    ap = argparse.ArgumentParser(description="Run bbpipe against many targets in parallel.")
    ap.add_argument("--targets", required=True, help="CSV/txt file of targets (one per line / first column)")
    ap.add_argument("--concurrency", type=int, default=3, help="how many pipelines run at once (default 3)")
    ap.add_argument("--scope", default="scope.yaml")
    ap.add_argument("--config", default="pipeline.yaml")
    ap.add_argument("--output", default="output")
    ap.add_argument("--wordlist", default=None,
                    help="wordlist for content discovery (default: auto-detect an installed one)")
    ap.add_argument("--dry-run", action="store_true", help="print commands, run nothing")
    ap.add_argument("--allow-dangerous", action="store_true", help="also run steps marked dangerous")
    ap.add_argument("--yes-authorized", action="store_true", help="skip the authorization prompt")
    args = ap.parse_args()
    if not args.wordlist:
        args.wordlist = bbpipe.default_wordlist()

    cfg = yaml.safe_load(open(args.config)) or {}
    scope = bbpipe.Scope.load(args.scope)

    targets = load_targets(args.targets)
    allowed = [t for t in targets if scope.is_allowed(bbpipe.parse_target(t)[0])]
    refused = [t for t in targets if not scope.is_allowed(bbpipe.parse_target(t)[0])]

    out(f"\n  targets in file : {len(targets)}")
    out(f"  in scope        : {len(allowed)}")
    if refused:
        out(f"  SKIPPED (out of scope): {len(refused)} -> {', '.join(refused[:8])}"
            f"{'…' if len(refused) > 8 else ''}")
    if not allowed:
        sys.exit("  no in-scope targets — nothing to do.")

    if args.concurrency < 1:
        args.concurrency = 1
    out(f"  concurrency     : {args.concurrency} at a time")
    if args.concurrency > 5:
        out("  ⚠ high concurrency can saturate bandwidth and trip target rate-limits/WAFs.")

    if not args.yes_authorized and not args.dry_run:
        print("\n  Only test assets you are explicitly authorized to test.")
        if input("  Type 'I am authorized' to continue > ").strip().lower() != "i am authorized":
            sys.exit("  aborted.")

    out(f"\n  ── running {len(allowed)} pipelines ──")
    results, done = [], 0
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(run_target, cfg, t, scope, args): t for t in allowed}
        for fut in cf.as_completed(futs):
            t = futs[fut]
            done += 1
            try:
                r = fut.result()
                results.append(r)
                out(f"  [{done}/{len(allowed)}] ✓ {t}  "
                    f"(done={r['done']} failed={r['failed']} blocked={r['blocked']})")
            except Exception as e:
                out(f"  [{done}/{len(allowed)}] ✗ {t}  error: {e}")

    # summary
    lines = ["target,done,failed,blocked,skipped"]
    for r in sorted(results, key=lambda x: x["target"]):
        lines.append(f"{r['target']},{r['done']},{r['failed']},{r['blocked']},{r['skipped']}")
    os.makedirs(args.output, exist_ok=True)
    summ = os.path.join(args.output, "batch_summary.csv")
    open(summ, "w").write("\n".join(lines) + "\n")
    out(f"\n  ── batch complete ──")
    out(f"  per-target results in: {args.output}/<target>/")
    out(f"  summary: {summ}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        out("\n  interrupted")
        sys.exit(130)
