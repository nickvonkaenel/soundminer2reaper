#!/usr/bin/env python3
"""
sm2reaper -- Convert Soundminer plugin-preset databases into Reaper FX chains.

Reads a Soundminer `pluginpresets.sqlite` database, parses each preset's plugin
"rack" (stored as an Apple plist inside the `human_data` column), and emits one
Reaper `.RfxChain` file per preset, mirroring the Soundminer folder tree.

Each plugin slot in a rack is a VST2 plugin with its opaque state chunk. That
chunk is wrapped in Reaper's VST2 state-container format (reverse-engineered from
Reaper `.ini` FX-preset banks and real `.RfxChain` files) and written into a
`<VST ...>` FX block.

Caveat: the presets reference plugins by their VST2 identity. A generated chain
only auto-loads in a Reaper install that actually has the *same VST2 plugin*
registered (the opaque chunk state itself is cross-platform). Plugins that aren't
installed will show "offline" in Reaper, but their state is preserved and they
relink once the plugin is present.

Pure standard library (sqlite3, plistlib, base64, struct). Python 3.8+.
"""

from __future__ import annotations

import argparse
import base64
import glob
import os
import plistlib
import re
import sqlite3
import struct
import sys
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# REAPER VST2 container constants, verified against REAPER preset banks and
# generated FX-chain files.
# ---------------------------------------------------------------------------

REAPER_VST_MARKER = bytes.fromhex("ee5eedfe")  # 0xFEED5EEE, little-endian
# Pin/routing header for a standard 2-in / 2-out plugin. For plugins found in the
# user's presets/*.ini we substitute that plugin's exact header instead.
STEREO_HEADER = (2, 1, 0, 2, 0, 2, 1, 0, 2, 0)
POST_LEN_INTS = (1, 0x00100000)  # hasChunk flag + fixed flags word, follow chunklen
BODY_GROUP = 96  # Reaper base64-encodes the chunk body in 96-byte groups per line

# XML 1.0 forbids most C0 control chars even inside <string>; strip them so the
# plist parses. The real 4-char fourcc is recovered from the descriptor's uid hex.
_BAD_XML_CTRL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PluginSlot:
    product: str
    vendor: str
    uid_int: int             # VST2 unique id as a 32-bit int
    chunk: bytes             # opaque effGetChunk() bytes
    bypassed: bool = False
    num_in: int = 2
    num_out: int = 2
    slot: int = 0
    uid_source: str = ""     # where the uid came from (diagnostics)
    is_waves_shell: bool = False

    @property
    def ondisk_fourcc(self) -> bytes:
        """Reaper writes the id as the little-endian bytes of the int, e.g. b'cDtS'."""
        return struct.pack("<I", self.uid_int)


@dataclass
class Preset:
    id: int
    name: str
    folder: str              # folder title, or "" for root
    slots: list = field(default_factory=list)
    notes: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Plist / Soundminer parsing
# ---------------------------------------------------------------------------

def _load_plist(raw: bytes):
    """Parse a Soundminer human_data plist, recovering from bad control bytes."""
    try:
        return plistlib.loads(raw)
    except Exception:
        cleaned = _BAD_XML_CTRL.sub(b" ", raw)
        return plistlib.loads(cleaned)


def _uid_from_hex(uid_hex: str):
    """'4e6f4566' -> 0x4e6f4566. Returns None on malformed input."""
    try:
        b = bytes.fromhex(uid_hex.strip())
    except ValueError:
        return None
    return int.from_bytes(b, "big") if len(b) == 4 else None


def _descriptor_uid(plug_data) -> str:
    """Pull uid="...." out of a v5 plug_data.plugin_descriptor XML string."""
    if not isinstance(plug_data, dict):
        return ""
    desc = plug_data.get("plugin_descriptor", "")
    if isinstance(desc, bytes):
        desc = desc.decode("utf-8", "replace")
    m = re.search(r'uid="([0-9A-Fa-f]{8})"', desc or "")
    return m.group(1) if m else ""


def _parse_plugindata(pd):
    """Inspect a v5 plug_data.plugindata blob (a VST .fxb / AU class-info dump).

    Returns (kind, fxID_bytes|None, chunk|None):
      'FBCh'  -> VST2 bank carrying an opaque effGetChunk() blob (chunk returned)
      'param' -> VST2 bank of float programs, no opaque chunk (FxBk/FxCk)
      'au'    -> binary-plist AudioUnit state (Mac-only; not convertible)
      'none' / 'unknown' otherwise
    """
    if not isinstance(pd, dict):
        return "none", None, None
    dat = pd.get("plugindata")
    if not isinstance(dat, (bytes, bytearray)) or len(dat) < 20:
        return "none", None, None
    dat = bytes(dat)
    if dat[:4] == b"CcnK":  # VST .fxb / .fxp (big-endian fields)
        fx = dat[8:12]
        fx_id = dat[16:20]
        if fx == b"FBCh" and len(dat) >= 160:  # bank with opaque chunk
            try:
                csz = struct.unpack(">i", dat[156:160])[0]
            except struct.error:
                return "FBCh", fx_id, None
            if 0 <= csz <= len(dat) - 160:
                return "FBCh", fx_id, dat[160:160 + csz]
            return "FBCh", fx_id, None
        return "param", fx_id, None  # FxBk / FxCk: float programs, no chunk
    if dat[:4] == b"bpli":  # 'bplist00' -> AudioUnit kAudioUnitProperty_ClassInfo
        return "au", None, None
    return "unknown", None, None


def _printable_fourcc(b: bytes) -> bool:
    return len(b) == 4 and all(0x20 <= c <= 0x7E for c in b)


def _extract_uid(d: dict, fxb_id):
    """Determine a plugin's VST2 unique id as an int. Returns (uid_int|None, source).

    Soundminer stores the id as a plain integer in big-endian character order;
    REAPER writes its little-endian bytes. We retain the integer and derive both
    forms where needed."""
    # 1) v5 descriptor uid hex (always XML-safe, survives control-byte scrubbing)
    uid_hex = _descriptor_uid(d.get("plug_data"))
    if uid_hex:
        v = _uid_from_hex(uid_hex)
        if v is not None:
            return v, "descriptor.uid"
    # 2) fxID from the .fxb inside plugindata (authoritative for VST2)
    if fxb_id and _printable_fourcc(fxb_id):
        return int.from_bytes(fxb_id, "big"), "fxb.fxID"
    # 3) plugin_ident tail: "VST-Name-<hash>-<uidhex>"
    pd = d.get("plug_data")
    if isinstance(pd, dict):
        ident = pd.get("plugin_ident", "")
        if isinstance(ident, bytes):
            ident = ident.decode("utf-8", "replace")
        m = re.search(r"-([0-9A-Fa-f]{8})$", ident or "")
        if m:
            v = _uid_from_hex(m.group(1))
            if v is not None:
                return v, "plugin_ident"
    # 4) UniqueID string, if it is a clean printable 4-char code (big-endian form)
    uid = d.get("UniqueID")
    if isinstance(uid, str):
        b = uid.encode("latin-1", "replace")
        if _printable_fourcc(b):
            return int.from_bytes(b, "big"), "UniqueID"
    return None, "unknown"


def _channel_counts(d: dict):
    """(num_in, num_out) from the v5 descriptor, defaulting to stereo."""
    pd = d.get("plug_data")
    if isinstance(pd, dict):
        desc = pd.get("plugin_descriptor", "")
        if isinstance(desc, bytes):
            desc = desc.decode("utf-8", "replace")
        mi = re.search(r'numInputs="(\d+)"', desc or "")
        mo = re.search(r'numOutputs="(\d+)"', desc or "")
        ni = int(mi.group(1)) if mi else 2
        no = int(mo.group(1)) if mo else 2
        return (ni or 2), (no or 2)
    return 2, 2


def _slot_index(d: dict, fallback: int) -> int:
    for key in ("slotnumber", "slotPosition"):
        v = d.get(key)
        if isinstance(v, int):
            return v
    return fallback


def _is_bypassed(d: dict) -> bool:
    if "bypass_state" in d:            # v5
        return bool(d.get("bypass_state"))
    if "isBypassed" in d:              # v4.1
        return bool(d.get("isBypassed"))
    return False


def _clean_str(v) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v or "" if isinstance(v, str) else ""


def harvest_name_uids(plists, templates: dict) -> dict:
    """Build a {product_name: uid_int} map from high-confidence id sources, used as
    a fallback for old-format slots that store no usable id of their own."""
    name_uid: dict = {}
    # Seed from installed-plugin templates (name from vst-<name>.ini <-> fourcc).
    for fourcc, tmpl in templates.items():
        name_uid.setdefault(tmpl["name"], struct.unpack("<I", fourcc)[0])
    for pl in plists:
        if not isinstance(pl, list):
            continue
        for d in pl:
            if not isinstance(d, dict):
                continue
            product = _clean_str(d.get("ProductName"))
            if not product or product.startswith("WaveShell"):
                continue
            kind, fx_id, _ = _parse_plugindata(d.get("plug_data"))
            uid, src = _extract_uid(d, fx_id)
            if uid is not None and src in ("descriptor.uid", "fxb.fxID", "plugin_ident"):
                name_uid.setdefault(product, uid)
    return name_uid


def parse_preset(pid: int, name: str, folder: str, raw: bytes, name_uid: dict = None) -> Preset:
    name_uid = name_uid or {}
    preset = Preset(id=pid, name=name, folder=folder)
    try:
        pl = _load_plist(raw)
    except Exception as exc:  # pragma: no cover - defensive
        preset.notes.append(f"plist parse failed: {exc}")
        return preset
    if not isinstance(pl, list):
        preset.notes.append("plist root is not an array of plugin dicts")
        return preset

    slots = []
    for i, d in enumerate(pl):
        if not isinstance(d, dict):
            continue
        product = _clean_str(d.get("ProductName"))
        vendor = _clean_str(d.get("VendorName"))
        is_shell = product.startswith("WaveShell")

        # State chunk: prefer the explicit VSTChunk; otherwise pull it out of the
        # .fxb embedded in plug_data.plugindata.
        kind, fx_id, fxb_chunk = _parse_plugindata(d.get("plug_data"))
        vstchunk = d.get("VSTChunk")
        if isinstance(vstchunk, (bytes, bytearray)):
            chunk = bytes(vstchunk)
        elif fxb_chunk is not None:
            chunk = fxb_chunk
        else:
            if kind == "au":
                preset.notes.append(f"slot {i} '{product}': AudioUnit state (Mac-only) -- skipped")
            elif kind == "param":
                preset.notes.append(f"slot {i} '{product}': VST2 param bank, no chunk -- skipped")
            else:
                preset.notes.append(f"slot {i} '{product}': no plugin state found -- skipped")
            continue

        uid_int, src = _extract_uid(d, fx_id)
        if uid_int is None and product in name_uid:
            uid_int, src = name_uid[product], "name-map"
        if uid_int is None:
            if is_shell:
                preset.notes.append(
                    f"slot {i} '{product}': Waves shell without sub-plugin identity -- skipped")
            else:
                preset.notes.append(f"slot {i} '{product}': could not determine VST id -- skipped")
            continue

        ni, no = _channel_counts(d)
        slots.append(PluginSlot(
            product=product, vendor=vendor, uid_int=uid_int, chunk=chunk,
            bypassed=_is_bypassed(d), num_in=ni, num_out=no,
            slot=_slot_index(d, i), uid_source=src, is_waves_shell=is_shell,
        ))

    slots.sort(key=lambda s: s.slot)
    preset.slots = slots
    return preset


# ---------------------------------------------------------------------------
# presets/*.ini -> fourcc header template map
# ---------------------------------------------------------------------------

def build_ini_templates(presets_dir: str) -> dict:
    """Map fourcc(bytes) -> {'header': (10 ints), 'name': plugin name} from
    REAPER VST2 preset .ini banks, so we reproduce each plugin's exact pin config."""
    templates: dict = {}
    for path in glob.glob(os.path.join(presets_dir, "vst-*.ini")):
        data_hex = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("Data="):
                        data_hex = line[5:].strip()
                        break
        except OSError:
            continue
        if not data_hex:
            continue
        try:
            raw = bytes.fromhex(data_hex)
        except ValueError:
            continue
        if len(raw) < 52 or raw[4:8] != REAPER_VST_MARKER:
            continue
        fourcc = raw[:4]
        header = struct.unpack("<10i", raw[8:48])
        name = os.path.basename(path)[len("vst-"):-len(".ini")]
        templates.setdefault(fourcc, {"header": header, "name": name})
    return templates


# ---------------------------------------------------------------------------
# Reaper VST2 block emission
# ---------------------------------------------------------------------------

def _vst_token(uid_int: int, display_name: str) -> str:
    """The <....> identity token: 'VST' + big-endian uid bytes + first 9 chars of
    the lowercased display name, null-padded to 16 bytes, as upper-hex."""
    name_bytes = display_name.lower().encode("latin-1", "replace")[:9]
    token = b"VST" + struct.pack(">I", uid_int) + name_bytes
    token = token.ljust(16, b"\x00")[:16]
    return token.hex().upper()


def _b64_lines(preamble: bytes, chunk: bytes, trailer: bytes):
    """Reproduce Reaper's exact line layout: the preamble on its own line, then the
    chunk in 96-byte groups, then the trailer as a final line. Each line is an
    independently-padded base64 string (Reaper decodes and concatenates per line)."""
    lines = [base64.b64encode(preamble).decode("ascii")]
    for off in range(0, len(chunk), BODY_GROUP):
        lines.append(base64.b64encode(chunk[off:off + BODY_GROUP]).decode("ascii"))
    lines.append(base64.b64encode(trailer).decode("ascii"))
    return lines


def build_vst_block(slot: PluginSlot, templates: dict, program_name: str = "") -> str:
    fourcc = slot.ondisk_fourcc
    decimal = slot.uid_int

    tmpl = templates.get(fourcc)
    if tmpl is not None:
        header = tmpl["header"]
    elif slot.num_in == 2 and slot.num_out == 2:
        header = STEREO_HEADER
    else:
        # Best-effort pin config for non-stereo plugins.
        header = (slot.num_in, 1, 0, 2, 0, slot.num_out, 1, 0, 2, 0)

    chunk = slot.chunk
    preamble = (
        fourcc
        + REAPER_VST_MARKER
        + struct.pack("<10i", *header)
        + struct.pack("<i", len(chunk))
        + struct.pack("<2i", *POST_LEN_INTS)
    )
    trailer = b"\x00" + program_name.encode("latin-1", "replace") + b"\x00" + struct.pack("<i", 16)

    display = f"VST: {slot.product}"
    if slot.vendor:
        display += f" ({slot.vendor})"
    filename = f"{slot.product}.dll"
    token = _vst_token(slot.uid_int, slot.product)

    out = []
    out.append(f"BYPASS {1 if slot.bypassed else 0} 0")
    out.append(f'<VST "{display}" "{filename}" 0 "" {decimal}<{token}> ""')
    for ln in _b64_lines(preamble, chunk, trailer):
        out.append("  " + ln)
    out.append(">")
    out.append("WAK 0 0")
    return "\n".join(out)


def build_rfxchain(preset: Preset, templates: dict) -> str:
    blocks = [build_vst_block(s, templates) for s in preset.slots]
    return "\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_SANITIZE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str) -> str:
    name = _SANITIZE.sub("_", name).strip().rstrip(".")
    name = name or "untitled"
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name


def load_presets(db_path: str):
    # Read-only mode prevents SQLite from mutating the source database or
    # creating journal files alongside it.
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as con:
        con.text_factory = bytes
        cur = con.cursor()
        cur.execute("SELECT id, title FROM folders WHERE deleted=0")
        folders = {
            fid: title.decode("utf-8", "replace")
            for fid, title in cur.fetchall()
        }
        cur.execute(
            "SELECT id, preset_name, human_data, folder_id FROM pluginpresets "
            "WHERE deleted=0 ORDER BY folder_id, ndx, id"
        )
        rows = cur.fetchall()
    out = []
    for pid, name, hd, folder_id in rows:
        name = name.decode("utf-8", "replace") if isinstance(name, bytes) else name
        folder = folders.get(folder_id, "") if folder_id else ""
        out.append((pid, name, folder, bytes(hd) if hd else b""))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert Soundminer presets to REAPER FX chains.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--db", default="pluginpresets.sqlite", help="Soundminer SQLite database")
    ap.add_argument("--presets-dir", default="presets",
                    help="directory of REAPER vst-*.ini banks (for exact pin-config templates)")
    ap.add_argument("--out", default="chains", help="Output directory root")
    ap.add_argument("--list", action="store_true", help="List presets and their plugins; write nothing")
    ap.add_argument("--sample", type=int, default=0, help="Convert only the first N matching presets")
    ap.add_argument("--only", action="append", default=[],
                    help="Only presets whose name contains this substring (repeatable)")
    ap.add_argument("--force", action="store_true",
                    help="replace existing .RfxChain files in the output directory")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"error: db not found: {args.db}", file=sys.stderr)
        return 2
    if args.sample < 0:
        print("error: --sample must be zero or greater", file=sys.stderr)
        return 2

    templates = build_ini_templates(args.presets_dir)
    known_fourccs = set(templates)

    raw_rows = load_presets(args.db)
    plists = []
    for _pid, _name, _folder, hd in raw_rows:
        try:
            plists.append(_load_plist(hd))
        except Exception:
            plists.append(None)
    name_uid = harvest_name_uids(plists, templates)

    presets = [parse_preset(pid, name, folder, hd, name_uid)
               for pid, name, folder, hd in raw_rows]

    if args.only:
        presets = [p for p in presets if any(s.lower() in p.name.lower() for s in args.only)]
    if args.sample:
        presets = presets[:args.sample]

    if args.list:
        for p in presets:
            loc = f"{p.folder}/" if p.folder else ""
            print(f"[{p.id}] {loc}{p.name}  ({len(p.slots)} plugin(s))")
            for s in p.slots:
                mark = "OK " if s.ondisk_fourcc in known_fourccs else "?? "
                fc = s.ondisk_fourcc.decode("latin-1", "replace")
                print(f"    {mark}{s.product} <{s.vendor}> id={fc!r} src={s.uid_source}")
            for n in p.notes:
                print(f"    !  {n}")
        return 0

    planned = []
    planned_paths = set()
    for p in presets:
        if not p.slots:
            continue
        subdir = os.path.join(args.out, safe_filename(p.folder)) if p.folder else args.out
        stem = safe_filename(p.name)
        path = os.path.join(subdir, stem + ".RfxChain")
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in planned_paths:
            # Sanitization can collapse distinct preset names to the same path.
            path = os.path.join(subdir, f"{stem} ({p.id}).RfxChain")
            normalized = os.path.normcase(os.path.abspath(path))
        planned_paths.add(normalized)
        planned.append((p, path))

    existing = [path for _p, path in planned if os.path.exists(path)]
    if existing and not args.force:
        print(
            f"error: refusing to replace {len(existing)} existing chain(s); "
            "choose another --out directory or pass --force",
            file=sys.stderr,
        )
        for path in existing[:10]:
            print(f"  {path}", file=sys.stderr)
        if len(existing) > 10:
            print(f"  ... and {len(existing) - 10} more", file=sys.stderr)
        return 2

    written = 0
    total_slots = 0
    matched_slots = 0
    unmatched = {}
    all_notes = []
    for p in presets:
        if not p.slots:
            all_notes.append(f"[{p.id}] {p.name}: no convertible plugins ({'; '.join(p.notes) or 'empty'})")
        for n in p.notes:
            if p.slots:
                all_notes.append(f"[{p.id}] {p.name}: {n}")

    for p, path in planned:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(build_rfxchain(p, templates))
        written += 1
        for s in p.slots:
            total_slots += 1
            if s.ondisk_fourcc in known_fourccs:
                matched_slots += 1
            else:
                unmatched[s.product] = unmatched.get(s.product, 0) + 1

    print(f"Wrote {written} chain(s) to {args.out!r}")
    print(f"Plugin slots: {total_slots} total, {matched_slots} matched an installed-plugin "
          f"template, {total_slots - matched_slots} not matched.")
    if unmatched:
        print("\nPlugins with no matching template in presets/ (may show offline in REAPER):")
        for name, c in sorted(unmatched.items(), key=lambda kv: -kv[1]):
            print(f"  {c:4d}  {name}")
    if all_notes:
        print(f"\nNotes ({len(all_notes)}):")
        for n in all_notes[:60]:
            print(f"  {n}")
        if len(all_notes) > 60:
            print(f"  ... and {len(all_notes) - 60} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
