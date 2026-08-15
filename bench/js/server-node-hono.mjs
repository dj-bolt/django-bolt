// Hono on Node: `node server-node-hono.mjs`
//
// Env:
//   HOST      bind address (default 127.0.0.1)
//   PORT      bind port    (default 8002)
//   PROCESSES worker processes (default 1) — matches `runbolt --processes N`.
//             Node's cluster module load-balances via SO_REUSEPORT on Linux,
//             the same mechanism Bolt uses.
import cluster from "node:cluster";
import { availableParallelism } from "node:os";

import { serve } from "@hono/node-server";

import app from "./app-hono.mjs";

const host = process.env.HOST ?? "127.0.0.1";
const port = Number(process.env.PORT ?? 8002);
const processes = Number(process.env.PROCESSES ?? 1);

if (processes > 1 && cluster.isPrimary) {
  console.log(
    `hono/node listening on http://${host}:${port} (${processes} processes, ${availableParallelism()} cores)`,
  );
  cluster.schedulingPolicy = cluster.SCHED_NONE; // let the OS balance, like SO_REUSEPORT
  for (let i = 0; i < processes; i += 1) cluster.fork();
  cluster.on("exit", (worker, code, signal) => {
    console.error(`worker ${worker.process.pid} exited (${signal || code})`);
  });
} else {
  serve({ fetch: app.fetch, hostname: host, port }, (info) => {
    if (processes === 1) {
      console.log(`hono/node listening on http://${info.address}:${info.port}`);
    }
  });
}
