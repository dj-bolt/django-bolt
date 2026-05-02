# Django-Bolt Benchmark
Generated: Sun 03 May 2026 12:02:38 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    176273.15   19828.67  189280.75
  Latency      543.26us   244.54us     5.56ms
  Latency Distribution
     50%   493.00us
     75%   627.00us
     90%   847.00us
     99%     1.66ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    117933.18   10634.25  128249.69
  Latency      814.31us   326.40us     5.45ms
  Latency Distribution
     50%   794.00us
     75%     0.97ms
     90%     1.17ms
     99%     1.89ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    122423.32   13442.84  131247.13
  Latency      801.80us   337.88us     6.02ms
  Latency Distribution
     50%   757.00us
     75%     0.94ms
     90%     1.24ms
     99%     2.23ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    103181.69    7788.16  110424.08
  Latency        0.96ms   307.97us     4.44ms
  Latency Distribution
     50%     0.89ms
     75%     1.17ms
     90%     1.50ms
     99%     2.30ms
### Cookie Endpoint (/cookie)
  Reqs/sec    102929.47    8263.76  107629.93
  Latency        0.95ms   324.75us     4.37ms
  Latency Distribution
     50%     0.87ms
     75%     1.18ms
     90%     1.54ms
     99%     2.37ms
### Exception Endpoint (/exc)
  Reqs/sec    140050.99   11628.39  148814.62
  Latency      697.43us   308.04us     5.48ms
  Latency Distribution
     50%   624.00us
     75%   818.00us
     90%     1.05ms
     99%     1.85ms
### HTML Response (/html)
  Reqs/sec    167332.61   21659.68  186203.81
  Latency      565.20us   411.13us    12.40ms
  Latency Distribution
     50%   488.00us
     75%   658.00us
     90%     0.86ms
     99%     2.06ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
 7290 / 10000 [===============================================================================>-----------------------------]  72.90% 36343/s
  Reqs/sec     37855.49    7111.00   41907.67
  Latency        2.64ms     1.18ms    19.68ms
  Latency Distribution
     50%     2.40ms
     75%     3.11ms
     90%     4.01ms
     99%     7.44ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     77398.52    5581.93   82260.43
  Latency        1.28ms   374.29us     5.69ms
  Latency Distribution
     50%     1.22ms
     75%     1.55ms
     90%     1.92ms
     99%     2.76ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     17090.08    1566.88   21741.76
  Latency        5.86ms     1.36ms    13.82ms
  Latency Distribution
     50%     5.80ms
     75%     7.00ms
     90%     7.80ms
     99%     9.91ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14978.18     867.38   16659.08
  Latency        6.65ms     1.89ms    16.65ms
  Latency Distribution
     50%     6.37ms
     75%     7.88ms
     90%     9.51ms
     99%    13.40ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     85287.45   11194.80  107606.65
  Latency        1.20ms   403.05us     6.34ms
  Latency Distribution
     50%     1.10ms
     75%     1.50ms
     90%     1.95ms
     99%     2.88ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    154651.26   14734.46  167357.14
  Latency      620.39us   271.70us     6.58ms
  Latency Distribution
     50%   551.00us
     75%   765.00us
     90%     0.91ms
     99%     1.81ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    146195.76   12306.21  153839.25
  Latency      663.00us   292.30us     5.49ms
  Latency Distribution
     50%   590.00us
     75%   751.00us
     90%     0.96ms
     99%     2.05ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
 8499 / 10000 [============================================================================================>----------------]  84.99% 14130/s
  Reqs/sec     14242.54    1106.67   15504.67
  Latency        6.99ms     2.05ms    21.25ms
  Latency Distribution
     50%     6.24ms
     75%     8.93ms
     90%    10.79ms
     99%    12.44ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10126.25    1058.91   13545.24
  Latency        9.86ms     4.40ms    32.41ms
  Latency Distribution
     50%     9.03ms
     75%    12.51ms
     90%    16.64ms
     99%    23.64ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     16863.00    1957.04   19035.18
  Latency        5.80ms     1.53ms    14.54ms
  Latency Distribution
     50%     5.57ms
     75%     6.64ms
     90%     8.67ms
     99%    10.68ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     11942.07     843.17   15178.88
  Latency        8.38ms     3.20ms    26.55ms
  Latency Distribution
     50%     7.73ms
     75%    10.12ms
     90%    12.96ms
     99%    19.63ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    102228.69    9462.70  109643.02
  Latency        0.96ms   354.38us     6.09ms
  Latency Distribution
     50%     0.89ms
     75%     1.23ms
     90%     1.54ms
     99%     2.36ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    100034.38    7061.95  105273.83
  Latency        0.98ms   339.08us     4.62ms
  Latency Distribution
     50%     0.90ms
     75%     1.22ms
     90%     1.60ms
     99%     2.51ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     65433.87    4213.88   69189.87
  Latency        1.51ms   410.01us     5.20ms
  Latency Distribution
     50%     1.44ms
     75%     1.83ms
     90%     2.28ms
     99%     3.16ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    102483.91    6287.01  106061.64
  Latency        0.96ms   306.51us     4.58ms
  Latency Distribution
     50%     0.89ms
     75%     1.18ms
     90%     1.49ms
     99%     2.24ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    109829.44   26894.76  163283.63
  Latency        0.99ms   343.34us     4.81ms
  Latency Distribution
     50%     0.91ms
     75%     1.23ms
     90%     1.59ms
     99%     2.37ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    102441.64    7075.32  108065.32
  Latency        0.96ms   326.55us     4.45ms
  Latency Distribution
     50%     0.88ms
     75%     1.20ms
     90%     1.55ms
     99%     2.38ms
### CBV Response Types (/cbv-response)
  Reqs/sec    106436.37    7311.15  110495.07
  Latency        0.92ms   307.95us     5.12ms
  Latency Distribution
     50%     0.86ms
     75%     1.14ms
     90%     1.42ms
     99%     2.07ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     15535.82    1130.10   16496.46
  Latency        6.40ms     1.55ms    17.14ms
  Latency Distribution
     50%     6.61ms
     75%     7.74ms
     90%     8.70ms
     99%    10.30ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    136706.41   12654.17  146726.16
  Latency      717.86us   358.95us     6.11ms
  Latency Distribution
     50%   645.00us
     75%   849.00us
     90%     1.10ms
     99%     2.23ms
### File Upload (POST /upload)
  Reqs/sec    117259.91    9169.49  123076.40
  Latency      829.93us   377.80us     6.54ms
  Latency Distribution
     50%   782.00us
     75%     0.94ms
     90%     1.19ms
     99%     2.13ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    113905.05    7487.42  119620.43
  Latency        0.85ms   293.83us     6.08ms
  Latency Distribution
     50%   806.00us
     75%     0.96ms
     90%     1.18ms
     99%     1.96ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9524.74     893.16   12409.66
  Latency       10.49ms     2.29ms    22.20ms
  Latency Distribution
     50%    10.45ms
     75%    12.43ms
     90%    13.90ms
     99%    16.64ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    150617.23   13075.04  159046.45
  Latency      652.76us   325.45us     5.29ms
  Latency Distribution
     50%   543.00us
     75%   763.00us
     90%     1.08ms
     99%     2.10ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    100516.89    8104.13  106283.97
  Latency        0.97ms   291.38us     5.31ms
  Latency Distribution
     50%     0.91ms
     75%     1.18ms
     90%     1.47ms
     99%     2.13ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     90345.13    9140.51   96432.39
  Latency        1.10ms   429.21us     5.96ms
  Latency Distribution
     50%     0.99ms
     75%     1.30ms
     90%     1.70ms
     99%     3.23ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    100076.11    7510.83  105169.77
  Latency        0.98ms   287.15us     3.92ms
  Latency Distribution
     50%     0.91ms
     75%     1.21ms
     90%     1.56ms
     99%     2.32ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    106132.16    8099.61  111127.92
  Latency        0.93ms   332.53us     6.57ms
  Latency Distribution
     50%     0.86ms
     75%     1.13ms
     90%     1.43ms
     99%     2.13ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    108417.60    6264.01  115618.72
  Latency        0.91ms   279.78us     4.31ms
  Latency Distribution
     50%     0.86ms
     75%     1.10ms
     90%     1.38ms
     99%     2.12ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    174773.83   11491.97  183946.35
  Latency      557.01us   301.48us     6.57ms
  Latency Distribution
     50%   496.00us
     75%   651.00us
     90%   827.00us
     99%     2.02ms

### Path Parameter - int (/items/12345)
  Reqs/sec    151491.25   13214.80  161010.92
  Latency      636.64us   248.81us     4.42ms
  Latency Distribution
     50%   578.00us
     75%   743.00us
     90%     0.93ms
     99%     1.83ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    150306.90   14085.27  161273.50
  Latency      644.38us   302.19us     5.58ms
  Latency Distribution
     50%   561.00us
     75%   734.00us
     90%     0.98ms
     99%     2.14ms

### Header Parameter (/header)
  Reqs/sec    103600.45    7876.16  108132.18
  Latency        0.95ms   358.72us     4.72ms
  Latency Distribution
     50%     0.85ms
     75%     1.17ms
     90%     1.52ms
     99%     2.52ms

### Cookie Parameter (/cookie)
  Reqs/sec    102974.17    6842.80  107741.75
  Latency        0.96ms   342.06us     5.90ms
  Latency Distribution
     50%     0.89ms
     75%     1.20ms
     90%     1.54ms
     99%     2.22ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     82797.73    5852.08   87944.91
  Latency        1.19ms   338.76us     5.52ms
  Latency Distribution
     50%     1.13ms
     75%     1.44ms
     90%     1.78ms
     99%     2.54ms
