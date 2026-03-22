# Django-Bolt Benchmark
Generated: Sun 22 Mar 2026 12:17:48 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    159476.11   16592.72  168822.07
  Latency      618.78us   355.38us     5.66ms
  Latency Distribution
     50%   510.00us
     75%   734.00us
     90%     1.08ms
     99%     2.23ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    114007.19    7955.10  119355.85
  Latency        0.85ms   407.85us     7.02ms
  Latency Distribution
     50%   736.00us
     75%     1.07ms
     90%     1.43ms
     99%     2.69ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    113782.36    7956.45  119945.09
  Latency        0.86ms   465.31us     7.61ms
  Latency Distribution
     50%   762.00us
     75%     1.06ms
     90%     1.37ms
     99%     2.75ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec     94583.23    6943.41  101772.41
  Latency        1.04ms   444.09us     5.55ms
  Latency Distribution
     50%     0.93ms
     75%     1.28ms
     90%     1.72ms
     99%     2.91ms
### Cookie Endpoint (/cookie)
  Reqs/sec     95809.15    7311.43  104935.33
  Latency        1.04ms   346.51us     4.50ms
  Latency Distribution
     50%     0.96ms
     75%     1.28ms
     90%     1.64ms
     99%     2.54ms
### Exception Endpoint (/exc)
  Reqs/sec     90328.33   24211.75  118182.19
  Latency        1.07ms   767.52us     7.95ms
  Latency Distribution
     50%   780.00us
     75%     1.29ms
     90%     2.32ms
     99%     4.78ms
### HTML Response (/html)
  Reqs/sec    147223.54    9837.78  157623.14
  Latency      661.76us   357.48us     5.68ms
  Latency Distribution
     50%   565.00us
     75%   785.00us
     90%     1.14ms
     99%     2.35ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     34264.72    6887.41   39494.49
  Latency        2.92ms     1.55ms    19.55ms
  Latency Distribution
     50%     2.61ms
     75%     3.52ms
     90%     4.60ms
     99%     9.39ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     71197.40    6635.75   82337.31
  Latency        1.40ms   442.41us     5.41ms
  Latency Distribution
     50%     1.31ms
     75%     1.73ms
     90%     2.17ms
     99%     3.34ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     16274.47    1700.12   17515.81
  Latency        6.10ms     2.26ms    20.90ms
  Latency Distribution
     50%     6.01ms
     75%     7.55ms
     90%     9.48ms
     99%    13.19ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14445.48    1497.98   16959.67
  Latency        6.90ms     2.22ms    33.06ms
  Latency Distribution
     50%     6.50ms
     75%     8.41ms
     90%    10.12ms
     99%    13.77ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     79059.59    5436.89   83433.91
  Latency        1.25ms   421.95us     5.28ms
  Latency Distribution
     50%     1.15ms
     75%     1.55ms
     90%     2.03ms
     99%     3.01ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    137133.39   10319.66  145008.55
  Latency      709.32us   396.50us     6.43ms
  Latency Distribution
     50%   627.00us
     75%   837.00us
     90%     1.17ms
     99%     2.42ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    131091.35   13409.43  140616.87
  Latency      736.65us   405.54us     5.68ms
  Latency Distribution
     50%   620.00us
     75%     0.85ms
     90%     1.25ms
     99%     2.71ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14607.40    1562.21   18265.54
  Latency        6.84ms     1.88ms    19.42ms
  Latency Distribution
     50%     6.77ms
     75%     8.00ms
     90%     9.28ms
     99%    13.78ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10460.55    1306.19   12605.26
  Latency        9.40ms     4.58ms    36.74ms
  Latency Distribution
     50%     8.71ms
     75%    12.01ms
     90%    16.08ms
     99%    23.91ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     17842.26     938.88   20155.08
  Latency        5.57ms     1.48ms    14.88ms
  Latency Distribution
     50%     5.51ms
     75%     6.75ms
     90%     7.95ms
     99%     9.83ms
### Users Mini10 (Sync) (/users/sync-mini10)
 9475 / 10000 [================================================>--]  94.75% 11811/s
  Reqs/sec     12030.87    1506.23   19826.67
  Latency        8.39ms     2.91ms    25.61ms
  Latency Distribution
     50%     7.83ms
     75%    10.18ms
     90%    12.75ms
     99%    18.05ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     98716.13    8290.42  105414.35
  Latency        0.99ms   363.02us     5.34ms
  Latency Distribution
     50%     0.90ms
     75%     1.22ms
     90%     1.61ms
     99%     2.48ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     99384.22    7427.84  104149.62
  Latency        0.99ms   333.29us     5.50ms
  Latency Distribution
     50%     0.92ms
     75%     1.22ms
     90%     1.53ms
     99%     2.27ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     67244.33    5604.23   73162.28
  Latency        1.48ms   465.55us     6.66ms
  Latency Distribution
     50%     1.38ms
     75%     1.73ms
     90%     2.12ms
     99%     3.54ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     93848.94    7174.00   99444.56
  Latency        1.05ms   375.02us     6.22ms
  Latency Distribution
     50%     0.99ms
     75%     1.32ms
     90%     1.66ms
     99%     2.59ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     92910.88    4879.49   96606.64
  Latency        1.05ms   329.23us     4.51ms
  Latency Distribution
     50%     0.97ms
     75%     1.34ms
     90%     1.68ms
     99%     2.44ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     95188.76    6968.43  101098.94
  Latency        1.03ms   373.27us     6.52ms
  Latency Distribution
     50%     0.96ms
     75%     1.29ms
     90%     1.67ms
     99%     2.39ms
### CBV Response Types (/cbv-response)
  Reqs/sec     98753.72    5203.94  102677.95
  Latency        0.99ms   383.01us     5.03ms
  Latency Distribution
     50%     0.88ms
     75%     1.25ms
     90%     1.62ms
     99%     2.67ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16503.27    1346.14   19294.60
  Latency        6.05ms     1.31ms    14.51ms
  Latency Distribution
     50%     5.92ms
     75%     7.02ms
     90%     8.01ms
     99%    10.21ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec     88783.02   38334.23  134782.28
  Latency        1.00ms   737.27us    10.14ms
  Latency Distribution
     50%   710.00us
     75%     1.19ms
     90%     2.06ms
     99%     4.29ms
### File Upload (POST /upload)
  Reqs/sec    112500.46   12567.52  128093.35
  Latency        0.87ms   424.00us     7.16ms
  Latency Distribution
     50%   794.00us
     75%     1.03ms
     90%     1.43ms
     99%     2.49ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    108419.11    9046.78  115740.97
  Latency        0.90ms   343.61us     6.13ms
  Latency Distribution
     50%   836.00us
     75%     1.05ms
     90%     1.44ms
     99%     2.44ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9305.09    1061.92   10820.41
  Latency       10.72ms     3.14ms    40.74ms
  Latency Distribution
     50%    10.08ms
     75%    12.60ms
     90%    15.05ms
     99%    21.25ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    137484.23   10098.43  144636.42
  Latency      705.65us   369.61us     5.13ms
  Latency Distribution
     50%   579.00us
     75%   837.00us
     90%     1.26ms
     99%     2.54ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     95020.32    8164.93  101173.07
  Latency        1.04ms   382.06us     6.07ms
  Latency Distribution
     50%     0.96ms
     75%     1.25ms
     90%     1.56ms
     99%     2.56ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     86538.77    8355.82   92260.57
  Latency        1.14ms   446.35us     6.36ms
  Latency Distribution
     50%     1.06ms
     75%     1.37ms
     90%     1.68ms
     99%     2.81ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     83740.46   14161.86   96996.99
  Latency        1.17ms   587.38us     6.43ms
  Latency Distribution
     50%     1.01ms
     75%     1.42ms
     90%     1.88ms
     99%     4.29ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec     97255.54    7057.22  102382.83
  Latency        1.01ms   348.95us     5.08ms
  Latency Distribution
     50%     0.93ms
     75%     1.28ms
     90%     1.60ms
     99%     2.41ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec     96798.64    7434.82  103733.11
  Latency        1.02ms   322.49us     4.45ms
  Latency Distribution
     50%     0.96ms
     75%     1.30ms
     90%     1.63ms
     99%     2.35ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    160316.32   16219.53  179658.89
  Latency      626.84us   353.48us     5.27ms
  Latency Distribution
     50%   534.00us
     75%   714.00us
     90%     1.07ms
     99%     2.46ms

### Path Parameter - int (/items/12345)
  Reqs/sec    139801.99   15082.44  150070.37
  Latency      699.82us   360.94us     5.17ms
  Latency Distribution
     50%   602.00us
     75%     0.85ms
     90%     1.17ms
     99%     2.38ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    139380.05   10986.36  149147.00
  Latency      695.66us   321.48us     8.62ms
  Latency Distribution
     50%   605.00us
     75%   845.00us
     90%     1.20ms
     99%     2.17ms

### Header Parameter (/header)
  Reqs/sec     95891.78    7577.74  104317.52
  Latency        1.01ms   371.23us     6.07ms
  Latency Distribution
     50%     0.95ms
     75%     1.24ms
     90%     1.60ms
     99%     2.44ms

### Cookie Parameter (/cookie)
  Reqs/sec     95526.65    5408.54  100943.85
  Latency        1.04ms   369.34us     5.25ms
  Latency Distribution
     50%     0.95ms
     75%     1.28ms
     90%     1.68ms
     99%     2.52ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     78518.53    4630.67   83147.92
  Latency        1.25ms   359.57us     5.62ms
  Latency Distribution
     50%     1.18ms
     75%     1.56ms
     90%     1.91ms
     99%     2.70ms
