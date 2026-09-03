#!/usr/bin/env python3
"""
diag4.py -- isolate what triggers the 403, and report the egress IP.

Run it BOTH locally and inside GitHub Actions. The comparison matters:
if it passes locally but fails on Actions, the block depends on the source
IP (GitHub runners sit in Azure datacentre ranges), not on our headers.

    python diag4.py
"""

import requests

BASE = "https://tickets.themagiciansstudy.com"
EVENT = "wZd"
URL = f"{BASE}/rest/events/${EVENT}?_branch=findByDomainNameOrHashId&_s=1"

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             "AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/131.0.0.0 Safari/537.36")
AGREED_UA = "MagiciansStudyCalendar/1.0 (+milo@example.com)"

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-AU,en;q=0.9",
    "source": "ONLINE",
    "hashsiteid": "",
    "hashuserid": "",
    "lang": "",
    "Referer": f"{BASE}/event?e={EVENT}",
}

# ---- where are we calling from? FriendlySky asked for this ----
print("=" * 62)
try:
    ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
    print(f"EGRESS IP: {ip}")
except Exception as e:
    print(f"EGRESS IP: could not determine ({e!r})")
print("=" * 62)
print()

CASES = [
    ("1. Chrome UA, no _client   (previously working)", CHROME_UA, False),
    ("2. Chrome UA, with _client                     ", CHROME_UA, True),
    ("3. Agreed UA, no _client                       ", AGREED_UA, False),
    ("4. Agreed UA, with _client (current config)    ", AGREED_UA, True),
]

for label, ua, use_client in CASES:
    url = URL + ("&_client=MagiciansStudyCalendar" if use_client else "")
    headers = dict(BASE_HEADERS, **{"User-Agent": ua})
    try:
        r = requests.get(url, headers=headers, timeout=20)
        verdict = "OK" if r.status_code == 200 else "BLOCKED"
        print(f"{label}  ->  {r.status_code}  {verdict}")
        if r.status_code != 200:
            print(f"      server   : {r.headers.get('server','')!r}")
            print(f"      cf-ray   : {r.headers.get('cf-ray','')!r}")
            print(f"      x-req-id : {r.headers.get('x-request-id','')!r}")
            print(f"      body     : {r.text[:200]!r}")
    except Exception as e:
        print(f"{label}  ->  ERROR {e!r}")
    print()
