# Django-Bolt Benchmark
Generated: Sun 24 May 2026 10:04:32 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec     47506.96    2838.61   49630.31
  Latency        2.05ms   476.78us     7.43ms
  Latency Distribution
     50%     1.89ms
     75%     2.60ms
     90%     3.24ms
     99%     3.84ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec     41543.13    2018.16   43959.48
  Latency        2.36ms   617.97us     7.83ms
  Latency Distribution
     50%     2.26ms
     75%     2.54ms
     90%     3.82ms
     99%     4.80ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec     41464.49    1798.08   42512.23
  Latency        2.38ms   609.39us     6.77ms
  Latency Distribution
     50%     2.12ms
     75%     2.91ms
     90%     3.79ms
     99%     4.51ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec     31115.38    1687.93   33144.94
  Latency        3.19ms     1.15ms    10.27ms
  Latency Distribution
     50%     3.02ms
     75%     3.98ms
     90%     5.19ms
     99%     7.51ms
### Cookie Endpoint (/cookie)
  Reqs/sec     31105.76    1577.77   32809.71
  Latency        3.19ms     1.18ms     9.71ms
  Latency Distribution
     50%     3.05ms
     75%     4.25ms
     90%     5.09ms
     99%     7.25ms
### Exception Endpoint (/exc)
  Reqs/sec     42675.69    2154.53   43943.65
  Latency        2.29ms   563.57us     6.93ms
  Latency Distribution
     50%     2.06ms
     75%     2.52ms
     90%     3.86ms
     99%     4.35ms
### HTML Response (/html)
  Reqs/sec     50369.82   19114.77  110497.24
  Latency        2.20ms   546.80us     6.32ms
  Latency Distribution
     50%     2.06ms
     75%     2.76ms
     90%     3.04ms
     99%     4.32ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     19339.25    1954.97   21212.86
  Latency        5.15ms     1.48ms    15.57ms
  Latency Distribution
     50%     4.76ms
     75%     5.77ms
     90%     7.65ms
     99%    10.59ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec     45056.99    1980.65   46148.32
  Latency        2.19ms   424.41us     5.29ms
  Latency Distribution
     50%     2.13ms
     75%     2.33ms
     90%     3.24ms
     99%     3.87ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec     42502.50    8743.55   45870.70
  Latency        2.19ms   500.21us     5.77ms
  Latency Distribution
     50%     2.12ms
     75%     2.62ms
     90%     3.43ms
     99%     4.06ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     34054.95    1694.14   36469.38
  Latency        2.88ms   525.64us     9.52ms
  Latency Distribution
     50%     2.84ms
     75%     3.58ms
     90%     4.06ms
     99%     4.46ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     33620.35    1410.77   35359.52
  Latency        2.94ms   448.95us     7.26ms
  Latency Distribution
     50%     2.94ms
     75%     3.16ms
     90%     3.81ms
     99%     4.44ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     21666.01    1213.04   24304.75
  Latency        4.58ms     1.45ms    12.07ms
  Latency Distribution
     50%     4.42ms
     75%     5.66ms
     90%     6.55ms
     99%     9.94ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     10772.50     934.59   13022.86
  Latency        9.27ms     2.43ms    20.12ms
  Latency Distribution
     50%     8.96ms
     75%    10.87ms
     90%    13.03ms
     99%    16.89ms
### Get User via Dependency (/auth/me-dependency)
 5790 / 10000 [================================================================================================================================================================================>--------------------------------------------------------------------------------------------------------------------------------]  57.90% 9616/s
  Reqs/sec      9197.31    1178.16   11165.92
  Latency       10.74ms     4.98ms    41.84ms
  Latency Distribution
     50%     9.94ms
     75%    14.04ms
     90%    17.78ms
     99%    26.05ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     20419.72    1230.86   21871.63
  Latency        4.83ms     2.13ms    17.70ms
  Latency Distribution
     50%     4.23ms
     75%     5.72ms
     90%     8.52ms
     99%    13.81ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec     39350.29    1686.69   41441.47
  Latency        2.51ms   534.67us    10.38ms
  Latency Distribution
     50%     2.40ms
     75%     2.93ms
     90%     3.49ms
     99%     4.39ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     35909.25    1312.40   37399.07
  Latency        2.75ms   478.22us     5.78ms
  Latency Distribution
     50%     2.57ms
     75%     3.18ms
     90%     3.54ms
     99%     4.62ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     11624.58     930.17   12739.79
  Latency        8.57ms     2.79ms    23.27ms
  Latency Distribution
     50%     8.17ms
     75%    10.14ms
     90%    12.48ms
     99%    18.70ms
### Users Full10 (Sync) (/users/sync-full10)
 3750 / 10000 [==================================================================================================================>----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  37.50% 9322/s
  Reqs/sec      9337.33     935.43   10765.44
  Latency       10.58ms     4.96ms    38.13ms
  Latency Distribution
     50%    10.37ms
     75%    13.94ms
     90%    18.13ms
     99%    24.55ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     13266.25     922.61   14534.59
  Latency        7.50ms     2.19ms    17.72ms
  Latency Distribution
     50%     7.37ms
     75%     8.91ms
     90%    10.60ms
     99%    14.89ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     10862.16    1002.66   16142.39
  Latency        9.23ms     2.94ms    25.67ms
  Latency Distribution
     50%     8.79ms
     75%    11.06ms
     90%    13.17ms
     99%    19.45ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     31751.01    1589.50   33591.60
  Latency        3.11ms     1.11ms     9.61ms
  Latency Distribution
     50%     2.76ms
     75%     4.11ms
     90%     4.99ms
     99%     7.44ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     33068.21    1813.78   36803.06
  Latency        3.01ms   763.97us     7.35ms
  Latency Distribution
     50%     2.90ms
     75%     3.51ms
     90%     4.29ms
     99%     5.89ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     26281.10    1404.21   28371.33
  Latency        3.79ms   846.88us     8.71ms
  Latency Distribution
     50%     3.62ms
     75%     4.32ms
     90%     5.22ms
     99%     6.93ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     31345.72    1607.17   33382.39
  Latency        3.15ms     0.88ms     8.53ms
  Latency Distribution
     50%     3.04ms
     75%     3.75ms
     90%     4.59ms
     99%     6.49ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     30147.70    1775.31   32596.82
  Latency        3.29ms     0.97ms     9.35ms
  Latency Distribution
     50%     3.21ms
     75%     3.88ms
     90%     4.90ms
     99%     6.79ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     32578.74    1812.38   34285.09
  Latency        3.05ms   842.38us     9.74ms
  Latency Distribution
     50%     2.90ms
     75%     3.58ms
     90%     4.41ms
     99%     6.28ms
### CBV Response Types (/cbv-response)
  Reqs/sec     34172.33    1684.72   35312.94
  Latency        2.90ms     0.95ms     7.37ms
  Latency Distribution
     50%     2.90ms
     75%     3.67ms
     90%     4.37ms
     99%     6.14ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     11972.89    1003.36   13608.41
  Latency        8.31ms     3.08ms    24.68ms
  Latency Distribution
     50%     8.11ms
     75%    10.38ms
     90%    12.44ms
     99%    17.74ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec     33246.62    1312.77   34073.08
  Latency        2.96ms   767.92us     6.99ms
  Latency Distribution
     50%     2.88ms
     75%     3.90ms
     90%     4.73ms
     99%     5.38ms
### File Upload (POST /upload)
  Reqs/sec     25649.32    1383.60   26350.88
  Latency        3.83ms   418.74us     8.28ms
  Latency Distribution
     50%     3.75ms
     75%     4.54ms
     90%     4.69ms
     99%     5.11ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec     24047.08    2851.59   25030.24
  Latency        4.02ms   554.81us     9.04ms
  Latency Distribution
     50%     3.79ms
     75%     4.93ms
     90%     5.37ms
     99%     6.01ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      7110.77     706.39    9809.49
  Latency       14.06ms     3.54ms    31.99ms
  Latency Distribution
     50%    13.62ms
     75%    16.60ms
     90%    19.44ms
     99%    24.26ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec     39465.35    2482.97   41466.29
  Latency        2.46ms   679.43us    11.65ms
  Latency Distribution
     50%     2.34ms
     75%     2.98ms
     90%     4.11ms
     99%     4.76ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     39547.14    1672.06   41902.98
  Latency        2.48ms   453.86us     7.99ms
  Latency Distribution
     50%     2.47ms
     75%     3.00ms
     90%     3.43ms
     99%     4.06ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     30239.68    2144.84   33235.83
  Latency        3.30ms     0.95ms     9.66ms
  Latency Distribution
     50%     3.19ms
     75%     3.84ms
     90%     4.82ms
     99%     6.71ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     39260.66    1379.96   40206.55
  Latency        2.51ms   484.03us     6.34ms
  Latency Distribution
     50%     2.55ms
     75%     3.11ms
     90%     3.32ms
     99%     4.17ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec     33567.05    1430.02   34397.09
  Latency        2.95ms   716.32us     7.21ms
  Latency Distribution
     50%     2.76ms
     75%     3.43ms
     90%     4.12ms
     99%     5.92ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec     33642.94    1749.97   35207.80
  Latency        2.95ms   690.37us     7.25ms
  Latency Distribution
     50%     2.85ms
     75%     3.42ms
     90%     4.11ms
     99%     5.56ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec     31209.18    1464.34   32672.60
  Latency        3.18ms   782.91us     8.60ms
  Latency Distribution
     50%     3.03ms
     75%     3.69ms
     90%     4.47ms
     99%     6.32ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec     31011.70    1357.80   32176.13
  Latency        3.19ms     0.90ms     7.94ms
  Latency Distribution
     50%     3.19ms
     75%     3.83ms
     90%     4.65ms
     99%     6.27ms

### Single union item — Like branch (/feed/2)
  Reqs/sec     30411.86    2284.91   32421.33
  Latency        3.21ms     0.97ms     8.39ms
  Latency Distribution
     50%     2.99ms
     75%     3.95ms
     90%     4.69ms
     99%     6.93ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     34115.97    1334.05   34817.07
  Latency        2.89ms   531.52us     7.18ms
  Latency Distribution
     50%     2.59ms
     75%     3.56ms
     90%     4.11ms
     99%     4.78ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec     47357.65    2768.08   51195.43
  Latency        2.07ms   498.24us     5.88ms
  Latency Distribution
     50%     2.06ms
     75%     2.61ms
     90%     2.95ms
     99%     4.00ms

### Path Parameter - int (/items/12345)
  Reqs/sec     39676.22    2220.25   41210.99
  Latency        2.48ms   655.35us     7.30ms
  Latency Distribution
     50%     2.19ms
     75%     3.17ms
     90%     3.86ms
     99%     4.88ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec     39744.48    1814.42   40789.62
  Latency        2.47ms   485.83us     5.61ms
  Latency Distribution
     50%     2.19ms
     75%     3.13ms
     90%     3.73ms
     99%     4.23ms

### Header Parameter (/header)
  Reqs/sec     30588.85    1916.76   34015.33
  Latency        3.24ms     1.08ms     9.73ms
  Latency Distribution
     50%     3.00ms
     75%     4.00ms
     90%     5.05ms
     99%     7.20ms

### Cookie Parameter (/cookie)
  Reqs/sec     31270.38    1492.14   32980.06
  Latency        3.17ms     0.89ms     9.32ms
  Latency Distribution
     50%     2.98ms
     75%     3.78ms
     90%     4.59ms
     99%     6.80ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     21710.07    1216.35   23453.70
  Latency        4.57ms     1.63ms    13.73ms
  Latency Distribution
     50%     4.38ms
     75%     5.85ms
     90%     6.80ms
     99%    10.44ms
