import { copyFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = resolve(here, "..", "..", "sm2reaper.py");
const destination = resolve(here, "..", "public", "sm2reaper.py");

await mkdir(dirname(destination), { recursive: true });
await copyFile(source, destination);
console.log("Synchronized the browser converter with sm2reaper.py");
