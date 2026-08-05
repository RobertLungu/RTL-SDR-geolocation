#!/usr/bin/env python3
"""filter.py -- distill a raw ADS-B capture into clean ranging input for the
self-position locator.

Input is the raw per-message CSV produced by capture.py:
    host_ts, icao, msg_type, callsign, altitude, lat, lon,
    ground_speed, track, vertical_rate, squawk, rssi

Only msg_type==3 (airborne position) rows carry lat/lon. The other msg types
(1,4,5,6,7,8 -- surveillance/ident) carry no position but DO carry icao + rssi,
so they are valuable as extra ranging points once we can interpolate where the
aircraft was at that instant.

HARD RULE (self-geolocation): this script uses NO receiver lat/lon anywhere.
Every filter is reference-free -- it reasons only about aircraft relative to one
another and to physics (max ground speed, ADS-B line-of-sight range), never
relative to an assumed receiver location.

PIPELINE
--------
Stage 1  Reject bogus / malformed rows (bad field count, bad icao, out-of-range
         position/altitude, unparseable numbers). Drops counted by reason.
Stage 2  Per-aircraft position-track distillation: sort by time, drop zero-dt and
         exact-duplicate fixes, then a greedy KINEMATIC GATE that rejects any fix
         implying >1200 km/h from the last accepted fix. >=2 survivors => located.
Stage 3  Inter-aircraft phantom check: aircraft heard at overlapping times must be
         within ~800 km of each other (2x ADS-B line-of-sight, since both reach
         one receiver). Impossible pairs are flagged; the weaker track is dropped.
Stage 4  RSSI enrichment: for each located aircraft, every rssi-bearing row of ANY
         msg_type gets a position by LINEAR INTERPOLATION between bracketing fixes
         (never extrapolated beyond the fix span), emitting a (position, rssi)
         ranging sample.

OUTPUTS (in --outdir, default work/):
    tracks_pruned.csv    clean fixes for located aircraft
                         [host_ts, icao, lat, lon, altitude, rssi]
    ranging_samples.csv  locator main input -- interpolated position for every
                         rssi-bearing frame of located aircraft
                         [host_ts, icao, lat, lon, altitude, rssi]

Stdlib only. Stays entirely inside adsb/. No GSM dependencies.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from collections import defaultdict

# --- Tunable physical limits (reference-free) ------------------------------

MAX_GROUND_SPEED_KMH = 1200.0   # kinematic gate; well above any airliner
MAX_PAIR_SEPARATION_KM = 800.0  # ~2x ADS-B line-of-sight at altitude
ALT_MIN_FT = -1500.0            # sane altitude band (below sea-level airports ok)
ALT_MAX_FT = 60000.0

OUT_COLUMNS = ["host_ts", "icao", "lat", "lon", "altitude", "rssi"]

POSITION_MSG_TYPE = "3"


# --- Geometry --------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometres."""
    r = 6371.0088
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _is_hex6(s: str) -> bool:
    if len(s) != 6:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


# --- Records ---------------------------------------------------------------

class Record:
    """A single accepted raw row, normalized."""
    __slots__ = ("ts", "icao", "msg_type", "alt", "lat", "lon", "rssi", "is_fix")

    def __init__(self, ts, icao, msg_type, alt, lat, lon, rssi, is_fix):
        self.ts = ts
        self.icao = icao
        self.msg_type = msg_type
        self.alt = alt          # float or None
        self.lat = lat          # float or None (only set when is_fix)
        self.lon = lon
        self.rssi = rssi        # float or None
        self.is_fix = is_fix    # True if a valid msg_type==3 position fix


# --- Stage 1: parse & sanitize --------------------------------------------

def parse_float(s: str):
    s = s.strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def stage1_load(in_path: str):
    """Read the raw CSV, returning (records, drop_counts, rows_in)."""
    drops = defaultdict(int)
    records: list[Record] = []
    rows_in = 0

    with open(in_path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return records, drops, rows_in
        ncols = len(header)

        for raw in reader:
            rows_in += 1
            if len(raw) != ncols:
                drops["malformed_field_count"] += 1
                continue

            row = dict(zip(header, raw))

            icao = row.get("icao", "").strip().upper()
            if not _is_hex6(icao):
                drops["bad_icao"] += 1
                continue

            ts = parse_float(row.get("host_ts", ""))
            if ts is None:
                drops["bad_host_ts"] += 1
                continue

            rssi = parse_float(row.get("rssi", ""))
            alt = parse_float(row.get("altitude", ""))
            msg_type = row.get("msg_type", "").strip()

            lat_raw = row.get("lat", "").strip()
            lon_raw = row.get("lon", "").strip()
            has_pos = lat_raw != "" and lon_raw != ""

            is_fix = False
            lat = lon = None

            if msg_type == POSITION_MSG_TYPE and has_pos:
                lat = parse_float(lat_raw)
                lon = parse_float(lon_raw)
                if lat is None or lon is None:
                    drops["pos_unparseable"] += 1
                    # keep row as a non-fix rssi frame below
                elif not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                    drops["pos_out_of_range"] += 1
                    lat = lon = None
                elif lat == 0.0 and lon == 0.0:
                    drops["pos_zero_zero"] += 1
                    lat = lon = None
                elif alt is None:
                    drops["alt_unparseable"] += 1
                    lat = lon = None
                elif not (ALT_MIN_FT <= alt <= ALT_MAX_FT):
                    drops["alt_out_of_band"] += 1
                    lat = lon = None
                else:
                    is_fix = True

            records.append(Record(ts, icao, msg_type, alt, lat, lon, rssi, is_fix))

    return records, drops, rows_in


# --- Stage 2: per-aircraft kinematic distillation -------------------------

def stage2_distill(records):
    """Return (accepted_fixes_by_icao, kinematic_rejects, dup_drops)."""
    fixes_by_icao = defaultdict(list)
    for rec in records:
        if rec.is_fix:
            fixes_by_icao[rec.icao].append(rec)

    accepted = {}
    kinematic_rejects = 0
    dup_drops = 0

    for icao, fixes in fixes_by_icao.items():
        fixes.sort(key=lambda r: r.ts)
        kept: list[Record] = []
        last = None
        for f in fixes:
            if last is not None:
                dt = f.ts - last.ts
                if dt <= 0:
                    dup_drops += 1
                    continue
                if f.lat == last.lat and f.lon == last.lon:
                    dup_drops += 1
                    continue
                dist = haversine_km(last.lat, last.lon, f.lat, f.lon)
                speed = dist / (dt / 3600.0)  # km/h
                if speed > MAX_GROUND_SPEED_KMH:
                    kinematic_rejects += 1
                    continue
            kept.append(f)
            last = f
        accepted[icao] = kept

    return accepted, kinematic_rejects, dup_drops


# --- Stage 3: inter-aircraft phantom check --------------------------------

def interp_position(fixes, ts):
    """Linearly interpolate (lat, lon, alt) at ts from sorted `fixes`.

    Returns None if ts is outside the fix span (no extrapolation).
    """
    if not fixes or ts < fixes[0].ts or ts > fixes[-1].ts:
        return None
    # Locate the bracketing pair. Linear scan is fine for these track sizes.
    for i in range(len(fixes) - 1):
        a, b = fixes[i], fixes[i + 1]
        if a.ts <= ts <= b.ts:
            span = b.ts - a.ts
            if span <= 0:
                return (a.lat, a.lon, a.alt)
            f = (ts - a.ts) / span
            lat = a.lat + f * (b.lat - a.lat)
            lon = a.lon + f * (b.lon - a.lon)
            if a.alt is not None and b.alt is not None:
                alt = a.alt + f * (b.alt - a.alt)
            else:
                alt = a.alt if a.alt is not None else b.alt
            return (lat, lon, alt)
    last = fixes[-1]
    return (last.lat, last.lon, last.alt)


def stage3_phantom_check(located):
    """Drop the weaker track of any aircraft pair too far apart while both heard.

    Returns (kept_located, flagged_pairs). `located` maps icao -> list[fix].
    """
    icaos = list(located.keys())
    flagged = []
    dropped = set()

    for i in range(len(icaos)):
        for j in range(i + 1, len(icaos)):
            a, b = icaos[i], icaos[j]
            if a in dropped or b in dropped:
                continue
            fa, fb = located[a], located[b]
            # Overlap window of the two tracks' time spans.
            lo = max(fa[0].ts, fb[0].ts)
            hi = min(fa[-1].ts, fb[-1].ts)
            if lo > hi:
                continue  # never heard simultaneously -> no constraint
            max_sep = 0.0
            for f in fa:
                if lo <= f.ts <= hi:
                    pb = interp_position(fb, f.ts)
                    if pb is None:
                        continue
                    sep = haversine_km(f.lat, f.lon, pb[0], pb[1])
                    if sep > max_sep:
                        max_sep = sep
            if max_sep > MAX_PAIR_SEPARATION_KM:
                # Trust the track with more fixes; drop the weaker as a phantom.
                weaker = a if len(fa) <= len(fb) else b
                flagged.append((a, b, max_sep, weaker))
                dropped.add(weaker)

    kept = {k: v for k, v in located.items() if k not in dropped}
    return kept, flagged


# --- Stage 4: RSSI enrichment ---------------------------------------------

def stage4_ranging(records, located):
    """Produce interpolated (position, rssi) samples for located aircraft.

    For every rssi-bearing record of a located aircraft whose timestamp falls
    within that aircraft's fix span, interpolate position and emit a sample.
    """
    samples = []
    recs_by_icao = defaultdict(list)
    for rec in records:
        if rec.icao in located and rec.rssi is not None:
            recs_by_icao[rec.icao].append(rec)

    for icao, fixes in located.items():
        if len(fixes) < 2:
            continue
        recs = sorted(recs_by_icao.get(icao, []), key=lambda r: r.ts)
        for rec in recs:
            pos = interp_position(fixes, rec.ts)
            if pos is None:
                continue  # outside fix span -> no extrapolation
            lat, lon, alt = pos
            samples.append({
                "host_ts": f"{rec.ts:.3f}",
                "icao": icao,
                "lat": f"{lat:.6f}",
                "lon": f"{lon:.6f}",
                "altitude": "" if alt is None else f"{alt:.1f}",
                "rssi": f"{rec.rssi:.1f}",
            })
    samples.sort(key=lambda s: (s["icao"], s["host_ts"]))
    return samples


# --- Output ----------------------------------------------------------------

def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def fix_to_outrow(rec: Record):
    return {
        "host_ts": f"{rec.ts:.3f}",
        "icao": rec.icao,
        "lat": f"{rec.lat:.6f}",
        "lon": f"{rec.lon:.6f}",
        "altitude": "" if rec.alt is None else f"{rec.alt:.1f}",
        "rssi": "" if rec.rssi is None else f"{rec.rssi:.1f}",
    }


# --- Main ------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Distill a raw ADS-B capture into clean ranging samples "
                    "(reference-free; no receiver lat/lon used).")
    p.add_argument("--in", dest="in_path", default=os.path.join("work", "csv_raw.csv"),
                   help="Raw capture CSV; relative paths resolve against the script "
                        "directory (default: work/csv_raw.csv).")
    p.add_argument("--outdir", default="work",
                   help="Output directory; relative paths resolve against the script "
                        "directory (default: work).")
    return p.parse_args(argv)


def resolve(path, script_dir):
    return path if os.path.isabs(path) else os.path.join(script_dir, path)


def main(argv):
    args = parse_args(argv)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    in_path = resolve(args.in_path, script_dir)
    outdir = resolve(args.outdir, script_dir)

    if not os.path.isfile(in_path):
        print(f"ERROR: input CSV not found: {in_path}", file=sys.stderr)
        return 1
    os.makedirs(outdir, exist_ok=True)

    # Stage 1
    records, drops, rows_in = stage1_load(in_path)

    # Stage 2
    accepted, kinematic_rejects, dup_drops = stage2_distill(records)
    located = {i: f for i, f in accepted.items() if len(f) >= 2}
    unlocated = {i: f for i, f in accepted.items() if len(f) < 2}
    # Aircraft that appear in the data but never produced any valid fix at all.
    all_icaos = {r.icao for r in records}
    no_fix = sorted(all_icaos - set(accepted.keys()))

    # Stage 3
    located, flagged_pairs = stage3_phantom_check(located)
    # Any aircraft dropped as a phantom becomes unlocated.
    for a, b, sep, weaker in flagged_pairs:
        if weaker in accepted:
            unlocated[weaker] = accepted[weaker]

    # Stage 4
    samples = stage4_ranging(records, located)

    # Outputs
    tracks_path = os.path.join(outdir, "tracks_pruned.csv")
    ranging_path = os.path.join(outdir, "ranging_samples.csv")

    track_rows = []
    for icao in sorted(located.keys()):
        for rec in located[icao]:
            track_rows.append(fix_to_outrow(rec))
    write_csv(tracks_path, track_rows)
    write_csv(ranging_path, samples)

    # --- Summary -----------------------------------------------------------
    total_drops = sum(drops.values())
    print("\n=============== filter summary ===============")
    print(f"input CSV               : {in_path}")
    print(f"data rows in            : {rows_in}")
    print(f"bogus rows dropped      : {total_drops}")
    if drops:
        for reason in sorted(drops):
            print(f"    {reason:<22}: {drops[reason]}")
    print(f"duplicate-fix drops     : {dup_drops}")
    print(f"kinematic-gate rejects  : {kinematic_rejects}")

    print(f"\nlocated aircraft (>=2 fixes): {len(located)}")
    for icao in sorted(located.keys()):
        print(f"    {icao}: {len(located[icao])} fixes")
    n_unloc = len(unlocated) + len(no_fix)
    print(f"unlocated aircraft          : {n_unloc}")
    for icao in sorted(unlocated.keys()):
        print(f"    {icao}: {len(unlocated[icao])} fix(es)")
    for icao in no_fix:
        print(f"    {icao}: 0 fixes")

    if flagged_pairs:
        print("\nphantom pairs flagged (>800 km while co-heard):")
        for a, b, sep, weaker in flagged_pairs:
            print(f"    {a} <-> {b}: {sep:.0f} km  (dropped {weaker})")
    else:
        print("\nphantom pairs flagged        : 0")

    print(f"\nranging samples produced    : {len(samples)}")
    print(f"  tracks_pruned.csv          : {tracks_path} ({len(track_rows)} rows)")
    print(f"  ranging_samples.csv        : {ranging_path} ({len(samples)} rows)")
    print("==============================================")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
