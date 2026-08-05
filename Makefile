# RTL-SDR GSM Self-Geolocation pipeline
#
#   scan      gather GSM cells with the RTL-SDR     -> cells.csv
#   locate    look tower positions up on OpenCellID -> cells_located.csv
#   position  RSSI-weighted position estimate       -> position.geojson
#   map       interactive Leaflet map               -> captures/captureN.html
#   process   locate-if-stale + position + map + open (no dongle / no scan)
#   all       run the whole chain and open the map
#
# Requirements:
#   - gr-gsm (provides the gnuradio/gsm python module used by tools/grgsm_scanner)
#   - an RTL-SDR dongle on USB
#   - python deps:  pip install -r requirements.txt
#   - an OpenCellID API key for `locate`:  export OPENCELLID_KEY=...

PY      ?= python3
TOOLS   := tools

# scan knobs (override on the command line, e.g. `make scan GAIN=45 PPM=-2`)
BAND    ?= GSM900
GAIN    ?= 40
PPM     ?= 0

# position knobs
N       ?= 8
SELECT  ?= closest
WEIGHT  ?= amplitude

# Auto-load OPENCELLID_KEY (and any other vars) from .env if present, so you
# don't have to `source .env` first. The file uses `export VAR=value`, which is
# valid Make syntax, so including it both sets and exports the variable.
-include .env

CELLS    := cells.csv
LOCATED  := cells_located.csv
POSITION := position.geojson
# Maps are archived as captures/captureN.html (auto-numbered by make_map_html.py).
LATEST_MAP = $(shell ls -1v captures/capture*.html 2>/dev/null | tail -1)

.PHONY: all process scan locate position map open export clean distclean help
.DEFAULT_GOAL := help

all: map open ## Run the full pipeline and open the map

process: $(LOCATED) position map open ## Re-analyse the existing scan (no dongle): position + map + open

scan $(CELLS): ## 1. Gather GSM cells with the RTL-SDR -> cells.csv
	BAND=$(BAND) GAIN=$(GAIN) PPM=$(PPM) ONCE=1 OUT=$(CELLS) $(TOOLS)/gsm_scan_loop.sh

locate $(LOCATED): $(CELLS) ## 2. Look tower positions up on OpenCellID -> cells_located.csv
	@test -n "$$OPENCELLID_KEY" || { echo "ERROR: export OPENCELLID_KEY=... first"; exit 1; }
	$(PY) $(TOOLS)/geolocate_cells.py $(CELLS) $(LOCATED)

position $(POSITION): $(LOCATED) ## 3. Calculate the RSSI-weighted position -> GeoJSON
	$(PY) $(TOOLS)/estimate_position.py $(LOCATED) --n $(N) --select $(SELECT) --weight $(WEIGHT) --out $(POSITION)

map: $(LOCATED) $(POSITION) ## 4. Render a new captures/captureN.html (auto-numbered archive)
	$(PY) $(TOOLS)/make_map_html.py

open: ## Open the newest captures/captureN.html in the default browser
	@test -n "$(LATEST_MAP)" || { echo "No captures/captureN.html yet — run 'make map' first."; exit 1; }
	@url="file://$(abspath $(LATEST_MAP))"; \
	echo "Opening $$url"; \
	xdg-open "$$url" >/dev/null 2>&1 \
	  || gio open "$$url" >/dev/null 2>&1 \
	  || sensible-browser "$$url" >/dev/null 2>&1 \
	  || { echo "Could not auto-launch a browser. Open this URL yourself:"; echo "  $$url"; }

export: $(LOCATED) ## Optional: export towers.geojson + towers.kml (QGIS / Google Earth)
	$(PY) $(TOOLS)/export_map.py $(LOCATED) towers

clean: ## Remove generated position artifacts (keeps CSVs + captureN.html archive)
	rm -f $(POSITION) towers.geojson towers.kml

distclean: clean ## Also remove the scanned + located CSVs
	rm -f $(CELLS) $(LOCATED)

help: ## Show this help
	@grep -hE '(^[a-zA-Z_$$()]+.*:.*##|^##)' $(MAKEFILE_LIST) \
	  | sed -E 's/\$$\([A-Z]+\) ?//g; s/:.*## /\t/; s/^## /\t  /' \
	  | awk -F'\t' '{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
