#!/usr/bin/env python3
"""
make_map_html.py — render a self-contained ADS-B geolocation map (captures/captureN.html).

Reads adsb_position.geojson (3 position estimates) and tracks_pruned.csv
(aircraft tracks) and emits a standalone Leaflet map.

The data is embedded directly in the HTML; only internet access for the
Leaflet CDN and OSM tiles is required.

Usage:
  ./make_map_html.py                        # defaults: work/ -> captures/captureN.html
  ./make_map_html.py pos.geojson tracks.csv out.html
"""

import csv
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def next_capture_path(captures_dir):
    """Next free captures/captureN.html (N = highest existing + 1)."""
    highest = 0
    if os.path.isdir(captures_dir):
        for name in os.listdir(captures_dir):
            m = re.fullmatch(r"capture(\d+)\.html", name)
            if m:
                highest = max(highest, int(m.group(1)))
    os.makedirs(captures_dir, exist_ok=True)
    return os.path.join(captures_dir, f"capture{highest + 1}.html")

# One distinct color per ICAO, in the order they appear.
ICAO_COLORS = ["#E87722", "#0099CC", "#AA44BB", "#22AA55"]

# ── helpers ────────────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometres."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def load_positions(posfile):
    """Return dict keyed by estimate name."""
    with open(posfile) as f:
        fc = json.load(f)
    out = {}
    for feat in fc.get("features", []):
        props = feat["properties"]
        lon, lat = feat["geometry"]["coordinates"]
        key = props["estimate"]
        out[key] = {"lat": lat, "lon": lon, **props}
    return out


def load_tracks(trackfile):
    """Return {icao: [{"lat":…,"lon":…,"alt":…,"rssi":…,"ts":…}, …]}."""
    tracks = {}
    with open(trackfile) as f:
        for row in csv.DictReader(f):
            icao = row["icao"]
            fix = {
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "alt": float(row["altitude"]),
                # filter.py leaves rssi empty on fixes with no signal reading
                "rssi": float(row["rssi"]) if row["rssi"] else None,
                "ts": float(row["host_ts"]),
            }
            tracks.setdefault(icao, []).append(fix)
    return tracks


# ── HTML template ──────────────────────────────────────────────────────────────

HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RTL-SDR ADS-B Geolocation</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body {{ margin: 0; height: 100%; font-family: system-ui, sans-serif; }}
  #map {{ height: 100%; }}
  .legend {{
    background: rgba(255,255,255,0.93);
    padding: 9px 12px;
    border-radius: 6px;
    line-height: 1.7em;
    box-shadow: 0 1px 4px rgba(0,0,0,0.3);
    font-size: 13px;
    max-width: 280px;
  }}
  .legend b {{ font-size: 13px; }}
  .dot {{
    display: inline-block; width: 11px; height: 11px;
    border-radius: 50%; margin-right: 6px;
    border: 1px solid #fff; vertical-align: middle;
  }}
  .popup b {{ font-size: 13px; }}
  .popup table {{ border-collapse: collapse; margin-top: 4px; font-size: 12px; }}
  .popup td {{ padding: 1px 6px 1px 0; }}
</style>
</head>
<body>
<div id="map"></div>
<script>
// ── embedded data ─────────────────────────────────────────────────────────────
const POSITIONS  = {positions_json};
const TRACKS     = {tracks_json};
const ICAO_META  = {icao_meta_json};
const ERROR_LINES = {error_lines_json};

// ── helpers ───────────────────────────────────────────────────────────────────
function haversineKm(lat1, lon1, lat2, lon2) {{
  const R = 6371.0;
  const dlat = (lat2 - lat1) * Math.PI / 180;
  const dlon = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dlat / 2) ** 2
          + Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180)
          * Math.sin(dlon / 2) ** 2;
  return R * 2 * Math.asin(Math.sqrt(a));
}}

// ── map setup ─────────────────────────────────────────────────────────────────
const map = L.map('map');
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
}}).addTo(map);

const bounds = [];

// ── aircraft tracks ───────────────────────────────────────────────────────────
TRACKS.forEach(aircraft => {{
  const meta = ICAO_META[aircraft.icao];
  const latlngs = aircraft.fixes.map(f => [f.lat, f.lon]);
  latlngs.forEach(ll => bounds.push(ll));

  L.polyline(latlngs, {{
    color: meta.color, weight: 2.5, opacity: 0.85
  }}).addTo(map).bindTooltip(aircraft.icao, {{sticky: true}});

  aircraft.fixes.forEach(f => {{
    const d = new Date(f.ts * 1000).toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
    L.circleMarker([f.lat, f.lon], {{
      radius: 3.5, color: meta.color, weight: 1,
      fillColor: meta.color, fillOpacity: 0.9
    }}).addTo(map).bindPopup(
      `<div class="popup"><b>${{aircraft.icao}}</b><table>` +
      `<tr><td>Altitude</td><td>${{Math.round(f.alt).toLocaleString()}} ft</td></tr>` +
      `<tr><td>RSSI</td><td>${{f.rssi == null ? 'n/a' : f.rssi.toFixed(1) + ' dBFS'}}</td></tr>` +
      `<tr><td>Time</td><td>${{d}}</td></tr></table></div>`
    );
  }});
}});

// ── error lines (estimate -> truth) ──────────────────────────────────────────
ERROR_LINES.forEach(line => {{
  const pl = L.polyline(line.latlngs, {{
    color: '#888', weight: 1.5, dashArray: '4 4', opacity: 0.7
  }}).addTo(map);
  pl.bindTooltip(line.label, {{permanent: false, sticky: true}});
  // place a permanent mid-point label
  const mid = [
    (line.latlngs[0][0] + line.latlngs[1][0]) / 2,
    (line.latlngs[0][1] + line.latlngs[1][1]) / 2
  ];
  L.marker(mid, {{
    icon: L.divIcon({{
      html: `<span style="background:rgba(255,255,255,0.85);padding:1px 4px;border-radius:3px;font-size:11px;color:#555;white-space:nowrap;">${{line.label}}</span>`,
      className: '', iconAnchor: [0, 0]
    }})
  }}).addTo(map);
}});

// ── position markers ──────────────────────────────────────────────────────────

// GSM truth reference — declared up front so the RSSI error popup can
// reference it (const is not hoisted; using it before this line throws).
const p_truth = POSITIONS.gsm_truth;

// 1. RSSI path-loss estimate — orange star + dashed uncertainty circle
const p_rssi = POSITIONS.rssi_pathloss;
bounds.push([p_rssi.lat, p_rssi.lon]);

const acc = p_rssi.accuracy_m;
if (acc) {{
  L.circle([p_rssi.lat, p_rssi.lon], {{
    radius: acc, color: '#E87722', weight: 2, dashArray: '6 5',
    fillColor: '#E87722', fillOpacity: 0.08
  }}).addTo(map).bindTooltip(`±${{Math.round(acc)}} m (optimistic local-curvature est.)`);
}}

const starIconOrange = L.divIcon({{
  html: '<div style="font-size:28px;line-height:28px;color:#E87722;text-shadow:0 0 4px #fff,0 0 2px #000;">&#9733;</div>',
  className: '', iconSize: [28, 28], iconAnchor: [14, 14]
}});
L.marker([p_rssi.lat, p_rssi.lon], {{icon: starIconOrange, zIndexOffset: 900}})
  .addTo(map)
  .bindPopup(
    `<div class="popup"><b>RSSI path-loss estimate</b>` +
    `<table>` +
    `<tr><td>Lat/Lon</td><td>${{p_rssi.lat.toFixed(5)}}, ${{p_rssi.lon.toFixed(5)}}</td></tr>` +
    `<tr><td>Accuracy</td><td>±${{Math.round(acc)}} m <em>(optimistic)</em></td></tr>` +
    `<tr><td>P0</td><td>${{p_rssi.P0_dbfs}} dBFS</td></tr>` +
    `<tr><td>n</td><td>${{p_rssi.path_loss_exponent_n}}</td></tr>` +
    `<tr><td>RMS residual</td><td>${{p_rssi.rms_residual_db}} dB</td></tr>` +
    `<tr><td style="color:#c00">Error vs truth</td><td style="color:#c00">~${{haversineKm(p_rssi.lat, p_rssi.lon, p_truth.lat, p_truth.lon).toFixed(1)}} km off</td></tr>` +
    `</table></div>`
  );

// 2. GSM truth reference — green star (p_truth declared earlier)
bounds.push([p_truth.lat, p_truth.lon]);

const starIconGreen = L.divIcon({{
  html: '<div style="font-size:28px;line-height:28px;color:#27AE60;text-shadow:0 0 4px #fff,0 0 2px #000;">&#9733;</div>',
  className: '', iconSize: [28, 28], iconAnchor: [14, 14]
}});
L.marker([p_truth.lat, p_truth.lon], {{icon: starIconGreen, zIndexOffset: 1000}})
  .addTo(map)
  .bindPopup(
    `<div class="popup"><b>GSM truth (reference)</b><table>` +
    `<tr><td>Lat/Lon</td><td>${{p_truth.lat.toFixed(5)}}, ${{p_truth.lon.toFixed(5)}}</td></tr>` +
    `<tr><td>Source</td><td>GSM tower geolocation</td></tr>` +
    `</table></div>`
  )
  .openPopup();

// ── fit view ──────────────────────────────────────────────────────────────────
map.fitBounds(bounds, {{padding: [40, 40]}});

// ── legend ────────────────────────────────────────────────────────────────────
const legend = L.control({{position: 'topright'}});
legend.onAdd = function () {{
  const div = L.DomUtil.create('div', 'legend');
  let html = '<b>Aircraft tracks</b><br>';
  TRACKS.forEach(a => {{
    const c = ICAO_META[a.icao].color;
    html += `<span class="dot" style="background:${{c}};border-color:#888;border-radius:0;"></span>${{a.icao}} (${{a.fixes.length}} fixes)<br>`;
  }});
  html += '<br><b>Position estimates</b><br>';
  const rssiErrKm = haversineKm(POSITIONS.rssi_pathloss.lat, POSITIONS.rssi_pathloss.lon, POSITIONS.gsm_truth.lat, POSITIONS.gsm_truth.lon).toFixed(1);
  html += '<span style="font-size:18px;color:#E87722;vertical-align:middle;">&#9733;</span> '
        + `&nbsp;RSSI path-loss (~${{rssiErrKm}} km off)<br>`;
  html += '<span style="font-size:18px;color:#27AE60;vertical-align:middle;">&#9733;</span> '
        + '&nbsp;GSM truth (reference)<br>';
  div.innerHTML = html;
  return div;
}};
legend.addTo(map);
</script>
</body>
</html>
"""


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    default_posfile   = os.path.join(HERE, "work", "adsb_position.geojson")
    default_trackfile = os.path.join(HERE, "work", "tracks_pruned.csv")
    default_outfile   = next_capture_path(os.path.join(HERE, "captures"))

    posfile   = sys.argv[1] if len(sys.argv) > 1 else default_posfile
    trackfile = sys.argv[2] if len(sys.argv) > 2 else default_trackfile
    outfile   = sys.argv[3] if len(sys.argv) > 3 else default_outfile

    # Load data
    positions = load_positions(posfile)
    tracks_raw = load_tracks(trackfile)

    # Assign a stable color per ICAO (sorted for reproducibility)
    icao_list = sorted(tracks_raw.keys())
    icao_meta = {
        icao: {"color": ICAO_COLORS[i % len(ICAO_COLORS)]}
        for i, icao in enumerate(icao_list)
    }

    tracks_json_data = [
        {"icao": icao, "fixes": tracks_raw[icao]}
        for icao in icao_list
    ]

    # Precompute error lines (estimate -> truth)
    truth = positions["gsm_truth"]
    error_lines = []
    for key, label in [("rssi_pathloss", "RSSI estimate")]:
        p = positions[key]
        dist_km = haversine_km(p["lat"], p["lon"], truth["lat"], truth["lon"])
        error_lines.append({
            "latlngs": [[p["lat"], p["lon"]], [truth["lat"], truth["lon"]]],
            "label": f"{label}: {dist_km:.1f} km from truth",
        })

    html = HTML.format(
        positions_json=json.dumps({
            k: {kk: vv for kk, vv in v.items()} for k, v in positions.items()
        }),
        tracks_json=json.dumps(tracks_json_data),
        icao_meta_json=json.dumps(icao_meta),
        error_lines_json=json.dumps(error_lines),
    )

    outfile_abs = os.path.abspath(outfile)
    with open(outfile_abs, "w") as f:
        f.write(html)

    total_fixes = sum(len(v) for v in tracks_raw.values())
    print(f"Wrote {total_fixes} track fixes ({len(icao_list)} aircraft) "
          f"+ {len(positions)} position markers -> {outfile_abs}")
    print(f"Open it with:  xdg-open {outfile_abs}")


if __name__ == "__main__":
    main()
