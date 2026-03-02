# Django-Bolt Benchmark
Generated: Tue 03 Mar 2026 12:02:02 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    163024.89   16427.87  178705.14
  Latency      595.95us   286.26us     5.78ms
  Latency Distribution
     50%   544.00us
     75%   683.00us
     90%     0.88ms
     99%     1.72ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    108659.12    6795.37  113247.35
  Latency        0.90ms   356.48us     6.74ms
  Latency Distribution
     50%     0.85ms
     75%     1.11ms
     90%     1.40ms
     99%     2.30ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    115059.83    8935.83  124187.95
  Latency        0.85ms   397.67us     6.42ms
  Latency Distribution
     50%   793.00us
     75%     1.03ms
     90%     1.33ms
     99%     2.56ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    104252.31    9326.67  108766.13
  Latency        0.94ms   354.61us     5.91ms
  Latency Distribution
     50%     0.86ms
     75%     1.18ms
     90%     1.49ms
     99%     2.46ms
### Cookie Endpoint (/cookie)
  Reqs/sec    103196.74    7486.82  108003.70
  Latency        0.95ms   357.57us     5.14ms
  Latency Distribution
     50%     0.86ms
     75%     1.16ms
     90%     1.52ms
     99%     2.59ms
### Exception Endpoint (/exc)
  Reqs/sec    131045.42   11329.81  138428.52
  Latency      745.20us   226.32us     3.28ms
  Latency Distribution
     50%   719.00us
     75%     0.88ms
     90%     1.14ms
     99%     1.79ms
### HTML Response (/html)
  Reqs/sec    148187.50   10283.42  158276.50
  Latency      645.33us   217.93us     4.98ms
  Latency Distribution
     50%   616.00us
     75%   796.00us
     90%     0.97ms
     99%     1.52ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     36926.41    6854.12   40204.96
  Latency        2.70ms     1.21ms    18.11ms
  Latency Distribution
     50%     2.45ms
     75%     3.15ms
     90%     4.09ms
     99%     8.25ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     77331.34    4886.60   80624.88
  Latency        1.28ms   430.52us     5.28ms
  Latency Distribution
     50%     1.19ms
     75%     1.63ms
     90%     2.07ms
     99%     3.14ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     17087.12    1654.07   20101.21
  Latency        5.84ms     1.86ms    16.06ms
  Latency Distribution
     50%     5.66ms
     75%     7.10ms
     90%     8.60ms
     99%    12.10ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15710.03     953.93   16674.67
  Latency        6.32ms     1.63ms    14.46ms
  Latency Distribution
     50%     6.21ms
     75%     7.60ms
     90%     8.84ms
     99%    11.37ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     84363.61    6680.46   92023.07
  Latency        1.16ms   392.24us     4.66ms
  Latency Distribution
     50%     1.08ms
     75%     1.45ms
     90%     1.83ms
     99%     2.87ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    143626.13   15029.20  155961.11
  Latency      673.59us   369.53us     5.39ms
  Latency Distribution
     50%   604.00us
     75%   725.00us
     90%     0.91ms
     99%     2.67ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     99802.74    7139.32  104342.34
  Latency        0.99ms   341.69us     4.54ms
  Latency Distribution
     50%     0.92ms
     75%     1.21ms
     90%     1.51ms
     99%     2.49ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14407.03    1038.18   15684.78
  Latency        6.91ms     1.84ms    19.21ms
  Latency Distribution
     50%     7.14ms
     75%     8.37ms
     90%     9.60ms
     99%    11.91ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10603.82    1254.57   13712.67
  Latency        9.42ms     4.78ms    35.27ms
  Latency Distribution
     50%     8.29ms
     75%    12.06ms
     90%    16.62ms
     99%    25.70ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     16704.64     766.31   17448.32
  Latency        5.95ms     2.01ms    14.46ms
  Latency Distribution
     50%     5.81ms
     75%     7.62ms
     90%     9.10ms
     99%    11.51ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12348.04     963.37   14636.37
  Latency        8.07ms     2.91ms    25.19ms
  Latency Distribution
     50%     7.51ms
     75%     9.76ms
     90%    12.46ms
     99%    17.93ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    110643.19    8450.07  117919.84
  Latency        0.89ms   328.26us     5.10ms
  Latency Distribution
     50%   816.00us
     75%     1.08ms
     90%     1.36ms
     99%     2.18ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     99582.71   26748.78  113346.83
  Latency        1.00ms     1.01ms    13.46ms
  Latency Distribution
     50%     0.85ms
     75%     1.14ms
     90%     1.46ms
     99%     2.67ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     69991.61    5268.92   76863.73
  Latency        1.43ms   346.60us     4.24ms
  Latency Distribution
     50%     1.34ms
     75%     1.71ms
     90%     2.08ms
     99%     2.87ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    103972.27    4747.33  109179.14
  Latency        0.94ms   315.14us     5.62ms
  Latency Distribution
     50%     0.88ms
     75%     1.15ms
     90%     1.44ms
     99%     2.14ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    101875.01    6290.42  106266.25
  Latency        0.96ms   302.92us     3.91ms
  Latency Distribution
     50%     0.89ms
     75%     1.20ms
     90%     1.49ms
     99%     2.27ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    103816.96    6010.98  110677.01
  Latency        0.95ms   310.81us     4.68ms
  Latency Distribution
     50%     0.89ms
     75%     1.17ms
     90%     1.48ms
     99%     2.29ms
### CBV Response Types (/cbv-response)
  Reqs/sec    113813.75   10519.18  122069.68
  Latency        0.87ms   288.67us     6.07ms
  Latency Distribution
     50%   813.00us
     75%     1.07ms
     90%     1.35ms
     99%     2.08ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     17228.47    1580.93   22381.86
  Latency        5.82ms     1.60ms    15.86ms
  Latency Distribution
     50%     5.69ms
     75%     7.11ms
     90%     8.33ms
     99%    10.87ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    133173.34    9212.48  139565.01
  Latency      728.66us   266.10us     5.55ms
  Latency Distribution
     50%   678.00us
     75%     0.88ms
     90%     1.12ms
     99%     1.70ms
### File Upload (POST /upload)
  Reqs/sec    114059.45    8559.89  120291.50
  Latency      843.62us   271.71us     5.51ms
  Latency Distribution
     50%   809.00us
     75%     1.05ms
     90%     1.31ms
     99%     1.90ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    107268.89    9658.58  115439.81
  Latency        0.92ms   348.33us     6.20ms
  Latency Distribution
     50%     0.88ms
     75%     1.09ms
     90%     1.40ms
     99%     2.35ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
 7890 / 10000 [======================================>----------]  78.90% 9844/s
  Reqs/sec      9984.16    1021.00   13254.44
  Latency       10.04ms     2.10ms    21.13ms
  Latency Distribution
     50%     9.82ms
     75%    11.45ms
     90%    12.83ms
     99%    17.19ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    138876.06    9566.50  144858.13
  Latency      695.00us   244.99us     5.08ms
  Latency Distribution
     50%   621.00us
     75%     0.86ms
     90%     1.13ms
     99%     1.77ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     98961.10    9475.12  105380.52
  Latency        0.99ms   360.79us     7.00ms
  Latency Distribution
     50%     0.91ms
     75%     1.20ms
     90%     1.51ms
     99%     2.29ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     88651.46    7266.17   93474.56
  Latency        1.11ms   447.98us     4.88ms
  Latency Distribution
     50%     0.98ms
     75%     1.32ms
     90%     1.82ms
     99%     3.27ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     94363.92    8599.11  102685.62
  Latency        1.05ms   363.25us     4.88ms
  Latency Distribution
     50%     0.99ms
     75%     1.31ms
     90%     1.62ms
     99%     2.52ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec     98235.58    7136.58  104940.90
  Latency        1.00ms   357.36us     5.04ms
  Latency Distribution
     50%     0.92ms
     75%     1.23ms
     90%     1.62ms
     99%     2.59ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    103554.64    7438.62  110138.37
  Latency        0.94ms   321.19us     4.70ms
  Latency Distribution
     50%     0.87ms
     75%     1.16ms
     90%     1.50ms
     99%     2.37ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    171028.89   11760.44  180734.67
  Latency      563.58us   258.35us     5.88ms
  Latency Distribution
     50%   539.00us
     75%   663.00us
     90%   842.00us
     99%     1.76ms

### Path Parameter - int (/items/12345)
  Reqs/sec    149969.56   11062.24  156286.72
  Latency      646.07us   314.46us     5.86ms
  Latency Distribution
     50%   598.00us
     75%   762.00us
     90%     0.96ms
     99%     1.67ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    149007.04   13532.97  156859.92
  Latency      654.24us   249.26us     4.66ms
  Latency Distribution
     50%   577.00us
     75%   818.00us
     90%     1.04ms
     99%     1.71ms

### Header Parameter (/header)
  Reqs/sec    100719.79    9763.64  109475.63
  Latency        0.97ms   350.23us     5.53ms
  Latency Distribution
     50%     0.90ms
     75%     1.18ms
     90%     1.50ms
     99%     2.28ms

### Cookie Parameter (/cookie)
  Reqs/sec    104510.29    7336.50  109947.69
  Latency        0.94ms   306.82us     4.24ms
  Latency Distribution
     50%     0.87ms
     75%     1.15ms
     90%     1.45ms
     99%     2.41ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     86593.08    7048.52   94461.60
  Latency        1.13ms   357.57us     6.86ms
  Latency Distribution
     50%     1.09ms
     75%     1.40ms
     90%     1.75ms
     99%     2.59ms
