# Git push cheat sheet — bbpipe

Quick reference for pushing this folder to GitHub as **SarthakK337**
(email stays private via the noreply address — your real Gmail is never exposed).

Always run these from **inside the `bbpipe` folder**, never the parent folder.

---

## First-time push (new repo)

```bash
cd "/Users/sarthakkhokhar/Documents/Sarthak/Get It/bbpipe"
git init
git add .
git commit -m "first commit"
git branch -M main
git remote add origin https://github.com/SarthakK337/bbpipe.git
git push -u origin main
```

`git add .` pushes everything **except** what `.gitignore` blocks
(`scope.yaml`, `output/`, `.bbpipe_state.json`, `__pycache__/`).

### If the push is rejected ("fetch first" / non-fast-forward)
The GitHub repo already has a commit (you created it *with* a README/license):

```bash
git pull --rebase origin main
git push -u origin main
```

---

## Everyday updates (after the first push)

```bash
cd "/Users/sarthakkhokhar/Documents/Sarthak/Get It/bbpipe"
git add .
git commit -m "describe what changed"
git push
```

---

## Clone it onto your Kali WSL box

```bash
git clone https://github.com/SarthakK337/bbpipe.git
cd bbpipe
pip install -r requirements.txt
python3 bbpipe.py --check          # verify tools
```

---

## Handy checks

```bash
git status                 # what's staged / changed
git log --oneline -5       # recent commits
git config user.name       # -> SarthakK337
git config user.email      # -> ...@users.noreply.github.com (private)
git remote -v              # confirm origin points to your repo
```

---

## Safety reminders

- Never `git add` a real `scope.yaml` or the `output/` folder — `.gitignore`
  already blocks them, so don't force-add with `git add -f`.
- Double-check you're in the `bbpipe` folder before `git init`, so the parent
  folder's files (including `.env`) never get committed.
