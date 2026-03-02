# Django-Bolt Benchmark
Generated: Mon 02 Mar 2026 10:29:40 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    146167.57   16904.02  160612.04
  Latency      674.71us   314.10us     4.00ms
  Latency Distribution
     50%   567.00us
     75%     0.85ms
     90%     1.08ms
     99%     2.25ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    107304.03    8524.26  116385.04
  Latency        0.91ms   498.94us     5.97ms
  Latency Distribution
     50%   847.00us
     75%     1.02ms
     90%     1.29ms
     99%     4.17ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    114935.16    7712.26  123652.80
  Latency        0.85ms   340.19us     5.37ms
  Latency Distribution
     50%   806.00us
     75%     1.02ms
     90%     1.29ms
     99%     2.51ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    101727.90    9063.64  109700.68
  Latency        0.96ms   322.62us     4.49ms
  Latency Distribution
     50%     0.90ms
     75%     1.18ms
     90%     1.49ms
     99%     2.31ms
### Cookie Endpoint (/cookie)
  Reqs/sec    104282.94   20774.42  141590.65
  Latency        1.01ms   388.62us     5.78ms
  Latency Distribution
     50%     0.94ms
     75%     1.24ms
     90%     1.57ms
     99%     2.86ms
### Exception Endpoint (/exc)
  Reqs/sec    114103.37   19386.81  128443.42
  Latency      793.93us   383.36us     7.27ms
  Latency Distribution
     50%   710.00us
     75%     0.98ms
     90%     1.24ms
     99%     2.11ms
### HTML Response (/html)
  Reqs/sec    134359.86    8331.09  142391.89
  Latency      718.59us   300.47us     3.71ms
  Latency Distribution
     50%   643.00us
     75%     0.87ms
     90%     1.14ms
     99%     2.33ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     35901.54    7067.57   40272.82
  Latency        2.77ms     1.44ms    21.15ms
  Latency Distribution
     50%     2.47ms
     75%     3.33ms
     90%     4.26ms
     99%     7.80ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     76549.06    6261.82   80359.92
  Latency        1.29ms   360.83us     4.20ms
  Latency Distribution
     50%     1.22ms
     75%     1.62ms
     90%     2.01ms
     99%     2.83ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     17384.21    1490.20   19339.60
  Latency        5.73ms     1.73ms    15.16ms
  Latency Distribution
     50%     5.33ms
     75%     7.09ms
     90%     8.58ms
     99%    11.09ms
### Get User via Dependency (/auth/me-dependency)
 3099 / 10000 [==============>---------------------------------]  30.99% 15452/s
  Reqs/sec     15549.08    1127.77   18245.85
  Latency        6.41ms     2.71ms    18.27ms
  Latency Distribution
     50%     6.00ms
     75%     8.47ms
     90%    10.78ms
     99%    14.51ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     81271.76    5987.40   87080.17
  Latency        1.20ms   446.73us     5.59ms
  Latency Distribution
     50%     1.10ms
     75%     1.48ms
     90%     1.97ms
     99%     3.16ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    102971.39   43503.30  135169.85
  Latency      805.47us   517.24us     6.66ms
  Latency Distribution
     50%   687.00us
     75%     0.88ms
     90%     1.34ms
     99%     3.42ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     92719.03    6602.19  100273.17
  Latency        1.07ms   363.29us     5.38ms
  Latency Distribution
     50%     0.98ms
     75%     1.31ms
     90%     1.71ms
     99%     2.56ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14040.11    1941.96   15848.14
  Latency        6.97ms     2.14ms    22.86ms
  Latency Distribution
     50%     6.76ms
     75%     8.57ms
     90%    10.20ms
     99%    13.30ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     11828.43    2579.12   25027.71
  Latency        8.62ms     3.62ms    31.88ms
  Latency Distribution
     50%     7.75ms
     75%    10.69ms
     90%    14.14ms
     99%    20.89ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     15925.39    1371.17   17941.15
  Latency        6.25ms     2.12ms    24.82ms
  Latency Distribution
     50%     6.04ms
     75%     7.37ms
     90%     9.10ms
     99%    13.47ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12785.92    1438.34   15810.35
  Latency        7.81ms     3.61ms    28.20ms
  Latency Distribution
     50%     6.90ms
     75%     9.93ms
     90%    13.23ms
     99%    20.18ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    108621.34    9992.55  116653.44
  Latency        0.90ms   296.24us     5.20ms
  Latency Distribution
     50%   837.00us
     75%     1.09ms
     90%     1.40ms
     99%     2.17ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    105080.47    7576.82  109725.29
  Latency        0.93ms   303.45us     5.06ms
  Latency Distribution
     50%     0.86ms
     75%     1.15ms
     90%     1.44ms
     99%     2.19ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     65881.10    4334.01   70548.95
  Latency        1.49ms   456.26us     8.66ms
  Latency Distribution
     50%     1.40ms
     75%     1.79ms
     90%     2.24ms
     99%     3.49ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    103291.28    9222.04  113096.30
  Latency        0.95ms   301.87us     4.14ms
  Latency Distribution
     50%     0.89ms
     75%     1.19ms
     90%     1.53ms
     99%     2.33ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    100433.18    6354.75  104373.00
  Latency        0.98ms   280.20us     4.64ms
  Latency Distribution
     50%     0.93ms
     75%     1.21ms
     90%     1.50ms
     99%     2.24ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     95292.37    8245.69  106465.89
  Latency        1.02ms   362.51us     4.84ms
  Latency Distribution
     50%     0.93ms
     75%     1.28ms
     90%     1.63ms
     99%     2.62ms
### CBV Response Types (/cbv-response)
  Reqs/sec    102085.46    7742.57  110881.30
  Latency        0.96ms   310.85us     4.01ms
  Latency Distribution
     50%     0.89ms
     75%     1.19ms
     90%     1.54ms
     99%     2.29ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     17110.68    1236.27   17966.78
  Latency        5.81ms     1.62ms    15.42ms
  Latency Distribution
     50%     5.59ms
     75%     6.94ms
     90%     8.28ms
     99%    10.96ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    123223.53    9731.05  136548.96
  Latency      790.98us   411.38us     5.39ms
  Latency Distribution
     50%   730.00us
     75%     0.90ms
     90%     1.19ms
     99%     3.47ms
### File Upload (POST /upload)
  Reqs/sec    107831.64   10721.11  116278.71
  Latency        0.91ms   375.99us     5.45ms
  Latency Distribution
     50%   833.00us
     75%     1.12ms
     90%     1.48ms
     99%     2.58ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    100349.02    6562.06  106795.34
  Latency        0.97ms   467.11us     5.44ms
  Latency Distribution
     50%     0.87ms
     75%     1.17ms
     90%     1.46ms
     99%     3.80ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9690.22    1009.89   10792.84
  Latency       10.26ms     3.14ms    23.58ms
  Latency Distribution
     50%    10.00ms
     75%    12.57ms
     90%    14.83ms
     99%    19.56ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    131816.54    7839.16  139564.13
  Latency      742.60us   356.52us     5.99ms
  Latency Distribution
     50%   680.00us
     75%     0.90ms
     90%     1.12ms
     99%     2.82ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     98233.57    8541.85  105301.15
  Latency        1.00ms   347.10us     5.15ms
  Latency Distribution
     50%     0.93ms
     75%     1.25ms
     90%     1.60ms
     99%     2.50ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     89375.05    6926.57   97880.73
  Latency        1.11ms   418.07us     4.49ms
  Latency Distribution
     50%     1.01ms
     75%     1.38ms
     90%     1.78ms
     99%     3.25ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    101626.03    7622.98  107399.75
  Latency        0.96ms   299.88us     5.42ms
  Latency Distribution
     50%     0.90ms
     75%     1.15ms
     90%     1.50ms
     99%     2.24ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    104548.69    7792.73  110584.04
  Latency        0.94ms   299.55us     5.14ms
  Latency Distribution
     50%     0.88ms
     75%     1.18ms
     90%     1.50ms
     99%     2.18ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    103343.98    7741.10  108168.66
  Latency        0.95ms   363.02us     4.37ms
  Latency Distribution
     50%     0.86ms
     75%     1.18ms
     90%     1.55ms
     99%     2.88ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    138664.65   41610.81  174014.21
  Latency      617.80us   295.99us     5.35ms
  Latency Distribution
     50%   577.00us
     75%   737.00us
     90%     0.96ms
     99%     1.92ms

### Path Parameter - int (/items/12345)
  Reqs/sec    131815.45    8174.57  140738.48
  Latency      735.29us   385.18us     5.29ms
  Latency Distribution
     50%   641.00us
     75%     0.87ms
     90%     1.16ms
     99%     2.46ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    139255.53   10606.77  148096.41
  Latency      707.22us   383.80us     4.63ms
  Latency Distribution
     50%   611.00us
     75%   821.00us
     90%     1.06ms
     99%     2.98ms

### Header Parameter (/header)
  Reqs/sec     99924.55    6818.97  107328.92
  Latency        0.98ms   312.16us     4.79ms
  Latency Distribution
     50%     0.92ms
     75%     1.22ms
     90%     1.52ms
     99%     2.29ms

### Cookie Parameter (/cookie)
  Reqs/sec    100911.41    7540.63  106495.50
  Latency        0.97ms   348.53us     5.02ms
  Latency Distribution
     50%     0.89ms
     75%     1.20ms
     90%     1.54ms
     99%     2.39ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     64725.51   12494.82   81591.91
  Latency        1.53ms     0.87ms    10.78ms
  Latency Distribution
     50%     1.30ms
     75%     1.92ms
     90%     2.72ms
     99%     5.63ms
