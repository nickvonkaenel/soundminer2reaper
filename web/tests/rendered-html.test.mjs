import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the browser converter", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>soundminer2reaper<\/title>/i);
  assert.match(html, /Convert Soundminer presets to REAPER FX chains/i);
  assert.match(html, /Runs locally in your browser/i);
  assert.match(html, /Add individual presets/i);
  assert.match(html, /One or more \.dsppreset files/i);
  assert.match(html, /Drop files here/i);
  assert.match(html, /Nothing is uploaded/i);
  assert.match(html, /Built-in REAPER scan cache/i);
  assert.match(html, /included automatically/i);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape/i);
});

test("ships the converter assets and removes the starter", async () => {
  const [packageJson, page, worker, adapter, converter, defaultCache] =
    await Promise.all([
      readFile(new URL("package.json", root), "utf8"),
      readFile(new URL("app/page.tsx", root), "utf8"),
      readFile(new URL("public/converter-worker.mjs", root), "utf8"),
      readFile(new URL("public/web_adapter.py", root), "utf8"),
      readFile(new URL("public/sm2reaper.py", root), "utf8"),
      readFile(new URL("public/default-vst3-scan-cache.ini", root), "utf8"),
    ]);

  assert.match(packageJson, /"name": "soundminer2reaper-web"/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  assert.match(page, /new Worker/);
  assert.match(page, /soundminer-reaper-chains\.zip/);
  assert.match(page, /\.dsppreset/);
  assert.match(worker, /pyodide\/v314\.0\.2/);
  assert.match(worker, /presetPaths/);
  assert.match(worker, /default-vst3-scan-cache\.ini/);
  assert.ok(
    worker.indexOf("cachePaths.push(path)") <
      worker.indexOf("cachePaths.push(DEFAULT_CACHE_PATH)"),
    "user-provided caches should take priority over the built-in cache",
  );
  assert.match(adapter, /import sm2reaper/);
  assert.match(converter, /__version__ = "0\.2\.0"/);
  assert.match(defaultCache, /^\[vstcache\]/);

  await assert.rejects(
    access(new URL("app/_sites-preview/SkeletonPreview.tsx", root)),
  );
  await assert.rejects(
    access(new URL("app/_sites-preview/preview.css", root)),
  );
  await access(new URL("dist/client/index.html", root));
  await access(new URL("dist/client/converter-worker.mjs", root));
  await access(new URL("dist/client/default-vst3-scan-cache.ini", root));
});
