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


class FilenameTests(unittest.TestCase):
    def test_replaces_unsafe_characters(self):
        self.assertEqual(sm2reaper.safe_filename('  A/B:"C"?  '), "A_B__C__")

    def test_avoids_windows_reserved_names(self):
        self.assertEqual(sm2reaper.safe_filename("CON"), "_CON")
        self.assertEqual(sm2reaper.safe_filename("nul.txt"), "_nul.txt")

    def test_supplies_fallback_for_empty_name(self):
        self.assertEqual(sm2reaper.safe_filename("..."), "untitled")


class ParsingTests(unittest.TestCase):
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
