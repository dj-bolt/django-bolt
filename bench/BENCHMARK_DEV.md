# Django-Bolt Benchmark
Generated: Sun Mar 22 01:43:26 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    188142.52   19527.81  206542.66
  Latency      506.47us   292.01us     9.03ms
  Latency Distribution
     50%   436.00us
     75%   610.00us
     90%   759.00us
     99%     1.68ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    128386.49   10015.64  134061.57
  Latency      754.09us   291.12us     6.95ms
  Latency Distribution
     50%   691.00us
     75%     0.90ms
     90%     1.14ms
     99%     1.96ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    132045.31   10702.94  140543.54
  Latency      738.72us   289.08us     5.47ms
  Latency Distribution
     50%   676.00us
     75%     0.86ms
     90%     1.05ms
     99%     1.71ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    106786.24    9062.58  113960.62
  Latency        0.92ms   293.21us     4.45ms
  Latency Distribution
     50%     0.85ms
     75%     1.12ms
     90%     1.41ms
     99%     2.15ms
### Cookie Endpoint (/cookie)
  Reqs/sec    107418.67    9791.34  113081.90
  Latency        0.92ms   262.24us     4.18ms
  Latency Distribution
     50%     0.86ms
     75%     1.13ms
     90%     1.42ms
     99%     2.04ms
### Exception Endpoint (/exc)
  Reqs/sec    146224.81    9776.33  153505.69
  Latency      661.96us   267.92us     4.86ms
  Latency Distribution
     50%   597.00us
     75%   794.00us
     90%     1.02ms
     99%     1.82ms
### HTML Response (/html)
  Reqs/sec    164108.20   15852.47  175556.92
  Latency      586.36us   216.90us     4.76ms
  Latency Distribution
     50%   562.00us
     75%   700.00us
     90%     0.95ms
     99%     1.50ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     40413.03    8084.94   45948.44
  Latency        2.48ms     1.23ms    16.67ms
  Latency Distribution
     50%     2.23ms
     75%     2.94ms
     90%     3.73ms
     99%     8.28ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     78754.30    5449.49   83259.84
  Latency        1.25ms   403.27us     6.16ms
  Latency Distribution
     50%     1.16ms
     75%     1.56ms
     90%     1.96ms
     99%     2.90ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     17408.05    1497.48   20088.66
  Latency        5.73ms     1.42ms    13.61ms
  Latency Distribution
     50%     5.80ms
     75%     6.78ms
     90%     7.68ms
     99%     9.84ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15258.55     818.18   16761.40
  Latency        6.51ms     2.22ms    16.20ms
  Latency Distribution
     50%     6.51ms
     75%     8.15ms
     90%     9.76ms
     99%    12.56ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     79723.26    5713.10   85642.14
  Latency        1.21ms   377.50us     5.20ms
  Latency Distribution
     50%     1.13ms
     75%     1.49ms
     90%     1.90ms
     99%     2.79ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    164729.82   16182.72  182562.24
  Latency      606.22us   232.31us     4.64ms
  Latency Distribution
     50%   569.00us
     75%   749.00us
     90%     0.88ms
     99%     1.43ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    146124.35   11093.25  153198.33
  Latency      665.28us   326.18us     6.40ms
  Latency Distribution
     50%   657.00us
     75%   793.00us
     90%     0.99ms
     99%     1.84ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14849.82    2091.92   25403.51
  Latency        6.83ms     1.07ms    16.43ms
  Latency Distribution
     50%     6.83ms
     75%     7.64ms
     90%     8.29ms
     99%     9.49ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10213.84    1027.19   12614.43
  Latency        9.74ms     4.08ms    28.09ms
  Latency Distribution
     50%     8.91ms
     75%    12.30ms
     90%    16.16ms
     99%    22.47ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     18132.41    2503.98   30504.24
  Latency        5.62ms     1.11ms    11.16ms
  Latency Distribution
     50%     5.50ms
     75%     6.49ms
     90%     7.51ms
     99%     9.05ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12046.33     927.57   15270.57
  Latency        8.29ms     3.77ms    26.61ms
  Latency Distribution
     50%     7.17ms
     75%    10.62ms
     90%    14.53ms
     99%    20.08ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    109713.93    7517.57  115934.72
  Latency        0.89ms   289.91us     4.78ms
  Latency Distribution
     50%   832.00us
     75%     1.08ms
     90%     1.37ms
     99%     2.10ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    107807.68    8409.69  112780.70
  Latency        0.91ms   319.51us     4.66ms
  Latency Distribution
     50%   839.00us
     75%     1.16ms
     90%     1.47ms
     99%     2.19ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     69295.89    5175.31   77800.43
  Latency        1.44ms   498.52us     6.29ms
  Latency Distribution
     50%     1.30ms
     75%     1.73ms
     90%     2.25ms
     99%     3.72ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    108755.52    7478.97  114249.86
  Latency        0.91ms   301.32us     3.99ms
  Latency Distribution
     50%   834.00us
     75%     1.13ms
     90%     1.45ms
     99%     2.29ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    105192.29    6223.29  109249.96
  Latency        0.93ms   310.52us     4.99ms
  Latency Distribution
     50%     0.87ms
     75%     1.14ms
     90%     1.44ms
     99%     2.17ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    107721.35    8298.46  113980.02
  Latency        0.91ms   292.84us     5.19ms
  Latency Distribution
     50%     0.86ms
     75%     1.10ms
     90%     1.36ms
     99%     1.97ms
### CBV Response Types (/cbv-response)
  Reqs/sec    112218.78    7203.91  119891.78
  Latency        0.87ms   273.33us     5.55ms
  Latency Distribution
     50%   816.00us
     75%     1.05ms
     90%     1.33ms
     99%     1.92ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16291.93    1102.13   17783.94
  Latency        6.11ms     2.10ms    15.56ms
  Latency Distribution
     50%     5.96ms
     75%     8.17ms
     90%     9.47ms
     99%    11.25ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    143913.17   12617.86  152821.46
  Latency      680.46us   250.63us     7.01ms
  Latency Distribution
     50%   641.00us
     75%   809.00us
     90%     1.03ms
     99%     1.74ms
### File Upload (POST /upload)
  Reqs/sec    124701.66   17597.24  154302.52
  Latency      826.90us   246.70us     5.54ms
  Latency Distribution
     50%   829.00us
     75%     0.98ms
     90%     1.18ms
     99%     1.93ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    115185.91    9201.18  122897.25
  Latency      831.11us   313.69us     7.60ms
  Latency Distribution
     50%   803.00us
     75%     0.99ms
     90%     1.19ms
     99%     2.03ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
 3799 / 10000 [==================>------------------------------]  37.99% 9475/s
  Reqs/sec      9661.69     849.58   10879.90
  Latency       10.31ms     2.59ms    25.64ms
  Latency Distribution
     50%    10.32ms
     75%    12.00ms
     90%    14.04ms
     99%    17.98ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    155736.55   11640.23  162865.07
  Latency      628.01us   275.06us     5.06ms
  Latency Distribution
     50%   586.00us
     75%   709.00us
     90%     0.91ms
     99%     2.04ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    101720.76    9406.80  108852.33
  Latency        0.96ms   341.77us     6.53ms
  Latency Distribution
     50%     0.89ms
     75%     1.17ms
     90%     1.48ms
     99%     2.22ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     91739.65    9084.50   97908.50
  Latency        1.07ms   393.38us     5.73ms
  Latency Distribution
     50%     1.00ms
     75%     1.27ms
     90%     1.56ms
     99%     2.46ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    102182.73    7691.34  109261.16
  Latency        0.96ms   293.59us     4.85ms
  Latency Distribution
     50%     0.91ms
     75%     1.18ms
     90%     1.45ms
     99%     2.11ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    110875.66    7104.28  115256.28
  Latency        0.89ms   249.92us     4.64ms
  Latency Distribution
     50%   846.00us
     75%     1.08ms
     90%     1.32ms
     99%     1.84ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    108282.01    9221.68  114297.78
  Latency        0.91ms   326.10us     6.13ms
  Latency Distribution
     50%   833.00us
     75%     1.11ms
     90%     1.39ms
     99%     2.10ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    186946.26   21829.03  202829.01
  Latency      521.42us   270.71us    11.35ms
  Latency Distribution
     50%   452.00us
     75%   610.00us
     90%   807.00us
     99%     1.56ms

### Path Parameter - int (/items/12345)
  Reqs/sec    164955.00   12526.79  173173.15
  Latency      586.05us   226.22us     4.67ms
  Latency Distribution
     50%   537.00us
     75%   702.00us
     90%   848.00us
     99%     1.64ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    163231.04   14321.41  173199.88
  Latency      590.67us   320.59us     5.81ms
  Latency Distribution
     50%   551.00us
     75%   702.00us
     90%     0.86ms
     99%     1.88ms

### Header Parameter (/header)
  Reqs/sec    107260.34    9672.17  112064.18
  Latency        0.92ms   299.14us     4.72ms
  Latency Distribution
     50%     0.85ms
     75%     1.12ms
     90%     1.42ms
     99%     2.10ms

### Cookie Parameter (/cookie)
  Reqs/sec    105996.76    8828.37  112115.61
  Latency        0.93ms   288.26us     4.23ms
  Latency Distribution
     50%     0.86ms
     75%     1.15ms
     90%     1.46ms
     99%     2.20ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     85454.39    5622.54   89839.04
  Latency        1.15ms   341.13us     4.06ms
  Latency Distribution
     50%     1.07ms
     75%     1.41ms
     90%     1.77ms
     99%     2.64ms
