"""One-shot diagnostic runnable via `railway run python _diag.py`.
Tests whether the Railway container's IP can reach H2S at all.
"""
import os
from curl_cffi import requests as cf

s = cf.Session(impersonate="chrome")
s.headers.update({
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.holland2stay.com",
    "Referer": "https://www.holland2stay.com/residences",
    "Store": "default",
})

print("=== 0. external ip ===")
try:
    r = s.get("https://api.ipify.org?format=json", timeout=15)
    print("ip:", r.text[:80])
except Exception as e:
    print("ip probe error:", e)

for url, method, body in [
    ("https://www.holland2stay.com/", "GET", None),
    ("https://www.holland2stay.com/residences", "GET", None),
    ("https://api.holland2stay.com/graphql", "POST", {"query": "{ products(pageSize:1, search: \"\"){ total_count }}"}),
    ("https://api.holland2stay.com/rest/V1/store/storeConfigs", "GET", None),
]:
    print(f"\n=== {method} {url} ===")
    try:
        if method == "GET":
            r = s.get(url, timeout=20)
        else:
            r = s.post(url, json=body, timeout=20)
        print("status:", r.status_code, "ctype:", r.headers.get("content-type"))
        body_head = r.text[:200].replace("\n", " ")
        print("body:", body_head)
    except Exception as e:
        print("ERROR:", type(e).__name__, e)
