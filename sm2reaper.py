#!/usr/bin/env python3
"""
sm2reaper -- Convert Soundminer plugin-preset databases into Reaper FX chains.

Reads a Soundminer `pluginpresets.sqlite` database, parses each preset's plugin
"rack" (stored as an Apple plist inside the `human_data` column), and emits one
Reaper `.RfxChain` file per preset, mirroring the Soundminer folder tree.

Handles both plugin formats Soundminer stores:

* VST2 -- an opaque effGetChunk() blob, wrapped in REAPER's VST2 state
  container.
* VST3 -- component and controller state, decoded from Soundminer's state format
  and wrapped in REAPER's VST3 state container. VST3 identity is resolved from a
  REAPER `reaper-vstplugins*.ini` scan cache.

A generated chain only auto-loads when the same plugin is registered in REAPER.
Missing plugins may appear offline, but retain their state for later relinking.
Audio Unit presets and VST2 float-parameter banks are reported and skipped.

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

__version__ = "0.2.0"

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

# Soundminer encodes VST3 component and controller state with least-significant-
# bit-first base64 packing and a shifted alphabet.
_STD_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_SM_ALPHA = "." + _STD_B64[:63]
_SM_IDX = {char: index for index, char in enumerate(_SM_ALPHA)}


def sm_state_decode(field: str) -> bytes:
    """Decode a Soundminer ``<length>.<body>`` VST3 state field."""
    field = field.strip()
    match = re.fullmatch(r"(\d+)\.(.*)", field, re.S)
    expected_length = int(match.group(1)) if match else None
    body = match.group(2) if match else field
    buffer = 0
    bit_count = 0
    output = bytearray()

    for char in body:
        value = _SM_IDX.get(char)
        if value is None:
            continue
        buffer |= value << bit_count
        bit_count += 6
        while bit_count >= 8:
            output.append(buffer & 0xFF)
            buffer >>= 8
            bit_count -= 8

    if expected_length is not None:
        if len(output) < expected_length:
            raise ValueError(
                f"decoded VST3 state is {len(output)} bytes; "
                f"expected {expected_length}"
            )
        del output[expected_length:]
    return bytes(output)


def vst3_pin_config(num_in: int, num_out: int):
    """Build REAPER's channel-mask pin map for a VST3 plugin."""
    num_in = max(num_in, 1)
    num_out = max(num_out, 1)
    values = [num_in]
    for pin in range(num_in):
        mask = 1 << pin
        values.extend((mask & 0xFFFFFFFF, (mask >> 32) & 0xFFFFFFFF))
    values.append(num_out)
    for pin in range(num_out):
        mask = 1 << pin
        values.extend((mask & 0xFFFFFFFF, (mask >> 32) & 0xFFFFFFFF))
    return values


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PluginSlot:
    product: str
    vendor: str
    kind: str = "vst2"       # "vst2" or "vst3"
    bypassed: bool = False
    num_in: int = 2
    num_out: int = 2
    slot: int = 0
    # VST2
    uid_int: int = 0         # VST2 unique id as a 32-bit int
    chunk: bytes = b""       # opaque effGetChunk() bytes
    uid_source: str = ""     # where the uid came from (diagnostics)
    is_waves_shell: bool = False
    # VST3
    decimal: int = 0         # REAPER's numeric plugin identifier
    guid_hex: str = ""       # VST3 class GUID as 32 uppercase hex characters
    filename: str = ""
    component: bytes = b""   # IComponent state
    controller: bytes = b""  # IEditController state
    matched: bool = False    # identity resolved from a REAPER scan cache

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


def _plugin_type(d: dict) -> str:
    """Return the plugin format stored in ``plug_data``, if present."""
    plug_data = d.get("plug_data")
    if not isinstance(plug_data, dict):
        return ""
    return _clean_str(plug_data.get("plugintype"))


def _vst3_state(d: dict):
    """Extract VST3 component and controller state from a ``VC2!`` blob."""
    plug_data = d.get("plug_data")
    if not isinstance(plug_data, dict):
        return None
    data = plug_data.get("plugindata")
    if not isinstance(data, (bytes, bytearray)) or data[:4] != b"VC2!":
        return None

    xml = bytes(data)[8:].split(b"\x00", 1)[0].decode("utf-8", "replace")
    component_match = re.search(
        r"<IComponent>(.*?)</IComponent>", xml, re.S
    )
    if not component_match:
        return None
    controller_match = re.search(
        r"<IEditController>(.*?)</IEditController>", xml, re.S
    )
    try:
        component = sm_state_decode(component_match.group(1))
        controller = (
            sm_state_decode(controller_match.group(1))
            if controller_match else b""
        )
    except ValueError:
        return None
    return component, controller


def parse_reaper_caches(paths):
    """Build VST3 identity lookup maps from REAPER plugin scan caches.

    Each identity is indexed by display name and by a normalized plugin
    filename. Invalid, missing, and VST2-only cache entries are ignored.
    """
    by_name = {}
    by_file = {}
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue

        for line in lines:
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            parts = value.split(",", 2)
            if len(parts) < 3:
                continue
            # REAPER's cache syntax opens the class ID with "{" but has no
            # matching closing brace before the following comma.
            identity_match = re.fullmatch(
                r"\s*(\d+)\{([0-9A-Fa-f]{32})\s*",
                parts[1],
            )
            if not identity_match:
                continue
            decimal = int(identity_match.group(1))
            guid = identity_match.group(2).upper()
            display_name = parts[2].strip()
            if display_name:
                by_name.setdefault(display_name, (decimal, guid))
            file_key = re.sub(r"[^a-z0-9.]", "", key.lower())
            if file_key:
                by_file.setdefault(file_key, (decimal, guid))
    return {"by_name": by_name, "by_file": by_file}


def _lookup_vst3_identity(
    caches: dict, product: str, vendor: str, filename: str
):
    """Resolve a VST3 numeric identifier and class GUID from cache metadata."""
    if not caches:
        return None
    display_name = f"{product} ({vendor})" if vendor else product
    identity = (
        caches.get("by_name", {}).get(display_name)
        or caches.get("by_name", {}).get(product)
    )
    if identity or not filename:
        return identity
    file_key = re.sub(r"[^a-z0-9.]", "", filename.lower())
    return caches.get("by_file", {}).get(file_key)


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


def parse_preset(
    pid: int,
    name: str,
    folder: str,
    raw: bytes,
    name_uid: dict = None,
    caches: dict = None,
) -> Preset:
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

        if _plugin_type(d).upper() == "VST3":
            state = _vst3_state(d)
            if state is None:
                preset.notes.append(
                    f"slot {i} '{product}': VST3 state not decodable -- skipped"
                )
                continue
            raw_path = _clean_str(d.get("plugInPath")).replace("\\", "/")
            filename = os.path.basename(raw_path)
            identity = _lookup_vst3_identity(
                caches or {}, product, vendor, filename
            )
            if identity is None:
                preset.notes.append(
                    f"slot {i} '{product}': VST3 identity not found in "
                    "a REAPER scan cache -- skipped"
                )
                continue
            decimal, guid = identity
            num_in, num_out = _channel_counts(d)
            component, controller = state
            slots.append(PluginSlot(
                kind="vst3",
                product=product,
                vendor=vendor,
                bypassed=_is_bypassed(d),
                num_in=num_in,
                num_out=num_out,
                slot=_slot_index(d, i),
                decimal=decimal,
                guid_hex=guid,
                filename=filename or f"{product}.vst3",
                component=component,
                controller=controller,
                matched=True,
            ))
            continue

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
            kind="vst2", product=product, vendor=vendor,
            uid_int=uid_int, chunk=chunk,
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


def build_vst2_block(
    slot: PluginSlot, templates: dict, program_name: str = ""
) -> str:
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


def build_vst3_block(slot: PluginSlot, program_name: str = "") -> str:
    """Build a REAPER VST3 FX block from component and controller state."""
    pin_config = vst3_pin_config(slot.num_in, slot.num_out)
    component = slot.component
    controller = slot.controller
    chunk = (
        struct.pack("<2i", len(component), 1)
        + component
        + struct.pack("<2i", len(controller), 0)
        + controller
    )
    preamble = (
        struct.pack("<I", slot.decimal)
        + REAPER_VST_MARKER
        + struct.pack(f"<{len(pin_config)}i", *pin_config)
        + struct.pack("<i", len(chunk))
        + struct.pack("<2i", 1, 0)
    )
    trailer = (
        b"\x00"
        + program_name.encode("latin-1", "replace")
        + b"\x00"
        + struct.pack("<i", 0)
    )

    display = f"VST3: {slot.product}"
    if slot.vendor:
        display += f" ({slot.vendor})"

    out = [
        f"BYPASS {1 if slot.bypassed else 0} 0",
        (
            f'<VST "{display}" "{slot.filename}" 0 "" '
            f"{slot.decimal}{{{slot.guid_hex}}} \"\""
        ),
    ]
    out.extend("  " + line for line in _b64_lines(preamble, chunk, trailer))
    out.extend((">", "WAK 0 0"))
    return "\n".join(out)


# Kept as a compatibility alias for callers of the original VST2-only module.
build_vst_block = build_vst2_block


def build_rfxchain(preset: Preset, templates: dict) -> str:
    blocks = [
        build_vst3_block(slot)
        if slot.kind == "vst3"
        else build_vst2_block(slot, templates)
        for slot in preset.slots
    ]
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


def default_reaper_caches():
    """Return scan-cache files from REAPER's standard per-user resource path."""
    roots = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(os.path.join(appdata, "REAPER"))
    elif sys.platform == "darwin":
        roots.append(
            os.path.join(
                os.path.expanduser("~"),
                "Library",
                "Application Support",
                "REAPER",
            )
        )
    else:
        config_home = os.environ.get(
            "XDG_CONFIG_HOME", os.path.expanduser("~/.config")
        )
        roots.append(os.path.join(config_home, "REAPER"))

    found = []
    for root in roots:
        found.extend(glob.glob(os.path.join(root, "reaper-vstplugins*.ini")))
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert Soundminer presets to REAPER FX chains.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--db", default="pluginpresets.sqlite", help="Soundminer SQLite database")
    ap.add_argument("--presets-dir", default="presets",
                    help="directory of REAPER vst-*.ini banks (for exact VST2 pin templates)")
    ap.add_argument(
        "--reaper-cache",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "additional reaper-vstplugins*.ini scan cache used to resolve "
            "VST3 identities (repeatable)"
        ),
    )
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

    cache_paths = []
    seen_cache_paths = set()
    for path in default_reaper_caches() + list(args.reaper_cache):
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized not in seen_cache_paths:
            seen_cache_paths.add(normalized)
            cache_paths.append(path)
    caches = parse_reaper_caches(cache_paths)

    raw_rows = load_presets(args.db)
    plists = []
    for _pid, _name, _folder, hd in raw_rows:
        try:
            plists.append(_load_plist(hd))
        except Exception:
            plists.append(None)
    name_uid = harvest_name_uids(plists, templates)

    presets = [parse_preset(pid, name, folder, hd, name_uid, caches)
               for pid, name, folder, hd in raw_rows]

    if args.only:
        presets = [p for p in presets if any(s.lower() in p.name.lower() for s in args.only)]
    if args.sample:
        presets = presets[:args.sample]

    def slot_matched(slot):
        if slot.kind == "vst3":
            return slot.matched
        return slot.ondisk_fourcc in known_fourccs

    if args.list:
        if caches["by_name"]:
            print(
                f"# REAPER VST3 cache: {len(caches['by_name'])} plugin(s)"
            )
        for p in presets:
            loc = f"{p.folder}/" if p.folder else ""
            print(f"[{p.id}] {loc}{p.name}  ({len(p.slots)} plugin(s))")
            for s in p.slots:
                mark = "OK " if slot_matched(s) else "?? "
                if s.kind == "vst3":
                    identity = s.guid_hex
                    source = "REAPER cache"
                else:
                    identity = repr(
                        s.ondisk_fourcc.decode("latin-1", "replace")
                    )
                    source = s.uid_source
                print(
                    f"    {mark}[{s.kind}] {s.product} <{s.vendor}> "
                    f"id={identity} src={source}"
                )
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
    slot_counts = {"vst2": 0, "vst3": 0}
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
            slot_counts[s.kind] += 1
            if slot_matched(s):
                matched_slots += 1
            elif s.kind == "vst2":
                unmatched[s.product] = unmatched.get(s.product, 0) + 1

    total_slots = slot_counts["vst2"] + slot_counts["vst3"]
    print(f"Wrote {written} chain(s) to {args.out!r}")
    print(
        f"Plugin slots: {total_slots} total "
        f"({slot_counts['vst3']} VST3, {slot_counts['vst2']} VST2)."
    )
    if slot_counts["vst3"]:
        print(
            f"  VST3: {slot_counts['vst3']} converted with identities "
            "from a REAPER scan cache."
        )
    if slot_counts["vst2"]:
        vst2_matched = matched_slots - slot_counts["vst3"]
        print(
            f"  VST2: {vst2_matched}/{slot_counts['vst2']} matched an "
            "installed-plugin pin template."
        )
    if unmatched:
        print(
            "\nVST2 plugins with no template in presets/ "
            "(still converted; may show offline in REAPER):"
        )
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
