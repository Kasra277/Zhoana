"""Pure-stdlib diagnostic — works even if curl_cffi can't load its native libs."""
import json
import urllib.request
import urllib.error

def probe(name, url, data=None, headers=None, is_json=False):
    headers = headers or {}
    if data is not None and is_json:
        body = json.dumps(data).encode()
        headers.setdefault("Content-Type", "application/json")
    else:
        body = data
    req = urllib.request.Request(url, data=body, headers=headers,
                                  method=("POST" if body else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            print(f"{name}: {r.status} ctype={r.headers.get('Content-Type','?')}")
            print(f"  body: {raw[:200]!r}")
    except urllib.error.HTTPError as e:
        body = e.read()
        print(f"{name}: HTTP {e.code} server={e.headers.get('Server','?')} cf={e.headers.get('cf-ray','none')}")
        print(f"  body: {body[:200]!r}")
    except Exception as e:
        print(f"{name}: {type(e).__name__}: {e}")

probe("ipify", "https://api.ipify.org?format=json")
probe("h2s-home", "https://www.holland2stay.com/",
      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64) Chrome/131 Safari/537.36"})
probe("h2s-residences", "https://www.holland2stay.com/residences",
      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64) Chrome/131 Safari/537.36"})
probe("h2s-api-graphql", "https://api.holland2stay.com/graphql",
      data={"query": "{products(pageSize:1,search:\"\"){total_count}}"},
      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64) Chrome/131 Safari/537.36",
               "Store": "default",
               "Origin": "https://www.holland2stay.com"},
      is_json=True)
