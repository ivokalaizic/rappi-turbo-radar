// Contraseña para toda la página (Basic Auth). Se configura en Vercel: variable UI_PASSWORD.
import { next } from "@vercel/functions";

export const config = { matcher: "/:path*" };

export default function middleware(request) {
  const password = process.env.UI_PASSWORD;
  const auth = request.headers.get("authorization") || "";
  const [scheme, encoded] = auth.split(" ");
  if (password && scheme === "Basic" && encoded) {
    const given = atob(encoded).split(":").slice(1).join(":");
    if (given === password) return next();
  }
  return new Response("Necesitás la contraseña.", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Turbo Radar", charset="UTF-8"' },
  });
}
