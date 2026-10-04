import { json, readBlob } from "./_blob.js";

export async function GET(request) {
  const storeId = Number(new URL(request.url).searchParams.get("store_id"));
  if (!Number.isInteger(storeId)) return json({ error: "store_id inválido" }, 400);
  const stream = await readBlob(`store-${storeId}.json.gz`);
  if (!stream) return json({ error: "No hay datos de esa tienda" }, 404);
  // Ya está comprimido: el navegador lo descomprime solo
  return new Response(stream, {
    headers: { "Content-Type": "application/json; charset=utf-8", "Content-Encoding": "gzip", "Cache-Control": "no-store" },
  });
}
