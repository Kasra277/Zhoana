"""Fresh reachability check from the container."""
import json, urllib.request, urllib.error, time

def probe(url, body=None, headers=None):
    headers = headers or {}
    data = None
    if body is not None:
        headers.setdefault("Content-Type", "application/json")
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers,
                                  method=("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            print(f"{url} -> {r.status} server={r.headers.get('Server','?')} cf={r.headers.get('cf-ray','none')}")
            print(f"  body: {raw[:160]!r}")
    except urllib.error.HTTPError as e:
        body = e.read()
        print(f"{url} -> {e.code} server={e.headers.get('Server','?')} cf={e.headers.get('cf-ray','none')}")
        print(f"  body: {body[:160]!r}")
    except Exception as e:
        print(f"{url} -> {type(e).__name__}: {e}")

ua = "Mozilla/5.0 (Windows NT 10.0; Win64) Chrome/131 Safari/537.36"
print("[", time.strftime("%H:%M:%S UTC"), "]")
probe("https://api.ipify.org?format=json")
probe("https://www.holland2stay.com/", headers={"User-Agent": ua})
probe("https://api.holland2stay.com/graphql",
      body={"query": '{products(pageSize:1,search:""){total_count}}'},
      headers={"User-Agent": ua, "Store": "default",
               "Origin": "https://www.holland2stay.com"})
