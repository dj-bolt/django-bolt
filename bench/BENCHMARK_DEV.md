# Django-Bolt Benchmark
Generated: Mon 09 Mar 2026 08:09:45 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    167179.92   14003.43  182452.23
  Latency      582.56us   279.40us     4.79ms
  Latency Distribution
     50%   520.00us
     75%   694.00us
     90%     0.95ms
     99%     1.92ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    118095.17   14710.48  140242.99
  Latency        0.86ms   430.13us     6.82ms
  Latency Distribution
     50%   756.00us
     75%     1.00ms
     90%     1.32ms
     99%     3.19ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    123174.34    7222.23  129515.80
  Latency      797.53us   373.37us     5.61ms
  Latency Distribution
     50%   730.00us
     75%     1.00ms
     90%     1.29ms
     99%     2.34ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    102167.58    8645.47  106540.70
  Latency        0.96ms   389.08us     5.01ms
  Latency Distribution
     50%     0.88ms
     75%     1.19ms
     90%     1.54ms
     99%     2.69ms
### Cookie Endpoint (/cookie)
  Reqs/sec    106415.97    8572.07  114141.62
  Latency        0.93ms   324.99us     5.29ms
  Latency Distribution
     50%     0.86ms
     75%     1.12ms
     90%     1.43ms
     99%     2.20ms
### Exception Endpoint (/exc)
  Reqs/sec    140893.44   10564.46  151175.64
  Latency      694.31us   286.99us     5.01ms
  Latency Distribution
     50%   657.00us
     75%     0.86ms
     90%     1.08ms
     99%     1.98ms
### HTML Response (/html)
  Reqs/sec    163096.49   12317.19  175525.46
  Latency      587.36us   328.15us     5.51ms
  Latency Distribution
     50%   547.00us
     75%   667.00us
     90%     0.88ms
     99%     2.20ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     37084.08    7457.42   41984.22
  Latency        2.68ms     1.49ms    20.79ms
  Latency Distribution
     50%     2.44ms
     75%     3.18ms
     90%     4.10ms
     99%     9.08ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     70667.95    7389.24   86339.06
  Latency        1.44ms   558.54us     5.19ms
  Latency Distribution
     50%     1.29ms
     75%     1.77ms
     90%     2.34ms
     99%     3.92ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     17250.32    1830.26   19165.40
  Latency        5.78ms     1.90ms    18.87ms
  Latency Distribution
     50%     5.76ms
     75%     7.32ms
     90%     8.47ms
     99%    11.37ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     16218.94    1231.52   20199.10
  Latency        6.18ms     1.48ms    16.52ms
  Latency Distribution
     50%     6.02ms
     75%     7.29ms
     90%     8.49ms
     99%    10.91ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     87511.93    4224.59   91380.38
  Latency        1.12ms   446.14us     4.74ms
  Latency Distribution
     50%     1.01ms
     75%     1.42ms
     90%     1.91ms
     99%     3.12ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    166356.76   18431.55  177890.81
  Latency      582.46us   253.93us     5.55ms
  Latency Distribution
     50%   551.00us
     75%   657.00us
     90%   787.00us
     99%     1.60ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    147873.63   10965.28  163307.55
  Latency      648.81us   348.42us     6.10ms
  Latency Distribution
     50%   576.00us
     75%   742.00us
     90%     0.96ms
     99%     2.29ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     15687.56    1576.03   17828.98
  Latency        6.35ms     2.16ms    22.09ms
  Latency Distribution
     50%     5.96ms
     75%     7.56ms
     90%     9.58ms
     99%    14.36ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     12443.37    1653.57   15463.94
  Latency        8.03ms     4.14ms    33.64ms
  Latency Distribution
     50%     6.86ms
     75%    10.14ms
     90%    14.38ms
     99%    22.85ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     19513.49    2562.60   31212.98
  Latency        5.21ms     1.47ms    12.53ms
  Latency Distribution
     50%     5.10ms
     75%     6.36ms
     90%     7.62ms
     99%     9.56ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     13314.50    1724.69   15947.00
  Latency        7.39ms     2.83ms    23.57ms
  Latency Distribution
     50%     6.81ms
     75%     9.00ms
     90%    11.46ms
     99%    17.39ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    109707.29   11088.27  119472.89
  Latency        0.88ms   301.01us     4.20ms
  Latency Distribution
     50%   814.00us
     75%     1.07ms
     90%     1.36ms
     99%     2.25ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    107473.35    8978.23  118531.37
  Latency        0.90ms   299.88us     4.36ms
  Latency Distribution
     50%   844.00us
     75%     1.09ms
     90%     1.40ms
     99%     2.16ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     66948.36    6103.09   72590.82
  Latency        1.47ms   551.98us     5.67ms
  Latency Distribution
     50%     1.37ms
     75%     1.78ms
     90%     2.28ms
     99%     4.16ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     98028.19    6742.68  104232.52
  Latency        1.00ms   375.63us     4.40ms
  Latency Distribution
     50%     0.91ms
     75%     1.24ms
     90%     1.63ms
     99%     2.74ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     90430.94    9592.27   98941.23
  Latency        1.07ms   453.74us     6.83ms
  Latency Distribution
     50%     0.97ms
     75%     1.33ms
     90%     1.73ms
     99%     3.21ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    101298.32    8153.47  108144.80
  Latency        0.96ms   320.88us     5.08ms
  Latency Distribution
     50%     0.89ms
     75%     1.19ms
     90%     1.52ms
     99%     2.35ms
### CBV Response Types (/cbv-response)
  Reqs/sec    104134.78   11234.93  113698.48
  Latency        0.94ms   301.88us     4.35ms
  Latency Distribution
     50%     0.88ms
     75%     1.18ms
     90%     1.47ms
     99%     2.21ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     17603.39    1340.36   19568.83
  Latency        5.66ms     1.68ms    16.56ms
  Latency Distribution
     50%     5.48ms
     75%     6.75ms
     90%     8.13ms
     99%    11.29ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    132265.00    9358.68  140202.00
  Latency      739.46us   291.49us     5.72ms
  Latency Distribution
     50%   683.00us
     75%     0.89ms
     90%     1.14ms
     99%     1.99ms
### File Upload (POST /upload)
  Reqs/sec    108274.40    7098.29  115907.65
  Latency        0.88ms   469.61us     5.60ms
  Latency Distribution
     50%   810.00us
     75%     1.05ms
     90%     1.39ms
     99%     3.36ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    112150.50   10869.07  120902.26
  Latency        0.87ms   392.11us     6.26ms
  Latency Distribution
     50%   800.00us
     75%     1.00ms
     90%     1.33ms
     99%     2.45ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9897.17     952.52   12059.85
  Latency       10.09ms     3.29ms    24.10ms
  Latency Distribution
     50%     9.43ms
     75%    12.62ms
     90%    15.10ms
     99%    19.95ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    136047.88   10117.43  147099.64
  Latency      717.04us   486.13us     6.14ms
  Latency Distribution
     50%   602.00us
     75%   798.00us
     90%     1.10ms
     99%     3.49ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    103090.25    8072.24  109708.03
  Latency        0.95ms   375.52us     5.54ms
  Latency Distribution
     50%     0.86ms
     75%     1.15ms
     90%     1.53ms
     99%     2.59ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     93169.00   14168.87  105087.92
  Latency        1.08ms   495.72us     6.48ms
  Latency Distribution
     50%     0.97ms
     75%     1.30ms
     90%     1.66ms
     99%     3.45ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    103065.25    8826.39  109147.56
  Latency        0.94ms   347.06us     4.73ms
  Latency Distribution
     50%     0.87ms
     75%     1.14ms
     90%     1.44ms
     99%     2.36ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    107132.76    7216.91  113970.96
  Latency        0.91ms   304.50us     4.30ms
  Latency Distribution
     50%     0.85ms
     75%     1.14ms
     90%     1.44ms
     99%     2.17ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec     94362.75   21562.98  109204.77
  Latency        1.06ms   626.61us     9.07ms
  Latency Distribution
     50%     0.93ms
     75%     1.28ms
     90%     1.67ms
     99%     4.16ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    164871.54   17816.20  185285.10
  Latency      614.44us   285.52us     5.16ms
  Latency Distribution
     50%   566.00us
     75%   722.00us
     90%     0.91ms
     99%     1.91ms

### Path Parameter - int (/items/12345)
  Reqs/sec    136578.60    9491.80  146390.24
  Latency      703.73us   316.26us     5.38ms
  Latency Distribution
     50%   648.00us
     75%   830.00us
     90%     1.11ms
     99%     2.11ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    129069.23    8607.78  136544.45
  Latency      757.80us   445.04us     5.41ms
  Latency Distribution
     50%   682.00us
     75%     0.89ms
     90%     1.14ms
     99%     3.42ms

### Header Parameter (/header)
  Reqs/sec    104172.31    6582.44  112311.04
  Latency        0.94ms   406.71us     5.62ms
  Latency Distribution
     50%   848.00us
     75%     1.13ms
     90%     1.44ms
     99%     3.11ms

### Cookie Parameter (/cookie)
  Reqs/sec    108742.15   11474.36  119460.00
  Latency        0.90ms   314.51us     5.38ms
  Latency Distribution
     50%   845.00us
     75%     1.12ms
     90%     1.40ms
     99%     2.17ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     87523.28    6634.04   92551.35
  Latency        1.12ms   370.67us     5.78ms
  Latency Distribution
     50%     1.04ms
     75%     1.37ms
     90%     1.78ms
     99%     2.63ms
