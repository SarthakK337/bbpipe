# Wordlists

Wordlists bundled with the repo so bbpipe works out-of-the-box on any machine,
even if `seclists` / `dirb` aren't installed.

- **`common.txt`** — a curated ~250-entry list of the highest-value web paths
  (secrets, admin panels, backups, VCS folders, API/docs, config files). Good for
  a fast first pass. This is bbpipe's guaranteed fallback if no system wordlist is found.

## Wordlist priority (auto-detected)

`bbpipe.py --check` shows which one it picked. The tool tries, in order:
1. `/usr/share/seclists/Discovery/Web-Content/common.txt`
2. `/usr/share/wordlists/seclists/Discovery/Web-Content/common.txt`
3. `/usr/share/wordlists/dirb/common.txt`
4. `/usr/share/wordlists/dirbuster/directory-list-2.3-medium.txt`
5. **this repo's `wordlists/common.txt`** ← always present

So bigger installed lists win; the bundled one is the safety net.

## Adding the bigger lists (4k / 29k / etc.) to the repo

SecLists is MIT-licensed, so you can commit copies. Copy the real lists from your
Kali install into this folder, then commit:

```bash
cd ~/bbpipe/wordlists
cp /usr/share/seclists/Discovery/Web-Content/common.txt          seclists-common-4700.txt
cp /usr/share/seclists/Discovery/Web-Content/raft-medium-directories.txt  raft-medium-dirs-30000.txt
cp /usr/share/seclists/Discovery/Web-Content/directory-list-2.3-medium.txt dirlist-medium-220000.txt
git add . && git commit -m "add bundled wordlists" && git push
```

Then point any run at one of them:

```bash
python3 bbpipe.py  --target TARGET --wordlist wordlists/raft-medium-dirs-30000.txt --dry-run
python3 dashboard.py --wordlist wordlists/raft-medium-dirs-30000.txt
```

Or in the dashboard: expand the ffuf step and change the `-w` path in the command box.

> Note: GitHub warns above 50 MB and blocks above 100 MB per file.
> `directory-list-2.3-medium.txt` (~2 MB) is fine; avoid committing giant
> multi-hundred-MB lists — keep those local.
