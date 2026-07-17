# bbpipe

A **deterministic, step-by-step bug-bounty recon & scan pipeline.**
It is *not* an AI tool — it just runs the Kali tools you'd run by hand, one step
at a time, and **asks you before every step** so you stay in full control.

At each step you choose:

```
[R]un   [E]dit command   [T] switch tool   [S]kip   [I]nfo   [Q]uit
```

Every step, tool, and exact command lives in [`pipeline.yaml`](pipeline.yaml) —
edit it however you like. When you're happy it works, run it fully unattended
with `--full-auto`.

> ⚠️ **Authorized testing only.** Point this only at assets you are explicitly
> permitted to test (a bug-bounty program's in-scope assets, your own lab, or
> PortSwigger's Web Security Academy). Automated scanning is **banned by many
> programs** — read the rules of engagement first. The `scope.yaml` guardrail
> makes the pipeline refuse any host you haven't listed.

---

## What it does (default pipeline)

| Phase | Tool(s) | Purpose |
|-------|---------|---------|
| 1. Subdomains | subfinder *(amass, assetfinder)* | discover hosts |
| — scope filter | *(built-in)* | drop anything not in `scope.yaml` |
| 2. Live hosts | httpx *(httprobe)* | keep only responding hosts |
| 3. Screenshots | gowitness *(aquatone)* | visual overview |
| 4. Fingerprint | whatweb | detect stack / CMS |
| 5. Content discovery | ffuf *(dirsearch, gobuster, feroxbuster)* | hidden paths/files |
| 6. URLs & params | gau *(waybackurls, katana)* | collect URLs |
| — IDOR flag | *(built-in)* | flag object-ref URLs for **manual** testing |
| 7. Detection | nuclei, nikto | known CVEs / misconfigs |
| 8. WordPress | wpscan *(off by default)* | WP-specific |
| 9. Injection ⚠️ | sqlmap, dalfox, commix *(off + dangerous)* | active exploitation |
| 10. Report | *(built-in)* | `REPORT.md` summary |

The **IDOR step is deliberately manual** — scanners can't confirm access-control
bugs. bbpipe just flags candidate URLs; you test them with the two-account
technique (Burp + Autorize).

---

## Setup on Windows (Kali WSL2) — step by step

### Step 0 — Open the Kali terminal
Any one of these:
- **Start menu** → type **"Kali Linux"** → open it, **or**
- Open **Windows Terminal** → click the **∨** dropdown → **Kali Linux**, **or**
- Open **PowerShell** and run: `wsl -d kali-linux`

You'll land at a Kali shell prompt (`┌──(user㉿…)`).

### Step 1 — Update & ensure git
```bash
sudo apt update
sudo apt install -y git
```

### Step 2 — Download the repo
```bash
git clone https://github.com/SarthakK337/bbpipe.git
cd bbpipe
```

### Step 3 — Install the Python bits
Kali blocks plain `pip install` (PEP 668), so install via apt — cleanest:
```bash
sudo apt install -y python3-yaml python3-flask
```
<sub>(Fallback if you insist on pip: `pip install --break-system-packages -r requirements.txt`)</sub>

### Step 4 — Install the security tools
```bash
sudo apt install -y ffuf gobuster feroxbuster nikto whatweb nuclei wpscan sqlmap commix seclists dirsearch amass

# Go-based tools (subfinder, httpx, gau, katana, dalfox, gowitness, assetfinder, waybackurls, httprobe)
sudo apt install -y golang-go
go install github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest
go install github.com/projectdiscovery/httpx/cmd/httpx@latest
go install github.com/projectdiscovery/katana/cmd/katana@latest
go install github.com/lc/gau/v2/cmd/gau@latest
go install github.com/hahwul/dalfox/v2@latest
go install github.com/tomnomnom/assetfinder@latest
go install github.com/tomnomnom/waybackurls@latest
go install github.com/tomnomnom/httprobe@latest
go install github.com/sensepost/gowitness@latest
```

### Step 5 — Make the Go tools findable
```bash
echo 'export PATH=$PATH:$HOME/go/bin' >> ~/.bashrc
source ~/.bashrc
```

### Step 6 — Check what's installed
```bash
python3 bbpipe.py --check
```
Green ✓ = ready, red ✗ = install it (or just `[S]`kip that step at runtime).

> **Steps 1–5 are one-time setup.** After that you only need Steps 7–9 (below /
> in Usage) for each new target.

---

## Usage

**1. Set your scope** (per program):
```bash
cp scope.example.yaml scope.yaml
nano scope.yaml            # list the domains you're authorized to test
```

**2. Dry run first** — see every command, execute nothing:
```bash
python3 bbpipe.py --target example.com --dry-run
```

**3. Interactive run** — approve/edit/swap each step (recommended while learning):
```bash
python3 bbpipe.py --target example.com
```

**4. Fully unattended** — once you trust it, no prompts at all:
```bash
# runs every ENABLED step automatically (dangerous steps still off unless you enabled them)
python3 bbpipe.py --target example.com --auto --yes-authorized

# TRUE hands-off: also auto-runs dangerous steps you've enabled in pipeline.yaml
python3 bbpipe.py --target example.com --full-auto
```

### The recommended progression
1. `--dry-run` → read the commands.
2. plain interactive → run it once, `[E]dit`/`[T]`weak anything that needs it.
3. `--auto` → hands-off for the safe phases.
4. enable dangerous steps in `pipeline.yaml` **only** where the program allows it, then `--full-auto`.

---

## Dashboard (point-and-click control)

Prefer a UI over the CLI? Run the local dashboard:

```bash
pip install flask pyyaml
python3 dashboard.py
# then open http://127.0.0.1:8777 in your browser
```

From the dashboard you can:
- enter a target (with a **live in-scope / not-in-scope** indicator),
- **toggle** any step on/off, **edit** its command, or **switch tools** from a dropdown,
- **Run all enabled**, **Run selected**, run **one step**, or **Dry-run**,
- **Stop** a run mid-way,
- watch **live output** stream in, with a status pill per step.

Notes:
- It uses the **same** `pipeline.yaml` and `scope.yaml` as the CLI. Edits made in
  the UI are saved to `.bbpipe_state.json` (git-ignored) so your commented
  `pipeline.yaml` is never overwritten. Edit the YAML directly and hit **↻ Reload config**.
- Dangerous steps only run if you tick **"Allow dangerous steps"**.
- ⚠️ **Security:** the dashboard executes shell commands and binds to
  `127.0.0.1` (localhost) **only** — never expose it to a network or run it on a
  shared box.

The CLI (`bbpipe.py`) and dashboard are interchangeable — use whichever you like.

---

## Full control — how to customise

- **Change any command:** edit [`pipeline.yaml`](pipeline.yaml) (permanent) or press `[E]` at runtime (just that run).
- **Swap a tool:** press `[T]` at runtime, or reorder the `alternatives:` list.
- **Turn a step on/off:** set `enabled: true|false` in `pipeline.yaml`.
- **Add a new step:** copy any block under a phase and change `id`, `tool`, `command`.
- **Different wordlist:** `--wordlist /path/to/list.txt`.

Placeholders usable in any command: `{target}`, `{target_url}`, `{out}`, `{wordlist}`.

---

## Output

Everything lands in `output/<target>/`:
```
output/example.com/
├── subdomains.txt / subdomains_inscope.txt
├── live_hosts.txt
├── screenshots/
├── whatweb.txt
├── ffuf.json
├── urls.txt / idor_candidates.txt
├── nuclei.txt / nikto.txt
├── logs/            # full stdout of every step
└── REPORT.md        # summary + manual follow-ups
```

`output/` and `scope.yaml` are git-ignored so you never accidentally commit scan
data or client scope.

---

## Push to GitHub (from Windows or WSL)

```bash
cd bbpipe
git init
git add .
git commit -m "Initial commit: bbpipe pipeline"
git branch -M main
git remote add origin https://github.com/<your-username>/bbpipe.git
git push -u origin main
```

---

## Disclaimer

For authorized security testing only. You are responsible for ensuring you have
permission to test any target. The authors accept no liability for misuse.
