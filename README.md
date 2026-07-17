# soundminer2reaper

`soundminer2reaper` is an independent command-line tool that converts a
Soundminer plugin-preset database (`pluginpresets.sqlite`) into REAPER FX chains
(`.RfxChain`). It creates one chain per preset and preserves the source folder
grouping. Both VST2 and VST3 plugin state are supported; unsupported Audio Unit
and parameter-only entries are reported and skipped.

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

### Browser app

The `web/` project provides a drag-and-drop version of the converter. It runs
the same Python implementation locally in a browser worker and returns the
generated chains plus a conversion report as a ZIP. Selected databases, plugin
caches, and preset banks are not uploaded or stored by the application.

For local web development:

```console
cd web
npm install
npm run dev
```

### Options

| Option | Description |
| --- | --- |
| `--db PATH` | Soundminer SQLite database. Defaults to `pluginpresets.sqlite`. |
| `--presets-dir DIR` | Optional directory of REAPER `vst-*.ini` preset banks. These provide exact VST2 pin/channel templates. Defaults to `presets`. |
| `--reaper-cache PATH` | Additional REAPER `reaper-vstplugins*.ini` scan cache used to resolve VST3 class GUIDs and numeric IDs. May be repeated. The standard per-user REAPER resource path is checked automatically. |
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
`human_data` column. For each supported slot, the converter recovers the plugin
identity and state, wraps the state in REAPER's container format, and writes a
`<VST>` FX block.

### VST2

The converter recovers the four-character VST2 unique ID and opaque state chunk.
State is read from `VSTChunk` when present, or from an `.fxb` embedded in
`plug_data.plugindata`. When supplied, REAPER `vst-*.ini` banks provide the
plugin's exact pin configuration.

### VST3

Soundminer stores VST3 `IComponent` and `IEditController` state in a custom
least-significant-bit-first base64 representation. The converter decodes both
state sections, looks up the plugin's class GUID and numeric identifier in a
REAPER `reaper-vstplugins*.ini` scan cache, generates the pin configuration from
the stored input/output counts, and builds a REAPER VST3 state container.

VST3 conversion requires a scan-cache entry for the plugin. The standard
per-user REAPER resource directory is checked automatically on Windows, macOS,
and Linux. Use `--reaper-cache` for portable installations, alternate resource
directories, or caches copied from another system.

## Compatibility and limitations

- Generated chains reference the original plugin identity. They load
  automatically only when the same plugin is registered in REAPER. Missing
  plugins may appear offline while retaining their state.
- VST2 plugin filenames are inferred from product names. REAPER generally
  resolves plugins by their identity token, but some installations may require
  manual relinking.
- A VST3 plugin absent from every supplied or auto-detected scan cache is
  reported and skipped. Let REAPER scan the plugin or provide another cache,
  then rerun the conversion.
- Audio Unit state is not converted because it has no direct VST2 equivalent.
- VST2 parameter banks that contain float programs instead of opaque chunks are
  reported and skipped.
- Slots with missing state or an unresolved plugin identity are reported and
  skipped.
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
