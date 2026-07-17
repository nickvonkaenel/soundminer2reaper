const PYODIDE_BASE =
  "https://cdn.jsdelivr.net/pyodide/v314.0.2/full/";

const encoder = new TextEncoder();
const decoder = new TextDecoder();

function postStatus(message, progress) {
  self.postMessage({ type: "status", message, progress });
}

function safeName(name, fallback) {
  const cleaned = name.replace(/[^a-zA-Z0-9._-]/g, "_");
  return cleaned || fallback;
}

async function fetchText(name) {
  const response = await fetch(new URL(name, self.location.href));
  if (!response.ok) {
    throw new Error(`Could not load ${name}.`);
  }
  return response.text();
}

let runtimePromise;

async function prepareRuntime() {
  if (runtimePromise) return runtimePromise;

  runtimePromise = (async () => {
    postStatus("Loading the private browser converter…", 18);
    const { loadPyodide } = await import(
      `${PYODIDE_BASE}pyodide.mjs`
    );
    const pyodide = await loadPyodide({
      indexURL: PYODIDE_BASE,
    });

    postStatus("Preparing Soundminer and REAPER support…", 34);
    const [converterSource, adapterSource] = await Promise.all([
      fetchText("sm2reaper.py"),
      fetchText("web_adapter.py"),
    ]);
    pyodide.FS.writeFile("/sm2reaper.py", encoder.encode(converterSource));
    pyodide.FS.writeFile("/web_adapter.py", encoder.encode(adapterSource));
    await pyodide.runPythonAsync(`
import importlib
import sys

if "/" not in sys.path:
    sys.path.insert(0, "/")
importlib.invalidate_caches()
import web_adapter
`);
    return pyodide;
  })();

  return runtimePromise;
}

self.onmessage = async (event) => {
  if (event.data?.type !== "convert") return;

  try {
    const pyodide = await prepareRuntime();
    postStatus("Copying files into the local workspace…", 46);
    pyodide.FS.mkdirTree("/work");
    pyodide.FS.mkdirTree("/work/caches");
    pyodide.FS.mkdirTree("/work/templates");

    const databasePath = "/work/pluginpresets.sqlite";
    pyodide.FS.writeFile(
      databasePath,
      new Uint8Array(event.data.database.buffer),
    );

    const cachePaths = [];
    event.data.caches.forEach((file, index) => {
      const path = `/work/caches/${index}-${safeName(
        file.name,
        `cache-${index}.ini`,
      )}`;
      pyodide.FS.writeFile(path, new Uint8Array(file.buffer));
      cachePaths.push(path);
    });

    event.data.templates.forEach((file, index) => {
      let name = safeName(file.name, `vst-template-${index}.ini`);
      if (!name.toLowerCase().startsWith("vst-")) {
        name = `vst-${name}`;
      }
      pyodide.FS.writeFile(
        `/work/templates/${name}`,
        new Uint8Array(file.buffer),
      );
    });

    postStatus("Converting plugin state and building chains…", 62);
    const config = {
      database: databasePath,
      cachePaths,
      templates: "/work/templates",
      output: "/work/chains",
      zipPath: "/work/soundminer-reaper-chains.zip",
      summaryPath: "/work/summary.json",
    };
    pyodide.globals.set("_web_config_json", JSON.stringify(config));
    await pyodide.runPythonAsync(`
import json
import web_adapter

web_adapter.run_conversion(json.loads(_web_config_json))
`);

    postStatus("Packaging the chains and report…", 91);
    const summary = JSON.parse(
      decoder.decode(pyodide.FS.readFile(config.summaryPath)),
    );
    const zipView = pyodide.FS.readFile(config.zipPath);
    const zip = zipView.buffer.slice(
      zipView.byteOffset,
      zipView.byteOffset + zipView.byteLength,
    );

    self.postMessage({ type: "result", zip, summary }, [zip]);
  } catch (error) {
    const rawMessage =
      error instanceof Error ? error.message : String(error);
    const message = rawMessage.includes("database")
      ? "The database could not be converted. Confirm it is a Soundminer plugin-preset database and try again."
      : `Conversion failed: ${rawMessage}`;
    self.postMessage({ type: "error", message });
  }
};
