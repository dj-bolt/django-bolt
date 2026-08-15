// Elysia on Bun: `bun server-bun-elysia.mjs`
//
// Env: HOST, PORT. Uses Elysia's own `.listen()` — its documented entrypoint,
// which compiles the route handlers — rather than driving `app.fetch` by hand,
// so Elysia is measured on its recommended path.
//
// `reusePort` lets N instances share one port; run this file once per process
// to scale (see compare.sh), since Bun has no cluster module.
import app from "./app-elysia.mjs";

app.listen(
  {
    hostname: process.env.HOST ?? "127.0.0.1",
    port: Number(process.env.PORT ?? 8004),
    reusePort: true,
  },
  (server) => {
    console.log(`elysia/bun listening on http://${server.hostname}:${server.port}`);
  },
);
