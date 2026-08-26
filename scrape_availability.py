#!/usr/bin/env python3
"""
scrape_availability.py  --  The Magician's Study live availability feed.

Reads the storefront's own REST API (no login, no codeword, no browser):

  1. GET /rest/events/${EVENT_HASH}?_branch=findByDomainNameOrHashId
        -> `games` array = every showtime (hashId, begDate, begTime, status)

  2. GET /rest/pkgs?_branch=findByGameIdAndUrlName&hashGameId=..&urlName=tickets
        -> the ticket package for that show (pkgId). Stable, so cached.

  3. GET /rest/onlinePageDispatcher/firstPage?hashPkgId=..
        -> read targetPkg.pkgItems, the SEG entries, each with an `inv` block:
              invTotal   seats in that section
              invSold    sold + comp
              invRemain  still sellable (holds excluded)

IMPORTANT: read targetPkg.pkgItems, NOT targetPkgItems. The latter is only
"segments currently offered for sale" and goes empty for sold-out and
cancelled shows -- which is why those used to come back as unknown. The
former always carries the real inventory.

CLASSIFICATION (all automatic -- no manual labels anywhere):

    remain > 0                        -> on_sale    (colour by seat count)
    remain = 0, holds small           -> sold_out   (red)
    remain = 0, holds >= half house   -> cancelled  (blacked out)

A cancelled show has its unsold seats swept into "hold", which is what
distinguishes it from a genuine sell-out. Verified against real data:
  28 Aug 21:30  total 65, sold 63, remain 0, held  2  -> sold out
   1 Oct 19:00  total 65, sold  0, remain 0, held 65  -> cancelled
  24 Sep 19:00  total 65, sold  6, remain 0, held 59  -> cancelled
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
HORIZON_DAYS = 365             # cover the whole published schedule
REQUEST_PAUSE = 0.4
TIMEOUT = 20

# A show with remain=0 is treated as CANCELLED (not sold out) when at least
# this fraction of the house sits on hold.
CANCELLED_HOLD_FRACTION = 0.5

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
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
})


def get_json(url):
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
# Step 1: every future show
# ----------------------------------------------------------------------------
def list_future_games():
    url = f"{BASE}/rest/events/${EVENT_HASH}?_branch=findByDomainNameOrHashId&_s=1"
    data = get_json(url)["data"]
    games = data.get("games", [])

    today = datetime.now(VENUE_TZ).date()
    horizon = today + timedelta(days=HORIZON_DAYS)

    out = []
    for g in games:
        beg = g.get("begDate")
        if not beg:
            continue
        d = datetime.strptime(beg, "%Y-%m-%d").date()
        if d < today or d > horizon:
            continue
        if g.get("status") != "Y":
            continue
        out.append({
            "hashGameId": g["hashId"],
            "date": beg,
            "time": g.get("begTime", "")[:5],
        })
    out.sort(key=lambda x: (x["date"], x["time"]))
    return out


# ----------------------------------------------------------------------------
# Step 2: game -> pkgId (cached)
# ----------------------------------------------------------------------------
def load_pkg_cache():
    if PKG_CACHE.exists():
        try:
            return json.loads(PKG_CACHE.read_text())
        except Exception:
            return {}
    return {}


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

    # Always read the package DEFINITION, which carries inventory even when
    # the show isn't currently offered for sale.
    pkg = data.get("targetPkg") or {}
    segs = [i for i in (pkg.get("pkgItems") or [])
            if i.get("pkgItemType") == "SEG"]
    if not segs:
        raise RuntimeError(f"pkg {pkg_hash}: no SEG entries in targetPkg.pkgItems")

    remain = total = sold = 0
    for pk in segs:
        inv = (pk.get("item") or {}).get("inv") or {}
        remain += int(inv.get("invRemain") or 0)
        total += int(inv.get("invTotal") or 0)
        sold += int(inv.get("invSold") or 0)

    # contract checks -- a shape change must fail loudly, not silently mislead
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

    return remain, total, sold, held, state


# ----------------------------------------------------------------------------
# Orchestrate
# ----------------------------------------------------------------------------
def main():
    games = list_future_games()
    if len(games) < 3:
        die(f"Only {len(games)} future shows found -- expected dozens. "
            f"Keeping the last good feed.")

    cache = load_pkg_cache()
    performances = []
    failures = []
    counts = {"on_sale": 0, "sold_out": 0, "cancelled": 0}

    for g in games:
        try:
            pkg = pkg_id_for(g["hashGameId"], cache)
            remain, total, sold, held, state = inventory_for(pkg)
            counts[state] += 1
            performances.append({
                "date": g["date"],
                "time": g["time"],
                "open": remain,
                "state": state,
            })
        except Exception as e:
            failures.append(f"{g['date']} {g['time']}: {e}")
            performances.append({
                "date": g["date"],
                "time": g["time"],
                "open": None,
                "state": "unknown",
            })

    ok = sum(1 for p in performances if p["state"] != "unknown")
    if ok < max(3, len(performances) // 2):
        die(f"Only {ok}/{len(performances)} shows classified. "
            f"Refusing to overwrite the good feed. First errors: {failures[:3]}")

    PKG_CACHE.write_text(json.dumps(cache, indent=2))

    feed = {
        "updated_at": datetime.now(VENUE_TZ).isoformat(),
        "venue_timezone": "America/Los_Angeles",
        "capacity": CAPACITY,
        "performances": performances,
    }
    write_atomic(OUT, feed)

    print(f"OK  {ok}/{len(performances)} shows classified: "
          f"{counts['on_sale']} on sale, {counts['sold_out']} sold out, "
          f"{counts['cancelled']} cancelled, {len(failures)} unknown.")
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
