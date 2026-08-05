# RTL-SDR Geolocation

IMPORTANT: At the end of every conversation where the project changed, update the Current Project State section of this CLAUDE.md so future sessions never need to re-read the whole project.

## Project Overview

Experiments in radio-based geolocation using an RTL-SDR dongle (RTL2838). Two independent pipelines:

- **GSM tower pipeline** — the original, working pipeline. Do not modify it when working on other areas.
- **ADS-B self-geolocation** (`adsb/`) — estimates the receiver's own position from aircraft ADS-B broadcasts, fully isolated from the GSM code. Reference-free: never seed it with the receiver's own coordinates; use global-CPR aircraft positions only.

Results are displayed on the existing Leaflet map (`map.html`) — reuse it rather than creating new display methods.

## Working conventions

- Route SDR/RF/GSM questions to the `sdr-rf-expert` agent; Python RTL-SDR/ADS-B scripting to `rtl-sdr-python-automation`; cleanup/Makefiles/HTML interfaces/restructuring to `project-build-orchestrator`.
- The user runs sudo/manual commands themselves — just provide them.
- No README/docs for incomplete work.

## Current Project State

- ADS-B self-geo pipeline works end-to-end: `adsb/capture.py` → `filter.py` → `locate.py` → `map.html`. Accuracy ~12–30 km, currently data-limited.
- Hardware caveat: the dongle's 1090 MHz reception is severely degraded but nonzero — it also emits noise phantoms (fake ICAOs / impossible positions). Verify each ICAO+position against a live tracker (e.g. FR24) before trusting it.
- Next step: capture more aircraft with diverse bearings/altitudes to improve the position fix.
- `.claude/agents/romanian-essay-writer.md` — side agent for composing/structuring/formatting essays in Romanian (unrelated to the SDR work).
- `studiu_geolocalizare.md` / `.odt` — Romanian technical study of both pipelines (GSM weighted-centroid algorithm; ADS-B RSSI path-loss fit), generated 2026-07-10. Regenerate after edits with: `pandoc studiu_geolocalizare.md -o studiu_geolocalizare.odt --reference-doc=studiu_template.odt` (title/author/date come from the md's YAML block; the template provides justified/keep-together/section-per-page styling).
