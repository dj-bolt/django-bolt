// Shared payloads for the JS comparison apps.
//
// These are the exact files the Django-Bolt example project serves from
// python/example/test_data/, so every runtime puts identical bytes on the wire.
// Parsed once at startup; serialization happens per request in each app.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const dataDir = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "python",
  "example",
  "test_data",
);

const load = (name) => JSON.parse(readFileSync(join(dataDir, name), "utf8"));

// Same body as Bolt's root route, down to the byte, so `/` is comparable too.
export const ROOT = { message: "Hello Django" };
export const JSON_1K = load("1K.json");
export const JSON_10K = load("10K.json");
