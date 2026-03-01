# Django-Bolt Benchmark
Generated: Sun 01 Mar 2026 07:13:48 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    115599.13    8106.34  121580.95
  Latency      849.55us   306.94us     4.03ms
  Latency Distribution
     50%   770.00us
     75%     1.04ms
     90%     1.36ms
     99%     2.33ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec     89811.28    6740.84   94869.62
  Latency        1.10ms   348.35us     5.16ms
  Latency Distribution
     50%     1.03ms
     75%     1.32ms
     90%     1.68ms
     99%     2.60ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec     89905.97    7184.73   93941.74
  Latency        1.10ms   396.34us     5.98ms
  Latency Distribution
     50%     1.02ms
     75%     1.35ms
     90%     1.74ms
     99%     2.66ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    102908.59    7442.18  109219.63
  Latency        0.95ms   349.52us     4.79ms
  Latency Distribution
     50%     0.86ms
     75%     1.17ms
     90%     1.53ms
     99%     2.47ms
### Cookie Endpoint (/cookie)
  Reqs/sec    102484.69   10091.08  112434.85
  Latency        0.96ms   336.97us     4.49ms
  Latency Distribution
     50%     0.89ms
     75%     1.17ms
     90%     1.48ms
     99%     2.40ms
### Exception Endpoint (/exc)
  Reqs/sec    101353.93    7284.79  106590.63
  Latency        0.97ms   326.74us     5.00ms
  Latency Distribution
     50%     0.90ms
     75%     1.20ms
     90%     1.52ms
     99%     2.44ms
### HTML Response (/html)
  Reqs/sec    113139.04    8678.28  119186.89
  Latency        0.87ms   285.16us     4.63ms
  Latency Distribution
     50%   803.00us
     75%     1.06ms
     90%     1.37ms
     99%     2.13ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     30137.11    7543.81   47659.87
  Latency        3.42ms     1.80ms    19.23ms
  Latency Distribution
     50%     3.12ms
     75%     4.13ms
     90%     5.43ms
     99%    10.90ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     72770.28   14660.73   81593.69
  Latency        1.36ms   823.34us    10.56ms
  Latency Distribution
     50%     1.24ms
     75%     1.54ms
     90%     1.86ms
     99%     3.42ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     20486.84   17967.70  115260.49
  Latency        5.76ms     1.77ms    15.85ms
  Latency Distribution
     50%     5.55ms
     75%     6.96ms
     90%     8.44ms
     99%    11.69ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15835.87     893.15   16918.76
  Latency        6.27ms     2.31ms    18.24ms
  Latency Distribution
     50%     5.54ms
     75%     7.92ms
     90%    10.20ms
     99%    13.65ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     79034.21    4412.13   83560.71
  Latency        1.23ms   418.51us     5.73ms
  Latency Distribution
     50%     1.17ms
     75%     1.50ms
     90%     1.86ms
     99%     2.99ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec     93926.27    9278.38  104778.31
  Latency        1.02ms   355.16us     4.71ms
  Latency Distribution
     50%     0.94ms
     75%     1.28ms
     90%     1.62ms
     99%     2.63ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     95451.72    6073.62  101299.32
  Latency        1.02ms   332.87us     5.67ms
  Latency Distribution
     50%     0.94ms
     75%     1.26ms
     90%     1.57ms
     99%     2.46ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14118.45    1248.26   16486.50
  Latency        7.07ms     2.21ms    18.98ms
  Latency Distribution
     50%     6.76ms
     75%     8.56ms
     90%    10.42ms
     99%    14.21ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     11644.07    1481.87   14455.02
  Latency        8.54ms     3.86ms    31.45ms
  Latency Distribution
     50%     7.94ms
     75%    10.86ms
     90%    14.27ms
     99%    20.88ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     16660.21     883.33   18150.74
  Latency        5.97ms     1.54ms    15.77ms
  Latency Distribution
     50%     5.85ms
     75%     7.05ms
     90%     8.29ms
     99%    10.94ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     13031.29    1367.36   15937.01
  Latency        7.60ms     2.87ms    26.84ms
  Latency Distribution
     50%     7.10ms
     75%     9.59ms
     90%    11.92ms
     99%    16.50ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    104318.98    9301.05  114454.80
  Latency        0.95ms   401.98us     5.13ms
  Latency Distribution
     50%     0.85ms
     75%     1.16ms
     90%     1.52ms
     99%     3.12ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    100395.98   13649.28  118724.62
  Latency        1.01ms   354.51us     5.16ms
  Latency Distribution
     50%     0.92ms
     75%     1.25ms
     90%     1.62ms
     99%     2.52ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     65389.29    5687.38   72172.76
  Latency        1.49ms   422.49us     5.26ms
  Latency Distribution
     50%     1.38ms
     75%     1.75ms
     90%     2.20ms
     99%     3.35ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     92359.17    9583.71  100853.87
  Latency        1.03ms   387.82us     4.77ms
  Latency Distribution
     50%     0.96ms
     75%     1.28ms
     90%     1.64ms
     99%     2.99ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     92638.44    8738.81  104471.49
  Latency        1.04ms   410.86us     5.61ms
  Latency Distribution
     50%     0.93ms
     75%     1.28ms
     90%     1.71ms
     99%     2.76ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     96884.21    6123.25  102529.66
  Latency        1.02ms   335.30us     4.41ms
  Latency Distribution
     50%     0.94ms
     75%     1.27ms
     90%     1.59ms
     99%     2.41ms
### CBV Response Types (/cbv-response)
  Reqs/sec    104033.21    8844.01  113399.76
  Latency        0.94ms   349.65us     5.67ms
  Latency Distribution
     50%     0.86ms
     75%     1.13ms
     90%     1.46ms
     99%     2.53ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16977.05    1259.30   18572.42
  Latency        5.82ms     1.92ms    16.07ms
  Latency Distribution
     50%     5.52ms
     75%     7.16ms
     90%     8.83ms
     99%    12.18ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    101980.73   13675.85  122218.76
  Latency        1.00ms   372.78us     5.47ms
  Latency Distribution
     50%     0.91ms
     75%     1.23ms
     90%     1.63ms
     99%     2.72ms
### File Upload (POST /upload)
  Reqs/sec     86495.68    7031.56   96094.05
  Latency        1.14ms   364.67us     4.93ms
  Latency Distribution
     50%     1.06ms
     75%     1.40ms
     90%     1.78ms
     99%     2.70ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec     89333.58   20471.33  134285.87
  Latency        1.20ms   437.15us     5.07ms
  Latency Distribution
     50%     1.13ms
     75%     1.53ms
     90%     1.98ms
     99%     3.02ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9925.61    1138.35   12545.15
  Latency       10.07ms     3.10ms    23.47ms
  Latency Distribution
     50%     9.98ms
     75%    12.44ms
     90%    14.63ms
     99%    19.35ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    104860.34    8441.43  110947.26
  Latency        0.94ms   343.96us     5.24ms
  Latency Distribution
     50%     0.87ms
     75%     1.15ms
     90%     1.46ms
     99%     2.48ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     99565.20    7069.26  106341.96
  Latency        0.98ms   332.12us     4.57ms
  Latency Distribution
     50%     0.91ms
     75%     1.20ms
     90%     1.52ms
     99%     2.45ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     90037.04    7878.02   98844.40
  Latency        1.09ms   409.65us     5.20ms
  Latency Distribution
     50%     0.98ms
     75%     1.34ms
     90%     1.71ms
     99%     3.04ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     98354.41    8289.78  105966.49
  Latency        1.00ms   377.68us     5.80ms
  Latency Distribution
     50%     0.90ms
     75%     1.22ms
     90%     1.63ms
     99%     2.80ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    103786.04    9319.08  111701.11
  Latency        0.95ms   319.77us     4.95ms
  Latency Distribution
     50%     0.86ms
     75%     1.19ms
     90%     1.55ms
     99%     2.33ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec     91323.72   20673.51  108390.96
  Latency        1.11ms   653.24us    10.84ms
  Latency Distribution
     50%     0.95ms
     75%     1.34ms
     90%     1.86ms
     99%     4.33ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    114765.27   10204.61  121934.26
  Latency        0.86ms   385.26us     5.23ms
  Latency Distribution
     50%   775.00us
     75%     1.05ms
     90%     1.34ms
     99%     3.19ms

### Path Parameter - int (/items/12345)
  Reqs/sec    101118.24    8774.09  112660.83
  Latency        0.96ms   379.10us     5.14ms
  Latency Distribution
     50%     0.88ms
     75%     1.17ms
     90%     1.49ms
     99%     2.67ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    106517.30    8860.47  113308.68
  Latency        0.92ms   339.89us     4.63ms
  Latency Distribution
     50%   831.00us
     75%     1.13ms
     90%     1.47ms
     99%     2.40ms

### Header Parameter (/header)
  Reqs/sec    104990.92   11244.13  112874.54
  Latency        0.94ms   311.18us     5.16ms
  Latency Distribution
     50%     0.88ms
     75%     1.17ms
     90%     1.49ms
     99%     2.29ms

### Cookie Parameter (/cookie)
  Reqs/sec     99653.97    8154.14  110097.81
  Latency        0.98ms   353.41us     4.56ms
  Latency Distribution
     50%     0.90ms
     75%     1.24ms
     90%     1.59ms
     99%     2.55ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     83852.74    8064.76   92997.15
  Latency        1.17ms   396.66us     5.79ms
  Latency Distribution
     50%     1.11ms
     75%     1.45ms
     90%     1.81ms
     99%     2.81ms
