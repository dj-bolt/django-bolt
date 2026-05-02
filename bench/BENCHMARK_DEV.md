# Django-Bolt Benchmark
Generated: Sat 02 May 2026 11:06:28 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    173158.07   12277.06  183166.20
  Latency      554.52us   363.05us     5.81ms
  Latency Distribution
     50%   467.00us
     75%   671.00us
     90%     0.88ms
     99%     2.18ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    119423.93   12136.53  129181.85
  Latency      807.60us   396.24us     6.46ms
  Latency Distribution
     50%   742.00us
     75%     0.92ms
     90%     1.18ms
     99%     2.22ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    113239.89   11327.13  124070.83
  Latency        0.86ms   431.67us     6.02ms
  Latency Distribution
     50%   767.00us
     75%     1.04ms
     90%     1.45ms
     99%     2.61ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec     94727.73    7217.10  103962.59
  Latency        1.05ms   349.87us     5.62ms
  Latency Distribution
     50%     0.98ms
     75%     1.30ms
     90%     1.67ms
     99%     2.47ms
### Cookie Endpoint (/cookie)
  Reqs/sec     97460.27    6200.28  103036.12
  Latency        1.01ms   341.13us     5.21ms
  Latency Distribution
     50%     0.93ms
     75%     1.26ms
     90%     1.61ms
     99%     2.33ms
### Exception Endpoint (/exc)
  Reqs/sec    134663.28    7661.43  140993.14
  Latency      717.84us   293.70us     5.40ms
  Latency Distribution
     50%   661.00us
     75%     0.86ms
     90%     1.13ms
     99%     1.91ms
### HTML Response (/html)
  Reqs/sec    136143.52   36291.25  166614.25
  Latency      638.29us   329.89us     5.04ms
  Latency Distribution
     50%   558.00us
     75%   795.00us
     90%     1.04ms
     99%     2.04ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     36289.16    5218.94   40392.94
  Latency        2.74ms     1.39ms    16.38ms
  Latency Distribution
     50%     2.29ms
     75%     3.42ms
     90%     4.86ms
     99%     8.22ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     75571.81    4254.13   79330.87
  Latency        1.30ms   313.73us     4.71ms
  Latency Distribution
     50%     1.24ms
     75%     1.56ms
     90%     1.91ms
     99%     2.68ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     19590.92   23173.60  148221.34
  Latency        6.38ms     2.09ms    21.05ms
  Latency Distribution
     50%     6.30ms
     75%     7.82ms
     90%     9.09ms
     99%    14.07ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14312.10     940.30   15557.90
  Latency        6.97ms     2.16ms    19.70ms
  Latency Distribution
     50%     6.63ms
     75%     8.30ms
     90%    10.25ms
     99%    13.97ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     76386.20    7207.68   84978.04
  Latency        1.30ms   469.30us     7.25ms
  Latency Distribution
     50%     1.20ms
     75%     1.65ms
     90%     2.09ms
     99%     3.37ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    139552.68   16251.35  151787.32
  Latency      697.79us   352.21us     6.00ms
  Latency Distribution
     50%   623.00us
     75%   837.00us
     90%     1.12ms
     99%     2.28ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    135091.81   14424.33  146600.69
  Latency      707.85us   399.96us     6.61ms
  Latency Distribution
     50%   642.00us
     75%   808.00us
     90%     1.05ms
     99%     2.76ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14149.30    1249.74   17431.73
  Latency        7.06ms     1.77ms    20.10ms
  Latency Distribution
     50%     7.24ms
     75%     8.51ms
     90%     9.79ms
     99%    11.64ms
### Users Full10 (Sync) (/users/sync-full10)
 6075 / 10000 [=============================================================================================================================================================>------------------------------------------------------------------------------------------------------]  60.75% 10087/s
  Reqs/sec     10072.39     836.81   12285.76
  Latency        9.88ms     4.68ms    40.40ms
  Latency Distribution
     50%     8.69ms
     75%    12.87ms
     90%    17.07ms
     99%    24.85ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     17174.48    1548.54   23361.15
  Latency        5.85ms     1.27ms    14.00ms
  Latency Distribution
     50%     5.80ms
     75%     6.76ms
     90%     7.98ms
     99%     9.61ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     11945.16     897.74   14368.73
  Latency        8.34ms     3.60ms    26.14ms
  Latency Distribution
     50%     7.31ms
     75%    10.33ms
     90%    14.44ms
     99%    19.94ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    106993.96    9706.42  113133.72
  Latency        0.92ms   319.68us     6.07ms
  Latency Distribution
     50%   836.00us
     75%     1.12ms
     90%     1.43ms
     99%     2.15ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     97912.79    6609.65  103678.02
  Latency        1.00ms   353.98us     5.00ms
  Latency Distribution
     50%     0.92ms
     75%     1.25ms
     90%     1.59ms
     99%     2.49ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     63511.85    4164.98   67074.95
  Latency        1.55ms   433.63us     6.27ms
  Latency Distribution
     50%     1.48ms
     75%     1.88ms
     90%     2.42ms
     99%     3.21ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     99293.45    7806.81  106387.60
  Latency        0.99ms   333.41us     5.57ms
  Latency Distribution
     50%     0.91ms
     75%     1.24ms
     90%     1.59ms
     99%     2.49ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     97556.80    7376.90  104189.46
  Latency        1.02ms   329.94us     5.74ms
  Latency Distribution
     50%     0.96ms
     75%     1.25ms
     90%     1.58ms
     99%     2.37ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     98508.01    4785.84  105704.70
  Latency        0.99ms   313.40us     4.51ms
  Latency Distribution
     50%     0.93ms
     75%     1.22ms
     90%     1.52ms
     99%     2.26ms
### CBV Response Types (/cbv-response)
  Reqs/sec    107571.44    7548.18  113862.19
  Latency        0.91ms   279.46us     5.56ms
  Latency Distribution
     50%   843.00us
     75%     1.13ms
     90%     1.43ms
     99%     2.11ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     15627.00    1461.84   19612.43
  Latency        6.40ms     1.24ms    15.39ms
  Latency Distribution
     50%     6.33ms
     75%     7.37ms
     90%     8.23ms
     99%    10.06ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    107187.06   35558.55  136462.44
  Latency      794.98us   434.20us     5.99ms
  Latency Distribution
     50%   704.00us
     75%     0.96ms
     90%     1.30ms
     99%     2.62ms
### File Upload (POST /upload)
  Reqs/sec    101430.57    4336.33  106241.79
  Latency        0.96ms   499.23us     8.27ms
  Latency Distribution
     50%   843.00us
     75%     1.23ms
     90%     1.66ms
     99%     2.92ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    101023.64    9806.24  110314.00
  Latency        0.97ms   394.01us     4.90ms
  Latency Distribution
     50%     0.88ms
     75%     1.19ms
     90%     1.63ms
     99%     2.78ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9122.08     862.83   10934.96
  Latency       10.92ms     3.02ms    25.36ms
  Latency Distribution
     50%    10.67ms
     75%    13.20ms
     90%    15.55ms
     99%    19.53ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    141412.82   12097.98  151285.62
  Latency      689.16us   424.57us     6.66ms
  Latency Distribution
     50%   602.00us
     75%   845.00us
     90%     1.11ms
     99%     2.17ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     97382.44    8399.56  109449.87
  Latency        1.03ms   409.70us     5.67ms
  Latency Distribution
     50%     0.95ms
     75%     1.27ms
     90%     1.63ms
     99%     2.79ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     78870.05    7674.06   85688.27
  Latency        1.25ms   468.10us     6.29ms
  Latency Distribution
     50%     1.17ms
     75%     1.52ms
     90%     1.90ms
     99%     3.22ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     83174.63   16186.18   98887.29
  Latency        1.19ms   582.56us     7.25ms
  Latency Distribution
     50%     1.06ms
     75%     1.45ms
     90%     1.86ms
     99%     4.32ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec     94661.22   10731.45  104469.09
  Latency        1.00ms   334.78us     5.90ms
  Latency Distribution
     50%     0.92ms
     75%     1.24ms
     90%     1.55ms
     99%     2.44ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    101088.48    7389.07  106636.73
  Latency        0.97ms   310.21us     5.72ms
  Latency Distribution
     50%     0.89ms
     75%     1.19ms
     90%     1.53ms
     99%     2.37ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    173629.31   13044.91  185420.79
  Latency      555.81us   300.89us     5.98ms
  Latency Distribution
     50%   486.00us
     75%   636.00us
     90%     0.87ms
     99%     1.93ms

### Path Parameter - int (/items/12345)
  Reqs/sec    152535.00   12428.62  163963.21
  Latency      629.68us   312.62us     6.87ms
  Latency Distribution
     50%   558.00us
     75%   685.00us
     90%     0.88ms
     99%     2.18ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    146138.12   15376.14  154834.38
  Latency      666.12us   336.07us     5.23ms
  Latency Distribution
     50%   582.00us
     75%   764.00us
     90%     1.01ms
     99%     2.31ms

### Header Parameter (/header)
  Reqs/sec     99965.24   10034.21  105764.15
  Latency        0.98ms   334.84us     5.49ms
  Latency Distribution
     50%     0.92ms
     75%     1.19ms
     90%     1.47ms
     99%     2.25ms

### Cookie Parameter (/cookie)
  Reqs/sec     99482.77    6983.45  104141.37
  Latency        0.98ms   323.01us     5.28ms
  Latency Distribution
     50%     0.93ms
     75%     1.21ms
     90%     1.52ms
     99%     2.26ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     80910.54    6270.16   85550.01
  Latency        1.22ms   362.61us     5.55ms
  Latency Distribution
     50%     1.17ms
     75%     1.51ms
     90%     1.90ms
     99%     2.66ms
