#!/usr/bin/env python3
"""
geolocate_cells.py — correlate scanned GSM cells with OpenCellID tower locations.

Reads cells.csv (from gsm_scan_loop.sh) and, for each unique MCC/MNC/LAC/CID,
queries OpenCellID for the estimated tower position, then writes cells_located.csv
with lat/lon/range appended.

Setup:
  1. Register a free account at https://opencellid.org/ and create an API key.
  2. export OPENCELLID_KEY="your_key_here"

Usage:
  export OPENCELLID_KEY=...           # required
  ./geolocate_cells.py                # reads ../cells.csv -> ../cells_located.csv
  ./geolocate_cells.py in.csv out.csv

Notes:
  * OpenCellID positions are crowdsourced estimates (tens–hundreds of metres),
    not exact tower coordinates. The 'range' column is the uncertainty radius (m).
  * Rows with cid=0 / mcc=0 (failed SI3 decodes) are skipped automatically.
  * Results are cached per (mcc,mnc,lac,cid) so duplicate cells across periodic
    scans cost only one API call.
"""

import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request

API = "https://opencellid.org/cell/get"
HERE = os.path.dirname(os.path.abspath(__file__))

# OpenCellID is behind Cloudflare, which blocks the default urllib User-Agent
# with HTTP 403 / error 1010. A normal browser UA is required.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def lookup(key, mcc, mnc, lac, cid, radio="GSM"):
    """Return (lat, lon, range_m) or (None, None, None) if not found."""
    params = urllib.parse.urlencode({
        "key": key, "mcc": mcc, "mnc": mnc,
        "lac": lac, "cellid": cid, "radio": radio, "format": "json",
    })
    req = urllib.request.Request(f"{API}?{params}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        # OpenCellID returns 404/JSON error when the cell is unknown
        return (None, None, None)
    except Exception as e:
        print(f"  ! query error for {mcc}-{mnc}-{lac}-{cid}: {e}", file=sys.stderr)
        return (None, None, None)
    if str(data.get("lat", "0")) in ("0", "0.0", "") or "lat" not in data:
        return (None, None, None)
    return (data.get("lat"), data.get("lon"), data.get("range"))


def main():
    key = os.environ.get("OPENCELLID_KEY")
    if not key:
        sys.exit("ERROR: set OPENCELLID_KEY (free key from https://opencellid.org/)")

    infile = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "cells.csv")
    outfile = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "..", "cells_located.csv")

    cache = {}
    found = miss = skipped = 0

    with open(infile) as f, open(outfile, "w", newline="") as out:
        reader = csv.DictReader(f)
        fields = reader.fieldnames + ["lat", "lon", "range_m"]
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()

        for row in reader:
            mcc, mnc = row["mcc"], row["mnc"]
            lac, cid = row["lac"], row["cid"]

            # skip failed decodes
            if cid in ("0", "") or mcc in ("0", ""):
                row.update(lat="", lon="", range_m="")
                writer.writerow(row)
                skipped += 1
                continue

            tup = (mcc, mnc, lac, cid)
            if tup not in cache:
                cache[tup] = lookup(key, mcc, mnc, lac, cid)
                time.sleep(0.4)  # be polite to the free API

            lat, lon, rng = cache[tup]
            row.update(lat=lat or "", lon=lon or "", range_m=rng or "")
            writer.writerow(row)
            if lat:
                found += 1
            else:
                miss += 1

    total = found + miss
    print(f"Located {found}/{total} cells "
          f"({miss} not in DB, {skipped} skipped failed-decode) -> {outfile}")


if __name__ == "__main__":
    main()
