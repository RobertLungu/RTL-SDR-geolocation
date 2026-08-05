#!/usr/bin/env python3
"""
export_map.py — turn cells_located.csv into map files.

Outputs (next to the input CSV):
  towers.geojson  — open in geojson.io, QGIS, kepler.gl, or a Leaflet map
  towers.kml      — open directly in Google Earth

Each tower is a point coloured by operator (MNC), labelled CID/RSSI, with the
OpenCellID 'range_m' uncertainty carried in the properties. Rows without a
location (DB miss or failed decode) are skipped.

Usage:
  ./export_map.py                      # ../cells_located.csv -> ../towers.{geojson,kml}
  ./export_map.py in.csv out_prefix
"""

import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# MCC 226 (Romania) operator names + colours (hex for KML is aabbggrr).
OPERATORS = {
    "1":  ("Vodafone RO",      "#E60000"),
    "3":  ("Telekom/Cosmote",  "#E20074"),
    "5":  ("Digi RCS-RDS",     "#0066CC"),
    "10": ("Orange RO",        "#FF7900"),
}


def op_info(mnc):
    return OPERATORS.get(mnc, (f"MNC {mnc}", "#888888"))


def hex_to_kml(color):
    """#RRGGBB -> KML aabbggrr (alpha ff)."""
    c = color.lstrip("#")
    r, g, b = c[0:2], c[2:4], c[4:6]
    return f"ff{b}{g}{r}".lower()


def read_rows(infile):
    with open(infile) as f:
        for row in csv.DictReader(f):
            if row.get("lat") and row.get("lon"):
                yield row


def write_geojson(rows, path):
    feats = []
    for r in rows:
        name, color = op_info(r["mnc"])
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [float(r["lon"]), float(r["lat"])]},
            "properties": {
                "operator": name, "mcc": r["mcc"], "mnc": r["mnc"],
                "lac": r["lac"], "cid": r["cid"], "arfcn": r["arfcn"],
                "freq_mhz": r["freq_mhz"], "rssi_dbm": r["rssi"],
                "range_m": r.get("range_m", ""),
                "marker-color": color,          # geojson.io / simplestyle
                "title": f"{name} CID {r['cid']} ({r['rssi']} dBm)",
            },
        })
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, indent=2)
    return len(feats)


def write_kml(rows, path):
    rows = list(rows)
    styles = "".join(
        f'<Style id="op{mnc}"><IconStyle><color>{hex_to_kml(color)}</color>'
        f'<scale>1.1</scale></IconStyle></Style>'
        for mnc, (_, color) in OPERATORS.items()
    )
    marks = []
    for r in rows:
        name, _ = op_info(r["mnc"])
        marks.append(
            f'<Placemark><name>{name} CID {r["cid"]}</name>'
            f'<styleUrl>#op{r["mnc"]}</styleUrl>'
            f'<description>ARFCN {r["arfcn"]} | {r["freq_mhz"]} MHz | '
            f'RSSI {r["rssi"]} dBm | LAC {r["lac"]} | range {r.get("range_m","?")} m'
            f'</description>'
            f'<Point><coordinates>{r["lon"]},{r["lat"]},0</coordinates></Point>'
            f'</Placemark>'
        )
    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        '<name>GSM Towers</name>' + styles + "".join(marks) +
        '</Document></kml>'
    )
    with open(path, "w") as f:
        f.write(kml)
    return len(rows)


def main():
    infile = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "cells_located.csv")
    prefix = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "..", "towers")

    rows = list(read_rows(infile))
    n1 = write_geojson(rows, prefix + ".geojson")
    n2 = write_kml(iter(rows), prefix + ".kml")
    print(f"Wrote {n1} towers -> {prefix}.geojson")
    print(f"Wrote {n2} towers -> {prefix}.kml")


if __name__ == "__main__":
    main()
