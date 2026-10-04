// Misma forma que data/schedule.json de la UI local: GitHub corre cada 15 min (con algo de atraso).
import { json, readIndex } from "./_blob.js";

export async function GET() {
  const { stores } = await readIndex();
  const runs = stores.map(s => s.run).filter(Boolean);
  if (!runs.length) return json({});
  return json({
    interval: 900,
    last_start: Math.max(...runs.map(r => r.started)),
    last_end: Math.max(...runs.map(r => r.finished)),
    running: false,
  });
}
