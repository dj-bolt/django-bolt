# Django-Bolt Benchmark
Generated: Mon Mar  2 11:10:42 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    134539.92   26575.27  157692.90
  Latency      735.52us   468.54us     5.22ms
  Latency Distribution
     50%   591.00us
     75%     0.85ms
     90%     1.25ms
     99%     3.25ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    113841.07   10314.29  123688.65
  Latency        0.86ms   487.44us     6.87ms
  Latency Distribution
     50%   769.00us
     75%     1.00ms
     90%     1.31ms
     99%     2.49ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    118517.54    8408.63  126835.39
  Latency      824.40us   286.08us     5.25ms
  Latency Distribution
     50%   784.00us
     75%     0.99ms
     90%     1.27ms
     99%     1.94ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    105931.35    8128.32  111791.60
  Latency        0.93ms   312.61us     4.34ms
  Latency Distribution
     50%     0.86ms
     75%     1.12ms
     90%     1.44ms
     99%     2.34ms
### Cookie Endpoint (/cookie)
  Reqs/sec    107148.43    6883.38  110980.87
  Latency        0.92ms   306.38us     4.50ms
  Latency Distribution
     50%     0.85ms
     75%     1.12ms
     90%     1.40ms
     99%     2.29ms
### Exception Endpoint (/exc)
  Reqs/sec    128447.30    8584.10  136776.17
  Latency      760.31us   382.64us     6.00ms
  Latency Distribution
     50%   736.00us
     75%     0.91ms
     90%     1.15ms
     99%     1.92ms
### HTML Response (/html)
  Reqs/sec    145868.80   11273.30  152551.53
  Latency      672.87us   295.93us     4.59ms
  Latency Distribution
     50%   614.00us
     75%   821.00us
     90%     1.05ms
     99%     1.96ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     39340.77    8768.17   43793.72
  Latency        2.53ms     1.55ms    17.76ms
  Latency Distribution
     50%     2.18ms
     75%     3.00ms
     90%     4.10ms
     99%     9.13ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     76616.47    7492.41   80470.63
  Latency        1.29ms   389.60us     4.86ms
  Latency Distribution
     50%     1.22ms
     75%     1.59ms
     90%     1.99ms
     99%     2.92ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     19630.85   10108.06   71663.18
  Latency        5.59ms     1.47ms    14.67ms
  Latency Distribution
     50%     5.41ms
     75%     6.78ms
     90%     7.83ms
     99%    10.20ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15897.51     938.35   17251.68
  Latency        6.24ms     2.01ms    16.93ms
  Latency Distribution
     50%     5.82ms
     75%     7.99ms
     90%     9.54ms
     99%    12.22ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     89760.55    6551.37   95050.36
  Latency        1.09ms   315.49us     4.26ms
  Latency Distribution
     50%     1.03ms
     75%     1.35ms
     90%     1.69ms
     99%     2.50ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    151909.87   10003.88  159900.67
  Latency      642.24us   359.00us     5.41ms
  Latency Distribution
     50%   570.00us
     75%   713.00us
     90%     0.93ms
     99%     2.36ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    101622.64    7514.84  107392.04
  Latency        0.97ms   330.69us     4.61ms
  Latency Distribution
     50%     0.89ms
     75%     1.18ms
     90%     1.51ms
     99%     2.57ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14515.20    1146.82   15461.06
  Latency        6.85ms     1.91ms    21.54ms
  Latency Distribution
     50%     6.73ms
     75%     8.29ms
     90%     9.70ms
     99%    12.60ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10703.64    1387.89   14228.72
  Latency        9.31ms     3.13ms    26.43ms
  Latency Distribution
     50%     8.80ms
     75%    11.39ms
     90%    14.01ms
     99%    18.83ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     16755.46     840.66   18050.05
  Latency        5.93ms     1.90ms    13.88ms
  Latency Distribution
     50%     5.54ms
     75%     7.46ms
     90%     9.15ms
     99%    11.81ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12501.86     875.27   14420.41
  Latency        7.97ms     2.82ms    23.80ms
  Latency Distribution
     50%     7.40ms
     75%     9.88ms
     90%    12.45ms
     99%    16.82ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    115602.14    8077.39  122390.67
  Latency        0.85ms   256.97us     4.58ms
  Latency Distribution
     50%   804.00us
     75%     1.04ms
     90%     1.29ms
     99%     1.96ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    106001.36    5894.02  110721.73
  Latency        0.93ms   289.14us     4.77ms
  Latency Distribution
     50%     0.87ms
     75%     1.17ms
     90%     1.45ms
     99%     2.24ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     67651.63    4960.32   71534.33
  Latency        1.45ms   575.10us     6.61ms
  Latency Distribution
     50%     1.28ms
     75%     1.79ms
     90%     2.44ms
     99%     4.10ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    105694.35    5357.60  110806.90
  Latency        0.93ms   283.21us     4.77ms
  Latency Distribution
     50%     0.89ms
     75%     1.15ms
     90%     1.42ms
     99%     2.15ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    101717.55    7577.72  110701.57
  Latency        0.96ms   338.86us     5.27ms
  Latency Distribution
     50%     0.89ms
     75%     1.18ms
     90%     1.50ms
     99%     2.34ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    101564.66    8847.05  111212.63
  Latency        0.96ms   309.61us     4.46ms
  Latency Distribution
     50%     0.89ms
     75%     1.19ms
     90%     1.49ms
     99%     2.20ms
### CBV Response Types (/cbv-response)
  Reqs/sec    106277.77    8609.25  116341.57
  Latency        0.92ms   280.41us     4.76ms
  Latency Distribution
     50%     0.86ms
     75%     1.14ms
     90%     1.46ms
     99%     2.10ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     17324.38    1320.68   18466.64
  Latency        5.74ms     1.71ms    14.69ms
  Latency Distribution
     50%     5.32ms
     75%     6.71ms
     90%     8.60ms
     99%    11.51ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    133671.69    8397.44  141115.38
  Latency      725.93us   278.11us     5.13ms
  Latency Distribution
     50%   690.00us
     75%     0.86ms
     90%     1.10ms
     99%     1.81ms
### File Upload (POST /upload)
  Reqs/sec    118984.64    7518.33  124506.48
  Latency      818.13us   256.73us     4.86ms
  Latency Distribution
     50%   788.00us
     75%     0.99ms
     90%     1.23ms
     99%     1.77ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    113024.17    8686.56  118165.02
  Latency        0.87ms   293.94us     5.54ms
  Latency Distribution
     50%   839.00us
     75%     1.06ms
     90%     1.30ms
     99%     1.91ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9984.61    1064.20   14427.00
  Latency       10.04ms     2.43ms    22.70ms
  Latency Distribution
     50%    10.26ms
     75%    11.86ms
     90%    13.33ms
     99%    16.72ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    140470.31   14180.00  150462.61
  Latency      693.86us   280.96us     4.33ms
  Latency Distribution
     50%   616.00us
     75%     0.87ms
     90%     1.09ms
     99%     2.04ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    104498.31   15487.48  132831.20
  Latency        0.99ms   350.73us     6.28ms
  Latency Distribution
     50%     0.94ms
     75%     1.23ms
     90%     1.60ms
     99%     2.50ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     97166.63    8190.70  101585.05
  Latency        1.02ms   356.54us     5.45ms
  Latency Distribution
     50%     0.94ms
     75%     1.22ms
     90%     1.54ms
     99%     2.43ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    103206.07    6413.09  108101.81
  Latency        0.95ms   287.86us     4.41ms
  Latency Distribution
     50%     0.89ms
     75%     1.18ms
     90%     1.48ms
     99%     2.22ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    107960.51    7817.79  115650.88
  Latency        0.91ms   303.65us     4.46ms
  Latency Distribution
     50%   835.00us
     75%     1.12ms
     90%     1.41ms
     99%     2.19ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    111119.58    7088.66  117539.33
  Latency        0.89ms   306.33us     4.21ms
  Latency Distribution
     50%   826.00us
     75%     1.13ms
     90%     1.41ms
     99%     2.23ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    166609.25   15903.28  176318.08
  Latency      582.89us   283.08us     4.74ms
  Latency Distribution
     50%   538.00us
     75%   683.00us
     90%     0.89ms
     99%     1.77ms

### Path Parameter - int (/items/12345)
  Reqs/sec    148869.04   10768.54  161589.11
  Latency      648.66us   306.89us     5.66ms
  Latency Distribution
     50%   643.00us
     75%   784.00us
     90%     0.97ms
     99%     1.50ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    147539.64   11440.64  157434.84
  Latency      667.35us   293.18us     4.36ms
  Latency Distribution
     50%   593.00us
     75%   819.00us
     90%     1.08ms
     99%     1.89ms

### Header Parameter (/header)
  Reqs/sec    106960.62    7491.76  112450.98
  Latency        0.91ms   284.63us     4.96ms
  Latency Distribution
     50%   846.00us
     75%     1.13ms
     90%     1.41ms
     99%     2.15ms

### Cookie Parameter (/cookie)
  Reqs/sec    106765.87    5973.22  112744.36
  Latency        0.92ms   293.10us     4.92ms
  Latency Distribution
     50%     0.86ms
     75%     1.14ms
     90%     1.45ms
     99%     2.12ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     88360.46    6884.74   95773.34
  Latency        1.10ms   396.55us     6.00ms
  Latency Distribution
     50%     1.02ms
     75%     1.36ms
     90%     1.74ms
     99%     2.61ms
