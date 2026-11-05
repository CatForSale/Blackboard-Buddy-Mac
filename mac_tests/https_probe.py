"""Inside a built app: does an https request work, before and after main.py's certificate fix?"""
import urllib.request


def get():
    try:
        urllib.request.urlopen("https://api.github.com/zen", timeout=20).read()
        return "ok"
    except Exception as e:
        return repr(e)[:200]


print("without the fix:", get(), flush=True)
import main  # noqa: E402  (applies the fix on a Mac)
print("with the fix:", get(), flush=True)
