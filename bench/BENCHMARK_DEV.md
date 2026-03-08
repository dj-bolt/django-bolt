# Django-Bolt Benchmark
Generated: Sat 07 Mar 2026 05:40:15 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    172753.17   22391.66  187053.20
  Latency      566.08us   344.59us     6.85ms
  Latency Distribution
     50%   500.00us
     75%   628.00us
     90%     0.87ms
     99%     1.95ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    120593.26    9832.62  129439.80
  Latency      818.79us   311.60us     5.51ms
  Latency Distribution
     50%   762.00us
     75%     0.97ms
     90%     1.21ms
     99%     2.02ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    123821.79   10897.95  133339.15
  Latency      779.74us   294.50us     5.41ms
  Latency Distribution
     50%   757.00us
     75%     0.93ms
     90%     1.12ms
     99%     1.93ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    100276.23    8477.12  106279.97
  Latency        0.97ms   331.90us     5.54ms
  Latency Distribution
     50%     0.89ms
     75%     1.21ms
     90%     1.56ms
     99%     2.31ms
### Cookie Endpoint (/cookie)
  Reqs/sec    103429.45    8256.71  109911.96
  Latency        0.95ms   276.75us     4.55ms
  Latency Distribution
     50%     0.90ms
     75%     1.15ms
     90%     1.42ms
     99%     2.08ms
### Exception Endpoint (/exc)
  Reqs/sec    129386.25   13511.65  138891.42
  Latency      751.21us   256.66us     5.51ms
  Latency Distribution
     50%   723.00us
     75%     0.92ms
     90%     1.13ms
     99%     1.75ms
### HTML Response (/html)
  Reqs/sec    148007.57   13989.76  160350.19
  Latency      651.41us   301.19us     5.61ms
  Latency Distribution
     50%   611.00us
     75%   789.00us
     90%     1.02ms
     99%     1.87ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     37803.35    8109.26   44473.58
  Latency        2.66ms     1.55ms    23.30ms
  Latency Distribution
     50%     2.32ms
     75%     3.16ms
     90%     4.21ms
     99%     9.03ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     77222.56    5659.26   81113.76
  Latency        1.27ms   342.00us     4.63ms
  Latency Distribution
     50%     1.23ms
     75%     1.53ms
     90%     1.86ms
     99%     2.72ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     17438.55    1200.87   19326.99
  Latency        5.71ms     1.41ms    18.84ms
  Latency Distribution
     50%     5.74ms
     75%     6.95ms
     90%     7.90ms
     99%     9.77ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15565.93     813.86   16646.78
  Latency        6.39ms     1.42ms    13.85ms
  Latency Distribution
     50%     6.20ms
     75%     7.37ms
     90%     8.63ms
     99%    11.11ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     87228.01    6545.25   92217.60
  Latency        1.12ms   348.57us     6.74ms
  Latency Distribution
     50%     1.07ms
     75%     1.38ms
     90%     1.73ms
     99%     2.40ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    155017.78   13239.06  163450.43
  Latency      630.77us   252.32us     7.94ms
  Latency Distribution
     50%   592.00us
     75%   707.00us
     90%     0.89ms
     99%     1.75ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    102177.92    7272.26  107881.39
  Latency        0.96ms   316.69us     5.34ms
  Latency Distribution
     50%     0.89ms
     75%     1.20ms
     90%     1.53ms
     99%     2.21ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14426.40     929.47   16120.81
  Latency        6.90ms     2.00ms    19.12ms
  Latency Distribution
     50%     7.09ms
     75%     8.65ms
     90%     9.84ms
     99%    12.23ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10022.72    1058.17   12591.72
  Latency        9.95ms     4.20ms    32.67ms
  Latency Distribution
     50%     9.13ms
     75%    12.54ms
     90%    16.46ms
     99%    23.03ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     16670.79     755.83   17372.72
  Latency        5.96ms     1.42ms    15.01ms
  Latency Distribution
     50%     5.77ms
     75%     7.10ms
     90%     8.33ms
     99%    10.27ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12256.80    1036.43   14738.06
  Latency        8.05ms     4.33ms    36.00ms
  Latency Distribution
     50%     7.03ms
     75%     9.41ms
     90%    13.65ms
     99%    24.88ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    108777.69    9141.80  115683.00
  Latency        0.90ms   321.09us     5.30ms
  Latency Distribution
     50%   829.00us
     75%     1.11ms
     90%     1.42ms
     99%     2.27ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    108054.99    6378.76  114500.95
  Latency        0.91ms   276.73us     3.85ms
  Latency Distribution
     50%   844.00us
     75%     1.11ms
     90%     1.43ms
     99%     2.18ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     71144.79    6621.37   83675.75
  Latency        1.42ms   347.24us     4.41ms
  Latency Distribution
     50%     1.35ms
     75%     1.63ms
     90%     2.02ms
     99%     2.89ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    106119.86    8093.90  112035.86
  Latency        0.92ms   299.61us     4.90ms
  Latency Distribution
     50%     0.86ms
     75%     1.13ms
     90%     1.40ms
     99%     2.08ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    103297.30    5588.74  107429.71
  Latency        0.95ms   303.56us     4.19ms
  Latency Distribution
     50%     0.88ms
     75%     1.17ms
     90%     1.49ms
     99%     2.34ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    103859.55    7332.08  109815.81
  Latency        0.94ms   294.38us     4.41ms
  Latency Distribution
     50%     0.87ms
     75%     1.17ms
     90%     1.45ms
     99%     2.19ms
### CBV Response Types (/cbv-response)
  Reqs/sec    109067.69    5861.10  113356.97
  Latency        0.90ms   285.76us     4.79ms
  Latency Distribution
     50%   845.00us
     75%     1.12ms
     90%     1.39ms
     99%     1.94ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16725.47    1208.37   18054.52
  Latency        5.95ms     1.68ms    14.55ms
  Latency Distribution
     50%     5.78ms
     75%     7.29ms
     90%     8.56ms
     99%    11.01ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    130986.66    9333.73  138692.24
  Latency      732.38us   276.19us     5.26ms
  Latency Distribution
     50%   644.00us
     75%     0.88ms
     90%     1.34ms
     99%     1.93ms
### File Upload (POST /upload)
  Reqs/sec    116971.54    9220.70  122702.59
  Latency      833.04us   248.79us     4.49ms
  Latency Distribution
     50%   787.00us
     75%     1.00ms
     90%     1.22ms
     99%     1.84ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    112451.02    7925.45  119557.84
  Latency        0.87ms   250.35us     4.11ms
  Latency Distribution
     50%     0.85ms
     75%     1.07ms
     90%     1.31ms
     99%     1.90ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9902.23     950.72   13257.58
  Latency       10.09ms     1.84ms    20.54ms
  Latency Distribution
     50%    10.15ms
     75%    11.38ms
     90%    12.60ms
     99%    15.86ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    147336.90   18313.81  159151.03
  Latency      667.26us   279.34us     5.70ms
  Latency Distribution
     50%   619.00us
     75%   777.00us
     90%     0.98ms
     99%     1.91ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    103678.55    9009.08  108659.10
  Latency        0.95ms   359.40us     5.46ms
  Latency Distribution
     50%     0.87ms
     75%     1.18ms
     90%     1.50ms
     99%     2.47ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     96704.52    8745.98  106555.46
  Latency        1.04ms   366.94us     5.83ms
  Latency Distribution
     50%     0.95ms
     75%     1.26ms
     90%     1.61ms
     99%     2.48ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    104853.84    6786.62  109088.29
  Latency        0.94ms   299.52us     5.87ms
  Latency Distribution
     50%     0.88ms
     75%     1.15ms
     90%     1.42ms
     99%     1.98ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    110363.09    6650.22  115808.21
  Latency        0.88ms   255.13us     5.45ms
  Latency Distribution
     50%   828.00us
     75%     1.07ms
     90%     1.35ms
     99%     1.97ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    111087.19    9766.90  117257.20
  Latency        0.89ms   290.09us     5.25ms
  Latency Distribution
     50%   823.00us
     75%     1.08ms
     90%     1.36ms
     99%     2.09ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    180126.03   11669.83  189094.84
  Latency      542.71us   291.87us     5.03ms
  Latency Distribution
     50%   489.00us
     75%   589.00us
     90%   777.00us
     99%     1.58ms

### Path Parameter - int (/items/12345)
  Reqs/sec    152031.30   13845.73  166130.49
  Latency      636.38us   274.35us     5.71ms
  Latency Distribution
     50%   582.00us
     75%   773.00us
     90%     0.99ms
     99%     1.83ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    145538.85    8579.79  152675.47
  Latency      668.21us   305.59us     5.64ms
  Latency Distribution
     50%   631.00us
     75%   804.00us
     90%     1.03ms
     99%     1.65ms

### Header Parameter (/header)
  Reqs/sec    102013.76    7342.16  106203.28
  Latency        0.96ms   326.16us     5.08ms
  Latency Distribution
     50%     0.90ms
     75%     1.20ms
     90%     1.50ms
     99%     2.36ms

### Cookie Parameter (/cookie)
  Reqs/sec    101603.23    7588.00  106427.72
  Latency        0.96ms   311.63us     4.70ms
  Latency Distribution
     50%     0.90ms
     75%     1.21ms
     90%     1.54ms
     99%     2.24ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     87614.19    5702.34   91616.18
  Latency        1.12ms   319.02us     5.13ms
  Latency Distribution
     50%     1.06ms
     75%     1.38ms
     90%     1.68ms
     99%     2.43ms
