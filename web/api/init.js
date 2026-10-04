import { json, readIndex } from "./_blob.js";

export async function GET() {
  const index = await readIndex();
  return json({ cloud: true, ...index });
}
