---
name: commit
description: "Stage, commit, and push changes with asset verification. Use when the user says 'commit', 'push', or 'commit and push'."
---

# Smart Commit & Push

1. Run `git status` — identify all modified, staged, and untracked files
2. **Asset check**: Look for untracked asset files (*.mp3, *.wav, *.png, *.jpg, *.svg, *.gif, *.ico, *.woff, *.woff2). If found, warn the user and suggest staging them
3. Check for files that should NOT be committed: .env, credentials.json, node_modules/, __pycache__/, *.pyc
4. Run `git diff --stat` to understand the scope of changes
5. Read `git log --oneline -5` to match the repo's commit message style
6. Draft a concise commit message (1-2 sentences) focusing on "why" not "what"
7. Stage relevant files (prefer specific files over `git add -A`)
8. Commit with the drafted message + `Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>`
9. Push to the current branch. If push fails due to remote changes, `git pull --rebase` then retry
10. Report the commit hash and branch
