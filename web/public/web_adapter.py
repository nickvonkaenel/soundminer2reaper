"""Browser-only adapter for the unmodified sm2reaper command-line module."""

from __future__ import annotations

import io
import json
import re
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import sm2reaper


def run_conversion(config):
    """Run the CLI module against browser-local paths and create a ZIP result."""
    database = config.get("database") or ""
    templates = config["templates"]
    output = Path(config["output"])
    zip_path = Path(config["zipPath"])
    summary_path = Path(config["summaryPath"])

    arguments = [
        "--db",
        database,
        "--presets-dir",
        templates,
        "--out",
        str(output),
    ]
    for preset_path in config.get("presetPaths", []):
        arguments.extend(("--dsppreset", preset_path))
    for cache_path in config.get("cachePaths", []):
        arguments.extend(("--reaper-cache", cache_path))

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        return_code = sm2reaper.main(arguments)

    report = stdout.getvalue()
    error_report = stderr.getvalue()
    if return_code:
        detail = error_report.strip() or report.strip() or "Unknown conversion error"
        raise RuntimeError(detail)

    chain_paths = sorted(output.rglob("*.RfxChain")) if output.exists() else []
    skipped_count = report.count("-- skipped")
    notes_match = re.search(r"Notes \((\d+)", report)
    note_count = int(notes_match.group(1)) if notes_match else 0

    report_text = (
        "soundminer2reaper browser conversion report\n"
        "============================================\n\n"
        f"{report}"
    )
    if error_report:
        report_text += f"\nDiagnostics\n-----------\n{error_report}"

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for chain_path in chain_paths:
            archive.write(chain_path, chain_path.relative_to(output).as_posix())
        archive.writestr("conversion-report.txt", report_text)

    summary = {
        "chainCount": len(chain_paths),
        "skippedCount": skipped_count or note_count,
        "report": report_text,
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    return summary
