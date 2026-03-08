# Django-Bolt Benchmark
Generated: Sun 08 Mar 2026 04:40:37 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    168772.82   10874.85  175061.92
  Latency      567.51us   244.18us     4.71ms
  Latency Distribution
     50%   519.00us
     75%   674.00us
     90%     0.87ms
     99%     1.76ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    112134.03    7479.22  118765.68
  Latency        0.87ms   311.84us     5.04ms
  Latency Distribution
     50%   822.00us
     75%     1.04ms
     90%     1.31ms
     99%     2.34ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    112136.52   23667.92  128300.25
  Latency        0.90ms     0.96ms    15.44ms
  Latency Distribution
     50%   713.00us
     75%     1.07ms
     90%     1.39ms
     99%     3.08ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    103789.52    7970.14  109806.07
  Latency        0.95ms   346.57us     5.06ms
  Latency Distribution
     50%     0.87ms
     75%     1.17ms
     90%     1.46ms
     99%     2.31ms
### Cookie Endpoint (/cookie)
  Reqs/sec    100821.99    6596.85  107773.96
  Latency        0.97ms   293.37us     4.46ms
  Latency Distribution
     50%     0.91ms
     75%     1.21ms
     90%     1.55ms
     99%     2.21ms
### Exception Endpoint (/exc)
  Reqs/sec    132312.26   13049.56  140079.17
  Latency      744.85us   332.90us     6.46ms
  Latency Distribution
     50%   697.00us
     75%     0.86ms
     90%     1.09ms
     99%     1.98ms
### HTML Response (/html)
  Reqs/sec    144390.87   12257.61  157078.15
  Latency      669.02us   376.40us     6.71ms
  Latency Distribution
     50%   578.00us
     75%   812.00us
     90%     1.09ms
     99%     3.00ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     36361.95    8136.85   41655.87
  Latency        2.74ms     1.40ms    18.89ms
  Latency Distribution
     50%     2.42ms
     75%     3.19ms
     90%     4.14ms
     99%     9.81ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     74800.80    4747.87   77447.11
  Latency        1.31ms   359.16us     5.36ms
  Latency Distribution
     50%     1.27ms
     75%     1.65ms
     90%     2.05ms
     99%     2.74ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     16865.38    1550.96   18982.12
  Latency        5.91ms     1.18ms    13.62ms
  Latency Distribution
     50%     5.92ms
     75%     6.66ms
     90%     7.43ms
     99%    10.09ms
### Get User via Dependency (/auth/me-dependency)
 5975 / 10000 [=========================================================================================>------------------------------------------------------------]  59.75% 14890/s
  Reqs/sec     14554.52    1502.58   15745.97
  Latency        6.84ms     2.11ms    24.57ms
  Latency Distribution
     50%     6.69ms
     75%     8.28ms
     90%     9.72ms
     99%    13.45ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     58291.30    4167.45   63511.39
  Latency        1.68ms   644.51us     8.45ms
  Latency Distribution
     50%     1.60ms
     75%     2.15ms
     90%     2.79ms
     99%     4.22ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    109473.67   18624.64  119472.35
  Latency        0.90ms   644.28us     9.41ms
  Latency Distribution
     50%   714.00us
     75%     1.04ms
     90%     1.62ms
     99%     4.00ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     96422.49    6852.84  104309.35
  Latency        1.01ms   657.21us     6.22ms
  Latency Distribution
     50%   781.00us
     75%     1.28ms
     90%     1.99ms
     99%     4.20ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14657.72    1106.08   16732.03
  Latency        6.80ms     1.31ms    16.10ms
  Latency Distribution
     50%     6.83ms
     75%     8.07ms
     90%     8.72ms
     99%    10.80ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     11872.20   10321.48   82261.65
  Latency        9.56ms     4.05ms    27.89ms
  Latency Distribution
     50%     9.10ms
     75%    12.34ms
     90%    15.72ms
     99%    21.25ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     17806.15    1409.26   23418.75
  Latency        5.63ms     1.64ms    16.10ms
  Latency Distribution
     50%     5.40ms
     75%     6.79ms
     90%     8.47ms
     99%    10.58ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12044.09    1017.44   14647.10
  Latency        8.27ms     2.87ms    23.29ms
  Latency Distribution
     50%     8.01ms
     75%    10.32ms
     90%    12.57ms
     99%    16.79ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    101042.86    8074.88  107524.29
  Latency        0.97ms   308.09us     3.74ms
  Latency Distribution
     50%     0.90ms
     75%     1.20ms
     90%     1.54ms
     99%     2.37ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    105457.25    7573.23  113547.62
  Latency        0.92ms   255.86us     4.33ms
  Latency Distribution
     50%     0.88ms
     75%     1.13ms
     90%     1.42ms
     99%     2.02ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     66022.63    4482.97   69664.27
  Latency        1.49ms   472.98us     5.45ms
  Latency Distribution
     50%     1.37ms
     75%     1.84ms
     90%     2.34ms
     99%     3.69ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    104669.61   17764.10  137262.08
  Latency        1.00ms   325.64us     4.61ms
  Latency Distribution
     50%     0.95ms
     75%     1.23ms
     90%     1.54ms
     99%     2.50ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    100802.87    8247.73  106563.34
  Latency        0.98ms   349.36us     5.49ms
  Latency Distribution
     50%     0.90ms
     75%     1.22ms
     90%     1.55ms
     99%     2.39ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    105607.78    9230.33  111260.35
  Latency        0.93ms   316.79us     4.79ms
  Latency Distribution
     50%     0.87ms
     75%     1.16ms
     90%     1.45ms
     99%     2.23ms
### CBV Response Types (/cbv-response)
  Reqs/sec    112853.32   11057.13  122369.44
  Latency        0.88ms   288.93us     4.59ms
  Latency Distribution
     50%   828.00us
     75%     1.07ms
     90%     1.31ms
     99%     1.96ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16662.08    1801.29   22802.32
  Latency        6.03ms     1.43ms    16.91ms
  Latency Distribution
     50%     5.83ms
     75%     7.12ms
     90%     8.22ms
     99%    10.73ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    130198.02   15641.78  144273.17
  Latency      744.44us   311.40us     5.11ms
  Latency Distribution
     50%   655.00us
     75%     0.93ms
     90%     1.19ms
     99%     2.26ms
### File Upload (POST /upload)
  Reqs/sec    111208.56    6240.72  117078.69
  Latency        0.87ms   307.42us     5.46ms
  Latency Distribution
     50%   825.00us
     75%     1.06ms
     90%     1.31ms
     99%     2.00ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    110506.88   11094.49  118746.57
  Latency        0.89ms   356.56us     5.81ms
  Latency Distribution
     50%     0.89ms
     75%     1.05ms
     90%     1.32ms
     99%     2.12ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9654.82    1323.43   15772.65
  Latency       10.42ms     3.25ms    22.59ms
  Latency Distribution
     50%    10.28ms
     75%    13.50ms
     90%    15.05ms
     99%    19.22ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    141924.57   13582.88  159034.36
  Latency      666.33us   337.02us     7.11ms
  Latency Distribution
     50%   580.00us
     75%   762.00us
     90%     1.05ms
     99%     1.94ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     95705.14    8794.07  105981.11
  Latency        1.02ms   339.13us     4.99ms
  Latency Distribution
     50%     0.94ms
     75%     1.28ms
     90%     1.61ms
     99%     2.39ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     82497.67    8881.85   91438.51
  Latency        1.19ms   491.43us     5.24ms
  Latency Distribution
     50%     1.08ms
     75%     1.50ms
     90%     1.99ms
     99%     3.40ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    100074.17    7966.91  105418.73
  Latency        0.98ms   292.98us     4.38ms
  Latency Distribution
     50%     0.93ms
     75%     1.22ms
     90%     1.53ms
     99%     2.18ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    104536.52    7443.97  110226.21
  Latency        0.94ms   272.52us     4.72ms
  Latency Distribution
     50%     0.88ms
     75%     1.15ms
     90%     1.43ms
     99%     2.06ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    105462.51    8015.75  109712.09
  Latency        0.94ms   261.31us     4.09ms
  Latency Distribution
     50%     0.87ms
     75%     1.16ms
     90%     1.47ms
     99%     2.11ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    177093.25   15530.89  187728.72
  Latency      547.83us   259.39us     6.14ms
  Latency Distribution
     50%   508.00us
     75%   661.00us
     90%   846.00us
     99%     1.77ms

### Path Parameter - int (/items/12345)
  Reqs/sec    151037.20   11113.15  164068.41
  Latency      632.15us   228.37us     3.50ms
  Latency Distribution
     50%   563.00us
     75%   801.00us
     90%     0.97ms
     99%     1.79ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    149258.31   11998.76  158296.09
  Latency      654.58us   267.54us     5.25ms
  Latency Distribution
     50%   595.00us
     75%   767.00us
     90%     1.00ms
     99%     1.72ms

### Header Parameter (/header)
  Reqs/sec    102926.87    8902.22  111030.76
  Latency        0.95ms   339.80us     4.80ms
  Latency Distribution
     50%     0.86ms
     75%     1.19ms
     90%     1.56ms
     99%     2.37ms

### Cookie Parameter (/cookie)
  Reqs/sec     93843.28   11324.04  103163.45
  Latency        1.04ms   450.79us     7.07ms
  Latency Distribution
     50%     0.95ms
     75%     1.27ms
     90%     1.61ms
     99%     2.78ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     84658.51    7144.54   90097.12
  Latency        1.16ms   368.90us     5.00ms
  Latency Distribution
     50%     1.07ms
     75%     1.43ms
     90%     1.80ms
     99%     2.80ms
