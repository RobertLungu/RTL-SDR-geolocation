#!/usr/bin/env python3
"""
estimate_position.py — estimate the receiver's position from scanned cells.

Reads cells_located.csv and estimates where the receiver is, using the
strongest N towers and their RSSI.

Method (per RF analysis for this uncalibrated / crowdsourced-tower dataset):
  PRIMARY   — Weighted Centroid (WCL). Robust, no path-loss calibration needed.
              weight w_i = 10**(RSSI_i / k);  k=20 (amplitude, more geometric
              spread) or k=10 (power, collapses toward nearest tower).
  SECONDARY — Log-distance trilateration. UNCALIBRATED / indicative only:
              the per-operator Tx-power differences make absolute ranges
              unreliable. Shown for comparison, not as a better answer.

Tower selection (default): the 8 cells physically nearest an initial estimate.
We seed a weighted centroid from the strongest cells, keep the 8 nearest it,
recompute, and iterate until the set settles — so far-away towers that drag the
centroid outward are dropped. Use --select rssi for the legacy "8 strongest".

IMPORTANT CAVEAT: WCL is biased toward the centroid of tower *positions*, not
your true position. With all towers in a small patch, the estimate says "which
part of the cluster you're nearest", not absolute location. Expected error
~500 m – 1.5 km. This demonstrates the technique; it does not replace GPS.

Usage:
  ./estimate_position.py                       # ../cells_located.csv
  ./estimate_position.py in.csv --n 6 --weight amplitude
"""

import argparse
import csv
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

# --- Log-distance path-loss model (GSM-900 downlink, ~947 MHz) ---
# The trilateration anchor P0 (received power at the reference distance d0) is
# DERIVED from the tower transmit power and a free-space reference at d0, rather
# than hand-guessed:  P0 = EIRP - FSPL(d0) + G_rx - L_cable + cal_offset.
PL_N        = 3.5      # path-loss exponent (urban macro; 3.0-4.0)
PL_D0       = 100.0    # reference distance (m) — free-space formula valid here
PL_F_MHZ    = 947.0    # centre frequency (MHz)
PL_TX_DBM   = 45.0     # tower EIRP (dBm) — typical GSM900 macro (43-47)
PL_G_RX_DBI = 0.0      # RX antenna gain (dBi); 0=stock whip, ~2 tuned dipole
PL_L_RX_DB  = 2.0      # cable + connector loss (dB)
PL_RSSI_CAL = 0.0      # calibration offset (dB); set via --rssi-cal

M_PER_DEG_LAT = 111320.0

# Residual consistency check: towers whose (de-meaned) RSSI residual vs the
# log-distance model exceeds max(RESID_SIGMA*std, RESID_FLOOR_DB) are dropped
# and the fix is re-solved once. The floor keeps ordinary 5-8 dB shadowing /
# per-operator Tx differences from being treated as outliers.
RESID_SIGMA    = 2.0
RESID_FLOOR_DB = 8.0


def fspl_db(dist_m, freq_mhz):
    """Free-space path loss (dB). Valid above ~10 m."""
    return 20 * math.log10(dist_m) + 20 * math.log10(freq_mhz * 1e6) - 147.55


def compute_p0(tx_dbm=PL_TX_DBM, d0=PL_D0, f_mhz=PL_F_MHZ,
               g_rx=PL_G_RX_DBI, l_rx=PL_L_RX_DB, cal=PL_RSSI_CAL):
    """Received-power anchor at d0 (dB) = EIRP - FSPL(d0) + G_rx - L_cable + cal.
    With the defaults: 45 - 72.0 + 0 - 2 + 0 = -29.0."""
    return tx_dbm - fspl_db(d0, f_mhz) + g_rx - l_rx + cal


PL_P0 = compute_p0()   # -29.0 dBm with the defaults above (was a guessed -35)


def load(infile):
    """Load all located cells, de-duplicated per physical cell.

    A cell can appear many times across periodic scans; we keep the single
    strongest (highest RSSI) observation per (mcc,mnc,lac,cid) so one tower
    occupies one slot instead of crowding the selection with duplicates."""
    best = {}
    with open(infile) as f:
        for r in csv.DictReader(f):
            if not (r.get("lat") and r.get("lon") and r.get("rssi")):
                continue
            key = (r.get("mcc"), r["mnc"], r.get("lac"), r["cid"])
            rec = {
                "lat": float(r["lat"]), "lon": float(r["lon"]),
                "rssi": float(r["rssi"]), "cid": r["cid"], "mnc": r["mnc"],
                "lac": r.get("lac", ""),
            }
            if key not in best or rec["rssi"] > best[key]["rssi"]:
                best[key] = rec
    return list(best.values())


def select_towers(towers, n, mode, k):
    """Pick the n towers to position from.

    mode="rssi"     -> n strongest by signal (legacy behaviour).
    mode="closest"  -> n physically nearest the receiver: seed a weighted
                       centroid from the strongest towers, keep the n nearest
                       that seed, recompute, and iterate until the set settles.
                       This drops far-away towers that drag the centroid out."""
    by_rssi = sorted(towers, key=lambda t: t["rssi"], reverse=True)
    if mode == "rssi" or len(towers) <= n:
        return by_rssi[:n]

    # seed from the strongest min(n, available) so the first centroid is sane
    chosen = by_rssi[:n]
    for _ in range(5):
        seed = weighted_centroid(chosen, k)
        ranked = sorted(towers, key=lambda t: equirect_dist_m(
            seed[0], seed[1], t["lat"], t["lon"]))
        nxt = ranked[:n]
        if {id(t) for t in nxt} == {id(t) for t in chosen}:
            break
        chosen = nxt
    return chosen


def weighted_centroid(towers, k):
    """k=20 amplitude weighting, k=10 power weighting."""
    w = np.array([10.0 ** (t["rssi"] / k) for t in towers])
    lat = np.array([t["lat"] for t in towers])
    lon = np.array([t["lon"] for t in towers])
    return float(np.sum(w * lat) / w.sum()), float(np.sum(w * lon) / w.sum())


def accuracy_radius(towers, center, k):
    """RSSI-weighted RMS spread of the towers about the estimate (metres).

    A defensible 1-sigma-style uncertainty: how concentrated the (weighted)
    tower geometry is around the centroid. Tighter, stronger clusters -> smaller
    circle. Not a guaranteed bound, but it scales with the real geometry instead
    of being a hard-coded guess."""
    w = np.array([10.0 ** (t["rssi"] / k) for t in towers])
    d = np.array([equirect_dist_m(center[0], center[1], t["lat"], t["lon"])
                  for t in towers])
    return float(math.sqrt(np.sum(w * d * d) / w.sum()))


def rssi_residuals(towers, center):
    """Per-tower RSSI residual vs the log-distance model at `center` (dB).

    predicted_i = P0 - 10*n*log10(d_i/d0); residual_i = measured - predicted.
    RSSI here is uncalibrated, so the mean residual is a common offset
    (calibration + Tx-power bias) shared by every tower — we subtract it and
    return de-meaned residuals, which measure *relative* distance-vs-strength
    consistency. A strongly negative residual = "too weak for how close it is"
    (or its crowdsourced coordinates are wrong)."""
    d = np.array([max(equirect_dist_m(center[0], center[1], t["lat"], t["lon"]),
                      PL_D0) for t in towers])
    pred = PL_P0 - 10.0 * PL_N * np.log10(d / PL_D0)
    resid = np.array([t["rssi"] for t in towers]) - pred
    return resid - resid.mean()


def equirect_dist_m(lat1, lon1, lat2, lon2):
    x = (lon2 - lon1) * M_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2))
    y = (lat2 - lat1) * M_PER_DEG_LAT
    return math.hypot(x, y)


def trilaterate(towers, seed, k):
    """Log-distance ranges + weighted least squares around `seed` (lat, lon).
    Uses scipy if available, else a coarse->fine numpy grid search."""
    tl = np.array([[t["lat"], t["lon"]] for t in towers])
    rssi = np.array([t["rssi"] for t in towers])
    dists = PL_D0 * 10.0 ** ((PL_P0 - rssi) / (10.0 * PL_N))   # estimated metres
    w = 10.0 ** (rssi / k)
    lat0 = seed[0]
    coslat = math.cos(math.radians(lat0))

    def cost(p):
        dlat, dlon = p
        dy = (tl[:, 0] - dlat) * M_PER_DEG_LAT
        dx = (tl[:, 1] - dlon) * M_PER_DEG_LAT * coslat
        est = np.sqrt(dx * dx + dy * dy)
        return float(np.sum(w * (est - dists) ** 2))

    try:
        from scipy.optimize import minimize
        res = minimize(cost, x0=np.array(seed), method="Nelder-Mead",
                       options={"xatol": 1e-7, "fatol": 1e-3})
        return float(res.x[0]), float(res.x[1])
    except ImportError:
        # dependency-free fallback: shrinking grid search around the seed
        best = seed
        span = 0.02  # ~2 km
        for _ in range(6):
            grid = np.linspace(-span, span, 21)
            cand = min(((best[0] + dla, best[1] + dlo)
                        for dla in grid for dlo in grid), key=cost)
            best, span = cand, span / 4
        return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile", nargs="?",
                    default=os.path.join(HERE, "..", "cells_located.csv"))
    ap.add_argument("--n", type=int, default=8, help="number of towers to use")
    ap.add_argument("--select", choices=["closest", "rssi"], default="closest",
                    help="closest=n nearest the seed estimate (drops far towers); "
                         "rssi=n strongest by signal")
    ap.add_argument("--weight", choices=["amplitude", "power"], default="amplitude",
                    help="amplitude=10^(RSSI/20) spread; power=10^(RSSI/10) nearest")
    ap.add_argument("--tx-power", type=float, default=PL_TX_DBM,
                    help="tower EIRP (dBm) for the trilateration model (default 45)")
    ap.add_argument("--rssi-cal", type=float, default=0.0,
                    help="RSSI calibration offset (dB) vs one known-distance tower; "
                         "shifts trilateration ranges toward absolute scale")
    ap.add_argument("--resid-sigma", type=float, default=RESID_SIGMA,
                    help="drop towers whose RSSI residual vs the path-loss model "
                         f"exceeds this many sigmas (floor {RESID_FLOOR_DB:.0f} dB); "
                         "0 disables the check")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "position.geojson"))
    args = ap.parse_args()

    # Re-derive the path-loss anchor from the requested Tx power + calibration
    # offset before trilaterate() reads the module global.
    global PL_P0
    PL_P0 = compute_p0(tx_dbm=args.tx_power, cal=args.rssi_cal)

    k = 20.0 if args.weight == "amplitude" else 10.0
    allt = load(args.infile)
    if len(allt) < 3:
        sys.exit(f"Need >=3 located towers, found {len(allt)}")
    towers = select_towers(allt, args.n, args.select, k)

    # Residual consistency check: does each tower's signal make sense for its
    # distance from the first-pass estimate? Drop the inconsistent ones (weak
    # nearby / strong faraway -> likely bad crowdsourced coords or blocked
    # path) and solve once more without them.
    wcl = weighted_centroid(towers, k)
    resid = rssi_residuals(towers, wcl)
    resid_by_id = {id(t): r for t, r in zip(towers, resid)}
    thresh = max(args.resid_sigma * float(resid.std()), RESID_FLOOR_DB)
    dropped = ([t for t, r in zip(towers, resid) if abs(r) > thresh]
               if args.resid_sigma > 0 else [])
    dropped_ids = {id(t) for t in dropped}
    kept = [t for t in towers if id(t) not in dropped_ids]
    if dropped and len(kept) >= 3:
        towers = kept
        wcl = weighted_centroid(towers, k)
    else:
        dropped = []

    tri = trilaterate(towers, wcl, k)
    sep = equirect_dist_m(*wcl, *tri)
    acc = accuracy_radius(towers, wcl, k)

    label = "nearest" if args.select == "closest" else "strongest"
    print(f"Using {len(towers)} {label} of {len(allt)} located towers "
          f"({args.weight} weighting, k={k:.0f}); "
          f"residual check drops |r| > {thresh:.1f} dB:")
    shown = towers + dropped
    for t in sorted(shown, key=lambda x: equirect_dist_m(
            wcl[0], wcl[1], x["lat"], x["lon"])):
        d = equirect_dist_m(wcl[0], wcl[1], t["lat"], t["lon"])
        mark = ("  << DROPPED (RSSI inconsistent with distance)"
                if id(t) in dropped_ids else "")
        print(f"  CID {t['cid']:>6} (MNC {t['mnc']:>2})  "
              f"{t['rssi']:>5.0f} dBm  {d:>5.0f} m  "
              f"resid {resid_by_id[id(t)]:+5.1f} dB  "
              f"@ {t['lat']:.5f}, {t['lon']:.5f}{mark}")
    print()
    print(f"PRIMARY   Weighted centroid : {wcl[0]:.5f}, {wcl[1]:.5f}")
    print(f"          -> https://www.google.com/maps?q={wcl[0]:.6f},{wcl[1]:.6f}")
    print(f"SECONDARY Trilateration*    : {tri[0]:.5f}, {tri[1]:.5f}   "
          f"(*uncalibrated, indicative only)")
    print(f"          estimates differ by ~{sep:.0f} m")
    print(f"          accuracy radius (weighted spread): ~{acc:.0f} m")
    print()
    print("Caveat (WCL): biased toward tower-cluster centre, not true position. "
          "Realistic error ~500 m - 1.5 km.")
    print("Caveat (trilateration): RSSI is uncalibrated (relative, not true dBm), "
          "so absolute ranges may be off 3-10x until --rssi-cal is set against one "
          "tower at a known distance.")
    print(f"          model: P_tx={args.tx_power:.0f} dBm EIRP, P0={PL_P0:.1f} dB, "
          f"n={PL_N}, d0={PL_D0:.0f} m, cal={args.rssi_cal:+.1f} dB")

    # GeoJSON with both estimates for overlay on towers.geojson
    feats = [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [wcl[1], wcl[0]]},
         "properties": {"estimate": "weighted_centroid", "marker-color": "#00AA00",
                        "marker-symbol": "star", "title": "YOU (WCL)",
                        "accuracy_m": round(acc),
                        # towers that fed this fix (post residual check), so the
                        # map can highlight them
                        "used_towers": [{"mnc": t["mnc"], "lac": t["lac"],
                                         "cid": t["cid"]} for t in towers]}},
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [tri[1], tri[0]]},
         "properties": {"estimate": "trilateration_uncalibrated",
                        "marker-color": "#AAAAAA", "title": "trilateration (indicative)"}},
    ]
    import json
    with open(args.out, "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, indent=2)
    print(f"\nWrote estimate -> {args.out}")


if __name__ == "__main__":
    main()
