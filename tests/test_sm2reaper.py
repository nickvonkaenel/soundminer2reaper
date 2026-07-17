import base64
import os
import plistlib
import sqlite3
import struct
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import sm2reaper


def encode_soundminer_state(data):
    """Test helper for Soundminer's least-significant-bit-first base64."""
    buffer = 0
    bit_count = 0
    encoded = []
    for byte in data:
        buffer |= byte << bit_count
        bit_count += 8
        while bit_count >= 6:
            encoded.append(sm2reaper._SM_ALPHA[buffer & 0x3F])
            buffer >>= 6
            bit_count -= 6
    if bit_count:
        encoded.append(sm2reaper._SM_ALPHA[buffer & 0x3F])
    return f"{len(data)}.{''.join(encoded)}"


class FilenameTests(unittest.TestCase):
    def test_replaces_unsafe_characters(self):
        self.assertEqual(sm2reaper.safe_filename('  A/B:"C"?  '), "A_B__C__")

    def test_avoids_windows_reserved_names(self):
        self.assertEqual(sm2reaper.safe_filename("CON"), "_CON")
        self.assertEqual(sm2reaper.safe_filename("nul.txt"), "_nul.txt")

    def test_supplies_fallback_for_empty_name(self):
        self.assertEqual(sm2reaper.safe_filename("..."), "untitled")


class ParsingTests(unittest.TestCase):
    def test_decodes_soundminer_vst3_state(self):
        state = bytes(range(32))
        encoded = encode_soundminer_state(state)

        self.assertEqual(sm2reaper.sm_state_decode(encoded), state)

    def test_rejects_truncated_soundminer_vst3_state(self):
        with self.assertRaises(ValueError):
            sm2reaper.sm_state_decode("10.A")

    def test_builds_vst3_pin_config(self):
        self.assertEqual(
            sm2reaper.vst3_pin_config(2, 1),
            [2, 1, 0, 2, 0, 1, 1, 0],
        )

    def test_parses_explicit_vst_chunk(self):
        descriptor = '<plugin uid="54657374" numInputs="1" numOutputs="2"/>'
        raw = plistlib.dumps([{
            "ProductName": "Example",
            "VendorName": "Vendor",
            "VSTChunk": b"state",
            "plug_data": {"plugin_descriptor": descriptor},
        }])

        preset = sm2reaper.parse_preset(1, "Preset", "", raw)

        self.assertEqual(len(preset.slots), 1)
        self.assertEqual(preset.slots[0].uid_int, 0x54657374)
        self.assertEqual(preset.slots[0].chunk, b"state")
        self.assertEqual((preset.slots[0].num_in, preset.slots[0].num_out), (1, 2))

    def test_parses_vst3_state_with_cache_identity(self):
        component = b"component-state"
        controller = b"controller-state"
        xml = (
            "<VST3PluginState>"
            f"<IComponent>{encode_soundminer_state(component)}</IComponent>"
            f"<IEditController>{encode_soundminer_state(controller)}</IEditController>"
            "</VST3PluginState>"
        ).encode()
        descriptor = '<plugin numInputs="2" numOutputs="2"/>'
        raw = plistlib.dumps([{
            "ProductName": "Example",
            "VendorName": "Vendor",
            "plugInPath": "/plugins/Example.vst3",
            "plug_data": {
                "plugintype": "VST3",
                "plugin_descriptor": descriptor,
                "plugindata": b"VC2!\x00\x00\x00\x00" + xml + b"\x00",
            },
        }])
        guid = "00112233445566778899AABBCCDDEEFF"
        caches = {
            "by_name": {"Example (Vendor)": (123456, guid)},
            "by_file": {},
        }

        preset = sm2reaper.parse_preset(
            1, "Preset", "", raw, caches=caches
        )

        self.assertEqual(len(preset.slots), 1)
        slot = preset.slots[0]
        self.assertEqual(slot.kind, "vst3")
        self.assertEqual(slot.decimal, 123456)
        self.assertEqual(slot.guid_hex, guid)
        self.assertEqual(slot.component, component)
        self.assertEqual(slot.controller, controller)

    def test_parses_reaper_vst3_cache(self):
        guid = "00112233445566778899aabbccddeeff"
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp, "reaper-vstplugins64.ini")
            cache_path.write_text(
                f"Example.vst3=100,{123456}{{{guid},Example (Vendor)\n",
                encoding="utf-8",
            )

            caches = sm2reaper.parse_reaper_caches([cache_path])

        expected = (123456, guid.upper())
        self.assertEqual(caches["by_name"]["Example (Vendor)"], expected)
        self.assertEqual(caches["by_file"]["example.vst3"], expected)

    def test_builds_reaper_block_with_round_trippable_state(self):
        slot = sm2reaper.PluginSlot(
            product="Example",
            vendor="Vendor",
            uid_int=0x54657374,
            chunk=b"opaque-state",
        )

        block = sm2reaper.build_vst_block(slot, {})
        encoded_lines = [
            line.strip()
            for line in block.splitlines()
            if line.startswith("  ")
        ]
        decoded = b"".join(base64.b64decode(line) for line in encoded_lines)

        self.assertEqual(decoded[:4], struct.pack("<I", slot.uid_int))
        self.assertIn(slot.chunk, decoded)

    def test_builds_reaper_vst3_block_with_both_state_sections(self):
        slot = sm2reaper.PluginSlot(
            kind="vst3",
            product="Example",
            vendor="Vendor",
            decimal=123456,
            guid_hex="00112233445566778899AABBCCDDEEFF",
            filename="Example.vst3",
            component=b"component-state",
            controller=b"controller-state",
        )

        block = sm2reaper.build_vst3_block(slot)
        encoded_lines = [
            line.strip()
            for line in block.splitlines()
            if line.startswith("  ")
        ]
        decoded = b"".join(
            base64.b64decode(line) for line in encoded_lines
        )

        self.assertIn(slot.component, decoded)
        self.assertIn(slot.controller, decoded)
        self.assertIn(f"{slot.decimal}{{{slot.guid_hex}}}", block)


class CommandTests(unittest.TestCase):
    def _make_database(self, directory):
        db_path = Path(directory, "pluginpresets.sqlite")
        descriptor = '<plugin uid="54657374" numInputs="2" numOutputs="2"/>'
        human_data = plistlib.dumps([{
            "ProductName": "Example",
            "VendorName": "Vendor",
            "VSTChunk": b"state",
            "plug_data": {"plugin_descriptor": descriptor},
        }])
        with closing(sqlite3.connect(db_path)) as con:
            con.execute(
                "CREATE TABLE folders "
                "(id INTEGER PRIMARY KEY, title TEXT, deleted INTEGER)"
            )
            con.execute(
                "CREATE TABLE pluginpresets "
                "(id INTEGER PRIMARY KEY, preset_name TEXT, human_data BLOB, "
                "folder_id INTEGER, deleted INTEGER, ndx INTEGER)"
            )
            con.execute(
                "INSERT INTO pluginpresets VALUES (?, ?, ?, ?, ?, ?)",
                (1, "Example preset", human_data, None, 0, 0),
            )
            con.commit()
        return db_path

    def test_refuses_to_replace_existing_output_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._make_database(tmp)
            out_dir = Path(tmp, "chains")
            args = ["--db", os.fspath(db_path), "--out", os.fspath(out_dir)]
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(sm2reaper.main(args), 0)
                self.assertEqual(sm2reaper.main(args), 2)
                self.assertEqual(sm2reaper.main([*args, "--force"]), 0)


if __name__ == "__main__":
    unittest.main()
