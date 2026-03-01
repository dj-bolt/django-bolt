# Django-Bolt Benchmark
Generated: Sun 01 Mar 2026 10:20:03 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec      4956.30     752.06    5916.49
  Latency       20.07ms     3.35ms    37.71ms
  Latency Distribution
     50%    18.79ms
     75%    20.03ms
     90%    25.83ms
     99%    32.90ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec      4836.92     302.87    5230.24
  Latency       20.57ms     1.46ms    32.81ms
  Latency Distribution
     50%    20.38ms
     75%    20.99ms
     90%    21.78ms
     99%    27.41ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec      4731.51     199.68    5083.09
  Latency       21.02ms     1.31ms    24.46ms
  Latency Distribution
     50%    20.93ms
     75%    21.57ms
     90%    22.35ms
     99%    23.96ms

## Response Type Endpoints
### Header Endpoint (/header)
