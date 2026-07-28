#!/usr/bin/env python3
"""
Announcer live display for tulospalvelu (Pekka Pirila).

UDP listener on port 15901 receives KILPT and VAIN_TULOST packets.
HTTP server on port 8081 serves a live announcer view.
Browser updates every 2 seconds via JS fetch('/data').

Configure in tulospalvelu:
    YHTEYS9=BRO:0/127.0.0.1
"""

import argparse
import json
import socket
import struct
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LISTEN_HOST = '0.0.0.0'
LISTEN_PORT = 15901
HTTP_HOST   = '0.0.0.0'
HTTP_PORT   = 8081
AWARDS_PORT = 8082  # second HTTP server: Top-3 per category for the awards desk
MATKA_PORT  = 8083  # third HTTP server: Top-N per distance+gender for the awards desk
MAX_VISIBLE = 20  # kept for reference; no longer used to trim the list
KILP_DAT_POLL_SEC = 5  # how often to re-read KILP.DAT for new competitors

# Control bytes (TpDef.h)
SOH = 0x01
STX = 0x02
ACK = 0x06
NAK = 0x15

# Packet class IDs (HkCom32.cpp)
PKGCLASS_ALKUT       = 0
PKGCLASS_KILPT       = 1
PKGCLASS_KILPPVT     = 2
PKGCLASS_VAIN_TULOST = 3
PKGCLASS_AIKAT       = 4
PKGCLASS_EMITT       = 6
PKGCLASS_SEURAT      = 7

# Wire layout offsets (empirically verified, matches web_results.py)
#   0-3:  lahetetty (INT32)
#   4-5:  portti    (INT16)
#   6:    id
#   7:    iid       (255 - id)
#   8:    pkgclass
#   9-10: len       (UINT16 LE)
#   11-12:checksum  (UINT16)
#   13+:  payload   (d union)
OFF_ID       = 6
OFF_IID      = 7
OFF_PKGCLASS = 8
OFF_LEN      = 9
OFF_CHECKSUM = 11
OFF_PAYLOAD  = 13

# VAIN_TULOST payload field offsets relative to OFF_PAYLOAD (d.v, pack=1)
VAIN_OFF_TARF   = 0   # char  tarf   (1 byte)
VAIN_OFF_PAKOTA = 1   # char  pakota (1 byte)
VAIN_OFF_DK     = 2   # INT16 dk
VAIN_OFF_BIB    = 4   # INT16 bib
VAIN_OFF_K_PV   = 6   # INT16 k_pv
VAIN_OFF_VALI   = 8   # INT16 vali  (-1=start, 0=finish, N=split)
VAIN_OFF_AIKA   = 10  # INT32 aika  (TOD ms from midnight)

MIN_DATAGRAM = OFF_PAYLOAD + VAIN_OFF_AIKA + 4  # 27 bytes

# KILPT payload offsets (ckilp[] starts at OFF_PAYLOAD+6)
CKILP_BASE      = OFF_PAYLOAD + 6   # = 19
KILP_OFF_KILPNO = 2    # INT16 LE — bib number
KILP_OFF_SNIMI  = 48   # wchar_t×25 — sukunimi, 50 bytes
KILP_OFF_ENIMI  = 98   # wchar_t×25 — etunimi,  50 bytes
KILP_OFF_SEURA  = 180  # wchar_t×32 — club,     64 bytes
KILP_OFF_SARJA  = 348  # INT16 LE  — 0-based category index
MIN_KILPT       = CKILP_BASE + KILP_OFF_SARJA + 2  # 369

KONETUNN = b'PY'

# ---------------------------------------------------------------------------
# Category lookup by class number (from KilpSrj.xml)
# ---------------------------------------------------------------------------

def load_sarjat_xml(path: str) -> tuple:
    """Parse KilpSrj.xml into ({class_no: (class_id, name)}, {class_no: valuku},
    {class_no: (gender, distance_km, distance_text)}).

    For each <Class ClassNo="N">: N (int) is the key. The first dict maps to
    (ClassId text, Name text stripped). The second dict maps to the class's
    split count (valuku) — the highest <Intermediary Order="K"> under the
    class's Race. Because the VAIN_TULOST `vali` index is class-relative (it
    equals the Intermediary Order for the competitor's own class — see
    HkDat.cpp: va[] is iterated 0..Sarjat[sarja].valuku[k_pv]), this K is the
    last / "lähestyminen" split index for that class.

    The third dict feeds the "Top matkoittain" awards view: gender is read
    from the first letter of ClassId (M/N convention, e.g. M63YL, N2140);
    distance is the class's own race distance, from the first
    Races/Race/Distance/Value found under the class (comma decimal separator
    converted to a float; the original text is kept too for display).
    """
    sarjat: dict = {}
    valuku: dict = {}
    extra: dict = {}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"Warning: cannot read {path}: {exc}", file=sys.stderr)
        return sarjat, valuku, extra

    for cls in root.iter('Class'):
        try:
            class_no = int(cls.get('ClassNo'))
        except (TypeError, ValueError):
            continue
        class_id   = (cls.findtext('ClassId') or '').strip()
        class_name = (cls.findtext('Name') or '').strip()
        sarjat[class_no] = (class_id, class_name)

        # Last split index = highest Intermediary Order under this class's
        # Race. max(Order) (not the element count) is robust if more than one
        # Race is present, since each Race re-numbers its intermediaries 1..K.
        orders = []
        for it in cls.findall('./Races/Race/Intermediaries/Intermediary'):
            try:
                orders.append(int(it.get('Order')))
            except (TypeError, ValueError):
                continue
        if orders:
            valuku[class_no] = max(orders)

        # Gender from the first letter of ClassId (M=miehet, N=naiset).
        gender = class_id[:1].upper() if class_id[:1].upper() in ('M', 'N') else ''

        # Distance from the first Race's Distance/Value (e.g. "63,6").
        dist_text = (cls.findtext('./Races/Race/Distance/Value') or '').strip()
        distance = None
        if dist_text:
            try:
                distance = float(dist_text.replace(',', '.'))
            except ValueError:
                distance = None
        if gender or distance is not None:
            extra[class_no] = (gender, distance, dist_text)

    if sarjat:
        print(f"Loaded {len(sarjat)} categories "
              f"({len(valuku)} with split counts, {len(extra)} with "
              f"gender/distance) from {path}", file=sys.stderr)
    else:
        print(f"Warning: no categories found in {path}", file=sys.stderr)
    return sarjat, valuku, extra

# ---------------------------------------------------------------------------
# Protocol helpers  (same as web_results.py)
# ---------------------------------------------------------------------------

def chksum(payload: bytes) -> int:
    """16-bit checksum: sum of UINT16 LE words (TpComY32.cpp:604)."""
    total = 0
    for i in range(0, len(payload) - 1, 2):
        total += struct.unpack_from('<H', payload, i)[0]
    if len(payload) % 2:
        total += payload[-1]
    return total & 0xFFFF


def build_raw_ack(packet_id: int) -> bytes:
    """4-byte ACK for read_UDPcli: [ACK][id][255-id][id]."""
    return bytes([ACK, packet_id, 255 - packet_id, packet_id])


def build_alkut_reply() -> bytes:
    """ALKUT reply with len=3 minimal-ack format (HkCom32.cpp:828)."""
    payload = b'\x01' + KONETUNN   # tunn=1, konetunn — 3 bytes
    return (
        bytes([1, 1, 254, PKGCLASS_ALKUT])
        + struct.pack('<H', 3)
        + struct.pack('<H', chksum(payload))
        + payload
    )


def wtext(data: bytes, abs_offset: int, max_chars: int) -> str:
    """Decode a null-terminated UTF-16-LE wchar_t string from a bytes buffer.

    Works on any bytes (UDP datagram or a KILP.DAT record slice) since it
    indexes purely by absolute offset.
    """
    raw = data[abs_offset : abs_offset + max_chars * 2]
    return raw.decode('utf-16-le', errors='replace').rstrip('\x00').rstrip('�')

# ---------------------------------------------------------------------------
# Competitor roster from KILP.DAT (dbboxm flat record store)
# ---------------------------------------------------------------------------

def load_kilp_dat(path: str) -> dict:
    """Read KILP.DAT into {bib: {'bib', 'name', 'club', 'sarja_idx'}}.

    Layout (verified against dbboxm/HkDat): record 0 is the dbboxm header;
    the uint16 at byte offset 6 is numrec (record count, incl. the header).
    The record length is not stored in the file, so it is derived as
    filesize // numrec. Records 1..numrec-1 each hold a packed kilptietue;
    a record is live when kilpstatus (INT16 @ rec+0) == 0 (nonzero = a
    deleted/free slot in the free list). The base fields use the same
    KILP_OFF_* offsets as the KILPT network packet, but applied from the
    record start — there is no CKILP_BASE prefix (that is network framing).
    """
    competitors: dict = {}
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError as exc:
        print(f"Warning: cannot read {path}: {exc}", file=sys.stderr)
        return competitors

    if len(data) < 8:
        print(f"Warning: {path} too short ({len(data)} bytes)", file=sys.stderr)
        return competitors

    numrec = struct.unpack_from('<H', data, 6)[0]   # header int2 == record count
    if numrec < 2:
        return competitors                          # only the header, no competitors
    reclen = len(data) // numrec
    if reclen < KILP_OFF_SARJA + 2:
        print(f"Warning: {path} reclen={reclen} too small to parse", file=sys.stderr)
        return competitors

    for r in range(1, numrec):
        base = r * reclen
        rec = data[base : base + reclen]
        if len(rec) < KILP_OFF_SARJA + 2:
            break                                   # truncated tail (torn read)
        if struct.unpack_from('<h', rec, 0)[0] != 0:
            continue                                # kilpstatus != 0 -> free slot
        bib = struct.unpack_from('<H', rec, KILP_OFF_KILPNO)[0]
        if bib <= 0:
            continue
        lastname  = wtext(rec, KILP_OFF_SNIMI, 25)
        firstname = wtext(rec, KILP_OFF_ENIMI, 25)
        club      = wtext(rec, KILP_OFF_SEURA, 32)
        sarja_idx = struct.unpack_from('<h', rec, KILP_OFF_SARJA)[0]
        competitors[bib] = {
            'bib':       bib,
            'name':      f"{firstname} {lastname}".strip(),
            'club':      club,
            'sarja_idx': sarja_idx,
        }
    return competitors


def _kilp_dat_loop(path: str) -> None:
    """Re-read KILP.DAT every KILP_DAT_POLL_SEC and merge into _competitors_by_bib.

    KILP.DAT only exposes the bib, so these entries go into the by-bib space
    only — matching the by-bib entries the UDP/KILPT handler writes — and
    freshly registered competitors appear without restarting Python. The by-dk
    space (populated only from live KILPT packets) is left untouched.
    """
    last_count = -1
    while True:
        competitors = load_kilp_dat(path)
        if competitors:
            with _lock:
                for bib, entry in competitors.items():
                    prev = _competitors_by_bib.get(bib)
                    # Same class-change guard as the KILPT handler (BUG 2): if
                    # this bib moved to a different class — e.g. a "jälki-ilmot"
                    # placeholder that just got a real name and real class via a
                    # KILP.DAT edit — its split was stored under the old class's
                    # vali index and is now stale. Discard so a fresh split can
                    # record under the correct class. dk is unknown on this path
                    # (KILP.DAT exposes only the bib), so pass None.
                    if prev is not None and prev.get('sarja_idx') != entry.get('sarja_idx'):
                        _discard_events_for_bib(bib, dk=None)
                    _competitors_by_bib[bib] = entry
            if len(competitors) != last_count:
                print(f"Loaded {len(competitors)} competitors from {path}",
                      file=sys.stderr)
                last_count = len(competitors)
        time.sleep(KILP_DAT_POLL_SEC)


def lookup_kilp_dat_bib(bib: int, path: str) -> dict:
    """Read KILP.DAT and return the freshest entry for one bib (or {}).

    Used for the on-demand approach-split refresh: re-parses the file and
    hands back just the requested bib's record so a late name correction in
    KILP.DAT is picked up the moment the runner reaches the last split. Every
    non-deleted record is returned as-is; Python makes no judgment about the
    name content.
    """
    if not path or bib <= 0:
        return {}
    return load_kilp_dat(path).get(bib, {})

# ---------------------------------------------------------------------------
# Shared state  (written by UDP thread, read by HTTP thread)
# ---------------------------------------------------------------------------

_lock                = threading.Lock()
# Two separate key spaces — never merged. dk is the sender's internal kilparr
# index; bib is the race number the operator assigns. They overlap numerically
# (a dk can equal an unrelated competitor's bib), so a single shared dict would
# alias two different people. Keep them apart and resolve bib-first for display.
_competitors_by_dk   = {}   # dk  → {bib, name, club, sarja_idx}
_competitors_by_bib  = {}   # bib → {bib, name, club, sarja_idx}
_events              = {}   # dk → {start_ms, split_ms, finish_ms, bib}
_arrival_order       = []   # dks newest-first (no cap); written under _lock
_sarjat              = {}   # class_no → (class_id, name)
_sarja_valuku        = {}   # class_no → last split index (valuku) from XML
_class_extra         = {}   # class_no → (gender 'M'/'N'/'', distance_km, distance_text)
_lahestyminen_split  = 2    # fallback vali number for classes missing from XML
_kilp_dat_path       = None # KILP.DAT path for on-demand approach-split lookups
awarded_categories   = set() # class_no (1-based ClassNo) whose prizes are handed out
                             # toggled from the /awards page; read under _lock
prizes_per_category  = 3     # podium depth AND the finisher threshold to show a
                             # card; 1-5, set via GET /awards/set_prizes?n=X;
                             # read/written under _lock

# --- "Top matkoittain" awards view (distance + gender, e.g. "Miehet 63,6 km") ---
awarded_groups          = set()  # group_id strings whose prizes are handed out
prizes_per_category_mtk = 3      # podium depth for the matka view; 1-5


def _resolve_comp(bib: int, dk: int) -> dict:
    """Competitor entry for display, bib-first.

    bib is the reliable identity — it is what the operator sees and assigns and
    what the finish-line crew calls out — so try _competitors_by_bib first and
    fall back to the internal dk index only when bib is unknown/unassigned.
    The stored entry is returned verbatim; Python makes no judgment about the
    name content (whatever is in KILP.DAT is what the operator wants shown).
    Caller must hold _lock.
    """
    if bib and bib in _competitors_by_bib:
        return _competitors_by_bib[bib]
    return _competitors_by_dk.get(dk, {})

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _elapsed_str(ms: int) -> str:
    neg = ms < 0
    ms  = abs(ms)
    s   = ms // 1000
    h, rem = divmod(s, 3600)
    m, s   = divmod(rem, 60)
    result = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return ("-" if neg else "") + result


def _format_ms(aika_ms, start_ms) -> str:
    if aika_ms is None:
        return ''
    elapsed = aika_ms - start_ms if start_ms is not None else aika_ms
    return _elapsed_str(elapsed)

# ---------------------------------------------------------------------------
# Build JSON payload for /data
# ---------------------------------------------------------------------------

def _build_data() -> dict:
    with _lock:
        # Rank finishers per category by elapsed finish time.
        cat_finishers: dict = {}
        for dk, ev in _events.items():
            if ev['finish_ms'] is not None:
                sarja_idx = _resolve_comp(ev.get('bib'), dk).get('sarja_idx', -1)
                elapsed = ev['finish_ms'] - (ev['start_ms'] or 0)
                cat_finishers.setdefault(sarja_idx, []).append((dk, elapsed))
        rank_map: dict = {}
        for lst in cat_finishers.values():
            lst.sort(key=lambda x: x[1])
            for i, (dk, _) in enumerate(lst):
                rank_map[dk] = i + 1

        # Rank competitors within category by approach split time (local fallback).
        # Overridden per-competitor by vasija from KILPPVT when available.
        cat_splitters: dict = {}
        for dk, ev in _events.items():
            if ev['split_ms'] is not None:
                sarja_idx = _resolve_comp(ev.get('bib'), dk).get('sarja_idx', -1)
                elapsed = ev['split_ms'] - (ev['start_ms'] or 0)
                cat_splitters.setdefault(sarja_idx, []).append((dk, elapsed))
        local_split_rank: dict = {}
        for lst in cat_splitters.values():
            lst.sort(key=lambda x: x[1])
            for i, (dk, _) in enumerate(lst):
                local_split_rank[dk] = i + 1

        newest_dk = _arrival_order[0] if _arrival_order else None

        rows = []
        for dk in _arrival_order:
            ev = _events.get(dk)
            if ev is None:
                continue
            # Fundamental display rule: a row appears ONLY when a real time
            # packet (approach split or finish) has arrived for it. A start-only
            # event, or a competitor known solely from KILPT/KILP.DAT, must not
            # show. _arrival_order is already populated only on split/finish,
            # but enforce it explicitly here so the rule is structural.
            if ev['split_ms'] is None and ev['finish_ms'] is None:
                continue
            comp = _resolve_comp(ev.get('bib'), dk)
            bib = comp.get('bib') or ev.get('bib') or dk
            # SARJA comes from sarja_idx (parsed from KILPT/KILP.DAT); blank
            # until the competitor is known, same as NIMI/SEURA. sarja_idx is
            # 0-based; _sarjat is keyed 1-based by ClassNo, hence the +1.
            sarja_idx = comp.get('sarja_idx', -1)
            _cid, cat = _sarjat.get(sarja_idx + 1, ('', ''))
            rows.append({
                'bib':        bib,
                'name':       comp.get('name', ''),
                'club':       comp.get('club', ''),
                'cat':        cat,
                'split':      _format_ms(ev['split_ms'], ev['start_ms']),
                'split_rank': local_split_rank.get(dk, ''),
                'finish':     _format_ms(ev['finish_ms'], ev['start_ms']),
                'rank':       rank_map.get(dk, ''),
                'has_finish': ev['finish_ms'] is not None,
                'is_newest':  dk == newest_dk,
            })

    return {'rows': rows}

# ---------------------------------------------------------------------------
# Awards view: Top-3 per category (only categories with a finisher)
# ---------------------------------------------------------------------------

def _build_awards(n: int) -> list:
    """Categories with at least one finisher, each with its top-``n``.

    Returns [{class_no, cat, awarded, top:[{bib,name,club,finish,elapsed}...]}...]
    sorted with not-yet-awarded categories first, then by category name. A
    category appears as soon as one competitor has finished; ``n``
    (prizes_per_category) only caps the display depth, so the podium shows
    however many have finished (1, 2, …) up to ``n``. Same finish ranking as
    _build_data (elapsed = finish - start). Caller must NOT hold _lock — this
    takes it.
    """
    with _lock:
        cat_finishers: dict = {}
        for dk, ev in _events.items():
            if ev['finish_ms'] is None:
                continue
            comp = _resolve_comp(ev.get('bib'), dk)
            sarja_idx = comp.get('sarja_idx', -1)
            cat_finishers.setdefault(sarja_idx, []).append({
                'bib':     comp.get('bib') or ev.get('bib') or dk,
                'name':    comp.get('name', ''),
                'club':    comp.get('club', ''),
                'elapsed': ev['finish_ms'] - (ev['start_ms'] or 0),
                'finish':  _format_ms(ev['finish_ms'], ev['start_ms']),
            })
        cats = []
        for sarja_idx, finishers in cat_finishers.items():
            # Visible as soon as anyone has finished; n caps only the depth.
            finishers.sort(key=lambda f: f['elapsed'])
            # sarja_idx is 0-based; _sarjat is keyed 1-based by ClassNo.
            class_no = sarja_idx + 1
            _cid, cat = _sarjat.get(class_no, ('', ''))
            cats.append({
                'class_no': class_no,
                'cat':      cat or (f'Sarja {class_no}' if class_no > 0
                                    else 'Tuntematon sarja'),
                'awarded':  class_no in awarded_categories,
                'top':      finishers[:n],
            })
    cats.sort(key=lambda c: (c['awarded'], c['cat'].lower()))
    return cats

# ---------------------------------------------------------------------------
# Awards view: Top-N per distance+gender, merged across all classes that
# share the same race distance and gender (e.g. all "63,6 km" men's classes
# — YL, 40v, 50v, ... — become one "Miehet 63,6 km" podium).
# ---------------------------------------------------------------------------

def _group_id(gender: str, distance) -> str:
    """Stable string key for a (gender, distance) group, used in URLs."""
    dist_key = f"{distance:.1f}" if distance is not None else 'x'
    return f"{gender or 'X'}_{dist_key}"


def _group_label(gender: str, distance, distance_text: str) -> str:
    who = 'Miehet' if gender == 'M' else 'Naiset' if gender == 'N' else 'Sarja'
    if distance_text:
        return f"{who} {distance_text} km"
    if distance is not None:
        return f"{who} {distance:g} km".replace('.', ',')
    return f"{who} (matka tuntematon)"


def _build_awards_by_distance(n: int) -> list:
    """Categories merged by (gender, distance), each with its top-``n``.

    Same shape/semantics as _build_awards (see that docstring), except the
    grouping key is the race distance + gender pulled from KilpSrj.xml
    instead of the raw sarja/class. Classes with no gender/distance info in
    the XML fall into a single 'Sarja (matka tuntematon)' catch-all group so
    they aren't silently dropped. Caller must NOT hold _lock — this takes it.
    """
    with _lock:
        group_finishers: dict = {}   # group_id -> list of finisher dicts
        group_meta: dict = {}        # group_id -> (label, awarded)
        for dk, ev in _events.items():
            if ev['finish_ms'] is None:
                continue
            comp = _resolve_comp(ev.get('bib'), dk)
            sarja_idx = comp.get('sarja_idx', -1)
            class_no = sarja_idx + 1
            gender, distance, dist_text = _class_extra.get(class_no, ('', None, ''))
            gid = _group_id(gender, distance)
            group_finishers.setdefault(gid, []).append({
                'bib':     comp.get('bib') or ev.get('bib') or dk,
                'name':    comp.get('name', ''),
                'club':    comp.get('club', ''),
                'elapsed': ev['finish_ms'] - (ev['start_ms'] or 0),
                'finish':  _format_ms(ev['finish_ms'], ev['start_ms']),
            })
            if gid not in group_meta:
                group_meta[gid] = _group_label(gender, distance, dist_text)

        groups = []
        for gid, finishers in group_finishers.items():
            finishers.sort(key=lambda f: f['elapsed'])
            groups.append({
                'group_id': gid,
                'cat':      group_meta[gid],
                'awarded':  gid in awarded_groups,
                'top':      finishers[:n],
            })
    groups.sort(key=lambda c: (c['awarded'], c['cat'].lower()))
    return groups

# ---------------------------------------------------------------------------
# HTML page  (static shell; table body filled by JS via /data)
# ---------------------------------------------------------------------------

_HTML = """\
<!DOCTYPE html>
<html lang="fi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kuuluttajalle</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #f5f5f5;
    color: #111;
    font-family: system-ui, -apple-system, sans-serif;
    font-size: 1.5rem;
    padding: 20px 24px;
  }
  h1 {
    font-size: 2rem;
    color: #222;
    margin-bottom: 16px;
    letter-spacing: -0.02em;
  }
  /* table-layout:fixed makes the per-column widths we set (and restore from
     localStorage) authoritative instead of content-driven. */
  table { width: 100%; border-collapse: collapse; table-layout: fixed; }
  thead tr { background: #333; }
  th {
    position: relative;   /* anchor for the absolutely-positioned resize handle */
    padding: 10px 14px;
    font-size: 1rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: #fff;
    white-space: nowrap;
    overflow: hidden;         /* clip long header text at the chosen width */
    text-overflow: ellipsis;
  }
  /* Drag target on the right edge of every header cell; invisible until hover. */
  .col-resize-handle {
    position: absolute;
    right: 0; top: 0;
    width: 5px; height: 100%;
    cursor: col-resize;
    background: transparent;
    user-select: none;
  }
  .col-resize-handle:hover { background: rgba(255, 255, 255, 0.5); }
  td { padding: 12px 14px; border-bottom: 1px solid #ddd; white-space: nowrap;
       overflow: hidden; text-overflow: ellipsis; }
  tbody tr:nth-child(even) td { background: #ececec; }

  /* Column alignment */
  th:first-child, td:first-child { text-align: right;  }
  th:nth-child(2), td:nth-child(2) { text-align: left; }
  th:nth-child(3), td:nth-child(3) { text-align: left; }
  th:nth-child(4), td:nth-child(4) { text-align: left; }
  th:nth-child(5), td:nth-child(5) { text-align: right; }
  th:nth-child(6), td:nth-child(6) { text-align: center; }
  th:nth-child(7), td:nth-child(7) { text-align: right; }
  th:last-child,  td:last-child  { text-align: center; }

  /* Finished */
  tr.finished td { background: #d4edda; }
  tr.finished .col-finish { color: #1a6e2a; font-weight: 700; }
  tr.finished .col-rank   { color: #1a6e2a; font-weight: 700; }

  /* Split rank cell colours */
  .split-rank-1    { background: #00e676; color: #000; font-weight: 700; }
  .split-rank-top3 { background: #ffee00; color: #000; font-weight: 700; }
  .split-rank-rest { background: #fff;    color: #000; }

  .col-bib    { font-family: ui-monospace, monospace; color: #555; }
  .col-name   { font-weight: 600; }
  .col-club   { color: #555; }
  .col-cat    { color: #445; }
  .col-split       { font-family: ui-monospace, monospace; }
  .col-split-rank  { min-width: 3ch; }
  .col-finish      { font-family: ui-monospace, monospace; }
  .col-rank        { min-width: 3ch; }

  .empty {
    text-align: center;
    color: #888;
    padding: 80px;
    font-style: italic;
    font-size: 1.3rem;
  }
</style>
</head>
<body>
<h1>Kuuluttajalle</h1>
<table>
  <thead>
    <tr>
      <th data-col="NO">No<span class="col-resize-handle"></span></th>
      <th data-col="NIMI">Nimi<span class="col-resize-handle"></span></th>
      <th data-col="SEURA">Seura<span class="col-resize-handle"></span></th>
      <th data-col="SARJA">Sarja<span class="col-resize-handle"></span></th>
      <th data-col="LAHESTYMINEN">L&auml;hestyminen<span class="col-resize-handle"></span></th>
      <th data-col="SIJA_SPLIT">Sija<span class="col-resize-handle"></span></th>
      <th data-col="MAALI">Maali<span class="col-resize-handle"></span></th>
      <th data-col="SIJA_FINISH">Sija<span class="col-resize-handle"></span></th>
    </tr>
  </thead>
  <tbody id="tbody">
    <tr><td colspan="8" class="empty">Odotetaan kilpailijoita…</td></tr>
  </tbody>
</table>
<script>
  const tbody = document.getElementById('tbody');

  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function splitRankClass(rank) {
    if (rank === 1)                    return 'split-rank-1';
    if (rank >= 2 && rank <= 3)        return 'split-rank-top3';
    if (typeof rank === 'number' && rank >= 4) return 'split-rank-rest';
    return '';
  }

  function update() {
    fetch('/data')
      .then(r => r.json())
      .then(data => {
        if (!data.rows.length) {
          tbody.innerHTML =
            '<tr><td colspan="8" class="empty">Odotetaan kilpailijoita…</td></tr>';
          return;
        }
        tbody.innerHTML = data.rows.map(r => {
          const cls = r.has_finish ? 'finished' : '';
          const srCls = splitRankClass(r.split_rank);
          return '<tr class="' + cls + '">'
            + '<td class="col-bib">'    + esc(r.bib)    + '</td>'
            + '<td class="col-name">'   + esc(r.name)   + '</td>'
            + '<td class="col-club">'   + esc(r.club)   + '</td>'
            + '<td class="col-cat">'    + esc(r.cat)    + '</td>'
            + '<td class="col-split">'      + esc(r.split)      + '</td>'
            + '<td class="col-split-rank ' + srCls + '">' + esc(r.split_rank) + '</td>'
            + '<td class="col-finish">'     + esc(r.finish)     + '</td>'
            + '<td class="col-rank">'       + esc(r.rank)       + '</td>'
            + '</tr>';
        }).join('');
      })
      .catch(() => {});   // stay silent on network hiccups
  }

  update();
  setInterval(update, 2000);

  // ---- Drag-to-resize columns (pure JS, widths persisted in localStorage) ----
  // Only the thead cells carry a data-col key; table-layout:fixed then makes the
  // whole column follow the header width. The tbody is re-rendered every 2s, but
  // the thead is untouched, so the widths (and their handles) survive refreshes.
  (function () {
    const PREFIX = 'col_';
    const ths = document.querySelectorAll('thead th[data-col]');

    // Restore any saved widths on load.
    ths.forEach(th => {
      const saved = localStorage.getItem(PREFIX + th.dataset.col);
      if (saved) th.style.width = saved + 'px';
    });

    let target = null, startX = 0, startW = 0;

    ths.forEach(th => {
      const handle = th.querySelector('.col-resize-handle');
      if (!handle) return;
      handle.addEventListener('mousedown', e => {
        target = th;
        startX = e.pageX;
        startW = th.getBoundingClientRect().width;
        // Suppress text selection / show the resize cursor while dragging.
        document.body.style.userSelect = 'none';
        document.body.style.cursor = 'col-resize';
        e.preventDefault();
      });
    });

    document.addEventListener('mousemove', e => {
      if (!target) return;
      const w = Math.max(40, startW + (e.pageX - startX));  // 40px floor
      target.style.width = w + 'px';
    });

    document.addEventListener('mouseup', () => {
      if (!target) return;
      const w = parseInt(target.style.width, 10);
      if (w) localStorage.setItem(PREFIX + target.dataset.col, w);
      target = null;
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
    });
  })();
</script>
</body>
</html>
"""

_HTML_BYTES = _HTML.encode('utf-8')

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == '/':
            self._respond(200, 'text/html; charset=utf-8', _HTML_BYTES)
        elif self.path == '/data':
            payload = json.dumps(_build_data()).encode('utf-8')
            self._respond(200, 'application/json', payload)
        else:
            self._respond(404, 'text/plain', b'Not found')

    def _respond(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

# ---------------------------------------------------------------------------
# Awards page  (JS-driven; polls /awards/data every 5s)
# ---------------------------------------------------------------------------
#
# Switched from server-render + <meta refresh> to client-side polling so JS
# state survives between refreshes. That persistence is what makes the two new
# live features possible: the browser notification fires when an *awarded*
# category's top-N changed since the previous poll, and a card header flashes
# when its category was absent from the previous poll (newly ready to award).
# Both diff against JS variables that a full page reload would have wiped.

_AWARDS_HTML = """\
<!DOCTYPE html>
<html lang="fi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Palkinnot</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #f5f5f5; color: #111;
    font-family: system-ui, -apple-system, sans-serif;
    font-size: 1.25rem; padding: 20px 24px;
  }
  h1 { font-size: 1.9rem; margin-bottom: 12px; letter-spacing: -0.02em; }
  .settings {
    display: flex; align-items: center; gap: 10px;
    background: #fff; border: 1px solid #ddd; border-radius: 8px;
    padding: 10px 16px; margin-bottom: 18px; font-size: 1.1rem;
  }
  .settings .n { min-width: 2ch; text-align: center;
                 font-weight: 700; font-size: 1.3rem; }
  .settings button {
    width: 2.2rem; height: 2.2rem; font-size: 1.4rem; line-height: 1;
    border: 1px solid #bbb; border-radius: 6px; background: #f0f0f0;
    cursor: pointer;
  }
  .settings button:hover { background: #e2e2e2; }
  .grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 18px; }
  @media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
  .cat {
    background: #fff; border: 1px solid #ddd; border-radius: 8px;
    padding: 14px 18px;
  }
  .cat-head { display: flex; align-items: center; justify-content: space-between;
              gap: 16px; margin-bottom: 8px; border-radius: 6px; padding: 4px 6px; }
  .cat-head h2 { font-size: 1.4rem; color: #222; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-size: 0.85rem; text-transform: uppercase;
       letter-spacing: 0.06em; color: #666; padding: 4px 10px; }
  td { padding: 6px 10px; border-bottom: 1px solid #eee; white-space: nowrap; }
  td.pos    { color: #888; width: 2ch; }
  td.bib    { font-family: ui-monospace, monospace; color: #555; text-align: right; width: 5ch; }
  td.name   { font-weight: 600; }
  td.finish { font-family: ui-monospace, monospace; text-align: right; }
  label.chk { font-size: 1.05rem; font-weight: 600; cursor: pointer;
              white-space: nowrap; user-select: none; }
  label.chk input { transform: scale(1.5); margin-right: 8px; vertical-align: middle; }
  /* Awarded categories: greyed out and struck through */
  .cat.awarded { opacity: 0.5; background: #efefef; }
  .cat.awarded h2 { text-decoration: line-through; }
  .cat.awarded td.name { text-decoration: line-through; }
  /* Newly ready-to-award category: flash the header orange/yellow a few times */
  @keyframes flash {
    0%, 100% { background: transparent; }
    25%      { background: #ffb300; }
    50%      { background: #ffee00; }
    75%      { background: #ffb300; }
  }
  .cat-head.flash { animation: flash 0.8s ease-in-out 4; }
  .empty { text-align: center; color: #888; padding: 60px; font-style: italic; }
</style>
</head>
<body>
<h1>Palkinnot &mdash; Top-N sarjoittain</h1>
<div class="settings">
  <span>Palkintoja per sarja:</span>
  <button type="button" onclick="setPrizes(-1)">&minus;</button>
  <span class="n" id="prizeCount">3</span>
  <button type="button" onclick="setPrizes(1)">+</button>
</div>
<div id="cats"><p class="empty">Ladataan&hellip;</p></div>
<script>
  let prizes     = 3;
  let seenCats   = null;   // Set of class_no seen last poll (null = first load)
  let prevTopSig = {};     // class_no -> signature of its top-N (awarded diff)

  // Ask for notification permission once, on load.
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // Signature of a top-N list: bib@finish per row. Changes when a finisher is
  // displaced from the podium or a podium finish time is corrected.
  function sigOf(top) {
    return top.map(f => f.bib + '@' + f.finish).join('|');
  }

  function notify(msg) {
    if ('Notification' in window && Notification.permission === 'granted') {
      new Notification(msg);
    }
  }

  function setPrizes(delta) {
    const n = Math.max(1, Math.min(5, prizes + delta));
    fetch('/awards/set_prizes?n=' + n).then(() => poll()).catch(() => {});
  }

  function toggleCat(classNo) {
    fetch('/awards/toggle?class_no=' + classNo, { method: 'POST' })
      .then(() => poll()).catch(() => {});
  }

  function render(data) {
    prizes = data.prizes;
    document.getElementById('prizeCount').textContent = prizes;
    const cats = data.cats;
    const container = document.getElementById('cats');

    if (!cats.length) {
      container.innerHTML =
        '<p class="empty">Ei viel&auml; palkittavia sarjoja.</p>';
    } else {
      container.innerHTML = '<div class="grid">' + cats.map(c => {
        // "New" = this class_no was absent from the previous poll cycle. Never
        // on the very first load (seenCats === null) so the whole board doesn't
        // flash at startup.
        const isNew   = seenCats !== null && !seenCats.has(c.class_no);
        const flash   = isNew ? ' flash' : '';
        const awarded = c.awarded ? ' awarded' : '';
        const rows = c.top.map((f, i) =>
          '<tr><td class="pos">' + (i + 1) + '.</td>'
          + '<td class="bib">'    + esc(f.bib)    + '</td>'
          + '<td class="name">'   + esc(f.name)   + '</td>'
          + '<td class="finish">' + esc(f.finish) + '</td></tr>').join('');
        return '<section class="cat' + awarded + '">'
          + '<div class="cat-head' + flash + '">'
          + '<h2>' + esc(c.cat) + '</h2>'
          + '<label class="chk"><input type="checkbox" ' + (c.awarded ? 'checked' : '')
          + ' onchange="toggleCat(' + c.class_no + ')"> Palkinnot jaettu</label>'
          + '</div>'
          + '<table><thead><tr><th>#</th><th>No</th><th>Nimi</th><th>Maali</th></tr></thead>'
          + '<tbody>' + rows + '</tbody></table>'
          + '</section>';
      }).join('') + '</div>';
    }

    // Notify when an already-awarded category's top-N changed vs. last poll —
    // a new finisher displaced someone on the podium, or a time was corrected.
    const newSig = {};
    cats.forEach(c => {
      const s = sigOf(c.top);
      newSig[c.class_no] = s;
      if (c.awarded && prevTopSig[c.class_no] !== undefined
          && prevTopSig[c.class_no] !== s) {
        notify('\\u26a0\\ufe0f ' + c.cat + ': uusi tulos palkintosijalla!');
      }
    });
    prevTopSig = newSig;
    seenCats   = new Set(cats.map(c => c.class_no));
  }

  function poll() {
    fetch('/awards/data').then(r => r.json()).then(render).catch(() => {});
  }

  poll();
  setInterval(poll, 5000);
</script>
</body>
</html>
"""

_AWARDS_HTML_BYTES = _AWARDS_HTML.encode('utf-8')


class _AwardsHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ('/', '/awards'):
            self._respond(200, 'text/html; charset=utf-8', _AWARDS_HTML_BYTES)
        elif parsed.path == '/awards/data':
            with _lock:
                n = prizes_per_category
            payload = json.dumps(
                {'prizes': n, 'cats': _build_awards(n)}).encode('utf-8')
            self._respond(200, 'application/json', payload)
        elif parsed.path == '/awards/set_prizes':
            self._set_prizes(parsed)
        else:
            self._respond(404, 'text/plain', b'Not found')

    def _set_prizes(self, parsed) -> None:
        """GET /awards/set_prizes?n=X — set the podium depth / award threshold."""
        global prizes_per_category
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            n = int(qs.get('n', ['3'])[0])
        except (TypeError, ValueError):
            n = 3
        n = max(1, min(5, n))                # clamp to the supported 1-5 range
        with _lock:
            prizes_per_category = n
        self._respond(200, 'application/json',
                      json.dumps({'prizes': n}).encode('utf-8'))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get('Content-Length') or 0)
        if length:
            self.rfile.read(length)          # drain body so the socket is clean
        if parsed.path == '/awards/toggle':
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                class_no = int(qs.get('class_no', ['0'])[0])
            except (TypeError, ValueError):
                class_no = 0
            awarded = False
            if class_no:
                with _lock:
                    if class_no in awarded_categories:
                        awarded_categories.discard(class_no)
                    else:
                        awarded_categories.add(class_no)
                        awarded = True
            # JS-driven page: return the new state as JSON; the client re-polls.
            self._respond(200, 'application/json',
                          json.dumps({'class_no': class_no,
                                      'awarded': awarded}).encode('utf-8'))
        else:
            self._respond(404, 'text/plain', b'Not found')

    def _respond(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

# ---------------------------------------------------------------------------
# Matka-awards page (JS-driven; polls /awards/data every 5s) — identical
# behaviour/markup to the per-sarja Palkinnot page above, just re-titled and
# pointed at the distance+gender grouping. Kept as a full copy (rather than
# templating one HTML string) so the two pages can diverge freely later
# without threading a "mode" flag through the JS.
# ---------------------------------------------------------------------------

_AWARDS_MTK_HTML = _AWARDS_HTML.replace(
    '<title>Palkinnot</title>', '<title>Palkinnot - matkoittain</title>'
).replace(
    '<h1>Palkinnot &mdash; Top-N sarjoittain</h1>',
    '<h1>Palkinnot &mdash; Top-N matkoittain</h1>'
).replace(
    "onchange=\"toggleCat(' + c.class_no + ')\"",
    "onchange=\"toggleCat(&quot;' + c.group_id + '&quot;)\""
).replace(
    "newSig[c.class_no] = s;",
    "newSig[c.group_id] = s;"
).replace(
    "if (c.awarded && prevTopSig[c.class_no] !== undefined\n          && prevTopSig[c.class_no] !== s) {",
    "if (c.awarded && prevTopSig[c.group_id] !== undefined\n          && prevTopSig[c.group_id] !== s) {"
).replace(
    "!seenCats.has(c.class_no)",
    "!seenCats.has(c.group_id)"
).replace(
    "seenCats   = new Set(cats.map(c => c.class_no));",
    "seenCats   = new Set(cats.map(c => c.group_id));"
).replace(
    "function toggleCat(classNo) {\n    fetch('/awards/toggle?class_no=' + classNo, { method: 'POST' })",
    "function toggleCat(groupId) {\n    fetch('/awards/toggle?group_id=' + encodeURIComponent(groupId), { method: 'POST' })"
).replace(
    "// Set of class_no seen last poll", "// Set of group_id seen last poll"
).replace(
    "// class_no -> signature", "// group_id -> signature"
).replace(
    "this class_no was absent", "this group_id was absent"
)

_AWARDS_MTK_HTML_BYTES = _AWARDS_MTK_HTML.encode('utf-8')


class _AwardsMatkaHandler(BaseHTTPRequestHandler):
    """Same routes as _AwardsHandler, but grouped by distance+gender."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ('/', '/awards'):
            self._respond(200, 'text/html; charset=utf-8', _AWARDS_MTK_HTML_BYTES)
        elif parsed.path == '/awards/data':
            with _lock:
                n = prizes_per_category_mtk
            payload = json.dumps(
                {'prizes': n, 'cats': _build_awards_by_distance(n)}).encode('utf-8')
            self._respond(200, 'application/json', payload)
        elif parsed.path == '/awards/set_prizes':
            self._set_prizes(parsed)
        else:
            self._respond(404, 'text/plain', b'Not found')

    def _set_prizes(self, parsed) -> None:
        """GET /awards/set_prizes?n=X — set the podium depth / award threshold."""
        global prizes_per_category_mtk
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            n = int(qs.get('n', ['3'])[0])
        except (TypeError, ValueError):
            n = 3
        n = max(1, min(5, n))
        with _lock:
            prizes_per_category_mtk = n
        self._respond(200, 'application/json',
                      json.dumps({'prizes': n}).encode('utf-8'))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get('Content-Length') or 0)
        if length:
            self.rfile.read(length)          # drain body so the socket is clean
        if parsed.path == '/awards/toggle':
            qs = urllib.parse.parse_qs(parsed.query)
            group_id = (qs.get('group_id', [''])[0] or '').strip()
            awarded = False
            if group_id:
                with _lock:
                    if group_id in awarded_groups:
                        awarded_groups.discard(group_id)
                    else:
                        awarded_groups.add(group_id)
                        awarded = True
            self._respond(200, 'application/json',
                          json.dumps({'group_id': group_id,
                                      'awarded': awarded}).encode('utf-8'))
        else:
            self._respond(404, 'text/plain', b'Not found')

    def _respond(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

# ---------------------------------------------------------------------------
# UDP listener  (daemon thread)
# ---------------------------------------------------------------------------

def _add_to_display(dk: int) -> None:
    if dk not in _arrival_order:
        _arrival_order.insert(0, dk)


def _discard_events_for_bib(bib: int, dk: int = None) -> None:
    """Drop any stored start/split/finish events for a bib (and its dk key).

    Called when a competitor's class just changed, so a split stored under the
    stale (old-class) vali index cannot be resurrected and shown as a fresh
    approach event. Events are keyed by dk, so match on the event's own 'bib'
    field; also drop the incoming dk key directly when one is known. The
    KILP.DAT poll path has no dk (KILP.DAT exposes only the bib), so it passes
    dk=None and relies on the 'bib'-field match. Caller must hold _lock.
    """
    stale = {k for k, ev in _events.items() if ev.get('bib') == bib}
    if dk is not None:
        stale.add(dk)
    for k in stale:
        _events.pop(k, None)
        if k in _arrival_order:
            _arrival_order.remove(k)


def _udp_loop() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((LISTEN_HOST, LISTEN_PORT))
    except OSError as exc:
        print(f"Cannot bind UDP port {LISTEN_PORT}: {exc}", file=sys.stderr)
        return

    print(f"UDP listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)

    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except OSError as exc:
            print(f"UDP socket error: {exc}", file=sys.stderr)
            continue

        # Short server-direction frames (4-byte prefix: 00 00 konetn)
        if len(data) < MIN_DATAGRAM and len(data) >= 5 and data[0] == 0 and data[1] == 0:
            continue

        if len(data) < 3 or data[0] != STX:
            print(f"DROP len={len(data)} byte0=0x{data[0]:02x} hex={data[:16].hex()}", flush=True)
            continue

        # 6-byte NAK keepalive
        if len(data) == 6 and data[5] == NAK:
            continue

        print(f"PKT len={len(data)} d0={data[0]:02x} d6={data[6]:02x} d7={data[7]:02x} d6+d7={data[6]+data[7]} d8={data[8] if len(data)>8 else 'N/A'}", flush=True)
        try:
            _handle_packet(sock, data, addr)
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            print(f"PACKET ERROR from {addr}: {exc}\n{tb}", flush=True)
            print(f"PACKET ERROR from {addr}: {exc}\n{tb}", file=sys.stderr, flush=True)


def _handle_packet(sock: socket.socket, data: bytes, addr) -> None:
    reply_port = struct.unpack('<H', data[1:3])[0]

    if len(data) < OFF_PKGCLASS + 1:
        return

    id_byte  = data[OFF_ID]
    iid_byte = data[OFF_IID]
    if (id_byte + iid_byte) != 255:
        return

    pkgclass = data[OFF_PKGCLASS]

    if pkgclass == PKGCLASS_ALKUT:
        reply = build_alkut_reply()
        sock.sendto(reply, (addr[0], reply_port))
        raw_ack = build_raw_ack(data[OFF_ID])
        time.sleep(0.1)
        sock.sendto(raw_ack, addr)
        return

    sock.sendto(build_raw_ack(data[OFF_ID]), addr)

    if pkgclass == PKGCLASS_KILPT:
        if len(data) < MIN_KILPT:
            print(f"KILPT dropped: len={len(data)} < MIN_KILPT={MIN_KILPT}", flush=True)
            return
        dk        = struct.unpack_from('<h', data, OFF_PAYLOAD + VAIN_OFF_DK)[0]
        b         = CKILP_BASE
        bib       = struct.unpack_from('<h', data, b + KILP_OFF_KILPNO)[0]
        lastname  = wtext(data, b + KILP_OFF_SNIMI, 25)
        firstname = wtext(data, b + KILP_OFF_ENIMI, 25)
        club      = wtext(data, b + KILP_OFF_SEURA, 32)
        sarja_idx = struct.unpack_from('<h', data, b + KILP_OFF_SARJA)[0]
        full_name = f"{firstname} {lastname}".strip()
        print(f"KILPT dk={dk} bib={bib} name={full_name!r} club={club!r}", flush=True)
        entry = {'bib': bib, 'name': full_name, 'club': club, 'sarja_idx': sarja_idx}
        with _lock:
            prev = _competitors_by_bib.get(bib) if bib > 0 else None
            # BUG 2: the competitor moved to a different class. The old split
            # was stored under the previous class's vali index, which may not
            # be the new class's approach split index. Discard so a mismatched
            # split can't linger on the board. (This is a data-consistency
            # guard, not a name judgment — it fires purely on sarja_idx.)
            if prev is not None and prev.get('sarja_idx') != sarja_idx:
                _discard_events_for_bib(bib, dk)
            _competitors_by_dk[dk] = entry
            if bib > 0:
                _competitors_by_bib[bib] = entry
        return

    if pkgclass == PKGCLASS_VAIN_TULOST:
        payload_len = struct.unpack_from('<H', data, OFF_LEN)[0]
        if len(data) < OFF_PAYLOAD + payload_len:
            return
        p        = OFF_PAYLOAD
        dk       = struct.unpack_from('<h', data, p + VAIN_OFF_DK)[0]
        vali_bib = struct.unpack_from('<h', data, p + VAIN_OFF_BIB)[0]
        vali     = struct.unpack_from('<h', data, p + VAIN_OFF_VALI)[0]
        aika     = struct.unpack_from('<i', data, p + VAIN_OFF_AIKA)[0]
        with _lock:
            # Events are keyed by dk (always present and unique per competitor),
            # so start/split/finish for one runner accumulate into one record.
            # Display info is resolved separately, bib-first, to dodge collisions.
            comp = _resolve_comp(vali_bib, dk)
            has_comp = bool(comp)
            key = dk
            if not has_comp:
                print(f"VAIN_TULOST dk={dk} bib={vali_bib} vali={vali} comp_known=False "
                      f"raw15={data[15]:02x}{data[16]:02x} "
                      f"known_bibs={sorted(_competitors_by_bib)} "
                      f"known_dks={sorted(_competitors_by_dk)}", flush=True)
            else:
                print(f"VAIN_TULOST dk={dk} bib={vali_bib} vali={vali} comp_known=True key={key}", flush=True)
            if key not in _events:
                _events[key] = {
                    'start_ms':  None,
                    'split_ms':  None,
                    'finish_ms': None,
                    'bib':       vali_bib,
                }
            elif vali_bib > 0:
                _events[key]['bib'] = vali_bib
            ev = _events[key]

            if vali == -1:
                ev['start_ms'] = aika
            elif vali == 0:
                ev['finish_ms'] = aika
                _add_to_display(key)
            else:
                # The "Lähestyminen" split is the competitor's own last split.
                # vali is class-relative (== Intermediary Order for this
                # competitor's class), so resolve the approach index from the
                # class's valuku; fall back to the global CLI value when the
                # class is unknown or absent from the XML. sarja_idx is 0-based;
                # _sarja_valuku is keyed 1-based by ClassNo, hence the +1.
                sarja_idx = comp.get('sarja_idx', -1)
                lahestyminen = _sarja_valuku.get(sarja_idx + 1, _lahestyminen_split)
                if vali == lahestyminen:
                    # Approach split — always re-read KILP.DAT for this bib so a
                    # name/club/sarja correction made after startup is reflected.
                    # Only on this split (names are settled by the last few
                    # hundred metres), and only when KILP.DAT is configured.
                    if _kilp_dat_path:
                        fresh = lookup_kilp_dat_bib(vali_bib, _kilp_dat_path)
                        if fresh:
                            _competitors_by_bib[vali_bib] = fresh
                    ev['split_ms'] = aika
                    _add_to_display(key)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _sarjat, _sarja_valuku, _class_extra, _lahestyminen_split, _kilp_dat_path
    parser = argparse.ArgumentParser(description='Announcer display for tulospalvelu')
    parser.add_argument('--sarjat-xml', metavar='FILE',
                        help='KilpSrj.xml with category definitions (ClassNo/ClassId/Name)')
    parser.add_argument('--lahestyminen', metavar='N', type=int, default=2,
                        help='fallback Lähestyminen split number for classes not '
                             'found in --sarjat-xml (default: 2). When the XML is '
                             'present, each class uses its own last split index.')
    parser.add_argument('--kilp-dat', metavar='PATH', default='KILP.DAT',
                        help=f'KILP.DAT competitor store to poll every '
                             f'{KILP_DAT_POLL_SEC}s for new competitors '
                             f'(default: KILP.DAT; pass empty to disable)')
    parser.add_argument('--awards-port', metavar='PORT', type=int, default=AWARDS_PORT,
                        help=f'port for the Top-3-per-category awards page '
                             f'(default: {AWARDS_PORT})')
    parser.add_argument('--matka-port', metavar='PORT', type=int, default=MATKA_PORT,
                        help=f'port for the Top-N-per-distance/gender awards page '
                             f'(default: {MATKA_PORT})')
    args = parser.parse_args()
    if args.sarjat_xml:
        _sarjat, _sarja_valuku, _class_extra = load_sarjat_xml(args.sarjat_xml)
    else:
        _sarjat, _sarja_valuku, _class_extra = {}, {}, {}
    _lahestyminen_split = args.lahestyminen
    _kilp_dat_path      = args.kilp_dat or None
    print(f"Lähestyminen split: per-class from XML "
          f"({len(_sarja_valuku)} classes), fallback {_lahestyminen_split}",
          file=sys.stderr)

    udp_thread = threading.Thread(target=_udp_loop, daemon=True, name='udp')
    udp_thread.start()

    if args.kilp_dat:
        kilp_thread = threading.Thread(target=_kilp_dat_loop, args=(args.kilp_dat,),
                                       daemon=True, name='kilp-dat')
        kilp_thread.start()

    # Second HTTP server: Top-3-per-category awards page, own daemon thread.
    awards_server = HTTPServer((HTTP_HOST, args.awards_port), _AwardsHandler)
    awards_thread = threading.Thread(target=awards_server.serve_forever,
                                     daemon=True, name='awards-http')
    awards_thread.start()
    print(f"Awards server on http://localhost:{args.awards_port}/", flush=True)

    # Third HTTP server: Top-N-per-distance/gender awards page ("matkoittain").
    matka_server = HTTPServer((HTTP_HOST, args.matka_port), _AwardsMatkaHandler)
    matka_thread = threading.Thread(target=matka_server.serve_forever,
                                    daemon=True, name='matka-http')
    matka_thread.start()
    print(f"Matka-awards server on http://localhost:{args.matka_port}/", flush=True)

    server = HTTPServer((HTTP_HOST, HTTP_PORT), _Handler)
    print(f"HTTP server on http://localhost:{HTTP_PORT}/  (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        awards_server.server_close()
        matka_server.server_close()


if __name__ == '__main__':
    main()
