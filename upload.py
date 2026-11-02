"""Put the latest source on GitHub.

Run it on its own (`python upload.py`) or let `build_bb9.py` call it after a build. It only
ever sends the source: .gitignore keeps your .env, your boards, your notes and the built app
on this laptop. If there is no GitHub remote yet it says how to add one and stops.
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def git(*args, quiet=False):
    r = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)
    if r.returncode and not quiet:
        print(r.stderr.strip() or r.stdout.strip())
    return r


def main(message=None):
    if not (HERE / ".git").exists():
        print("This folder is not a git repository yet. Run: git init")
        return 1

    remote = git("remote", "get-url", "origin", quiet=True)
    if remote.returncode:
        print("No GitHub repository is linked yet. Make one at https://github.com/new,")
        print("then run:\n")
        print('  git remote add origin https://github.com/<you>/blackboard-buddy.git')
        print("  git push -u origin main")
        return 1

    # anything private that slipped past .gitignore stops the whole thing
    staged = git("status", "--porcelain").stdout.splitlines()
    risky = [l[3:].strip('"') for l in staged if Path(l[3:].strip('"')).name in {".env", ".env.local"}]
    if risky:
        print("Refusing to upload: these are private ->", ", ".join(risky))
        return 1

    if not staged:
        print("Nothing has changed since the last upload.")
    else:
        git("add", "-A")
        msg = message or "Update " + time.strftime("%d %b %Y, %H:%M")
        if git("commit", "-m", msg).returncode:
            return 1
        print("Committed:", msg)

    push = git("push", "origin", "HEAD")
    if push.returncode:
        print("Could not push. If it is asking to sign in, run `git push` once in a terminal.")
        return 1
    print("Uploaded to", remote.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main(" ".join(sys.argv[1:]) or None))
