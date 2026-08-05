#!/usr/bin/env python3
"""capture.py -- self-geolocation-safe raw ADS-B capture via dump1090-mutability.

PURPOSE
-------
Capture a clean, per-message log of decoded ADS-B / Mode-S traffic to a CSV for use
in a SELF-geolocation pipeline (we are trying to *estimate* the receiver position,
so we must never feed the assumed receiver position back into the decoder).

WHY NO --lat/--lon (the whole point)
------------------------------------
dump1090 supports `--lat`/`--lon` to enable *local* CPR decoding, where a single
position frame is resolved relative to the receiver's own known location. For a
self-geolocation experiment that is circular: seeding the decoder with the position
we are trying to recover would contaminate every emitted lat/lon.

This script therefore launches dump1090 WITHOUT `--lat`/`--lon`. With no reference
position, dump1090 can only emit positions via:
  * global CPR  -- an even+odd airborne frame pair, independent of our location, or
  * aircraft-relative local CPR -- relative to the aircraft's own prior global fix.
Both are independent of where *we* are, which is exactly what we want. The script
hard-refuses to accept or pass any receiver lat/lon (see `_assert_no_receiver_pos`).

OUTPUT
------
A raw, one-row-per-SBS-MSG CSV with the host receive timestamp first:
    host_ts, icao, msg_type, callsign, altitude, lat, lon,
    ground_speed, track, vertical_rate, squawk, rssi

Fields are read from the dump1090 SBS-1 BaseStation stream (TCP 127.0.0.1:30003).
Blanks are left where a given MSG subtype does not carry that field.

RSSI
----
The SBS-30003 stream carries NO signal level. We still want RSSI for later soft
ranging, so a background thread also reads the Beast binary stream (TCP
127.0.0.1:30005), which prepends a 1-byte signal level to every frame. We keep a
"latest signal level per ICAO" map and stamp each SBS row with the most recent
RSSI (in dBFS) seen for that aircraft. This is best-effort and non-blocking: if the
Beast stream is unavailable the `rssi` column is simply left blank and capture
continues. See the RSSI CAVEAT note printed in the run summary.

HARDWARE
--------
Marginal RTL2838 (R820T). dump1090-mutability is at /usr/bin/dump1090-mutability.
Expect only a trickle of CRC-valid messages (~tens to low hundreds over 5 min).

USAGE
-----
    python3 capture.py --duration 300
    python3 capture.py --duration 300 --out work/csv_raw.csv

Stdlib only (socket, csv, subprocess, argparse, signal, threading). No heavy deps.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

# --- Constants -------------------------------------------------------------

DUMP1090_BIN = "/usr/bin/dump1090-mutability"
NET_HOST = "127.0.0.1"
SBS_PORT = 30003       # SBS-1 BaseStation text output (no signal level)
BEAST_PORT = 30005     # Beast binary output (carries 1-byte signal level)

DEFAULT_DURATION = 300  # seconds
DEFAULT_OUT = os.path.join("work", "csv_raw.csv")

CSV_COLUMNS = [
    "host_ts", "icao", "msg_type", "callsign", "altitude", "lat", "lon",
    "ground_speed", "track", "vertical_rate", "squawk", "rssi",
]

# SBS-1 BaseStation field layout, 0-based indices after splitting an SBS line
# on commas. Reference: a row like
#   MSG,3,1,1,4CAD89,1,2026/06/30,...,,37025,,,44.43728,26.08047,,,,,,0
#   0   1 2 3 4      5 6          ...  11    12 13 14       15      16 17
SBS_IDX = {
    "msg_type": 1,        # transmission type (1..8)
    "icao": 4,            # HexIdent
    "callsign": 10,
    "altitude": 11,
    "ground_speed": 12,
    "track": 13,
    "lat": 14,            # field 15 (1-based)
    "lon": 15,            # field 16 (1-based)
    "vertical_rate": 16,
    "squawk": 17,
}

# Mode-S downlink formats whose ICAO address is carried in plaintext (AA field,
# message bytes 1..3). For other DFs the ICAO is recovered via CRC/AP overlay,
# which the raw Beast stream does not resolve for us, so we skip RSSI for them.
PLAINTEXT_ICAO_DFS = frozenset((11, 17, 18))


# --- Small helpers ---------------------------------------------------------

def eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)
    sys.stderr.flush()


def _assert_no_receiver_pos(cmd: list[str]) -> None:
    """Refuse to run if a receiver lat/lon ever sneaks into the dump1090 args.

    This is the structural guarantee behind the 'self-geolocation-safe' claim.
    """
    for tok in cmd:
        low = tok.lower()
        if low in ("--lat", "--lon") or low.startswith("--lat=") or low.startswith("--lon="):
            raise SystemExit(
                "REFUSING TO RUN: receiver --lat/--lon present in dump1090 command. "
                "This is a self-geolocation pipeline; seeding the decoder with the "
                "assumed receiver position is forbidden (it would make positions "
                "circular). Remove the coordinates."
            )


def sig_to_dbfs(sig: int) -> float | None:
    """Convert a Beast signal-level byte (0..255) to approximate dBFS.

    dump1090 emits the signal byte as round(sqrt(power) * 255) where `power` is
    the per-message mean magnitude normalized to [0, 1]. So power = (sig/255)**2
    and the level in dBFS is 10*log10(power). Returns None for a zero byte.
    """
    if sig <= 0:
        return None
    power = (sig / 255.0) ** 2
    return round(10.0 * math.log10(power), 1)


# --- Beast (port 30005) RSSI reader ---------------------------------------

def _read_escaped(buf: bytearray, pos: int, count: int):
    """Read `count` logical bytes from `buf` starting at `pos`, undoing Beast
    0x1a escaping (a literal 0x1a in payload is doubled as 0x1a 0x1a).

    Returns (list_of_bytes, new_pos) or None if more data is needed / the frame
    looks corrupt (an un-doubled 0x1a, which signals a new frame boundary).
    """
    out: list[int] = []
    n = len(buf)
    while len(out) < count:
        if pos >= n:
            return None  # incomplete; wait for more bytes
        b = buf[pos]
        if b == 0x1A:
            if pos + 1 >= n:
                return None
            if buf[pos + 1] == 0x1A:
                out.append(0x1A)
                pos += 2
            else:
                return None  # un-escaped 0x1a -> start of a new frame, bail
        else:
            out.append(b)
            pos += 1
    return out, pos


def _beast_icao(data: list[int]) -> str | None:
    """Extract the plaintext ICAO from a raw Mode-S message for DF 11/17/18."""
    if len(data) < 4:
        return None
    df = data[0] >> 3
    if df in PLAINTEXT_ICAO_DFS:
        return "%02X%02X%02X" % (data[1], data[2], data[3])
    return None


def _parse_beast_buffer(buf: bytearray, rssi_map: dict[str, int]) -> bytearray:
    """Consume whole Beast frames from `buf`, updating rssi_map[icao] = sig.

    Returns the unconsumed tail (a partial frame) to be prepended next round.
    """
    # Beast frame: 0x1a, <type>, <6-byte MLAT timestamp>, <1-byte signal>, <data>
    # type 0x31 = Mode A/C (2 data bytes), 0x32 = Mode-S short (7), 0x33 = long (14)
    msg_len_by_type = {0x31: 2, 0x32: 7, 0x33: 14}
    pos = 0
    n = len(buf)
    while pos < n:
        if buf[pos] != 0x1A:
            nxt = buf.find(0x1A, pos)
            if nxt < 0:
                return bytearray()  # no frame start in view; drop noise
            pos = nxt
            continue
        if pos + 1 >= n:
            return buf[pos:]  # need the type byte
        mtype = buf[pos + 1]
        msg_len = msg_len_by_type.get(mtype)
        if msg_len is None:
            # 0x1a 0x1a (escaped data outside a frame) or 0x34 status/other: resync.
            pos += 1
            continue
        res = _read_escaped(buf, pos + 2, 6 + 1 + msg_len)
        if res is None:
            return buf[pos:]  # incomplete frame, keep for next chunk
        fields, new_pos = res
        sig = fields[6]                      # signal level byte
        data = fields[7:7 + msg_len]         # raw Mode-S message bytes
        icao = _beast_icao(data)
        if icao:
            rssi_map[icao] = sig
        pos = new_pos
    return bytearray()


def beast_rssi_worker(stop: threading.Event, rssi_map: dict[str, int]) -> None:
    """Background loop: connect to Beast 30005 and keep rssi_map fresh.

    Fully best-effort. Any failure just retries; it never raises into the caller
    and never blocks the SBS capture.
    """
    buf = bytearray()
    while not stop.is_set():
        try:
            sock = socket.create_connection((NET_HOST, BEAST_PORT), timeout=2.0)
        except OSError:
            time.sleep(0.5)
            continue
        sock.settimeout(1.0)
        buf.clear()
        try:
            while not stop.is_set():
                try:
                    chunk = sock.recv(8192)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                # Guard against unbounded growth if we ever lose sync.
                if len(buf) > 1 << 16:
                    del buf[:-4096]
                buf = _parse_beast_buffer(buf, rssi_map)
        finally:
            try:
                sock.close()
            except OSError:
                pass


# --- SBS (port 30003) parsing ---------------------------------------------

def parse_sbs_line(line: str, rssi_map: dict[str, int]) -> dict[str, str] | None:
    """Parse one SBS-1 'MSG' line into a CSV row dict, or None to skip it."""
    parts = line.split(",")
    if not parts or parts[0] != "MSG":
        return None  # ignore STA/ID/AIR/SEL/CLK and blank lines

    def field(idx: int) -> str:
        return parts[idx].strip() if idx < len(parts) else ""

    icao = field(SBS_IDX["icao"]).upper()

    rssi = ""
    sig = rssi_map.get(icao)
    if sig is not None:
        dbfs = sig_to_dbfs(sig)
        if dbfs is not None:
            rssi = f"{dbfs}"

    return {
        "host_ts": f"{time.time():.3f}",
        "icao": icao,
        "msg_type": field(SBS_IDX["msg_type"]),
        "callsign": field(SBS_IDX["callsign"]),
        "altitude": field(SBS_IDX["altitude"]),
        "lat": field(SBS_IDX["lat"]),
        "lon": field(SBS_IDX["lon"]),
        "ground_speed": field(SBS_IDX["ground_speed"]),
        "track": field(SBS_IDX["track"]),
        "vertical_rate": field(SBS_IDX["vertical_rate"]),
        "squawk": field(SBS_IDX["squawk"]),
        "rssi": rssi,
    }


# --- dump1090 --stats parsing ---------------------------------------------

def parse_stats(text: str) -> dict[str, int | None] | None:
    """Parse the final (cumulative) 'Statistics:' block from dump1090 stdout.

    dump1090-mutability prints intermediate per-interval blocks (--stats-every,
    which reset each interval) and a final cumulative block at shutdown. We take
    the LAST block. Counts are matched by keyword substring so this works whether
    or not the demodulator/'Local receiver:' section is present.
    """
    blocks = text.split("Statistics:")
    if len(blocks) < 2:
        return None
    block = blocks[-1]

    section = None  # "local" | "net" | None
    preamble: int | None = None
    local_crc: int | None = None
    net_crc: int | None = None
    other_crc: int | None = None
    vals: dict[str, int | None] = {
        "airborne_pos": None,
        "surface_pos": None,
        "global_cpr_valid": None,
        "unique_aircraft": None,
        "total_usable": None,
    }

    line_re = re.compile(r"^(\d+)\s+(.*)$")
    for raw in block.splitlines():
        s = raw.strip()
        m = line_re.match(s)
        if not m:
            low = s.lower()
            if low.startswith("local receiver"):
                section = "local"
            elif low.startswith("messages from network"):
                section = "net"
            continue
        cnt = int(m.group(1))
        label = m.group(2).lower()

        if "preamble" in label:
            preamble = cnt
        if "accepted with correct crc" in label:
            if section == "local":
                local_crc = cnt
            elif section == "net":
                net_crc = cnt
            else:
                other_crc = cnt
        if "airborne position messages received" in label:
            vals["airborne_pos"] = cnt
        elif "surface position messages received" in label:
            vals["surface_pos"] = cnt
        if "global cpr attempts with valid positions" in label:
            vals["global_cpr_valid"] = cnt
        if "unique aircraft tracks" in label:
            vals["unique_aircraft"] = cnt
        if "total usable messages" in label:
            vals["total_usable"] = cnt

    vals["preamble"] = preamble
    # Prefer the demodulator (local receiver) CRC count when present.
    vals["accepted_crc"] = (
        local_crc if local_crc is not None
        else net_crc if net_crc is not None
        else other_crc
    )
    return vals


# --- Subprocess plumbing ---------------------------------------------------

def stdout_reader(proc: subprocess.Popen, sink: list[str]) -> None:
    """Drain dump1090's combined stdout/stderr into `sink` until EOF."""
    assert proc.stdout is not None
    for line in proc.stdout:
        sink.append(line)


def connect_with_retry(host: str, port: int, stop: threading.Event,
                       total_timeout: float = 15.0) -> socket.socket | None:
    """Try to connect until success, the deadline, or a stop request."""
    deadline = time.time() + total_timeout
    while time.time() < deadline and not stop.is_set():
        try:
            return socket.create_connection((host, port), timeout=2.0)
        except OSError:
            time.sleep(0.4)
    return None


# --- Main ------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Self-geolocation-safe ADS-B capture via dump1090-mutability "
                    "(never passes receiver --lat/--lon).",
    )
    p.add_argument("--duration", type=int, default=DEFAULT_DURATION,
                   help=f"Capture duration in seconds (default: {DEFAULT_DURATION}).")
    p.add_argument("--out", default=None,
                   help="Output CSV path; relative paths resolve against the script "
                        f"directory (default: {DEFAULT_OUT}).")
    p.add_argument("--timestamp", action="store_true",
                   help="Insert a UTC timestamp into the default output filename "
                        "(csv_raw_YYYYmmdd_HHMMSS.csv). Ignored if --out is given.")
    p.add_argument("--gain", default=None,
                   help="RTL-SDR gain in dB passed to dump1090 (e.g. 49.6, or -10 "
                        "for auto-gain). Default: dump1090's default (max gain).")
    p.add_argument("--device-index", type=int, default=None,
                   help="RTL device index passed to dump1090 (default: 0).")
    return p.parse_args(argv)


def resolve_out_path(args: argparse.Namespace, script_dir: str) -> str:
    if args.out:
        out = args.out
    elif args.timestamp:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = os.path.join("work", f"csv_raw_{ts}.csv")
    else:
        out = DEFAULT_OUT
    if not os.path.isabs(out):
        out = os.path.join(script_dir, out)
    return out


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.duration <= 0:
        eprint("ERROR: --duration must be a positive number of seconds.")
        return 2

    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = resolve_out_path(args, script_dir)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    if not (os.path.isfile(DUMP1090_BIN) and os.access(DUMP1090_BIN, os.X_OK)):
        eprint(f"ERROR: dump1090 not found or not executable at {DUMP1090_BIN}")
        return 1

    # Build the dump1090 command. NOTE: NO --lat / --lon, ever (see module docstring).
    cmd = [
        DUMP1090_BIN,
        "--net",
        "--net-bind-address", NET_HOST,
        "--stats",
        "--stats-every", str(args.duration),
    ]
    if args.gain is not None:
        cmd += ["--gain", str(args.gain)]
    if args.device_index is not None:
        cmd += ["--device-index", str(args.device_index)]
    _assert_no_receiver_pos(cmd)  # structural cleanliness guarantee

    stop = threading.Event()

    def _handle_signal(signum, _frame):
        eprint(f"\n[capture] caught signal {signum}, shutting down cleanly...")
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    print(f"[capture] launching: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # fold dump1090 errors into one stream
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        eprint(f"ERROR: failed to launch dump1090: {exc}")
        return 1

    stdout_lines: list[str] = []
    reader = threading.Thread(target=stdout_reader, args=(proc, stdout_lines), daemon=True)
    reader.start()

    rssi_map: dict[str, int] = {}
    beast_thread = threading.Thread(
        target=beast_rssi_worker, args=(stop, rssi_map), daemon=True)
    beast_thread.start()

    rows = 0
    rows_with_pos = 0
    exit_code = 0
    sbs: socket.socket | None = None
    csvfile = None

    try:
        # Give dump1090 a moment to claim the dongle and bind ports; bail early
        # with a useful message if it dies (e.g. device busy / not found).
        print(f"[capture] waiting for SBS port {SBS_PORT} ...")
        sbs = connect_with_retry(NET_HOST, SBS_PORT, stop, total_timeout=15.0)
        if sbs is None:
            if proc.poll() is not None:
                time.sleep(0.2)  # let the reader flush
                tail = "".join(stdout_lines[-15:]).strip()
                eprint("ERROR: dump1090 exited before the SBS port opened. "
                       "The RTL-SDR may be busy (held by another process) or absent.")
                if tail:
                    eprint("----- dump1090 output (tail) -----")
                    eprint(tail)
                    eprint("----------------------------------")
                return 1
            eprint(f"ERROR: could not connect to SBS port {SBS_PORT} within timeout.")
            return 1

        print(f"[capture] connected to SBS {NET_HOST}:{SBS_PORT}; "
              f"capturing for {args.duration}s -> {out_path}")

        csvfile = open(out_path, "w", newline="", encoding="utf-8")
        writer = csv.DictWriter(csvfile, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        sbs.settimeout(1.0)
        text_buf = ""
        start_time = time.time()
        end_time = start_time + args.duration
        last_countdown = -1  # last whole elapsed second we drew, to redraw ~1/s

        while time.time() < end_time and not stop.is_set():
            # Live single-line countdown to stderr (time only). Driven by the
            # loop's ~1Hz cadence (the 1.0s socket timeout); no extra thread.
            elapsed = int(time.time() - start_time)
            if elapsed != last_countdown:
                last_countdown = elapsed
                remaining = max(0, args.duration - elapsed)
                sys.stderr.write(
                    f"\r[capture] {elapsed}s elapsed / {remaining}s remaining ")
                sys.stderr.flush()

            try:
                data = sbs.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                eprint("[capture] SBS socket error; stopping read loop.")
                break
            if not data:
                eprint("[capture] SBS stream closed by dump1090.")
                break

            text_buf += data.decode("ascii", "replace")
            while "\n" in text_buf:
                line, text_buf = text_buf.split("\n", 1)
                line = line.strip("\r ")
                if not line:
                    continue
                row = parse_sbs_line(line, rssi_map)
                if row is None:
                    continue
                writer.writerow(row)
                rows += 1
                if row["lat"] and row["lon"]:
                    rows_with_pos += 1
            csvfile.flush()

        # Terminate the in-place countdown line so later output starts cleanly.
        if last_countdown >= 0:
            sys.stderr.write("\n")
            sys.stderr.flush()

    except Exception as exc:  # never let an unexpected error skip cleanup
        eprint(f"[capture] unexpected error: {exc!r}")
        exit_code = 1
    finally:
        stop.set()
        if sbs is not None:
            try:
                sbs.close()
            except OSError:
                pass
        if csvfile is not None:
            csvfile.close()

        # Terminate dump1090 -> SIGTERM makes it print its final cumulative stats.
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                eprint("[capture] dump1090 did not stop on SIGTERM; killing.")
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        reader.join(timeout=5)
        beast_thread.join(timeout=3)

    # --- Summary -----------------------------------------------------------
    stats = parse_stats("".join(stdout_lines))

    print("\n========== capture summary ==========")
    print(f"output CSV              : {out_path}")
    print(f"CSV data rows           : {rows}")
    print(f"  rows with lat & lon   : {rows_with_pos}")

    if stats is None:
        eprint("WARNING: could not parse a dump1090 stats block from its output.")
    else:
        def show(label: str, val) -> None:
            print(f"{label:<24}: {val if val is not None else 'n/a'}")
        print("---- dump1090 --stats (cumulative) ----")
        show("Mode-S preambles", stats.get("preamble"))
        show("accepted w/ good CRC", stats.get("accepted_crc"))
        show("total usable messages", stats.get("total_usable"))
        show("airborne position msgs", stats.get("airborne_pos"))
        show("surface position msgs", stats.get("surface_pos"))
        show("global CPR valid pos", stats.get("global_cpr_valid"))
        show("unique aircraft tracks", stats.get("unique_aircraft"))

    if rows_with_pos == 0 and rows > 0:
        print("\nNOTE: no positions decoded. With this marginal dongle and no --lat/--lon "
              "reference, positions require a global even+odd CPR pair within ~10s from "
              "the same aircraft -- often not achievable on weak reception. This is "
              "expected; the raw message log is still useful.")
    print("=====================================")
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
