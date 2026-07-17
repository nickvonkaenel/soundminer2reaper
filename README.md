# soundminer2reaper

`soundminer2reaper` is an independent command-line tool that converts a
Soundminer plugin-preset database (`pluginpresets.sqlite`) into REAPER FX chains
(`.RfxChain`). It creates one chain per preset and preserves the source folder
grouping.

The converter uses only the Python standard library. Python 3.8 or newer is
required.

> [!IMPORTANT]
> Work from a backup of your preset database. The converter opens the database
> read-only, but generated chains should still be tested in a non-critical
> REAPER project before being used in production.

## Quick start

Run the script directly:

```console
# Inspect the presets without writing any files
python sm2reaper.py --db /path/to/pluginpresets.sqlite --list

# Preview presets whose names contain a value
python sm2reaper.py --db /path/to/pluginpresets.sqlite --list --only "Preset name"

# Convert every supported preset
python sm2reaper.py --db /path/to/pluginpresets.sqlite --out ./chains
```

You can also install the command locally:

```console
python -m pip install .
sm2reaper --db /path/to/pluginpresets.sqlite --out ./chains
```

### Options

| Option | Description |
| --- | --- |
| `--db PATH` | Soundminer SQLite database. Defaults to `pluginpresets.sqlite`. |
| `--presets-dir DIR` | Optional directory of REAPER `vst-*.ini` preset banks. These provide exact plugin pin/channel templates. Defaults to `presets`. |
| `--out DIR` | Output root. Defaults to `chains`. |
| `--list` | Print presets and plugins without writing chains. |
| `--only TEXT` | Include preset names containing `TEXT`, case-insensitively. May be repeated. |
| `--sample N` | Process only the first `N` matching presets. |
| `--force` | Replace chain files that already exist in the output directory. |
| `--version` | Print the installed version. |

Without `--force`, conversion stops before writing if a destination file already
exists. If two source names become identical after filename sanitization, the
later chain receives its preset ID as a suffix.

## How it works

Soundminer stores each plugin rack as an Apple property list in the
`human_data` column. For each supported slot, the converter recovers the VST2
unique ID and opaque state chunk, wraps the chunk in REAPER's VST2 state
container, and writes a `<VST>` FX block.

State is read from `VSTChunk` when present, or from an `.fxb` embedded in
`plug_data.plugindata`. When supplied, REAPER `vst-*.ini` banks are used as
templates for the plugin's exact pin configuration.

## Compatibility and limitations

- Generated chains reference the original VST2 plugin identity. They load
  automatically only when the same VST2 plugin is registered in REAPER.
  Missing plugins may appear offline while retaining their state.
- Plugin filenames are inferred from product names. REAPER generally resolves
  plugins by their identity token, but some installations may require manual
  relinking.
- Audio Unit state is not converted because it has no direct VST2 equivalent.
- VST2 parameter banks that contain float programs instead of opaque chunks are
  reported and skipped.
- Slots with missing state or an unresolved VST2 ID are reported and skipped.
- Conversion coverage depends on the formats and metadata present in the source
  library. Review the command output for skipped slots.

Preset databases, REAPER preset banks, and generated chains may contain licensed
plugin data or identifying names. They are excluded by the included
`.gitignore`; review staged files before publishing.

## Development

Run the standard-library test suite and a syntax check:

```console
python -m unittest discover -s tests -v
python -m py_compile sm2reaper.py
```

## License

Released under the [MIT License](LICENSE).

Soundminer and REAPER are trademarks of their respective owners. This project
is not affiliated with or endorsed by either product's developer.
