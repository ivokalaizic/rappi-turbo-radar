// Sube a Vercel Blob (privado) los archivos de `python -m turbo export`. Lo corre GitHub Actions.
// Uso: BLOB_READ_WRITE_TOKEN=... node web/scripts/upload.mjs data/export
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { put } from "@vercel/blob";

const dir = process.argv[2] || "data/export";
const files = (await readdir(dir)).sort((a, b) => (a === "index.json") - (b === "index.json")); // index al final
for (const name of files) {
  const body = await readFile(join(dir, name));
  await put(name, body, {
    access: "private", addRandomSuffix: false, allowOverwrite: true,
    contentType: name.endsWith(".gz") ? "application/gzip" : "application/json",
  });
  console.log(`subido ${name} (${Math.round(body.length / 1024)} KB)`);
}
