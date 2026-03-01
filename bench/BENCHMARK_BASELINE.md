# Django-Bolt Benchmark
Generated: Sun 01 Mar 2026 05:43:46 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec     73192.07   15167.49   81480.92
  Latency        1.36ms     0.97ms    20.45ms
  Latency Distribution
     50%     1.18ms
     75%     1.54ms
     90%     1.99ms
     99%     5.16ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec     65069.88    6265.15   76762.75
  Latency        1.51ms   471.12us     6.12ms
  Latency Distribution
     50%     1.38ms
     75%     1.77ms
     90%     2.28ms
     99%     3.59ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec     62155.69    6581.82   68585.60
  Latency        1.59ms   540.92us     7.13ms
  Latency Distribution
     50%     1.48ms
     75%     1.88ms
     90%     2.41ms
     99%     4.06ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec     71654.38    5140.45   76304.09
  Latency        1.38ms   475.41us     5.17ms
  Latency Distribution
     50%     1.24ms
     75%     1.63ms
     90%     2.08ms
     99%     3.71ms
### Cookie Endpoint (/cookie)
  Reqs/sec     71877.57    7695.97   78869.26
  Latency        1.38ms   487.40us     6.01ms
  Latency Distribution
     50%     1.24ms
     75%     1.69ms
     90%     2.13ms
     99%     3.44ms
### Exception Endpoint (/exc)
  Reqs/sec     70316.07    5127.36   74961.14
  Latency        1.40ms   506.89us     5.79ms
  Latency Distribution
     50%     1.26ms
     75%     1.68ms
     90%     2.25ms
     99%     3.71ms
### HTML Response (/html)
  Reqs/sec     73951.03    9105.67   84658.50
  Latency        1.34ms   549.16us     6.39ms
  Latency Distribution
     50%     1.20ms
     75%     1.67ms
     90%     2.17ms
     99%     3.75ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     18730.31   10145.28   32869.12
  Latency        5.45ms     7.83ms   106.25ms
  Latency Distribution
     50%     3.84ms
     75%     5.31ms
     90%     7.82ms
     99%    45.22ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     55890.73    5135.23   60430.93
  Latency        1.76ms   521.97us     6.11ms
  Latency Distribution
     50%     1.64ms
     75%     2.13ms
     90%     2.64ms
     99%     3.92ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     14569.90    6971.67   54454.37
  Latency        7.37ms     2.28ms    20.04ms
  Latency Distribution
     50%     7.14ms
     75%     8.69ms
     90%    10.37ms
     99%    16.45ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     13815.17    1006.75   15658.07
  Latency        7.20ms     2.00ms    23.76ms
  Latency Distribution
     50%     7.03ms
     75%     8.64ms
     90%    10.15ms
     99%    13.49ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     62342.13    5257.16   70088.77
  Latency        1.57ms   533.92us     6.05ms
  Latency Distribution
     50%     1.45ms
     75%     1.85ms
     90%     2.34ms
     99%     4.18ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec     68016.26    6787.11   78693.95
  Latency        1.47ms   560.82us     5.63ms
  Latency Distribution
     50%     1.40ms
     75%     1.82ms
     90%     2.39ms
     99%     3.87ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     76020.31   19627.00  123527.67
  Latency        1.41ms   500.30us     5.59ms
  Latency Distribution
     50%     1.28ms
     75%     1.72ms
     90%     2.22ms
     99%     3.66ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     12584.63    1283.97   14205.47
  Latency        7.90ms     3.02ms    24.46ms
  Latency Distribution
     50%     7.72ms
     75%    10.17ms
     90%    12.27ms
     99%    16.75ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10373.63    1343.05   12995.44
  Latency        9.65ms     3.60ms    31.96ms
  Latency Distribution
     50%     8.90ms
     75%    11.75ms
     90%    14.89ms
     99%    21.63ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     14668.12    1341.43   17405.59
  Latency        6.78ms     2.10ms    16.88ms
  Latency Distribution
     50%     6.55ms
     75%     8.22ms
     90%    10.09ms
     99%    13.24ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     11568.51    1390.84   14868.65
  Latency        8.58ms     3.38ms    37.90ms
  Latency Distribution
     50%     7.89ms
     75%    10.59ms
     90%    13.62ms
     99%    19.78ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     75980.16    5244.83   84656.43
  Latency        1.31ms   459.90us     5.91ms
  Latency Distribution
     50%     1.19ms
     75%     1.55ms
     90%     2.04ms
     99%     3.54ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     70714.99    6502.37   77468.16
  Latency        1.39ms   427.87us     4.91ms
  Latency Distribution
     50%     1.27ms
     75%     1.62ms
     90%     2.11ms
     99%     3.37ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     51120.92    3307.82   56759.38
  Latency        1.93ms   562.26us     5.69ms
  Latency Distribution
     50%     1.85ms
     75%     2.30ms
     90%     2.86ms
     99%     4.18ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     72609.47    6081.61   78466.29
  Latency        1.36ms   550.79us     7.59ms
  Latency Distribution
     50%     1.22ms
     75%     1.66ms
     90%     2.17ms
     99%     4.11ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     66109.93    6291.23   71591.97
  Latency        1.49ms   568.44us     7.98ms
  Latency Distribution
     50%     1.35ms
     75%     1.83ms
     90%     2.40ms
     99%     3.88ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     70302.58    7433.33   80811.74
  Latency        1.41ms   461.41us     5.96ms
  Latency Distribution
     50%     1.29ms
     75%     1.74ms
     90%     2.21ms
     99%     3.43ms
### CBV Response Types (/cbv-response)
  Reqs/sec     75797.79    7816.59   85740.97
  Latency        1.30ms   466.55us     5.72ms
  Latency Distribution
     50%     1.17ms
     75%     1.51ms
     90%     1.94ms
     99%     3.33ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     14839.75    1464.59   16671.00
  Latency        6.70ms     2.23ms    21.19ms
  Latency Distribution
     50%     6.29ms
     75%     7.99ms
     90%     9.92ms
     99%    14.35ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec     67055.75    8987.05   82216.95
  Latency        1.50ms   558.79us     6.34ms
  Latency Distribution
     50%     1.37ms
     75%     1.81ms
     90%     2.37ms
     99%     4.11ms
### File Upload (POST /upload)
  Reqs/sec     58216.78    5404.89   65704.55
  Latency        1.70ms   627.65us     7.34ms
  Latency Distribution
     50%     1.59ms
     75%     2.10ms
     90%     2.72ms
     99%     4.15ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec     55614.01    3479.17   59276.42
  Latency        1.77ms   693.22us     7.34ms
  Latency Distribution
     50%     1.62ms
     75%     2.21ms
     90%     2.88ms
     99%     4.73ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      8729.75    1217.03   10821.41
  Latency       11.42ms     3.64ms    32.93ms
  Latency Distribution
     50%    11.14ms
     75%    13.72ms
     90%    16.72ms
     99%    21.92ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec     71381.81    6267.20   78303.03
  Latency        1.38ms   473.13us     6.08ms
  Latency Distribution
     50%     1.28ms
     75%     1.63ms
     90%     2.15ms
     99%     3.57ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     69263.35    6240.91   80117.83
  Latency        1.45ms   490.97us     5.14ms
  Latency Distribution
     50%     1.34ms
     75%     1.72ms
     90%     2.23ms
     99%     3.71ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     60272.96    8668.94   75291.47
  Latency        1.67ms   632.93us     8.07ms
  Latency Distribution
     50%     1.51ms
     75%     1.99ms
     90%     2.62ms
     99%     4.53ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     67112.31    5863.87   73843.91
  Latency        1.49ms   568.78us     6.56ms
  Latency Distribution
     50%     1.33ms
     75%     1.81ms
     90%     2.37ms
     99%     3.93ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec     76438.53    7632.75   88507.08
  Latency        1.31ms   534.70us     6.06ms
  Latency Distribution
     50%     1.15ms
     75%     1.51ms
     90%     2.02ms
     99%     4.04ms

### Path Parameter - int (/items/12345)
  Reqs/sec     72703.99    5196.84   78750.30
  Latency        1.36ms   437.82us     4.55ms
  Latency Distribution
     50%     1.24ms
     75%     1.60ms
     90%     2.12ms
     99%     3.35ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec     74318.14    6128.82   81402.46
  Latency        1.33ms   425.49us     5.32ms
  Latency Distribution
     50%     1.20ms
     75%     1.53ms
     90%     2.03ms
     99%     3.26ms

### Header Parameter (/header)
  Reqs/sec     72999.73    6867.87   82658.13
  Latency        1.35ms   524.47us     9.12ms
  Latency Distribution
     50%     1.22ms
     75%     1.60ms
     90%     2.13ms
     99%     3.30ms

### Cookie Parameter (/cookie)
  Reqs/sec     72689.45   10877.83   84955.87
  Latency        1.36ms   508.21us     5.79ms
  Latency Distribution
     50%     1.21ms
     75%     1.58ms
     90%     2.15ms
     99%     3.90ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     61963.44    6524.24   69021.54
  Latency        1.58ms   508.87us     6.13ms
  Latency Distribution
     50%     1.46ms
     75%     1.94ms
     90%     2.41ms
     99%     3.73ms
