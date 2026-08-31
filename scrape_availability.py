#!/usr/bin/env python3
"""
scrape_availability.py  --  The Magician's Study live availability feed.

Runs in two tiers, per the access pattern agreed with FriendlySky:

    python scrape_availability.py near     # shows in the next 14 days  (hourly)
    python scrape_availability.py full     # every show                 (once daily)

Approved volume: ~408/day (near, hourly) + ~196/day (full, daily) = ~604/day,
about 0.007 requests/second sustained.

AGREED CONSTRAINTS -- do not change these without asking FriendlySky first:
  * User-Agent:  MagiciansStudyCalendar/1.0 (+milo@example.com)
  * Every request carries _client=MagiciansStudyCalendar so they can identify
    our traffic in their access logs.
  * One request at a time, with a gap between them. No concurrency.
  * Read-only storefront calls only. No cart, no checkout, no back office.

ENDPOINTS (all GET, public storefront, no auth):
  1. /rest/events/${EVENT_HASH}?_branch=findByDomainNameOrHashId
       -> `games` array: hashId, begDate, begTime, urlName, status
  2. /rest/pkgs?_branch=findByGameIdAndUrlName&hashGameId=..&urlName=tickets
       -> package id for the show. Cached permanently; ~0 calls in steady state.
  3. /rest/onlinePageDispatcher/firstPage?hashPkgId=..
       -> read targetPkg.pkgItems (the SEG entries), each with an `inv` block:
            invTotal / invSold / invRemain

Read targetPkg.pkgItems, NOT targetPkgItems: the latter is only "segments
currently offered for sale" and goes empty for sold-out and cancelled shows.

CLASSIFICATION (fully automatic, no manual labels):
    remain > 0                       -> on_sale
    remain = 0, holds small          -> sold_out   (red, labelled SOLD OUT)
    remain = 0, holds >= half house  -> cancelled  (hidden from the calendar)

A cancelled show has its unsold seats swept into hold, which is what separates
it from a genuine sell-out. Verified against real data:
    28 Aug 21:30  total 65, sold 63, remain 0, held  2  -> sold out
     1 Oct 19:00  total 65, sold  0, remain 0, held 65  -> cancelled
"""

import json
import sys
import time
import pathlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
BASE = "https://tickets.themagiciansstudy.com"
EVENT_HASH = "wZd"
VENUE_TZ = ZoneInfo("America/Los_Angeles")

CAPACITY = 65                  # house size; sanity check only
NEAR_DAYS = 14                 # "near" tier window
HORIZON_DAYS = 365             # how far ahead the full sweep goes
REQUEST_PAUSE = 0.4            # seconds between requests; serial, no concurrency
TIMEOUT = 20

CANCELLED_HOLD_FRACTION = 0.5

# Agreed with FriendlySky so they can identify and shape our traffic.
CLIENT_ID = "MagiciansStudyCalendar"
USER_AGENT = "MagiciansStudyCalendar/1.0 (+milo@example.com)"

OUT = pathlib.Path("availability.json")
PKG_CACHE = pathlib.Path("pkg_cache.json")

SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-AU,en;q=0.9",
    # headers the storefront's own JS sends; the API 401s without them
    "source": "ONLINE",
    "hashsiteid": "",
    "hashuserid": "",
    "lang": "",
    "Referer": f"{BASE}/event?e={EVENT_HASH}",
    "User-Agent": USER_AGENT,
})


def get_json(url):
    """GET with the agreed _client tag appended, one retry, serial only."""
    url = url + ("&" if "?" in url else "?") + f"_client={CLIENT_ID}"
    for attempt in (1, 2):
        try:
            r = SESSION.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5)
    return None


# ----------------------------------------------------------------------------
# Step 1: list shows (one call, whichever tier we're running)
# ----------------------------------------------------------------------------
def list_games(tier):
    url = f"{BASE}/rest/events/${EVENT_HASH}?_branch=findByDomainNameOrHashId&_s=1"
    data = get_json(url)["data"]
    games = data.get("games", [])

    today = datetime.now(VENUE_TZ).date()
    cutoff = today + timedelta(days=NEAR_DAYS if tier == "near" else HORIZON_DAYS)

    out = []
    for g in games:
        beg = g.get("begDate")
        if not beg:
            continue
        d = datetime.strptime(beg, "%Y-%m-%d").date()
        if d < today or d > cutoff:
            continue
        if g.get("status") != "Y":
            continue
        url_name = g.get("urlName") or ""
        out.append({
            "hashGameId": g["hashId"],
            "date": beg,
            "time": g.get("begTime", "")[:5],
            # Guest-facing purchase link. Deliberately WITHOUT _client -- that
            # tag is for our automated calls only; guest clicks are ordinary
            # customer traffic and must not be tagged as ours.
            "url": f"{BASE}/event/{url_name}/tickets/seg?e={EVENT_HASH}" if url_name else None,
        })
    out.sort(key=lambda x: (x["date"], x["time"]))
    return out


# ----------------------------------------------------------------------------
# Step 2: game -> pkgId (cached permanently)
# ----------------------------------------------------------------------------
def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def pkg_id_for(game_hash, cache):
    if game_hash in cache:
        return cache[game_hash]
    url = (f"{BASE}/rest/pkgs?_branch=findByGameIdAndUrlName"
           f"&hashGameId={game_hash}&urlName=tickets&_s=1")
    time.sleep(REQUEST_PAUSE)
    data = get_json(url).get("data")
    if not data:
        raise RuntimeError(f"no pkg for game {game_hash}")
    cache[game_hash] = data["hashId"]
    return cache[game_hash]


# ----------------------------------------------------------------------------
# Step 3: pkgId -> inventory + state
# ----------------------------------------------------------------------------
def inventory_for(pkg_hash):
    url = f"{BASE}/rest/onlinePageDispatcher/firstPage?hashPkgId={pkg_hash}"
    time.sleep(REQUEST_PAUSE)
    data = get_json(url).get("data", {})

    pkg = data.get("targetPkg") or {}
    segs = [i for i in (pkg.get("pkgItems") or []) if i.get("pkgItemType") == "SEG"]
    if not segs:
        raise RuntimeError(f"pkg {pkg_hash}: no SEG entries in targetPkg.pkgItems")

    remain = total = sold = 0
    for pk in segs:
        inv = (pk.get("item") or {}).get("inv") or {}
        remain += int(inv.get("invRemain") or 0)
        total += int(inv.get("invTotal") or 0)
        sold += int(inv.get("invSold") or 0)

    if total == 0:
        raise RuntimeError(f"pkg {pkg_hash}: invTotal is 0")
    if total > CAPACITY + 5:
        raise RuntimeError(f"pkg {pkg_hash}: invTotal {total} exceeds house")
    if remain < 0 or remain > total:
        raise RuntimeError(f"pkg {pkg_hash}: invRemain {remain} outside 0..{total}")

    held = total - sold - remain
    if remain > 0:
        state = "on_sale"
    elif held >= total * CANCELLED_HOLD_FRACTION:
        state = "cancelled"
    else:
        state = "sold_out"

    return remain, state


# ----------------------------------------------------------------------------
# Orchestrate
# ----------------------------------------------------------------------------
def main():
    tier = sys.argv[1] if len(sys.argv) > 1 else "full"
    if tier not in ("near", "full"):
        die(f"unknown tier {tier!r} -- use 'near' or 'full'")

    games = list_games(tier)
    floor = 3 if tier == "near" else 20
    if len(games) < floor:
        die(f"Only {len(games)} shows found for tier '{tier}' -- expected more. "
            f"Keeping the last good feed.")

    cache = load_json(PKG_CACHE, {})
    previous = load_json(OUT, {}).get("performances", [])
    prev_by_key = {(p["date"], p["time"]): p for p in previous}

    fresh = {}
    failures = []
    counts = {"on_sale": 0, "sold_out": 0, "cancelled": 0}

    for g in games:
        key = (g["date"], g["time"])
        try:
            pkg = pkg_id_for(g["hashGameId"], cache)
            remain, state = inventory_for(pkg)
            counts[state] += 1
            fresh[key] = {"date": g["date"], "time": g["time"],
                          "open": remain, "state": state, "url": g["url"]}
        except Exception as e:
            failures.append(f"{g['date']} {g['time']}: {e}")
            # keep whatever we knew before rather than blanking the show
            fresh[key] = prev_by_key.get(key, {
                "date": g["date"], "time": g["time"],
                "open": None, "state": "unknown", "url": g["url"]})

    ok = sum(1 for k in fresh if fresh[k]["state"] != "unknown")
    if ok < max(3, len(fresh) // 2):
        die(f"Only {ok}/{len(fresh)} shows classified in tier '{tier}'. "
            f"Refusing to overwrite the good feed. First errors: {failures[:3]}")

    # The near tier refreshes only its window; everything outside it is carried
    # over from the last full sweep, so hourly runs never wipe future dates.
    merged = dict(prev_by_key)
    merged.update(fresh)

    today = datetime.now(VENUE_TZ).date().isoformat()
    performances = [p for k, p in sorted(merged.items()) if p["date"] >= today]

    PKG_CACHE.write_text(json.dumps(cache, indent=2))
    write_atomic(OUT, {
        "updated_at": datetime.now(VENUE_TZ).isoformat(),
        "tier": tier,
        "venue_timezone": "America/Los_Angeles",
        "capacity": CAPACITY,
        "performances": performances,
    })

    print(f"OK [{tier}]  refreshed {len(fresh)} shows "
          f"({counts['on_sale']} on sale, {counts['sold_out']} sold out, "
          f"{counts['cancelled']} cancelled, {len(failures)} failed); "
          f"feed now holds {len(performances)} shows.")
    for f in failures[:10]:
        print("   -", f)


def write_atomic(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.replace(path)


def die(msg):
    print("FAIL " + msg, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
