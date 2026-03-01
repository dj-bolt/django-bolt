# Django-Bolt Benchmark
Generated: Sun 01 Mar 2026 09:28:04 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    114174.20   10589.94  123454.55
  Latency        0.87ms   328.91us     4.66ms
  Latency Distribution
     50%   803.00us
     75%     1.05ms
     90%     1.29ms
     99%     2.26ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec     88106.22    6758.25   93117.00
  Latency        1.11ms   344.47us     7.07ms
  Latency Distribution
     50%     1.06ms
     75%     1.33ms
     90%     1.61ms
     99%     2.25ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec     86208.79    5799.15   89933.43
  Latency        1.14ms   303.78us     5.08ms
  Latency Distribution
     50%     1.09ms
     75%     1.38ms
     90%     1.70ms
     99%     2.40ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    100661.75    6039.77  105102.87
  Latency        0.97ms   304.43us     4.82ms
  Latency Distribution
     50%     0.91ms
     75%     1.20ms
     90%     1.51ms
     99%     2.23ms
### Cookie Endpoint (/cookie)
  Reqs/sec     99913.02    7804.55  105170.48
  Latency        0.98ms   301.43us     4.33ms
  Latency Distribution
     50%     0.91ms
     75%     1.22ms
     90%     1.57ms
     99%     2.38ms
### Exception Endpoint (/exc)
  Reqs/sec     98547.87    6489.56  104641.57
  Latency        1.01ms   346.17us     4.26ms
  Latency Distribution
     50%     0.92ms
     75%     1.24ms
     90%     1.66ms
     99%     2.61ms
### HTML Response (/html)
  Reqs/sec    107369.87    5873.07  110599.24
  Latency        0.91ms   275.32us     4.80ms
  Latency Distribution
     50%     0.85ms
     75%     1.14ms
     90%     1.43ms
     99%     2.09ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     30079.80    5592.28   34341.92
  Latency        3.32ms     1.53ms    22.39ms
  Latency Distribution
     50%     3.03ms
     75%     3.92ms
     90%     5.05ms
     99%     9.04ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     77314.86    5977.73   84292.78
  Latency        1.28ms   373.24us     5.29ms
  Latency Distribution
     50%     1.21ms
     75%     1.58ms
     90%     1.98ms
     99%     2.79ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     17096.94    1586.60   20872.86
  Latency        5.85ms     1.21ms    13.95ms
  Latency Distribution
     50%     5.77ms
     75%     6.64ms
     90%     7.57ms
     99%    10.24ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14878.85    1129.74   16152.84
  Latency        6.62ms     1.88ms    16.52ms
  Latency Distribution
     50%     6.41ms
     75%     8.09ms
     90%     9.51ms
     99%    12.42ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     83844.96    6503.49   88707.19
  Latency        1.17ms   399.51us     4.68ms
  Latency Distribution
     50%     1.06ms
     75%     1.46ms
     90%     1.90ms
     99%     2.97ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec     99800.44    7684.83  104099.99
  Latency        0.98ms   306.37us     4.27ms
  Latency Distribution
     50%     0.92ms
     75%     1.23ms
     90%     1.54ms
     99%     2.38ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     90103.82    4715.37   94586.22
  Latency        1.09ms   387.73us     4.63ms
  Latency Distribution
     50%     0.99ms
     75%     1.32ms
     90%     1.81ms
     99%     2.91ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     13564.43    1033.85   14538.27
  Latency        7.33ms     2.25ms    19.96ms
  Latency Distribution
     50%     7.33ms
     75%     8.79ms
     90%    11.15ms
     99%    13.21ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec      9720.17    1006.19   12156.69
  Latency       10.27ms     5.21ms    43.97ms
  Latency Distribution
     50%     8.89ms
     75%    12.15ms
     90%    16.72ms
     99%    31.46ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     15628.80     775.13   16503.11
  Latency        6.35ms     1.64ms    14.00ms
  Latency Distribution
     50%     6.08ms
     75%     7.50ms
     90%     9.40ms
     99%    11.32ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     11893.49     763.98   13127.91
  Latency        8.36ms     3.89ms    27.51ms
  Latency Distribution
     50%     7.80ms
     75%    11.20ms
     90%    14.48ms
     99%    19.43ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    107620.27   10737.94  115966.30
  Latency        0.91ms   325.96us     5.83ms
  Latency Distribution
     50%   848.00us
     75%     1.09ms
     90%     1.36ms
     99%     2.17ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    100870.72    6464.65  104905.36
  Latency        0.97ms   297.94us     4.84ms
  Latency Distribution
     50%     0.91ms
     75%     1.20ms
     90%     1.54ms
     99%     2.22ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     63842.14    4277.40   67276.31
  Latency        1.55ms   469.17us     4.70ms
  Latency Distribution
     50%     1.51ms
     75%     1.94ms
     90%     2.40ms
     99%     3.41ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     98749.40    7475.51  103375.20
  Latency        0.99ms   304.13us     4.62ms
  Latency Distribution
     50%     0.94ms
     75%     1.24ms
     90%     1.54ms
     99%     2.27ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     96804.97    5075.27  101589.88
  Latency        1.02ms   306.69us     4.08ms
  Latency Distribution
     50%     0.94ms
     75%     1.25ms
     90%     1.59ms
     99%     2.37ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    100785.44    9092.23  106235.83
  Latency        0.98ms   340.33us     5.99ms
  Latency Distribution
     50%     0.92ms
     75%     1.19ms
     90%     1.46ms
     99%     2.16ms
### CBV Response Types (/cbv-response)
  Reqs/sec    103566.00    7758.75  108836.75
  Latency        0.95ms   293.73us     4.46ms
  Latency Distribution
     50%     0.89ms
     75%     1.16ms
     90%     1.47ms
     99%     2.15ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16253.26    1041.32   16978.65
  Latency        6.12ms     1.27ms    13.82ms
  Latency Distribution
     50%     6.47ms
     75%     7.18ms
     90%     7.80ms
     99%     9.24ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec     95715.17    6966.63  101113.27
  Latency        1.02ms   412.60us     5.43ms
  Latency Distribution
     50%     0.92ms
     75%     1.28ms
     90%     1.67ms
     99%     2.74ms
### File Upload (POST /upload)
  Reqs/sec     87188.20    6831.18   91685.65
  Latency        1.13ms   369.02us     5.79ms
  Latency Distribution
     50%     1.06ms
     75%     1.39ms
     90%     1.72ms
     99%     2.59ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec     84012.86    5700.72   89304.25
  Latency        1.17ms   406.05us     4.45ms
  Latency Distribution
     50%     1.08ms
     75%     1.45ms
     90%     1.89ms
     99%     3.00ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9622.95    1027.88   11749.70
  Latency       10.39ms     2.66ms    23.65ms
  Latency Distribution
     50%    10.33ms
     75%    12.32ms
     90%    13.91ms
     99%    17.67ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    103652.07    9629.04  109039.54
  Latency        0.95ms   345.12us     6.22ms
  Latency Distribution
     50%     0.88ms
     75%     1.13ms
     90%     1.41ms
     99%     2.31ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    104041.66   19747.88  142040.62
  Latency        1.01ms   301.81us     4.75ms
  Latency Distribution
     50%     0.95ms
     75%     1.23ms
     90%     1.55ms
     99%     2.31ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     89686.47    6900.99   94347.94
  Latency        1.10ms   374.94us     5.01ms
  Latency Distribution
     50%     1.00ms
     75%     1.33ms
     90%     1.68ms
     99%     2.77ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     98042.32    6173.30  102741.30
  Latency        1.00ms   320.13us     4.95ms
  Latency Distribution
     50%     0.94ms
     75%     1.22ms
     90%     1.50ms
     99%     2.25ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    105312.36    8017.22  111142.79
  Latency        0.93ms   292.18us     5.41ms
  Latency Distribution
     50%     0.86ms
     75%     1.14ms
     90%     1.44ms
     99%     2.05ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    106041.35    6792.92  111090.24
  Latency        0.92ms   238.75us     5.22ms
  Latency Distribution
     50%     0.87ms
     75%     1.13ms
     90%     1.38ms
     99%     1.95ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    111927.62   10031.49  119019.79
  Latency        0.88ms   322.77us     5.25ms
  Latency Distribution
     50%   812.00us
     75%     1.07ms
     90%     1.34ms
     99%     2.07ms

### Path Parameter - int (/items/12345)
  Reqs/sec    102663.17   10243.88  109801.62
  Latency        0.96ms   373.01us     5.77ms
  Latency Distribution
     50%     0.90ms
     75%     1.17ms
     90%     1.46ms
     99%     2.21ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    102493.06    8512.98  107430.32
  Latency        0.95ms   280.23us     4.65ms
  Latency Distribution
     50%     0.90ms
     75%     1.18ms
     90%     1.46ms
     99%     2.13ms

### Header Parameter (/header)
  Reqs/sec    100686.21    5881.67  105533.31
  Latency        0.97ms   283.64us     4.29ms
  Latency Distribution
     50%     0.92ms
     75%     1.19ms
     90%     1.50ms
     99%     2.16ms

### Cookie Parameter (/cookie)
  Reqs/sec    102212.76    6985.48  107279.33
  Latency        0.96ms   347.11us     4.35ms
  Latency Distribution
     50%     0.87ms
     75%     1.19ms
     90%     1.51ms
     99%     2.49ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     81688.22    5438.70   86370.36
  Latency        1.20ms   403.17us     6.07ms
  Latency Distribution
     50%     1.09ms
     75%     1.47ms
     90%     1.91ms
     99%     2.93ms
