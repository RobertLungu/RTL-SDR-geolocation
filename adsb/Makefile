# =============================================================================
# ADS-B Self-Geolocation Pipeline — Makefile
# Working directory: adsb/
# =============================================================================

# Capture duration in seconds. Default: 600 s (10 minutes).
# Override at the command line: make capture DUR=300
DUR    ?= 600
PYTHON ?= python3

# Output files (used as Make targets / prerequisites)
RAW       := work/csv_raw.csv
PRUNED    := work/tracks_pruned.csv
RANGING   := work/ranging_samples.csv
GEOJSON   := work/adsb_position.geojson
# Maps are archived as captures/captureN.html (auto-numbered by make_map_html.py).
LATEST_MAP = $(shell ls -1v captures/capture*.html 2>/dev/null | tail -1)

# -----------------------------------------------------------------------------
# Default goal
# -----------------------------------------------------------------------------
.DEFAULT_GOAL := help

.PHONY: help capture filter locate map process all open clean clean-all

help:
	@echo ""
	@echo "ADS-B Self-Geolocation Pipeline"
	@echo ""
	@echo "Targets:"
	@echo "  capture   Run capture.py for DUR seconds (default 600 s / 10 min)."
	@echo "              Requires RTL-SDR dongle + dump1090-mutability."
	@echo "              Override: make capture DUR=300"
	@echo "  filter    Filter raw capture -> tracks_pruned.csv + ranging_samples.csv"
	@echo "  locate    Geolocate from ranging samples -> adsb_position.geojson"
	@echo "  map       Render a new captures/captureN.html (auto-numbered archive)"
	@echo "  process   filter + locate + map  (re-analyse an existing capture; no dongle)"
	@echo "  all       capture + process      (full end-to-end pipeline)"
	@echo "  open      Open the newest captures/captureN.html in the default browser"
	@echo "  clean     Remove derived CSV/geojson (keeps csv_raw.csv + captureN.html archive)"
	@echo "  clean-all Also remove work/csv_raw.csv (captureN.html archive is NEVER deleted)"
	@echo ""

# -----------------------------------------------------------------------------
# Stage 1 — capture
# Hardware-dependent; not wired to a file-prerequisite because the dongle
# state is external.  Always runs when invoked explicitly.
# -----------------------------------------------------------------------------
capture:
	$(PYTHON) capture.py --duration $(DUR)

# -----------------------------------------------------------------------------
# Stage 2 — filter
# -----------------------------------------------------------------------------
$(PRUNED) $(RANGING): $(RAW)
	$(PYTHON) filter.py

# Convenience phony alias
filter: $(PRUNED) $(RANGING)

# -----------------------------------------------------------------------------
# Stage 3 — locate
# -----------------------------------------------------------------------------
$(GEOJSON): $(RANGING)
	$(PYTHON) locate.py

locate: $(GEOJSON)

# -----------------------------------------------------------------------------
# Stage 4 — map
# Always renders a NEW auto-numbered captures/captureN.html; the archive is the
# point, so this is phony rather than a file target.
# -----------------------------------------------------------------------------
map: $(GEOJSON) $(PRUNED)
	$(PYTHON) make_map_html.py

# -----------------------------------------------------------------------------
# Composite targets
# -----------------------------------------------------------------------------
process: filter locate map open

all: capture process

# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
open:
	@test -n "$(LATEST_MAP)" || { echo "No captures/captureN.html yet — run 'make map' first."; exit 1; }
	@url="file://$(abspath $(LATEST_MAP))"; \
	echo "Opening $$url"; \
	xdg-open "$$url" >/dev/null 2>&1 \
	  || gio open "$$url" >/dev/null 2>&1 \
	  || sensible-browser "$$url" >/dev/null 2>&1 \
	  || { echo "Could not auto-launch a browser. Open this URL yourself:"; echo "  $$url"; }

# Remove derived files; preserve the raw capture (precious hardware data) and
# the captureN.html archive (never deleted by any target).
clean:
	rm -f $(PRUNED) $(RANGING) $(GEOJSON)

# Also remove the raw capture — intentionally kept separate and named clearly
clean-all: clean
	rm -f $(RAW)
