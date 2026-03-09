# Django-Bolt Benchmark
Generated: Sun 08 Mar 2026 07:32:13 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    145540.35   23801.14  162626.85
  Latency      680.10us   448.16us     5.20ms
  Latency Distribution
     50%   542.00us
     75%   779.00us
     90%     1.16ms
     99%     3.15ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    106235.76   11356.28  119844.76
  Latency        0.90ms   372.81us     5.72ms
  Latency Distribution
     50%   802.00us
     75%     1.09ms
     90%     1.47ms
     99%     2.50ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    106010.79   12072.64  118362.65
  Latency        0.92ms   418.66us     5.93ms
  Latency Distribution
     50%     0.88ms
     75%     1.13ms
     90%     1.52ms
     99%     2.58ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    110248.81   13537.15  122671.18
  Latency        0.90ms   374.60us     7.11ms
  Latency Distribution
     50%   823.00us
     75%     1.10ms
     90%     1.43ms
     99%     2.36ms
### Cookie Endpoint (/cookie)
  Reqs/sec    112534.88   10087.47  118402.50
  Latency        0.88ms   348.15us     6.02ms
  Latency Distribution
     50%   775.00us
     75%     1.09ms
     90%     1.47ms
     99%     2.28ms
### Exception Endpoint (/exc)
  Reqs/sec    134089.71    9157.84  147122.04
  Latency      730.21us   523.33us     7.76ms
  Latency Distribution
     50%   557.00us
     75%     0.91ms
     90%     1.49ms
     99%     3.40ms
### HTML Response (/html)
  Reqs/sec    161651.23   16896.39  173160.33
  Latency      607.55us   431.11us     5.60ms
  Latency Distribution
     50%   463.00us
     75%   785.00us
     90%     1.20ms
     99%     2.94ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     34338.70    8302.10   40916.97
  Latency        2.90ms     2.17ms    33.42ms
  Latency Distribution
     50%     2.42ms
     75%     3.55ms
     90%     4.88ms
     99%    10.16ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     86912.67    6221.65   92844.80
  Latency        1.12ms   431.63us     5.86ms
  Latency Distribution
     50%     1.02ms
     75%     1.37ms
     90%     1.77ms
     99%     3.05ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
 7890 / 10000 [======================================================================================================================>-------------------------------]  78.90% 19687/s
  Reqs/sec     18520.43    3536.74   22705.90
  Latency        5.37ms     2.44ms    28.21ms
  Latency Distribution
     50%     4.90ms
     75%     6.55ms
     90%     8.54ms
     99%    14.23ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     19138.98    2030.21   25236.52
  Latency        5.24ms     2.01ms    18.52ms
  Latency Distribution
     50%     4.83ms
     75%     6.51ms
     90%     8.33ms
     99%    12.12ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     90360.83    8098.56   98846.76
  Latency        1.09ms   361.43us     6.28ms
  Latency Distribution
     50%     1.03ms
     75%     1.36ms
     90%     1.71ms
     99%     2.55ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    150525.83   11505.05  157797.21
  Latency      654.87us   514.25us     5.98ms
  Latency Distribution
     50%   482.00us
     75%   799.00us
     90%     1.31ms
     99%     3.22ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    139842.07   14437.12  147820.44
  Latency      694.26us   444.94us     5.33ms
  Latency Distribution
     50%   580.00us
     75%     0.87ms
     90%     1.26ms
     99%     3.00ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14894.89    1187.09   17929.88
  Latency        6.70ms     1.75ms    16.99ms
  Latency Distribution
     50%     6.79ms
     75%     7.96ms
     90%     9.11ms
     99%    11.63ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10426.82    1141.33   13243.64
  Latency        9.59ms     3.83ms    32.22ms
  Latency Distribution
     50%     8.94ms
     75%    11.93ms
     90%    15.18ms
     99%    21.90ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     18233.43    1279.73   21240.75
  Latency        5.48ms     1.36ms    13.33ms
  Latency Distribution
     50%     5.21ms
     75%     6.57ms
     90%     7.78ms
     99%     9.83ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     11943.45     960.07   13542.88
  Latency        8.35ms     2.77ms    24.95ms
  Latency Distribution
     50%     7.89ms
     75%    10.05ms
     90%    12.62ms
     99%    17.22ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    106514.54   10373.99  114763.59
  Latency        0.93ms   370.33us     5.33ms
  Latency Distribution
     50%   847.00us
     75%     1.14ms
     90%     1.43ms
     99%     2.44ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    102073.12    8472.94  111538.88
  Latency        0.95ms   329.65us     4.89ms
  Latency Distribution
     50%     0.88ms
     75%     1.20ms
     90%     1.54ms
     99%     2.35ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     58203.31   12809.52   69713.48
  Latency        1.61ms   672.10us     9.04ms
  Latency Distribution
     50%     1.49ms
     75%     1.92ms
     90%     2.54ms
     99%     4.75ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    104092.71    6994.28  108839.88
  Latency        0.94ms   341.89us     4.87ms
  Latency Distribution
     50%     0.86ms
     75%     1.18ms
     90%     1.50ms
     99%     2.46ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    102849.92    7448.25  109924.96
  Latency        0.96ms   261.86us     4.08ms
  Latency Distribution
     50%     0.90ms
     75%     1.19ms
     90%     1.48ms
     99%     2.08ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    103218.29   11026.88  110322.59
  Latency        0.95ms   313.46us     5.09ms
  Latency Distribution
     50%     0.89ms
     75%     1.17ms
     90%     1.49ms
     99%     2.18ms
### CBV Response Types (/cbv-response)
  Reqs/sec    109237.39    8645.92  115419.40
  Latency        0.90ms   292.92us     3.74ms
  Latency Distribution
     50%   829.00us
     75%     1.12ms
     90%     1.46ms
     99%     2.23ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16586.52    1122.38   17438.62
  Latency        5.98ms     1.56ms    14.10ms
  Latency Distribution
     50%     5.79ms
     75%     7.21ms
     90%     8.41ms
     99%    11.09ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    131861.35    9320.14  138786.36
  Latency      729.86us   297.08us     5.50ms
  Latency Distribution
     50%   671.00us
     75%     0.86ms
     90%     1.11ms
     99%     2.07ms
### File Upload (POST /upload)
  Reqs/sec    113844.41    9279.55  123771.38
  Latency        0.87ms   342.89us     6.05ms
  Latency Distribution
     50%   849.00us
     75%     1.03ms
     90%     1.29ms
     99%     2.22ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    107645.66    7856.10  112697.59
  Latency        0.91ms   307.88us     5.83ms
  Latency Distribution
     50%     0.87ms
     75%     1.12ms
     90%     1.43ms
     99%     2.16ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9659.03     895.59   10769.54
  Latency       10.31ms     3.18ms    26.87ms
  Latency Distribution
     50%     9.81ms
     75%    12.90ms
     90%    15.23ms
     99%    19.31ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    147510.50    9461.00  153222.42
  Latency      663.88us   271.00us     5.05ms
  Latency Distribution
     50%   607.00us
     75%   814.00us
     90%     1.03ms
     99%     1.71ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    100507.26    9721.69  106415.60
  Latency        0.98ms   370.13us     6.21ms
  Latency Distribution
     50%     0.90ms
     75%     1.18ms
     90%     1.52ms
     99%     2.40ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     90943.55    8100.27  100696.20
  Latency        1.09ms   363.17us     4.90ms
  Latency Distribution
     50%     1.01ms
     75%     1.32ms
     90%     1.64ms
     99%     2.75ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    100783.63    8738.54  106373.07
  Latency        0.97ms   342.69us     5.65ms
  Latency Distribution
     50%     0.89ms
     75%     1.19ms
     90%     1.52ms
     99%     2.34ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    107012.25    9247.74  112984.56
  Latency        0.92ms   342.73us     5.18ms
  Latency Distribution
     50%   846.00us
     75%     1.13ms
     90%     1.45ms
     99%     2.22ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    109534.34    8911.31  115545.32
  Latency        0.90ms   293.86us     5.16ms
  Latency Distribution
     50%   835.00us
     75%     1.08ms
     90%     1.35ms
     99%     2.10ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    175410.97   16473.34  184997.83
  Latency      549.94us   244.48us     4.39ms
  Latency Distribution
     50%   486.00us
     75%   660.00us
     90%   827.00us
     99%     1.80ms

### Path Parameter - int (/items/12345)
  Reqs/sec    149157.00   10634.98  155134.21
  Latency      656.46us   311.51us     5.13ms
  Latency Distribution
     50%   564.00us
     75%   806.00us
     90%     1.10ms
     99%     2.11ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    145882.12   11594.28  159655.89
  Latency      669.22us   328.67us     5.83ms
  Latency Distribution
     50%   600.00us
     75%   812.00us
     90%     1.08ms
     99%     1.98ms

### Header Parameter (/header)
  Reqs/sec    104410.38   10472.93  113659.34
  Latency        0.94ms   379.04us     5.47ms
  Latency Distribution
     50%     0.85ms
     75%     1.14ms
     90%     1.48ms
     99%     2.47ms

### Cookie Parameter (/cookie)
  Reqs/sec    108463.91    7864.38  114499.39
  Latency        0.91ms   290.26us     4.77ms
  Latency Distribution
     50%   847.00us
     75%     1.10ms
     90%     1.44ms
     99%     2.30ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     87105.46    6017.51   92194.75
  Latency        1.13ms   384.65us     6.11ms
  Latency Distribution
     50%     1.06ms
     75%     1.39ms
     90%     1.73ms
     99%     2.61ms
