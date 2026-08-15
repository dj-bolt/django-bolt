// Elysia app for the Django-Bolt comparison benchmark.
//
// Same three routes and same payload files as app-hono.mjs. Returning a plain
// object is Elysia's idiomatic JSON response: it serializes per request, so the
// measurement includes serialization cost exactly as it does for Hono and Bolt.
import { Elysia } from "elysia";

import { JSON_10K, JSON_1K, ROOT } from "./data.mjs";

const app = new Elysia()
  .get("/", () => ROOT)
  .get("/1k-json", () => JSON_1K)
  .get("/10k-json", () => JSON_10K);

export default app;
