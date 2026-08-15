// Hono app for the Django-Bolt comparison benchmark.
//
// Routes mirror python/example/testproject/api.py one-for-one:
//   GET /          -> tiny body, routing-overhead control (also the readiness probe)
//   GET /1k-json   -> test_data/1K.json   (1171 B serialized)
//   GET /10k-json  -> test_data/10K.json  (10864 B serialized)
//
// Like Bolt, the payload is serialized on every request (JSON.stringify via
// c.json), not pre-encoded once at startup.
import { Hono } from "hono";

import { JSON_10K, JSON_1K, ROOT } from "./data.mjs";

const app = new Hono();

app.get("/", (c) => c.json(ROOT));
app.get("/1k-json", (c) => c.json(JSON_1K));
app.get("/10k-json", (c) => c.json(JSON_10K));

export default app;
