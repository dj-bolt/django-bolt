# Django-Bolt Benchmark
Generated: Mon 02 Mar 2026 10:29:08 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    134103.49   17621.37  152609.92
  Latency      730.22us   458.71us     6.64ms
  Latency Distribution
     50%   625.00us
     75%     0.87ms
     90%     1.24ms
     99%     3.05ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec     98414.55    5937.46  104169.81
  Latency        1.00ms   490.64us     6.00ms
  Latency Distribution
     50%     0.89ms
     75%     1.18ms
     90%     1.61ms
     99%     3.57ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec     96290.67    4884.11  102546.59
  Latency        1.01ms   728.05us     8.04ms
  Latency Distribution
     50%   789.00us
     75%     1.14ms
     90%     1.72ms
     99%     4.67ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec     81948.16   12580.85   90527.74
  Latency        1.22ms   790.68us    10.95ms
  Latency Distribution
     50%     1.05ms
     75%     1.48ms
     90%     2.00ms
     99%     4.56ms
### Cookie Endpoint (/cookie)
  Reqs/sec     82182.14    7029.91   91098.02
  Latency        1.21ms   566.15us     8.13ms
  Latency Distribution
     50%     1.11ms
     75%     1.51ms
     90%     1.97ms
     99%     3.61ms
### Exception Endpoint (/exc)
  Reqs/sec     99642.47   20608.66  119980.96
  Latency        1.01ms   588.62us     7.54ms
  Latency Distribution
     50%   841.00us
     75%     1.21ms
     90%     1.72ms
     99%     3.96ms
### HTML Response (/html)
  Reqs/sec     99397.06   48480.63  132113.59
  Latency      806.05us   452.98us     4.91ms
  Latency Distribution
     50%   686.00us
     75%     0.94ms
     90%     1.30ms
     99%     3.21ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     22786.26    9935.84   34073.41
  Latency        4.43ms     6.70ms    92.65ms
  Latency Distribution
     50%     3.22ms
     75%     4.52ms
     90%     6.67ms
     99%    28.91ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     66604.97    5632.42   77205.81
  Latency        1.48ms   680.89us     7.62ms
  Latency Distribution
     50%     1.27ms
     75%     1.77ms
     90%     2.44ms
     99%     4.64ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     15578.73    1595.43   17670.71
  Latency        6.40ms     2.29ms    18.04ms
  Latency Distribution
     50%     6.05ms
     75%     7.99ms
     90%     9.94ms
     99%    13.36ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14778.75     980.09   16474.29
  Latency        6.72ms     2.43ms    20.29ms
  Latency Distribution
     50%     6.30ms
     75%     8.20ms
     90%    10.46ms
     99%    14.80ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     75228.73    8413.53   85416.43
  Latency        1.29ms   588.29us     8.00ms
  Latency Distribution
     50%     1.16ms
     75%     1.61ms
     90%     2.24ms
     99%     3.92ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    130714.36   10965.70  140029.16
  Latency      746.49us   416.37us     5.51ms
  Latency Distribution
     50%   648.00us
     75%   805.00us
     90%     1.13ms
     99%     3.35ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     88571.62    8299.22   98801.15
  Latency        1.10ms   413.65us     4.85ms
  Latency Distribution
     50%     0.99ms
     75%     1.36ms
     90%     1.76ms
     99%     2.90ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     13773.49    1342.13   15859.99
  Latency        7.24ms     2.30ms    21.83ms
  Latency Distribution
     50%     7.05ms
     75%     8.89ms
     90%    10.65ms
     99%    14.27ms
### Users Full10 (Sync) (/users/sync-full10)
 4690 / 10000 [======================>-------------------------]  46.90% 11696/s
  Reqs/sec     11706.96    1646.30   12978.38
  Latency        8.39ms     3.96ms    34.31ms
  Latency Distribution
     50%     7.50ms
     75%    10.60ms
     90%    14.34ms
     99%    21.80ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     16107.91    1570.75   22615.50
  Latency        6.24ms     2.42ms    20.21ms
  Latency Distribution
     50%     5.93ms
     75%     7.84ms
     90%     9.86ms
     99%    13.65ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12639.43    1297.04   14754.00
  Latency        7.86ms     4.01ms    31.66ms
  Latency Distribution
     50%     6.79ms
     75%    10.55ms
     90%    14.36ms
     99%    20.22ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     90202.88   21622.86  107799.76
  Latency        0.99ms   417.27us     5.47ms
  Latency Distribution
     50%     0.89ms
     75%     1.21ms
     90%     1.61ms
     99%     2.97ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     88001.25    8481.51   99352.53
  Latency        1.10ms   539.61us     7.93ms
  Latency Distribution
     50%     0.97ms
     75%     1.33ms
     90%     1.78ms
     99%     3.38ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     59249.35    3160.87   64235.46
  Latency        1.67ms   651.33us     6.99ms
  Latency Distribution
     50%     1.51ms
     75%     1.99ms
     90%     2.59ms
     99%     4.62ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     88690.01   13534.89  100804.24
  Latency        1.11ms   473.61us     5.50ms
  Latency Distribution
     50%     0.99ms
     75%     1.35ms
     90%     1.83ms
     99%     3.42ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     86585.14    5102.61   93349.22
  Latency        1.13ms   443.62us     5.59ms
  Latency Distribution
     50%     1.02ms
     75%     1.40ms
     90%     1.89ms
     99%     3.10ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     87641.79    9152.93  101464.55
  Latency        1.12ms   431.44us     5.63ms
  Latency Distribution
     50%     1.01ms
     75%     1.39ms
     90%     1.86ms
     99%     2.97ms
### CBV Response Types (/cbv-response)
  Reqs/sec     83836.99    6410.72   92579.94
  Latency        1.15ms   546.28us     6.94ms
  Latency Distribution
     50%     1.01ms
     75%     1.44ms
     90%     1.92ms
     99%     3.64ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16219.07    1317.70   18353.06
  Latency        6.12ms     2.15ms    24.34ms
  Latency Distribution
     50%     5.75ms
     75%     7.35ms
     90%     9.12ms
     99%    13.93ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    113372.19   10434.04  128395.68
  Latency        0.86ms   517.13us     6.69ms
  Latency Distribution
     50%   776.00us
     75%     1.00ms
     90%     1.30ms
     99%     3.38ms
### File Upload (POST /upload)
  Reqs/sec     84588.79   14485.73   95714.28
  Latency        1.16ms   639.19us    10.30ms
  Latency Distribution
     50%     1.02ms
     75%     1.37ms
     90%     1.95ms
     99%     4.52ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec     94426.48    6074.43  102787.85
  Latency        1.03ms   416.37us     6.74ms
  Latency Distribution
     50%     0.95ms
     75%     1.27ms
     90%     1.68ms
     99%     2.88ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9169.88    1077.32   10955.14
  Latency       10.89ms     2.75ms    24.42ms
  Latency Distribution
     50%    10.85ms
     75%    12.69ms
     90%    14.46ms
     99%    19.73ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    120760.39   16606.91  141928.84
  Latency      842.99us   432.11us     5.19ms
  Latency Distribution
     50%   723.00us
     75%     1.01ms
     90%     1.39ms
     99%     3.00ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     92308.46   11677.04  102594.32
  Latency        1.07ms   431.52us     5.00ms
  Latency Distribution
     50%     0.96ms
     75%     1.27ms
     90%     1.65ms
     99%     3.24ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     82999.77    7534.25   89154.55
  Latency        1.18ms   484.92us     6.34ms
  Latency Distribution
     50%     1.06ms
     75%     1.43ms
     90%     1.85ms
     99%     3.65ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     86075.73    7123.31   93307.42
  Latency        1.14ms   444.35us     5.29ms
  Latency Distribution
     50%     1.04ms
     75%     1.42ms
     90%     1.84ms
     99%     3.17ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec     97411.77    8609.92  106678.21
  Latency        1.02ms   415.09us     5.21ms
  Latency Distribution
     50%     0.91ms
     75%     1.23ms
     90%     1.64ms
     99%     2.86ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec     95629.95    7616.98  103103.95
  Latency        1.04ms   345.35us     4.66ms
  Latency Distribution
     50%     0.96ms
     75%     1.26ms
     90%     1.62ms
     99%     2.58ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    139691.35   14751.69  157766.20
  Latency      706.04us   482.14us     6.52ms
  Latency Distribution
     50%   599.00us
     75%   784.00us
     90%     1.17ms
     99%     3.43ms

### Path Parameter - int (/items/12345)
  Reqs/sec    126476.38   13686.14  140399.82
  Latency      772.79us   411.44us     5.70ms
  Latency Distribution
     50%   664.00us
     75%     0.90ms
     90%     1.28ms
     99%     2.72ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    121913.12   20355.44  137062.05
  Latency      788.09us   374.01us     7.54ms
  Latency Distribution
     50%   694.00us
     75%     0.92ms
     90%     1.27ms
     99%     2.77ms

### Header Parameter (/header)
  Reqs/sec     93730.72    9323.11  103160.94
  Latency        1.03ms   456.68us     5.97ms
  Latency Distribution
     50%     0.92ms
     75%     1.24ms
     90%     1.62ms
     99%     3.36ms

### Cookie Parameter (/cookie)
  Reqs/sec     89476.81   11462.04   97516.69
  Latency        1.11ms   509.85us     5.34ms
  Latency Distribution
     50%     0.98ms
     75%     1.33ms
     90%     1.75ms
     99%     3.78ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     80053.20    6531.77   86832.78
  Latency        1.23ms   506.19us     5.93ms
  Latency Distribution
     50%     1.10ms
     75%     1.49ms
     90%     2.01ms
     99%     3.68ms
