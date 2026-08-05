#!/usr/bin/env python3
"""locate.py -- estimate the RECEIVER's own position from ADS-B ranging samples.

Input is work/ranging_samples.csv (from filter.py):
    host_ts, icao, lat, lon, altitude, rssi
where altitude is in FEET and rssi is in dBFS.

This is SELF-geolocation: we recover where the receiver is from how aircraft of
known position were heard. The receiver's own coordinates are NEVER a solver
input or initial guess. The GSM truth point is used ONLY to score the result at
the very end (great-circle error), never to seed the optimizer.

PRIMARY -- log-distance path-loss least squares
    rssi_i = P0 - 10*n*log10(d_i)
where d_i is the 3D distance (m) from the unknown receiver (rx_lat, rx_lon,
rx_alt) to aircraft i at (lat_i, lon_i, alt_i). Aircraft sit ~10.7 km up, so the
vertical leg dominates geometry and the 3D distance is essential.

    For any fixed candidate receiver, the model is LINEAR in (P0, n): regress
    rssi on log10(d) in closed form to get (P0, n) and the residual SSE. So the
    outer optimizer only searches the 2D receiver ground position. We do a
    coarse-to-fine grid search over the aircraft bbox padded by +-100 km (no
    truth seed), then polish with scipy.optimize.least_squares.

Output: an adsb_position.geojson with features keyed like the GSM position.geojson
(properties.estimate / .title, geometry [lon, lat]).

numpy + scipy. Stays inside adsb/; no GSM file edits.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

import numpy as np
from scipy.optimize import least_squares

# --- Constants -------------------------------------------------------------

FT_TO_M = 0.3048
RX_ALT_M = 100.0  # assumed receiver antenna altitude (terrain ~Bucharest plain)
EARTH_R = 6371008.8  # mean Earth radius, metres

# Distance floor (m) applied before log10(d). This is the anti-collapse fix:
# without it, an overhead/low aircraft makes d -> 0, log10(d) -> -inf, and the
# free path-loss fit is rewarded for parking the receiver ON the strongest-signal
# track point. Clamping d to a floor removes that singularity so the estimate is
# driven by genuine geometry, not by sitting on the planes.
#   Empirically swept on the 70k-sample / 27-aircraft 20-min capture: floors
#   <=3 km give the tightest fix (1.98 km vs truth); 5 km over-regularises
#   (2.07 km) and >=8 km blows up (4-5 km). 1.5 km keeps the anti-collapse guard
#   while staying in the flat-optimum region. CLI-tunable.
DIST_FLOOR_M = 1500.0

# GSM truth point -- SCORING ONLY, never an input to the solver.
GSM_TRUTH_LAT = 44.4467
GSM_TRUTH_LON = 26.0400


# --- Geometry helpers ------------------------------------------------------

def equirect_xy(lat, lon, lat0, lon0):
    """Local equirectangular (ENU-ish) projection to metres about (lat0, lon0).

    x = east, y = north. Adequate over the ~100s-of-km scale here.
    """
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    x = np.radians(lon - lon0) * np.cos(np.radians(lat0)) * EARTH_R
    y = np.radians(lat - lat0) * EARTH_R
    return x, y


def xy_to_latlon(x, y, lat0, lon0):
    lat = lat0 + np.degrees(y / EARTH_R)
    lon = lon0 + np.degrees(x / (EARTH_R * np.cos(np.radians(lat0))))
    return lat, lon


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlam = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlam / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(a))


def geometry_coverage(rx_x, rx_y, ax, ay):
    """Bearing coverage of the aircraft as seen from the estimated receiver.

    Returns (occupied_octants, max_gap_deg, counts[8], balance). One-sided
    geometry does NOT inflate the reported 1-sigma accuracy, yet it biases the
    fix toward the data mass by km. We surface it so a confident-looking number
    is not mistaken for an unbiased one.

    balance = share of samples in the emptiest semicircle (0..0.5). Occupancy
    alone is not enough: a capture with 97% of fixes to the N/NE still "occupies"
    all 8 octants via a handful of stragglers and slides the fix multi-km along
    the traffic mass (observed 2026-07-03: two 8-km misses passed the old
    octant/gap test). Density must be two-sided, not just presence.
    """
    ang = (np.degrees(np.arctan2(ay - rx_y, ax - rx_x)) + 360.0) % 360.0
    counts = np.histogram(ang, bins=8, range=(0, 360))[0]
    occupied = int(np.sum(counts > 0))
    occ_deg = np.sort(ang)
    if len(occ_deg) < 2:
        max_gap = 360.0
    else:
        gaps = np.diff(occ_deg)
        wrap = (occ_deg[0] + 360.0) - occ_deg[-1]
        max_gap = float(max(gaps.max(), wrap))
    # Emptiest-semicircle share via a sliding 180-deg window over sorted angles:
    # max samples in any semicircle -> the opposite semicircle is the emptiest.
    n = len(occ_deg)
    ext = np.concatenate([occ_deg, occ_deg + 360.0])
    max_in_half = int(np.max(np.searchsorted(ext, occ_deg + 180.0, side="right")
                             - np.arange(n)))
    balance = 1.0 - max_in_half / n
    return occupied, max_gap, counts, balance


# --- Data loading ----------------------------------------------------------

def load_samples(path):
    """Return arrays ts, icao, lat, lon, alt_m, rssi from the ranging CSV."""
    ts, icao, lat, lon, alt_m, rssi = [], [], [], [], [], []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                la = float(row["lat"]); lo = float(row["lon"])
                rs = float(row["rssi"])
                al = float(row["altitude"]) if row["altitude"].strip() else np.nan
            except (ValueError, KeyError):
                continue
            ts.append(float(row.get("host_ts", "nan") or "nan"))
            icao.append(row["icao"].strip().upper())
            lat.append(la); lon.append(lo)
            alt_m.append(al * FT_TO_M)
            rssi.append(rs)
    return (np.array(ts), np.array(icao), np.array(lat), np.array(lon),
            np.array(alt_m), np.array(rssi))


# --- Primary: path-loss least squares -------------------------------------

def fit_p0_n(log_d, rssi):
    """Closed-form linear LS of rssi = P0 - 10*n*log10(d).

    Regress rssi on u = log10(d): rssi = a + b*u, with a=P0, b=-10n.
    Returns (P0, n, sse, residuals).
    """
    u = log_d
    A = np.column_stack([np.ones_like(u), u])
    coef, _, _, _ = np.linalg.lstsq(A, rssi, rcond=None)
    a, b = coef
    pred = A @ coef
    resid = rssi - pred
    sse = float(resid @ resid)
    P0 = a
    n = -b / 10.0
    return P0, n, sse, resid


def sse_at_receiver(rx_x, rx_y, ax, ay, az, rssi, dist_floor=DIST_FLOOR_M):
    """SSE of the best-fit path-loss model for a receiver at (rx_x, rx_y).

    az is aircraft altitude (m); receiver altitude is RX_ALT_M. 3D distance.
    d is clamped to dist_floor (anti-collapse; see DIST_FLOOR_M).
    """
    dx = ax - rx_x
    dy = ay - rx_y
    dz = az - RX_ALT_M
    d = np.sqrt(dx * dx + dy * dy + dz * dz)
    d = np.maximum(d, dist_floor)
    log_d = np.log10(d)
    _, _, sse, _ = fit_p0_n(log_d, rssi)
    return sse


def grid_search(ax, ay, az, rssi, pad_m=100_000.0, dist_floor=DIST_FLOOR_M):
    """Coarse-to-fine grid search for the SSE-minimizing receiver ground point.

    Box = aircraft xy bbox padded by +-pad_m. No truth seed.
    """
    x_min, x_max = ax.min() - pad_m, ax.max() + pad_m
    y_min, y_max = ay.min() - pad_m, ay.max() + pad_m

    best = None
    # Three refinement passes, each zooming into the prior best cell.
    for n_grid, shrink in ((41, 0.25), (31, 0.10), (31, None)):
        xs = np.linspace(x_min, x_max, n_grid)
        ys = np.linspace(y_min, y_max, n_grid)
        for gx in xs:
            for gy in ys:
                sse = sse_at_receiver(gx, gy, ax, ay, az, rssi, dist_floor)
                if best is None or sse < best[2]:
                    best = (gx, gy, sse)
        if shrink is not None:
            half_x = (x_max - x_min) * shrink
            half_y = (y_max - y_min) * shrink
            x_min, x_max = best[0] - half_x, best[0] + half_x
            y_min, y_max = best[1] - half_y, best[1] + half_y
    return best[0], best[1], best[2]


def polish_least_squares(x0, y0, ax, ay, az, rssi, dist_floor=DIST_FLOOR_M):
    """Refine the receiver xy with least_squares over per-sample residuals.

    For each trial receiver, (P0, n) are re-fit in closed form, then the residual
    vector rssi - model is returned so least_squares can use its Jacobian/cov.
    d is clamped to dist_floor (anti-collapse; see DIST_FLOOR_M).
    """
    def residuals(p):
        rx_x, rx_y = p
        dx = ax - rx_x; dy = ay - rx_y; dz = az - RX_ALT_M
        d = np.maximum(np.sqrt(dx * dx + dy * dy + dz * dz), dist_floor)
        log_d = np.log10(d)
        P0, n, _, resid = fit_p0_n(log_d, rssi)
        return resid

    # Robust loss: soft_l1 down-weights multipath/outlier RSSIs instead of letting
    # them tug the fit (measured 2.90 -> 2.07 km vs plain L2 on the 27-aircraft
    # set). Needs the 'trf' method; 'lm' does not support a robust loss.
    res = least_squares(residuals, x0=[x0, y0], method="trf", loss="soft_l1")
    rx_x, rx_y = res.x

    # 1-sigma position uncertainty from the Jacobian covariance, scaled by the
    # residual variance: cov = sigma^2 * (J^T J)^-1.
    m, k = res.jac.shape
    dof = max(m - k - 2, 1)  # -2 for the implicitly-fit P0,n
    sigma2 = 2.0 * res.cost / dof
    try:
        JTJ_inv = np.linalg.inv(res.jac.T @ res.jac)
        cov = sigma2 * JTJ_inv
        # 1-sigma radius ~ sqrt of larger eigenvalue of the xy covariance.
        eig = np.linalg.eigvalsh(cov)
        accuracy_m = float(np.sqrt(max(eig[-1], 0.0)))
    except np.linalg.LinAlgError:
        accuracy_m = float("nan")

    # Final fit stats at the solution.
    dx = ax - rx_x; dy = ay - rx_y; dz = az - RX_ALT_M
    d = np.maximum(np.sqrt(dx * dx + dy * dy + dz * dz), dist_floor)
    P0, n, sse, resid = fit_p0_n(np.log10(d), rssi)
    rms_db = float(np.sqrt(np.mean(resid ** 2)))
    return rx_x, rx_y, P0, n, rms_db, accuracy_m


# --- Main ------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Self-geolocate the ADS-B receiver from ranging samples "
                    "(no receiver position is ever used as input).")
    p.add_argument("--in", dest="in_path",
                   default=os.path.join("work", "ranging_samples.csv"),
                   help="Ranging CSV (default: work/ranging_samples.csv).")
    p.add_argument("--out",
                   default=os.path.join("work", "adsb_position.geojson"),
                   help="Output GeoJSON (default: work/adsb_position.geojson).")
    p.add_argument("--dist-floor", type=float, default=DIST_FLOOR_M,
                   help="Distance floor in metres applied before log10(d), the "
                        "anti-collapse fix (default: %(default)g). Raise if the "
                        "estimate still snaps onto low/overhead tracks.")
    return p.parse_args(argv)


def resolve(path, script_dir):
    return path if os.path.isabs(path) else os.path.join(script_dir, path)


def feature(lon, lat, estimate, title, extra=None):
    props = {"estimate": estimate, "title": title}
    if extra:
        props.update(extra)
    return {
        "type": "Feature",
        "properties": props,
        "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
    }


def main(argv):
    args = parse_args(argv)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    in_path = resolve(args.in_path, script_dir)
    out_path = resolve(args.out, script_dir)

    if not os.path.isfile(in_path):
        print(f"ERROR: input CSV not found: {in_path}", file=sys.stderr)
        return 1

    ts, icao, lat, lon, alt_m, rssi = load_samples(in_path)
    if len(rssi) < 10:
        print(f"ERROR: too few ranging samples ({len(rssi)}).", file=sys.stderr)
        return 1

    # Projection origin = centroid of aircraft positions (NOT the receiver).
    lat0 = float(np.mean(lat))
    lon0 = float(np.mean(lon))
    ax, ay = equirect_xy(lat, lon, lat0, lon0)
    az = alt_m

    # --- Primary: path-loss least squares ---
    gx, gy, _ = grid_search(ax, ay, az, rssi, dist_floor=args.dist_floor)
    rx_x, rx_y, P0, n, rms_db, accuracy_m = polish_least_squares(
        gx, gy, ax, ay, az, rssi, dist_floor=args.dist_floor)
    pl_lat, pl_lon = xy_to_latlon(rx_x, rx_y, lat0, lon0)
    pl_lat = float(pl_lat); pl_lon = float(pl_lon)
    pl_err = float(haversine_m(pl_lat, pl_lon, GSM_TRUTH_LAT, GSM_TRUTH_LON))

    # Geometry-coverage check: a wide empty bearing arc biases the fix toward the
    # data mass by km even when the reported 1-sigma looks tight. Flag it.
    occ, max_gap, oct_counts, balance = geometry_coverage(rx_x, rx_y, ax, ay)
    # Balance threshold 0.15: the emptiest half-sky must hold >=15% of samples.
    # Calibrated on real captures: the 1.74 km fix had ~0.3+, the two 8-km
    # one-sided misses of 2026-07-03 had 0.01-0.1 yet passed the old test.
    geom_ok = occ >= 6 and max_gap <= 150.0 and balance >= 0.15

    # --- GeoJSON output ---
    features = [
        feature(pl_lon, pl_lat, "rssi_pathloss", "RSSI path-loss estimate", {
            "accuracy_m": round(accuracy_m, 1) if np.isfinite(accuracy_m) else None,
            "P0_dbfs": round(float(P0), 2),
            "path_loss_exponent_n": round(float(n), 3),
            "rms_residual_db": round(rms_db, 3),
            "error_vs_truth_m": round(pl_err, 1),
            "geom_octants_occupied": occ,
            "geom_max_gap_deg": round(max_gap, 1),
            "geom_balance": round(balance, 3),
            "geom_ok": geom_ok,
        }),
    ]
    features.append(feature(GSM_TRUTH_LON, GSM_TRUTH_LAT, "gsm_truth",
                            "GSM truth (reference)"))

    geojson = {"type": "FeatureCollection", "features": features}
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(geojson, fh, indent=2)

    # --- Summary ---
    print("\n=============== self-geolocation summary ===============")
    print(f"input                 : {in_path}")
    print(f"ranging samples       : {len(rssi)} from {len(set(icao))} aircraft")
    print(f"projection origin     : lat0={lat0:.5f} lon0={lon0:.5f} (aircraft centroid)")
    print(f"distance floor        : {args.dist_floor:.0f} m (anti-collapse)")
    print("\n--- PRIMARY: RSSI path-loss least squares ---")
    print(f"estimated position    : lat={pl_lat:.5f}  lon={pl_lon:.5f}")
    print(f"fitted P0             : {P0:.2f} dBFS")
    print(f"path-loss exponent n  : {n:.3f}")
    print(f"RMS residual          : {rms_db:.3f} dB")
    acc_str = f"{accuracy_m:.0f} m" if np.isfinite(accuracy_m) else "n/a"
    print(f"1-sigma accuracy      : {acc_str}")
    print(f"ERROR vs GSM truth    : {pl_err:.0f} m  ({pl_err/1000.0:.2f} km)")

    labels = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]
    cov = "  ".join(f"{l}:{c}" for l, c in zip(labels, oct_counts))
    print(f"\ngeometry coverage     : {occ}/8 octants, widest gap {max_gap:.0f} deg, "
          f"balance {balance:.2f} (emptiest half-sky share; >=0.15 needed)")
    print(f"  {cov}")
    if not geom_ok:
        print("  WARNING: one-sided geometry. The 1-sigma above understates the")
        print("  true error -- when the traffic mass sits on one side of the sky")
        print("  the fix slides multi-km along it. Capture at a time with traffic")
        print("  in the empty half (S/SW corridors) before trusting this estimate.")

    print("\nHONEST NOTE: RSSI path-loss accuracy scales with the data — more")
    print("aircraft at diverse bearings/altitudes tightens the fit; sparse or")
    print("co-altitude traffic leaves it weakly constrained (multi-km error).")
    print(f"\noutput geojson        : {out_path}")
    print("========================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
