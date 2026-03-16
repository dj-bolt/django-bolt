# Django-Bolt Benchmark
Generated: Mon 16 Mar 2026 01:52:53 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    173209.02   15350.21  185024.21
  Latency      557.65us   327.83us     4.90ms
  Latency Distribution
     50%   491.00us
     75%   614.00us
     90%   835.00us
     99%     2.09ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    113963.50   12048.13  121575.28
  Latency        0.86ms   415.63us     7.00ms
  Latency Distribution
     50%   784.00us
     75%     1.01ms
     90%     1.35ms
     99%     2.56ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    126633.53    7358.64  130704.62
  Latency      768.27us   320.51us     8.25ms
  Latency Distribution
     50%   724.00us
     75%     0.92ms
     90%     1.16ms
     99%     2.03ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    107420.36    9773.28  113609.81
  Latency        0.91ms   277.72us     5.22ms
  Latency Distribution
     50%   843.00us
     75%     1.11ms
     90%     1.40ms
     99%     2.12ms
### Cookie Endpoint (/cookie)
  Reqs/sec    108999.39    6967.58  113960.10
  Latency        0.90ms   333.57us     6.00ms
  Latency Distribution
     50%   839.00us
     75%     1.08ms
     90%     1.35ms
     99%     2.16ms
### Exception Endpoint (/exc)
  Reqs/sec    140356.59   14617.22  148524.28
  Latency      690.39us   314.25us     5.85ms
  Latency Distribution
     50%   625.00us
     75%   813.00us
     90%     1.12ms
     99%     2.17ms
### HTML Response (/html)
  Reqs/sec    167573.72   13960.77  175647.42
  Latency      573.93us   343.12us     7.14ms
  Latency Distribution
     50%   500.00us
     75%   664.00us
     90%     0.89ms
     99%     2.15ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     37099.77    8654.04   42301.57
  Latency        2.70ms     1.51ms    22.33ms
  Latency Distribution
     50%     2.42ms
     75%     3.21ms
     90%     4.17ms
     99%     8.91ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     77813.37    4919.91   81787.79
  Latency        1.27ms   393.72us     7.93ms
  Latency Distribution
     50%     1.19ms
     75%     1.53ms
     90%     1.91ms
     99%     2.91ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     17601.22    1608.83   20987.31
  Latency        5.67ms     1.64ms    14.87ms
  Latency Distribution
     50%     5.45ms
     75%     7.05ms
     90%     8.13ms
     99%    10.70ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15835.97     993.90   17672.89
  Latency        6.29ms     1.60ms    14.59ms
  Latency Distribution
     50%     6.10ms
     75%     7.45ms
     90%     8.80ms
     99%    11.34ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     84965.01    7413.46   91095.50
  Latency        1.16ms   402.15us     6.84ms
  Latency Distribution
     50%     1.08ms
     75%     1.44ms
     90%     1.83ms
     99%     2.81ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    145784.38   20980.98  163903.98
  Latency      631.08us   302.74us     6.02ms
  Latency Distribution
     50%   613.00us
     75%   733.00us
     90%     0.87ms
     99%     1.79ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    146751.20   15048.61  157735.96
  Latency      670.25us   364.69us     7.39ms
  Latency Distribution
     50%   645.00us
     75%   775.00us
     90%     0.92ms
     99%     2.09ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14730.29    2011.73   16992.58
  Latency        6.76ms     2.51ms    24.88ms
  Latency Distribution
     50%     6.30ms
     75%     8.08ms
     90%    10.10ms
     99%    16.25ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     11323.96    1463.31   14263.19
  Latency        8.82ms     3.59ms    28.98ms
  Latency Distribution
     50%     8.08ms
     75%    10.94ms
     90%    14.11ms
     99%    20.74ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     18610.84    1427.71   21194.24
  Latency        5.36ms     1.75ms    14.74ms
  Latency Distribution
     50%     5.19ms
     75%     6.76ms
     90%     8.16ms
     99%    10.70ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12399.44    1093.32   16032.98
  Latency        8.02ms     2.83ms    23.57ms
  Latency Distribution
     50%     7.54ms
     75%     9.73ms
     90%    12.25ms
     99%    17.50ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    100104.07   28116.78  119384.22
  Latency        1.02ms     1.03ms    13.98ms
  Latency Distribution
     50%     0.85ms
     75%     1.15ms
     90%     1.51ms
     99%     3.29ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    104226.81    9238.13  113182.76
  Latency        0.94ms   416.11us     5.57ms
  Latency Distribution
     50%     0.85ms
     75%     1.13ms
     90%     1.46ms
     99%     2.97ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     66466.78    5988.92   71618.76
  Latency        1.48ms   486.11us     6.65ms
  Latency Distribution
     50%     1.38ms
     75%     1.75ms
     90%     2.20ms
     99%     3.80ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     88544.77   22803.51  107039.87
  Latency        1.00ms   334.11us     4.94ms
  Latency Distribution
     50%     0.94ms
     75%     1.27ms
     90%     1.60ms
     99%     2.42ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     99707.61    8714.18  105890.17
  Latency        0.98ms   302.41us     5.51ms
  Latency Distribution
     50%     0.92ms
     75%     1.22ms
     90%     1.53ms
     99%     2.22ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     99395.70   10111.20  107675.08
  Latency        0.99ms   372.50us     4.84ms
  Latency Distribution
     50%     0.91ms
     75%     1.25ms
     90%     1.61ms
     99%     2.79ms
### CBV Response Types (/cbv-response)
  Reqs/sec    106509.18    9567.02  113927.26
  Latency        0.92ms   306.09us     4.57ms
  Latency Distribution
     50%     0.85ms
     75%     1.17ms
     90%     1.47ms
     99%     2.17ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16705.53    1579.51   18276.42
  Latency        5.95ms     1.80ms    19.18ms
  Latency Distribution
     50%     5.66ms
     75%     7.11ms
     90%     8.60ms
     99%    11.89ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    118506.27    9414.06  125018.15
  Latency      822.81us   407.87us     6.48ms
  Latency Distribution
     50%   745.00us
     75%     1.02ms
     90%     1.33ms
     99%     2.80ms
### File Upload (POST /upload)
  Reqs/sec    107082.08    7417.15  114999.61
  Latency        0.91ms   376.83us     6.66ms
  Latency Distribution
     50%   827.00us
     75%     1.12ms
     90%     1.47ms
     99%     2.35ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    110827.10    9002.91  116730.06
  Latency        0.88ms   338.73us     6.43ms
  Latency Distribution
     50%   797.00us
     75%     1.07ms
     90%     1.39ms
     99%     2.17ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9962.13    1148.71   13959.92
  Latency       10.06ms     2.53ms    22.14ms
  Latency Distribution
     50%     9.86ms
     75%    11.96ms
     90%    13.73ms
     99%    17.60ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    142798.24   10822.93  152393.99
  Latency      682.30us   325.88us     6.64ms
  Latency Distribution
     50%   580.00us
     75%     0.86ms
     90%     1.11ms
     99%     1.92ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    106706.19   22889.01  151162.79
  Latency        1.00ms   353.43us     4.76ms
  Latency Distribution
     50%     0.93ms
     75%     1.20ms
     90%     1.56ms
     99%     2.44ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     89087.04    6727.46   95024.71
  Latency        1.10ms   383.55us     5.39ms
  Latency Distribution
     50%     1.02ms
     75%     1.33ms
     90%     1.70ms
     99%     2.69ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     98471.39    8037.75  104493.79
  Latency        0.99ms   310.32us     4.32ms
  Latency Distribution
     50%     0.93ms
     75%     1.24ms
     90%     1.57ms
     99%     2.34ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    103695.99    9207.58  111725.35
  Latency        0.95ms   326.11us     4.31ms
  Latency Distribution
     50%     0.87ms
     75%     1.17ms
     90%     1.53ms
     99%     2.48ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec     84385.36   18323.44  111351.16
  Latency        1.16ms   557.93us     7.39ms
  Latency Distribution
     50%     0.99ms
     75%     1.38ms
     90%     1.96ms
     99%     3.79ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    177897.69   15327.33  189514.90
  Latency      544.92us   300.47us     5.69ms
  Latency Distribution
     50%   481.00us
     75%   636.00us
     90%     0.87ms
     99%     1.94ms

### Path Parameter - int (/items/12345)
  Reqs/sec    156941.34   14272.58  170951.00
  Latency      618.05us   276.17us     4.97ms
  Latency Distribution
     50%   569.00us
     75%   712.00us
     90%     0.90ms
     99%     2.05ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    153128.44   13955.24  167465.19
  Latency      644.50us   315.02us     5.67ms
  Latency Distribution
     50%   588.00us
     75%   758.00us
     90%     0.98ms
     99%     1.76ms

### Header Parameter (/header)
  Reqs/sec    104893.06    7912.09  110943.74
  Latency        0.94ms   348.71us     5.78ms
  Latency Distribution
     50%     0.86ms
     75%     1.16ms
     90%     1.48ms
     99%     2.42ms

### Cookie Parameter (/cookie)
  Reqs/sec    104280.46    8614.43  112417.50
  Latency        0.94ms   316.57us     5.58ms
  Latency Distribution
     50%     0.87ms
     75%     1.15ms
     90%     1.48ms
     99%     2.25ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     84795.93    5950.36   89882.96
  Latency        1.15ms   406.66us     5.23ms
  Latency Distribution
     50%     1.05ms
     75%     1.41ms
     90%     1.88ms
     99%     2.92ms
