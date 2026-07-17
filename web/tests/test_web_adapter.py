import json
import plistlib
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

PUBLIC_DIR = Path(__file__).resolve().parents[1] / "public"
sys.dont_write_bytecode = True
sys.path.insert(0, str(PUBLIC_DIR))

import web_adapter


class WebAdapterTests(unittest.TestCase):
    def test_conversion_writes_chain_and_report_zip(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = root / "pluginpresets.sqlite"
            output = root / "chains"
            zip_path = root / "result.zip"
            summary_path = root / "summary.json"
            templates = root / "templates"
            templates.mkdir()

            descriptor = (
                '<plugin uid="54657374" numInputs="2" numOutputs="2"/>'
            )
            human_data = plistlib.dumps([{
                "ProductName": "Example",
                "VendorName": "Vendor",
                "VSTChunk": b"state",
                "plug_data": {"plugin_descriptor": descriptor},
            }])

            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE folders "
                    "(id INTEGER PRIMARY KEY, title TEXT, deleted INTEGER)"
                )
                connection.execute(
                    "CREATE TABLE pluginpresets "
                    "(id INTEGER PRIMARY KEY, preset_name TEXT, human_data BLOB, "
                    "folder_id INTEGER, deleted INTEGER, ndx INTEGER)"
                )
                connection.execute(
                    "INSERT INTO pluginpresets VALUES (?, ?, ?, ?, ?, ?)",
                    (1, "Example preset", human_data, None, 0, 0),
                )
                connection.commit()

            summary = web_adapter.run_conversion({
                "database": str(database),
                "templates": str(templates),
                "cachePaths": [],
                "output": str(output),
                "zipPath": str(zip_path),
                "summaryPath": str(summary_path),
            })

            self.assertEqual(summary["chainCount"], 1)
            self.assertEqual(
                json.loads(summary_path.read_text(encoding="utf-8"))[
                    "chainCount"
                ],
                1,
            )
            with zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"Example preset.RfxChain", "conversion-report.txt"},
                )

    def test_conversion_accepts_standalone_dsppreset_without_database(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export_path = root / "Standalone example.dsppreset"
            output = root / "chains"
            zip_path = root / "result.zip"
            summary_path = root / "summary.json"
            templates = root / "templates"
            templates.mkdir()

            descriptor = (
                '<plugin uid="54657374" numInputs="2" numOutputs="2"/>'
            )
            export_path.write_bytes(plistlib.dumps([{
                "ProductName": "Example",
                "VendorName": "Vendor",
                "VSTChunk": b"state",
                "plug_data": {"plugin_descriptor": descriptor},
            }]))

            summary = web_adapter.run_conversion({
                "database": "",
                "presetPaths": [str(export_path)],
                "templates": str(templates),
                "cachePaths": [],
                "output": str(output),
                "zipPath": str(zip_path),
                "summaryPath": str(summary_path),
            })

            self.assertEqual(summary["chainCount"], 1)
            with zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "Standalone example.RfxChain",
                        "conversion-report.txt",
                    },
                )


if __name__ == "__main__":
    unittest.main()
