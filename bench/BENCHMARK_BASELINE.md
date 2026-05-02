# Django-Bolt Benchmark
Generated: Mon Mar 30 04:14:59 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    194850.80   16094.25  204288.29
  Latency      501.84us   262.07us     7.67ms
  Latency Distribution
     50%   450.00us
     75%   579.00us
     90%   706.00us
     99%     1.84ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    125898.51    8949.33  130612.47
  Latency      774.96us   309.76us     4.49ms
  Latency Distribution
     50%   733.00us
     75%     0.91ms
     90%     1.12ms
     99%     2.07ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    129793.48   11051.86  137351.53
  Latency      752.17us   362.29us     6.83ms
  Latency Distribution
     50%   657.00us
     75%     0.97ms
     90%     1.21ms
     99%     1.96ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    106956.71   10027.33  112932.49
  Latency        0.92ms   328.31us     4.66ms
  Latency Distribution
     50%   846.00us
     75%     1.12ms
     90%     1.43ms
     99%     2.36ms
### Cookie Endpoint (/cookie)
  Reqs/sec    106884.41    4856.82  109992.30
  Latency        0.91ms   243.05us     4.44ms
  Latency Distribution
     50%     0.88ms
     75%     1.12ms
     90%     1.38ms
     99%     2.00ms
### Exception Endpoint (/exc)
  Reqs/sec    146154.36   13497.42  158163.62
  Latency      664.86us   324.37us     5.74ms
  Latency Distribution
     50%   596.00us
     75%   806.00us
     90%     1.01ms
     99%     1.69ms
### HTML Response (/html)
  Reqs/sec    169210.04   16601.33  179453.13
  Latency      572.56us   280.36us     6.64ms
  Latency Distribution
     50%   491.00us
     75%   690.00us
     90%     0.91ms
     99%     1.77ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     40824.97    7772.67   45064.08
  Latency        2.47ms     1.13ms    17.87ms
  Latency Distribution
     50%     2.22ms
     75%     2.85ms
     90%     3.64ms
     99%     7.67ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     81136.13    7461.94   92875.14
  Latency        1.24ms   351.97us     5.03ms
  Latency Distribution
     50%     1.17ms
     75%     1.52ms
     90%     1.93ms
     99%     2.73ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     17462.58    1358.13   19871.25
  Latency        5.71ms     1.59ms    15.31ms
  Latency Distribution
     50%     5.41ms
     75%     6.37ms
     90%     8.45ms
     99%    11.57ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15256.22     806.24   15982.60
  Latency        6.50ms     2.14ms    17.50ms
  Latency Distribution
     50%     6.38ms
     75%     8.29ms
     90%     9.92ms
     99%    12.52ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     85650.81    4849.85   90915.42
  Latency        1.15ms   330.18us     3.92ms
  Latency Distribution
     50%     1.07ms
     75%     1.41ms
     90%     1.81ms
     99%     2.50ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    159817.40   13832.49  169637.06
  Latency      604.77us   319.44us     6.13ms
  Latency Distribution
     50%   548.00us
     75%   647.00us
     90%   805.00us
     99%     2.07ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    153965.23   11527.47  159858.81
  Latency      633.10us   289.30us     5.07ms
  Latency Distribution
     50%   600.00us
     75%   717.00us
     90%   836.00us
     99%     1.85ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14597.46    1204.20   17763.19
  Latency        6.84ms     1.93ms    21.22ms
  Latency Distribution
     50%     6.53ms
     75%     7.96ms
     90%    10.42ms
     99%    12.13ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10139.29     924.13   12868.72
  Latency        9.83ms     4.93ms    35.67ms
  Latency Distribution
     50%     8.39ms
     75%    12.62ms
     90%    17.73ms
     99%    26.13ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     17820.08    1469.26   24373.57
  Latency        5.63ms     1.26ms    12.15ms
  Latency Distribution
     50%     5.31ms
     75%     6.77ms
     90%     7.93ms
     99%     9.46ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     11975.79     739.58   13106.59
  Latency        8.31ms     3.20ms    25.91ms
  Latency Distribution
     50%     7.78ms
     75%    10.53ms
     90%    13.32ms
     99%    18.07ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    114414.33   11034.05  120954.39
  Latency        0.87ms   292.25us     4.96ms
  Latency Distribution
     50%   808.00us
     75%     1.05ms
     90%     1.32ms
     99%     1.99ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    113910.34    8818.62  122183.76
  Latency        0.87ms   260.62us     4.48ms
  Latency Distribution
     50%   812.00us
     75%     1.06ms
     90%     1.37ms
     99%     2.03ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     69518.17    5570.44   78415.71
  Latency        1.44ms   384.07us     6.43ms
  Latency Distribution
     50%     1.37ms
     75%     1.68ms
     90%     2.04ms
     99%     2.86ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    109871.27    8546.57  116282.79
  Latency        0.89ms   262.86us     4.43ms
  Latency Distribution
     50%   835.00us
     75%     1.09ms
     90%     1.38ms
     99%     2.05ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    105596.21    8566.26  115208.43
  Latency        0.93ms   308.56us     4.04ms
  Latency Distribution
     50%     0.86ms
     75%     1.12ms
     90%     1.46ms
     99%     2.28ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    110264.26    8351.67  117176.12
  Latency        0.89ms   270.38us     4.02ms
  Latency Distribution
     50%   831.00us
     75%     1.10ms
     90%     1.43ms
     99%     2.10ms
### CBV Response Types (/cbv-response)
  Reqs/sec    116476.22    9102.72  123980.82
  Latency        0.85ms   273.19us     5.18ms
  Latency Distribution
     50%   801.00us
     75%     1.03ms
     90%     1.30ms
     99%     1.85ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16493.33    1202.97   19758.58
  Latency        6.05ms     1.17ms    15.58ms
  Latency Distribution
     50%     6.09ms
     75%     6.97ms
     90%     7.75ms
     99%     9.47ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    138453.49    9052.37  144096.05
  Latency      698.02us   235.81us     4.68ms
  Latency Distribution
     50%   657.00us
     75%     0.86ms
     90%     1.08ms
     99%     1.61ms
### File Upload (POST /upload)
  Reqs/sec    119320.67    9706.09  128150.02
  Latency      809.31us   340.60us     6.05ms
  Latency Distribution
     50%   790.00us
     75%     0.99ms
     90%     1.17ms
     99%     1.77ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    113290.40    7634.24  119139.91
  Latency        0.86ms   287.24us     5.89ms
  Latency Distribution
     50%   820.00us
     75%     1.10ms
     90%     1.36ms
     99%     1.88ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9634.53     818.54   11186.09
  Latency       10.34ms     2.03ms    23.89ms
  Latency Distribution
     50%    10.01ms
     75%    11.56ms
     90%    13.47ms
     99%    16.92ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    160097.89   18595.07  177868.76
  Latency      623.36us   269.09us     5.15ms
  Latency Distribution
     50%   563.00us
     75%   728.00us
     90%     0.95ms
     99%     1.68ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    100731.86    7557.23  105227.13
  Latency        0.98ms   366.73us     5.25ms
  Latency Distribution
     50%     0.91ms
     75%     1.20ms
     90%     1.51ms
     99%     2.43ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     90688.36    9134.47   97430.76
  Latency        1.09ms   400.82us     6.58ms
  Latency Distribution
     50%     1.01ms
     75%     1.29ms
     90%     1.59ms
     99%     2.62ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    101802.74    7766.23  107770.21
  Latency        0.96ms   278.79us     4.49ms
  Latency Distribution
     50%     0.90ms
     75%     1.19ms
     90%     1.49ms
     99%     2.10ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    108356.68    7507.14  113785.57
  Latency        0.90ms   240.81us     4.47ms
  Latency Distribution
     50%   847.00us
     75%     1.10ms
     90%     1.39ms
     99%     1.96ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    108616.86    6435.21  114535.02
  Latency        0.90ms   256.84us     5.84ms
  Latency Distribution
     50%   846.00us
     75%     1.10ms
     90%     1.37ms
     99%     2.03ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    201138.74   24068.67  217958.50
  Latency      487.96us   407.07us    12.02ms
  Latency Distribution
     50%   421.00us
     75%   538.00us
     90%   687.00us
     99%     2.12ms

### Path Parameter - int (/items/12345)
  Reqs/sec    129607.47   66485.44  176218.28
  Latency      597.71us   219.93us     4.47ms
  Latency Distribution
     50%   571.00us
     75%   724.00us
     90%     0.91ms
     99%     1.58ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    148055.76   24383.10  170091.59
  Latency      609.39us   368.64us     6.21ms
  Latency Distribution
     50%   539.00us
     75%   754.00us
     90%     0.94ms
     99%     1.77ms

### Header Parameter (/header)
  Reqs/sec    106830.86    8797.77  112119.58
  Latency        0.92ms   340.26us     5.23ms
  Latency Distribution
     50%     0.86ms
     75%     1.11ms
     90%     1.40ms
     99%     2.29ms

### Cookie Parameter (/cookie)
  Reqs/sec    106405.95    6881.54  111874.28
  Latency        0.92ms   294.14us     4.45ms
  Latency Distribution
     50%     0.86ms
     75%     1.14ms
     90%     1.44ms
     99%     2.11ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     86595.41    5167.49   90468.11
  Latency        1.14ms   372.67us     6.87ms
  Latency Distribution
     50%     1.06ms
     75%     1.39ms
     90%     1.74ms
     99%     2.42ms
