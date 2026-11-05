"""Like the 1.3.2 app: an https request with no certificate fix at all."""
import ssl
import urllib.request
print("certificates looked for in:", ssl.get_default_verify_paths(), flush=True)
try:
    urllib.request.urlopen("https://api.github.com/zen", timeout=20).read()
    print("no fix: ok", flush=True)
except Exception as e:
    print("no fix:", repr(e)[:200], flush=True)
