#!/usr/bin/env python3
"""
make_map_html.py — render a single self-contained interactive map (captures/captureN.html).

Reads cells_located.csv (+ position.geojson if present) and emits a standalone
Leaflet map you can open in any browser: pan/zoom, operator-coloured tower
markers with popups (CID/LAC/ARFCN/RSSI/range), OpenCellID uncertainty circles,
and a star at the RSSI-weighted position estimate.

The tower/position data is embedded directly in the HTML, so the file is
portable — it only needs internet access for the Leaflet library and OSM tiles.

Usage:
  ./make_map_html.py                       # ../cells_located.csv -> ../captures/captureN.html
  ./make_map_html.py in.csv out.html pos.geojson
"""

import csv
import json
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

# MCC 226 (Romania) operator names + colours.
OPERATORS = {
    "1":  ("Vodafone RO",     "#E60000"),
    "3":  ("Telekom/Cosmote", "#E20074"),
    "5":  ("Digi RCS-RDS",    "#0066CC"),
    "10": ("Orange RO",       "#FF7900"),
}


def op_info(mnc):
    return OPERATORS.get(mnc, (f"MNC {mnc}", "#888888"))


def load_towers(infile):
    towers = []
    with open(infile) as f:
        for r in csv.DictReader(f):
            if not (r.get("lat") and r.get("lon")):
                continue
            name, color = op_info(r["mnc"])
            towers.append({
                "lat": float(r["lat"]), "lon": float(r["lon"]),
                "operator": name, "color": color, "mnc": r["mnc"],
                "cid": r.get("cid", ""), "lac": r.get("lac", ""),
                "arfcn": r.get("arfcn", ""), "freq": r.get("freq_mhz", ""),
                "rssi": r.get("rssi", ""), "range": r.get("range_m", ""),
            })
    return towers


def load_position(posfile):
    """Returns (estimates_by_name, used_tower_keys). used_tower_keys is the set
    of (mnc, lac, cid) that fed the final position fix, if the estimator
    recorded them (empty set for older position.geojson files)."""
    if not os.path.exists(posfile):
        return None, set()
    pos = json.load(open(posfile))
    out, used = {}, set()
    for feat in pos.get("features", []):
        est = feat["properties"].get("estimate")
        lon, lat = feat["geometry"]["coordinates"]
        out[est] = {"lat": lat, "lon": lon,
                    "title": feat["properties"].get("title", est),
                    "accuracy_m": feat["properties"].get("accuracy_m")}
        for t in feat["properties"].get("used_towers", []):
            used.add((str(t.get("mnc")), str(t.get("lac")), str(t.get("cid"))))
    return out, used


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RTL-SDR GSM Geolocation</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body {{ margin: 0; height: 100%; font-family: system-ui, sans-serif; }}
  #map {{ height: 100%; }}
  .legend {{ background: rgba(255,255,255,0.92); padding: 8px 10px; border-radius: 6px;
            line-height: 1.5em; box-shadow: 0 1px 4px rgba(0,0,0,0.3); font-size: 13px; }}
  .legend .dot {{ display: inline-block; width: 11px; height: 11px; border-radius: 50%;
                 margin-right: 6px; border: 1px solid #fff; vertical-align: middle; }}
  .popup b {{ font-size: 13px; }}
  .popup table {{ border-collapse: collapse; margin-top: 4px; font-size: 12px; }}
  .popup td {{ padding: 1px 6px 1px 0; }}
</style>
</head>
<body>
<div id="map"></div>
<script>
const TOWERS = {towers_json};
const POSITION = {position_json};

const map = L.map('map');
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
}}).addTo(map);

const bounds = [];

TOWERS.forEach(t => {{
  bounds.push([t.lat, t.lon]);
  L.circleMarker([t.lat, t.lon], {{
    radius: t.used ? 9 : 8,
    color: t.used ? '#00A000' : '#fff', weight: t.used ? 3 : 1.5,
    fillColor: t.color, fillOpacity: 0.95
  }}).addTo(map).bindPopup(
    `<div class="popup"><b>${{t.operator}}</b><table>` +
    `<tr><td>CID</td><td>${{t.cid}}</td></tr>` +
    `<tr><td>LAC</td><td>${{t.lac}}</td></tr>` +
    `<tr><td>ARFCN</td><td>${{t.arfcn}} (${{t.freq}} MHz)</td></tr>` +
    `<tr><td>RSSI</td><td>${{t.rssi}} dBm</td></tr>` +
    `<tr><td>Range</td><td>±${{t.range || '?'}} m</td></tr></table></div>`
  ).bindTooltip(`${{t.rssi}} dBm`, {{permanent: true, direction: 'top',
                 offset: [0, -8], className: 'rssi-lbl'}});
}});

const starIcon = L.divIcon({{
  html: '<div style="font-size:30px; line-height:30px; text-shadow:0 0 3px #000;">&#11088;</div>',
  className: '', iconSize: [30, 30], iconAnchor: [15, 15]
}});

if (POSITION && POSITION.weighted_centroid) {{
  const p = POSITION.weighted_centroid;
  bounds.push([p.lat, p.lon]);
  const acc = parseFloat(p.accuracy_m);
  if (acc) {{
    L.circle([p.lat, p.lon], {{
      radius: acc, color: '#00A000', weight: 2, dashArray: '6 5',
      fillColor: '#00C000', fillOpacity: 0.10
    }}).addTo(map).bindTooltip(`±${{Math.round(acc)}} m (1σ est.)`);
  }}
  L.marker([p.lat, p.lon], {{icon: starIcon, zIndexOffset: 1000}})
    .addTo(map)
    .bindPopup(`<b>${{p.title}}</b><br>${{p.lat.toFixed(5)}}, ${{p.lon.toFixed(5)}}` +
               (acc ? `<br>accuracy &plusmn;${{Math.round(acc)}} m` : ''))
    .openPopup();
}}
if (POSITION && POSITION.trilateration_uncalibrated) {{
  const p = POSITION.trilateration_uncalibrated;
  L.circleMarker([p.lat, p.lon], {{
    radius: 6, color: '#444', weight: 1, fillColor: '#bbb', fillOpacity: 0.8
  }}).addTo(map).bindPopup(`<b>${{p.title}}</b><br>${{p.lat.toFixed(5)}}, ${{p.lon.toFixed(5)}}`);
}}

map.fitBounds(bounds, {{padding: [40, 40]}});

const legend = L.control({{position: 'topright'}});
legend.onAdd = function () {{
  const div = L.DomUtil.create('div', 'legend');
  div.innerHTML = '<b>Operators</b><br>' + {legend_json}
    .map(o => `<span class="dot" style="background:${{o[1]}}"></span>${{o[0]}}`).join('<br>')
    + '<br><span class="dot" style="background:#ddd;border:2px solid #00A000"></span>Used in position fix'
    + '<br><span style="font-size:18px">&#11088;</span> Your position (WCL)';
  return div;
}};
legend.addTo(map);
</script>
</body>
</html>
"""


def main():
    infile = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "cells_located.csv")
    outfile = sys.argv[2] if len(sys.argv) > 2 else next_capture_path(os.path.join(HERE, "..", "captures"))
    posfile = sys.argv[3] if len(sys.argv) > 3 else os.path.join(HERE, "..", "position.geojson")

    towers = load_towers(infile)
    if not towers:
        sys.exit(f"No located towers in {infile} — run geolocate_cells.py first.")
    position, used = load_position(posfile)
    for t in towers:
        t["used"] = (t["mnc"], t["lac"], t["cid"]) in used

    # operators actually present, in a stable order, for the legend
    present = []
    seen = set()
    for t in towers:
        if t["operator"] not in seen:
            seen.add(t["operator"])
            present.append([t["operator"], t["color"]])

    html = HTML.format(
        towers_json=json.dumps(towers),
        position_json=json.dumps(position),
        legend_json=json.dumps(present),
    )
    with open(outfile, "w") as f:
        f.write(html)
    print(f"Wrote {len(towers)} towers -> {outfile}")
    print(f"Open it with:  xdg-open {os.path.abspath(outfile)}")


if __name__ == "__main__":
    main()
