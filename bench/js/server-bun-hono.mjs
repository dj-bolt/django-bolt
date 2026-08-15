// Hono on Bun: `bun server-bun-hono.mjs`
//
// Env: HOST, PORT. `Bun.serve({ fetch: app.fetch })` is Hono's documented Bun
// entrypoint. `reusePort` lets N instances share one port; run this file once
// per process to scale (see compare.sh), since Bun has no cluster module.
import app from "./app-hono.mjs";

const server = Bun.serve({
  fetch: app.fetch,
  hostname: process.env.HOST ?? "127.0.0.1",
  port: Number(process.env.PORT ?? 8003),
  reusePort: true,
});

console.log(`hono/bun listening on http://${server.hostname}:${server.port}`);
