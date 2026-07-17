#!/usr/bin/env python3
"""
bbpipe — a deterministic, step-by-step bug-bounty recon/scan pipeline.

This is NOT an AI tool. It simply runs the Kali tools you would run by hand,
one step at a time, and asks you before EVERY step so you stay in full control.

At each step you can:  [R]un  [E]dit the command  [T] switch tool  [S]kip  [I]nfo  [Q]uit

Every step, tool and exact command lives in pipeline.yaml — edit it freely.
Scope (which targets are allowed) lives in scope.yaml — the pipeline refuses
to touch anything not explicitly in scope.

USAGE (run inside your Kali WSL shell):
    python3 bbpipe.py --target example.com --scope scope.yaml
    python3 bbpipe.py --target example.com --dry-run      # show commands, run nothing
    python3 bbpipe.py --check                             # check which tools are installed
    python3 bbpipe.py --target example.com --auto         # run enabled steps without prompting
                                                          # (dangerous steps still confirm)

Only ever point this at assets you are explicitly authorized to test.
"""

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency. Run:  pip install pyyaml")

# ----------------------------------------------------------------------------
# Terminal colors (no external deps)
# ----------------------------------------------------------------------------
class C:
    R = "\033[0m"; B = "\033[1m"; DIM = "\033[2m"
    RED = "\033[91m"; GRN = "\033[92m"; YEL = "\033[93m"
    BLU = "\033[94m"; MAG = "\033[95m"; CYN = "\033[96m"

def hr(char="─", n=70):
    print(C.DIM + char * n + C.R)

def banner(text, color=C.CYN):
    print(f"\n{color}{C.B}{text}{C.R}")

# ----------------------------------------------------------------------------
# Scope enforcement
# ----------------------------------------------------------------------------
class Scope:
    def __init__(self, in_scope=None, out_of_scope=None):
        self.in_scope = in_scope or []
        self.out_of_scope = out_of_scope or []

    @classmethod
    def load(cls, path):
        if not path or not os.path.exists(path):
            return cls()
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls(data.get("in_scope"), data.get("out_of_scope"))

    def _match(self, host, patterns):
        host = host.lower().strip()
        for p in patterns:
            if fnmatch.fnmatch(host, p.lower().strip()):
                return True
        return False

    def is_allowed(self, host):
        if self._match(host, self.out_of_scope):
            return False
        if not self.in_scope:          # no scope file -> allow (CLI --target only)
            return True
        return self._match(host, self.in_scope)

    def describe(self):
        return (f"in-scope:     {', '.join(self.in_scope) or '(none set)'}\n"
                f"out-of-scope: {', '.join(self.out_of_scope) or '(none)'}")

# ----------------------------------------------------------------------------
# Wordlist auto-detection — pick the first list that actually exists on this box
# ----------------------------------------------------------------------------
WORDLIST_CANDIDATES = [
    "/usr/share/seclists/Discovery/Web-Content/common.txt",
    "/usr/share/wordlists/seclists/Discovery/Web-Content/common.txt",
    "/usr/share/wordlists/dirb/common.txt",
    "/usr/share/wordlists/dirbuster/directory-list-2.3-medium.txt",
]

def default_wordlist():
    for c in WORDLIST_CANDIDATES:
        if os.path.exists(c):
            return c
    return WORDLIST_CANDIDATES[0]  # nothing found; report path so the error is clear


# ----------------------------------------------------------------------------
# Command helpers
# ----------------------------------------------------------------------------
def resolve(template, ctx):
    """Fill {placeholders} in a command template."""
    out = template
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", str(v))
    return out

def input_with_prefill(prompt, text):
    """input() but with an editable pre-filled default (uses readline on POSIX)."""
    try:
        import readline
        def hook():
            readline.insert_text(text)
            readline.redisplay()
        readline.set_pre_input_hook(hook)
        try:
            return input(prompt)
        finally:
            readline.set_pre_input_hook()
    except Exception:
        # Fallback: show current, let them type a full replacement (empty = keep)
        print(f"{C.DIM}current: {text}{C.R}")
        new = input(prompt)
        return new or text

def run_shell(cmd, logfile):
    """Run cmd in bash, stream to terminal AND save to logfile via tee."""
    os.makedirs(os.path.dirname(logfile), exist_ok=True)
    # pipefail so a failing tool (not tee) is reported
    wrapped = f"set -o pipefail; {cmd} 2>&1 | tee {shlex_quote(logfile)}"
    print(f"{C.DIM}$ {cmd}{C.R}\n")
    rc = subprocess.call(["bash", "-c", wrapped])
    if rc == 0:
        print(f"\n{C.GRN}✓ done (log: {logfile}){C.R}")
    else:
        print(f"\n{C.YEL}⚠ exited with code {rc} (log: {logfile}){C.R}")
    return rc

def shlex_quote(s):
    import shlex
    return shlex.quote(s)

# ----------------------------------------------------------------------------
# Built-in (pure-Python) steps — no external tool needed
# ----------------------------------------------------------------------------
def builtin_scope_filter(ctx, scope, params):
    """Keep only in-scope hosts from an input file."""
    src = resolve(params.get("input", "{out}/subdomains.txt"), ctx)
    dst = resolve(params.get("output", "{out}/subdomains_inscope.txt"), ctx)
    if not os.path.exists(src):
        print(f"{C.YEL}skip: {src} not found (run subdomain step first){C.R}")
        return
    kept, dropped = [], []
    for line in open(src):
        h = line.strip()
        if not h:
            continue
        host = re.sub(r"^https?://", "", h).split("/")[0]
        (kept if scope.is_allowed(host) else dropped).append(h)
    with open(dst, "w") as f:
        f.write("\n".join(kept) + ("\n" if kept else ""))
    print(f"{C.GRN}kept {len(kept)} in-scope, dropped {len(dropped)} -> {dst}{C.R}")
    if dropped:
        print(f"{C.DIM}dropped (out of scope): {', '.join(dropped[:8])}"
              f"{'...' if len(dropped) > 8 else ''}{C.R}")

# Parameter names that commonly indicate an object reference worth manual IDOR testing.
IDOR_PARAMS = ("id", "uid", "userid", "user_id", "user", "account", "acct",
               "order", "orderid", "invoice", "doc", "document", "file",
               "num", "no", "key", "profile", "pid", "group", "team", "org",
               "customer", "cid", "record", "ref")

def builtin_idor_candidates(ctx, scope, params):
    """Flag URLs whose parameters look like object references -> MANUAL IDOR testing."""
    src = resolve(params.get("input", "{out}/urls.txt"), ctx)
    dst = resolve(params.get("output", "{out}/idor_candidates.txt"), ctx)
    if not os.path.exists(src):
        print(f"{C.YEL}skip: {src} not found (run URL-collection step first){C.R}")
        return
    rx = re.compile(r"[?&](" + "|".join(IDOR_PARAMS) + r")=", re.I)
    hits = sorted({l.strip() for l in open(src) if l.strip() and rx.search(l)})
    with open(dst, "w") as f:
        f.write("\n".join(hits) + ("\n" if hits else ""))
    print(f"{C.GRN}{len(hits)} IDOR-candidate URLs -> {dst}{C.R}")
    print(f"{C.MAG}NOTE: scanners can't confirm IDOR. Test these MANUALLY with the "
          f"two-account technique (Burp + Autorize).{C.R}")
    for h in hits[:10]:
        print(f"  {C.DIM}{h}{C.R}")

def builtin_report(ctx, scope, params):
    """Write a simple markdown summary of everything produced this run."""
    out = ctx["out"]
    dst = os.path.join(out, "REPORT.md")
    lines = [f"# Recon report — {ctx['target']}",
             f"_Generated {datetime.now().strftime('%d/%m/%Y %H:%M')}_\n",
             "## Output files\n"]
    for root, _, files in os.walk(out):
        for fn in sorted(files):
            p = os.path.join(root, fn)
            if os.path.abspath(p) == os.path.abspath(dst):
                continue
            size = os.path.getsize(p)
            rel = os.path.relpath(p, out)
            lines.append(f"- `{rel}` — {size} bytes")
    idor = os.path.join(out, "idor_candidates.txt")
    if os.path.exists(idor):
        n = sum(1 for _ in open(idor))
        lines += ["\n## Manual follow-up",
                  f"- **{n} IDOR-candidate URLs** in `idor_candidates.txt` — "
                  f"test with the two-account technique (Burp + Autorize)."]
    with open(dst, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"{C.GRN}report written -> {dst}{C.R}")

BUILTINS = {
    "scope_filter": builtin_scope_filter,
    "idor_candidates": builtin_idor_candidates,
    "report": builtin_report,
}

# ----------------------------------------------------------------------------
# Interactive per-step menu
# ----------------------------------------------------------------------------
def choose_tool(step):
    alts = step.get("alternatives", [])
    if not alts:
        print(f"{C.YEL}no alternatives defined for this step{C.R}")
        return None
    print(f"\n{C.B}Alternative tools:{C.R}")
    print(f"  0) {step['tool']}  (current)  {C.DIM}{step.get('command','')}{C.R}")
    for i, a in enumerate(alts, 1):
        installed = "" if shutil.which(a['tool']) else f" {C.RED}(not installed){C.R}"
        print(f"  {i}) {a['tool']}{installed}  {C.DIM}{a.get('command','')}{C.R}")
    sel = input("pick number > ").strip()
    if not sel.isdigit():
        return None
    idx = int(sel)
    if idx == 0:
        return {"tool": step["tool"], "command": step.get("command", "")}
    if 1 <= idx <= len(alts):
        return alts[idx - 1]
    return None

def handle_step(step, ctx, scope, args):
    tool = step.get("tool", "?")
    name = step.get("name", step.get("id", "step"))
    dangerous = step.get("dangerous", False)
    is_builtin = step.get("builtin") is not None

    # Resolve current command for display
    cur_cmd = resolve(step.get("command", ""), ctx) if not is_builtin else \
        f"(built-in: {step['builtin']})"

    hr()
    tag = f"{C.RED}{C.B}[DANGEROUS]{C.R} " if dangerous else ""
    print(f"{tag}{C.B}{name}{C.R}")
    print(f"tool: {C.CYN}{tool}{C.R}"
          + ("" if is_builtin or shutil.which(tool.split()[0])
             else f"  {C.RED}(not installed){C.R}"))
    if step.get("info"):
        print(f"{C.DIM}{step['info']}{C.R}")
    print(f"cmd:  {cur_cmd}")

    # Unattended execution:
    #   --auto      : run normal steps automatically; dangerous steps still prompt to confirm
    #   --full-auto : run everything automatically, including dangerous steps
    if args.auto and (not dangerous or args.full_auto):
        if dangerous:
            print(f"{C.RED}[full-auto] running DANGEROUS step unattended.{C.R}")
        return _execute(step, ctx, scope, cur_cmd, args)

    while True:
        opts = "[R]un  [E]dit  [T]ool  [S]kip  [I]nfo  [Q]uit"
        choice = input(f"{C.B}{opts} > {C.R}").strip().lower()

        if choice in ("r", "run", ""):
            if dangerous:
                print(f"{C.RED}This step is active/aggressive and may be banned by the "
                      f"program. Type the target to confirm authorization.{C.R}")
                if input(f"confirm target [{ctx['target']}] > ").strip() != ctx["target"]:
                    print(f"{C.YEL}not confirmed — skipping{C.R}")
                    return "skipped"
            return _execute(step, ctx, scope, cur_cmd, args)

        elif choice in ("e", "edit"):
            if is_builtin:
                print(f"{C.YEL}built-in steps have no shell command to edit{C.R}")
                continue
            new = input_with_prefill("edit > ", cur_cmd)
            if new.strip():
                cur_cmd = new.strip()
                print(f"{C.GRN}command updated for this run{C.R}")

        elif choice in ("t", "tool"):
            picked = choose_tool(step)
            if picked:
                step["tool"] = picked["tool"]
                step["command"] = picked.get("command", "")
                cur_cmd = resolve(step["command"], ctx)
                print(f"{C.GRN}switched to {picked['tool']}{C.R}")
                print(f"cmd:  {cur_cmd}")

        elif choice in ("s", "skip"):
            print(f"{C.DIM}skipped{C.R}")
            return "skipped"

        elif choice in ("i", "info"):
            print(f"{C.DIM}{step.get('info','(no extra info)')}{C.R}")

        elif choice in ("q", "quit"):
            print(f"{C.YEL}quitting pipeline{C.R}")
            sys.exit(0)

        else:
            print(f"{C.DIM}unknown option{C.R}")

def _execute(step, ctx, scope, cur_cmd, args):
    if step.get("builtin"):
        BUILTINS[step["builtin"]](ctx, scope, step.get("params", {}))
        return "done"
    if args.dry_run:
        print(f"{C.YEL}[dry-run] would execute:{C.R} {cur_cmd}")
        return "dry-run"
    logfile = os.path.join(ctx["out"], "logs", f"{step.get('id','step')}.log")
    run_shell(cur_cmd, logfile)
    return "done"

# ----------------------------------------------------------------------------
# Tool availability check
# ----------------------------------------------------------------------------
def check_tools(config):
    banner("Tool availability check")
    seen = set()
    for phase in config.get("phases", []):
        for step in phase.get("steps", []):
            for t in [step.get("tool", "")] + [a["tool"] for a in step.get("alternatives", [])]:
                bin_ = t.split()[0] if t else ""
                if not bin_ or bin_ in seen or step.get("builtin"):
                    continue
                seen.add(bin_)
                ok = shutil.which(bin_)
                mark = f"{C.GRN}✓{C.R}" if ok else f"{C.RED}✗{C.R}"
                print(f"  {mark} {bin_}" + ("" if ok else f"  {C.DIM}(missing){C.R}"))
    print(f"\n{C.DIM}Install missing tools — see README.md for the Kali install commands.{C.R}")

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Deterministic step-by-step bug-bounty pipeline.")
    ap.add_argument("--target", help="primary target domain, e.g. example.com")
    ap.add_argument("--scope", default="scope.yaml", help="scope file (default: scope.yaml)")
    ap.add_argument("--config", default="pipeline.yaml", help="pipeline config (default: pipeline.yaml)")
    ap.add_argument("--output", default="output", help="output base dir (default: output)")
    ap.add_argument("--wordlist", default=None,
                    help="wordlist for content discovery (default: auto-detect an installed one)")
    ap.add_argument("--dry-run", action="store_true", help="print commands, run nothing")
    ap.add_argument("--auto", action="store_true", help="run enabled steps without prompting (DANGEROUS steps still confirm)")
    ap.add_argument("--full-auto", action="store_true",
                    help="fully unattended: no prompts at all, auto-run enabled steps INCLUDING dangerous ones. "
                         "Use only after you've tested interactively.")
    ap.add_argument("--check", action="store_true", help="check which tools are installed and exit")
    ap.add_argument("--yes-authorized", action="store_true", help="skip the authorization prompt")
    args = ap.parse_args()

    # --full-auto implies unattended everything
    if args.full_auto:
        args.auto = True
        args.yes_authorized = True

    if not args.wordlist:
        args.wordlist = default_wordlist()

    if not os.path.exists(args.config):
        sys.exit(f"config not found: {args.config}")
    with open(args.config) as f:
        config = yaml.safe_load(f)

    if args.check:
        check_tools(config)
        wl = args.wordlist or default_wordlist()
        if os.path.exists(wl):
            print(f"\n  {C.GRN}✓{C.R} wordlist: {wl}")
        else:
            print(f"\n  {C.RED}✗{C.R} no wordlist found — run: {C.B}sudo apt install -y seclists{C.R}")
        return

    if not args.target:
        sys.exit("--target is required (or use --check)")

    scope = Scope.load(args.scope)

    # Scope gate
    if not scope.is_allowed(args.target):
        sys.exit(f"{C.RED}REFUSING: {args.target} is not in scope ({args.scope}).{C.R}\n"
                 f"{scope.describe()}")

    out = os.path.join(args.output, args.target)
    os.makedirs(out, exist_ok=True)
    ctx = {"target": args.target, "target_url": f"https://{args.target}",
           "out": out, "wordlist": args.wordlist}

    banner(f"bbpipe — target: {args.target}", C.MAG)
    print(scope.describe())
    print(f"output: {out}")
    if args.dry_run:
        print(f"{C.YEL}DRY RUN — nothing will actually execute.{C.R}")

    # Authorization acknowledgement
    if not args.yes_authorized and not args.dry_run:
        print(f"\n{C.RED}{C.B}Only test assets you are explicitly authorized to test.{C.R}")
        if input("Type 'I am authorized' to continue > ").strip().lower() != "i am authorized":
            sys.exit("aborted.")

    # Walk phases/steps
    for phase in config.get("phases", []):
        banner(f"══ {phase.get('name','Phase')} ══", C.BLU)
        for step in phase.get("steps", []):
            if not step.get("enabled", True):
                print(f"{C.DIM}(disabled in config) {step.get('name', step.get('id'))}{C.R}")
                continue
            handle_step(step, ctx, scope, args)

    banner("Pipeline complete.", C.GRN)
    print(f"Results in: {out}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{C.YEL}interrupted{C.R}")
        sys.exit(130)
