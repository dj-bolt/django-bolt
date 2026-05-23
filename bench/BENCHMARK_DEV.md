# Django-Bolt Benchmark
Generated: Sun 24 May 2026 12:05:14 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    177299.06   21634.96  195438.30
  Latency      541.15us   356.47us     6.10ms
  Latency Distribution
     50%   459.00us
     75%   660.00us
     90%     0.89ms
     99%     1.76ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    123634.19   11472.55  132468.32
  Latency      787.15us   306.96us     5.70ms
  Latency Distribution
     50%   737.00us
     75%     0.92ms
     90%     1.10ms
     99%     2.19ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    121918.61    9565.57  129951.88
  Latency      793.01us   345.72us     5.86ms
  Latency Distribution
     50%   719.00us
     75%     0.91ms
     90%     1.22ms
     99%     2.34ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    102784.39    9227.22  108758.48
  Latency        0.96ms   313.09us     5.28ms
  Latency Distribution
     50%     0.88ms
     75%     1.17ms
     90%     1.48ms
     99%     2.23ms
### Cookie Endpoint (/cookie)
  Reqs/sec    101954.40    7975.85  109196.13
  Latency        0.96ms   306.32us     4.64ms
  Latency Distribution
     50%     0.91ms
     75%     1.22ms
     90%     1.52ms
     99%     2.22ms
### Exception Endpoint (/exc)
  Reqs/sec    129329.74   17453.71  143384.86
  Latency      759.97us   407.54us     6.37ms
  Latency Distribution
     50%   641.00us
     75%     0.88ms
     90%     1.25ms
     99%     2.63ms
### HTML Response (/html)
  Reqs/sec    145800.73   28677.98  175250.81
  Latency      622.58us   380.15us     6.75ms
  Latency Distribution
     50%   559.00us
     75%   723.00us
     90%     0.95ms
     99%     2.49ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     30389.36    7058.07   38511.08
  Latency        3.23ms     1.70ms    31.91ms
  Latency Distribution
     50%     2.82ms
     75%     3.90ms
     90%     5.19ms
     99%    10.51ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     77358.30    5095.17   80854.48
  Latency        1.28ms   380.28us     5.94ms
  Latency Distribution
     50%     1.19ms
     75%     1.57ms
     90%     1.97ms
     99%     2.90ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
 6599 / 10000 [========================================================================================================================================>----------------------------------------------------------------------]  65.99% 16462/s
  Reqs/sec     16741.12    1261.39   18851.62
  Latency        5.96ms     1.80ms    15.21ms
  Latency Distribution
     50%     6.26ms
     75%     7.45ms
     90%     8.33ms
     99%    10.51ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14718.74     763.72   15600.67
  Latency        6.75ms     2.02ms    15.91ms
  Latency Distribution
     50%     6.46ms
     75%     8.26ms
     90%     9.82ms
     99%    13.38ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     88773.31   18799.65  129605.63
  Latency        1.19ms   360.48us     5.03ms
  Latency Distribution
     50%     1.14ms
     75%     1.51ms
     90%     1.88ms
     99%     2.58ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    143705.46    7507.82  150343.92
  Latency      678.76us   310.92us     4.47ms
  Latency Distribution
     50%   593.00us
     75%   797.00us
     90%     1.08ms
     99%     2.21ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    143736.33   16790.95  157066.12
  Latency      688.84us   395.11us     5.93ms
  Latency Distribution
     50%   602.00us
     75%   767.00us
     90%     1.01ms
     99%     2.30ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     13974.79    1314.35   15845.31
  Latency        7.12ms     2.33ms    20.67ms
  Latency Distribution
     50%     7.76ms
     75%     9.22ms
     90%    10.38ms
     99%    12.24ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec      9919.15    1107.07   12489.85
  Latency        9.97ms     3.96ms    37.66ms
  Latency Distribution
     50%     9.30ms
     75%    12.36ms
     90%    15.58ms
     99%    23.05ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     17083.24    1325.16   21335.41
  Latency        5.86ms     1.69ms    14.60ms
  Latency Distribution
     50%     5.80ms
     75%     7.48ms
     90%     8.69ms
     99%    10.26ms
### Users Mini10 (Sync) (/users/sync-mini10)
 7050 / 10000 [=================================================================================================================================================>-------------------------------------------------------------]  70.50% 11721/s
  Reqs/sec     11824.56     899.06   14930.67
  Latency        8.45ms     3.19ms    25.70ms
  Latency Distribution
     50%     8.00ms
     75%    10.60ms
     90%    13.31ms
     99%    18.37ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    104293.91    8948.36  110239.66
  Latency        0.94ms   314.21us     4.88ms
  Latency Distribution
     50%     0.87ms
     75%     1.16ms
     90%     1.47ms
     99%     2.18ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    105608.63    7947.64  111284.25
  Latency        0.93ms   321.11us     5.05ms
  Latency Distribution
     50%   844.00us
     75%     1.13ms
     90%     1.46ms
     99%     2.39ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     64872.28    5143.34   69361.53
  Latency        1.53ms   456.14us     6.36ms
  Latency Distribution
     50%     1.45ms
     75%     1.82ms
     90%     2.24ms
     99%     3.11ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    103793.80    5995.79  108325.62
  Latency        0.94ms   295.58us     4.74ms
  Latency Distribution
     50%     0.88ms
     75%     1.16ms
     90%     1.44ms
     99%     2.08ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     99599.91    6303.82  103057.63
  Latency        0.98ms   303.71us     4.88ms
  Latency Distribution
     50%     0.91ms
     75%     1.24ms
     90%     1.58ms
     99%     2.31ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    100836.35    8159.81  108081.97
  Latency        0.97ms   347.90us     6.18ms
  Latency Distribution
     50%     0.90ms
     75%     1.21ms
     90%     1.52ms
     99%     2.17ms
### CBV Response Types (/cbv-response)
  Reqs/sec    105406.19    5546.69  109886.24
  Latency        0.93ms   295.87us     4.32ms
  Latency Distribution
     50%     0.87ms
     75%     1.14ms
     90%     1.45ms
     99%     2.13ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     15388.85    1681.48   16989.64
  Latency        6.38ms     1.20ms    18.15ms
  Latency Distribution
     50%     6.24ms
     75%     7.32ms
     90%     8.26ms
     99%    10.12ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    140212.68   11942.22  148169.06
  Latency      693.35us   311.02us     6.32ms
  Latency Distribution
     50%   635.00us
     75%   793.00us
     90%     1.01ms
     99%     1.83ms
### File Upload (POST /upload)
  Reqs/sec    117877.27    8019.72  123917.67
  Latency      835.09us   293.34us     4.79ms
  Latency Distribution
     50%   794.00us
     75%     1.03ms
     90%     1.26ms
     99%     2.00ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    114378.70    8600.45  122532.45
  Latency        0.85ms   240.13us     4.90ms
  Latency Distribution
     50%   821.00us
     75%     1.02ms
     90%     1.22ms
     99%     1.89ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9525.61    1186.95   15781.44
  Latency       10.58ms     1.83ms    20.97ms
  Latency Distribution
     50%    10.51ms
     75%    11.81ms
     90%    13.12ms
     99%    16.38ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    143906.17   11613.43  153595.56
  Latency      666.44us   367.99us     6.38ms
  Latency Distribution
     50%   565.00us
     75%   816.00us
     90%     1.00ms
     99%     2.10ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     98701.11    8107.02  104730.18
  Latency        0.99ms   380.37us     6.39ms
  Latency Distribution
     50%     0.91ms
     75%     1.23ms
     90%     1.57ms
     99%     2.42ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     90367.02    7526.67   95420.66
  Latency        1.09ms   401.04us     5.86ms
  Latency Distribution
     50%     0.99ms
     75%     1.30ms
     90%     1.67ms
     99%     2.72ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     99901.75    6951.83  104474.91
  Latency        0.98ms   330.91us     5.80ms
  Latency Distribution
     50%     0.91ms
     75%     1.18ms
     90%     1.48ms
     99%     2.19ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    104490.30    8563.44  109259.91
  Latency        0.94ms   299.31us     4.92ms
  Latency Distribution
     50%     0.88ms
     75%     1.15ms
     90%     1.43ms
     99%     2.24ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    105221.81    7838.19  110014.18
  Latency        0.94ms   315.11us     6.52ms
  Latency Distribution
     50%     0.87ms
     75%     1.13ms
     90%     1.42ms
     99%     2.28ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    172081.11   11208.29  179784.91
  Latency      559.43us   285.59us     4.92ms
  Latency Distribution
     50%   503.00us
     75%   632.00us
     90%     0.85ms
     99%     1.84ms

### Path Parameter - int (/items/12345)
  Reqs/sec    154067.15   15559.61  166635.19
  Latency      627.31us   295.86us     5.17ms
  Latency Distribution
     50%   593.00us
     75%   764.00us
     90%     0.92ms
     99%     1.76ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    149901.97   14870.80  159472.24
  Latency      645.30us   344.16us     5.21ms
  Latency Distribution
     50%   579.00us
     75%   758.00us
     90%     0.96ms
     99%     2.02ms

### Header Parameter (/header)
  Reqs/sec    103170.25    7227.48  108398.86
  Latency        0.95ms   337.05us     5.35ms
  Latency Distribution
     50%     0.88ms
     75%     1.17ms
     90%     1.47ms
     99%     2.24ms

### Cookie Parameter (/cookie)
  Reqs/sec    103820.70    9109.01  112112.06
  Latency        0.95ms   338.34us     4.39ms
  Latency Distribution
     50%     0.87ms
     75%     1.17ms
     90%     1.51ms
     99%     2.40ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     84833.04    5361.28   89267.31
  Latency        1.16ms   338.36us     4.91ms
  Latency Distribution
     50%     1.10ms
     75%     1.42ms
     90%     1.73ms
     99%     2.54ms
