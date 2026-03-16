# Django-Bolt Benchmark
Generated: Mon 16 Mar 2026 01:55:21 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    171169.55   16056.50  185896.20
  Latency      564.00us   303.04us     5.43ms
  Latency Distribution
     50%   470.00us
     75%   674.00us
     90%     0.95ms
     99%     1.94ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    116400.10   12126.23  128467.12
  Latency      818.78us   331.64us     5.50ms
  Latency Distribution
     50%   735.00us
     75%     1.00ms
     90%     1.28ms
     99%     2.25ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    121763.55   10057.24  131016.21
  Latency      799.02us   337.57us     5.27ms
  Latency Distribution
     50%   709.00us
     75%     0.97ms
     90%     1.29ms
     99%     2.26ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    100342.30    8413.64  106140.17
  Latency        0.98ms   353.02us     5.02ms
  Latency Distribution
     50%     0.92ms
     75%     1.19ms
     90%     1.47ms
     99%     2.36ms
### Cookie Endpoint (/cookie)
  Reqs/sec     94216.40    6879.64  102200.52
  Latency        1.05ms   376.90us     5.04ms
  Latency Distribution
     50%     0.97ms
     75%     1.30ms
     90%     1.66ms
     99%     2.71ms
### Exception Endpoint (/exc)
  Reqs/sec    135710.25   12626.48  143277.56
  Latency      721.08us   304.87us     6.46ms
  Latency Distribution
     50%   658.00us
     75%     0.88ms
     90%     1.14ms
     99%     1.99ms
### HTML Response (/html)
  Reqs/sec    150435.91   14115.04  161285.26
  Latency      643.31us   411.33us     6.22ms
  Latency Distribution
     50%   539.00us
     75%   735.00us
     90%     1.13ms
     99%     3.02ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     35232.17    7367.40   39461.79
  Latency        2.84ms     1.45ms    21.66ms
  Latency Distribution
     50%     2.55ms
     75%     3.42ms
     90%     4.44ms
     99%     8.55ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     72795.59    5057.83   77449.33
  Latency        1.35ms   415.92us     5.21ms
  Latency Distribution
     50%     1.26ms
     75%     1.63ms
     90%     2.06ms
     99%     3.19ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     16906.09    1443.31   19166.16
  Latency        5.90ms     1.25ms    13.77ms
  Latency Distribution
     50%     5.82ms
     75%     6.84ms
     90%     7.70ms
     99%    10.07ms
### Get User via Dependency (/auth/me-dependency)
 2975 / 10000 [========================>---------------------------------------------------------]  29.75% 14830/s
 9075 / 10000 [==========================================================================>-------]  90.75% 15090/s
  Reqs/sec     15160.46     834.21   16563.58
  Latency        6.56ms     1.67ms    14.27ms
  Latency Distribution
     50%     6.64ms
     75%     7.92ms
     90%     9.05ms
     99%    11.19ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     79429.55    5639.93   86334.60
  Latency        1.22ms   429.22us     5.35ms
  Latency Distribution
     50%     1.09ms
     75%     1.56ms
     90%     2.07ms
     99%     3.09ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    148366.29   18693.95  167775.79
  Latency      622.61us   322.11us     5.99ms
  Latency Distribution
     50%   576.00us
     75%   682.00us
     90%   820.00us
     99%     2.27ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    142994.39   12642.19  154517.37
  Latency      665.90us   252.83us     5.18ms
  Latency Distribution
     50%   615.00us
     75%   758.00us
     90%     1.02ms
     99%     1.81ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14801.05    1923.87   23929.10
  Latency        6.83ms     1.56ms    20.71ms
  Latency Distribution
     50%     6.98ms
     75%     8.07ms
     90%     8.94ms
     99%    11.32ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10317.50    1098.58   13189.40
  Latency        9.60ms     4.06ms    30.53ms
  Latency Distribution
     50%     8.75ms
     75%    12.03ms
     90%    15.76ms
     99%    22.38ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     17433.32    2291.05   20092.78
  Latency        5.58ms     1.51ms    12.77ms
  Latency Distribution
     50%     5.43ms
     75%     7.04ms
     90%     8.06ms
     99%     9.80ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     11989.54     679.38   13447.85
  Latency        8.31ms     3.56ms    30.98ms
  Latency Distribution
     50%     7.66ms
     75%    10.21ms
     90%    13.18ms
     99%    20.89ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     98740.96    8391.17  109218.61
  Latency        0.99ms   321.48us     4.86ms
  Latency Distribution
     50%     0.93ms
     75%     1.23ms
     90%     1.54ms
     99%     2.36ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    101381.91    6864.73  108580.08
  Latency        0.97ms   311.64us     4.44ms
  Latency Distribution
     50%     0.91ms
     75%     1.20ms
     90%     1.54ms
     99%     2.25ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     62061.14    8083.23   68264.80
  Latency        1.55ms   465.53us     5.87ms
  Latency Distribution
     50%     1.47ms
     75%     1.86ms
     90%     2.32ms
     99%     3.55ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     94215.98    8078.48  100194.58
  Latency        1.05ms   342.78us     4.75ms
  Latency Distribution
     50%     0.97ms
     75%     1.32ms
     90%     1.67ms
     99%     2.47ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    100325.56    7556.51  105825.67
  Latency        0.98ms   347.79us     6.95ms
  Latency Distribution
     50%     0.91ms
     75%     1.20ms
     90%     1.52ms
     99%     2.50ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    100307.91    8934.32  105465.80
  Latency        0.98ms   336.91us     4.84ms
  Latency Distribution
     50%     0.90ms
     75%     1.23ms
     90%     1.56ms
     99%     2.47ms
### CBV Response Types (/cbv-response)
  Reqs/sec    109983.00    8781.15  116353.84
  Latency        0.89ms   269.22us     4.97ms
  Latency Distribution
     50%     0.85ms
     75%     1.08ms
     90%     1.34ms
     99%     1.91ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16329.43    1265.38   17812.83
  Latency        6.08ms     1.89ms    19.04ms
  Latency Distribution
     50%     5.83ms
     75%     7.21ms
     90%     9.07ms
     99%    12.28ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    132183.23   13325.76  145831.82
  Latency      735.06us   336.77us     5.75ms
  Latency Distribution
     50%   659.00us
     75%     0.92ms
     90%     1.15ms
     99%     2.14ms
### File Upload (POST /upload)
  Reqs/sec    114217.72    7874.02  123498.49
  Latency        0.85ms   389.74us     7.59ms
  Latency Distribution
     50%   768.00us
     75%     1.03ms
     90%     1.32ms
     99%     2.31ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    115074.67   12009.75  129253.02
  Latency        0.87ms   347.61us     5.62ms
  Latency Distribution
     50%   802.00us
     75%     0.98ms
     90%     1.31ms
     99%     2.30ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9304.35     956.13   10764.53
  Latency       10.72ms     2.58ms    25.52ms
  Latency Distribution
     50%    10.71ms
     75%    12.44ms
     90%    14.11ms
     99%    18.67ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    146012.25   11713.84  157582.73
  Latency      650.73us   383.23us     6.11ms
  Latency Distribution
     50%   582.00us
     75%   754.00us
     90%     0.96ms
     99%     2.23ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     99654.01   10749.75  116292.59
  Latency        1.02ms   310.02us     4.97ms
  Latency Distribution
     50%     0.95ms
     75%     1.25ms
     90%     1.56ms
     99%     2.31ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     86046.42    4837.14   91699.58
  Latency        1.14ms   371.29us     4.74ms
  Latency Distribution
     50%     1.07ms
     75%     1.39ms
     90%     1.76ms
     99%     2.81ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     98333.01    8485.81  103738.50
  Latency        0.99ms   316.39us     5.03ms
  Latency Distribution
     50%     0.94ms
     75%     1.21ms
     90%     1.52ms
     99%     2.30ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec     99671.12    8316.88  107173.43
  Latency        0.99ms   345.26us     5.08ms
  Latency Distribution
     50%     0.92ms
     75%     1.22ms
     90%     1.53ms
     99%     2.38ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    104526.13    8500.09  109440.41
  Latency        0.94ms   307.44us     4.54ms
  Latency Distribution
     50%     0.87ms
     75%     1.16ms
     90%     1.45ms
     99%     2.18ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    175552.68   17023.98  185573.53
  Latency      556.40us   311.83us     5.34ms
  Latency Distribution
     50%   476.00us
     75%   666.00us
     90%     0.92ms
     99%     2.01ms

### Path Parameter - int (/items/12345)
  Reqs/sec    158351.48   14701.02  168321.28
  Latency      614.65us   361.51us     5.47ms
  Latency Distribution
     50%   553.00us
     75%   687.00us
     90%     0.88ms
     99%     2.16ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    153661.65   13872.43  167005.27
  Latency      629.98us   308.63us     6.19ms
  Latency Distribution
     50%   549.00us
     75%   754.00us
     90%     0.99ms
     99%     1.88ms

### Header Parameter (/header)
  Reqs/sec    101234.71    8477.51  107710.37
  Latency        0.97ms   343.10us     6.07ms
  Latency Distribution
     50%     0.90ms
     75%     1.17ms
     90%     1.50ms
     99%     2.28ms

### Cookie Parameter (/cookie)
  Reqs/sec    103889.79    9328.12  110016.31
  Latency        0.94ms   308.42us     4.72ms
  Latency Distribution
     50%     0.89ms
     75%     1.17ms
     90%     1.46ms
     99%     2.24ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     83544.84    5808.34   87354.81
  Latency        1.17ms   501.73us     6.02ms
  Latency Distribution
     50%     1.02ms
     75%     1.40ms
     90%     1.99ms
     99%     3.56ms
