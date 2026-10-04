// Los resúmenes que sube GitHub Actions (python -m turbo export) viven en Vercel Blob, privados.
import { get } from "@vercel/blob";

export async function readBlob(pathname) {
  const res = await get(pathname, { access: "private", useCache: false });
  return res && res.statusCode === 200 ? res.stream : null;
}

export async function readIndex() {
  const stream = await readBlob("index.json");
  return stream ? JSON.parse(await new Response(stream).text()) : { stores: [] };
}

export const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
