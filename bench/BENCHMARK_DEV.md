# Django-Bolt Benchmark
Generated: Mon 02 Mar 2026 11:23:15 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    159808.32   17585.21  171670.34
  Latency      621.20us   296.84us     6.37ms
  Latency Distribution
     50%   566.00us
     75%   753.00us
     90%     0.98ms
     99%     2.13ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    115299.03   12154.91  133349.66
  Latency        0.87ms   335.03us     5.34ms
  Latency Distribution
     50%   833.00us
     75%     1.06ms
     90%     1.34ms
     99%     2.18ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    111183.29   12071.25  120235.56
  Latency        0.88ms   443.70us     5.89ms
  Latency Distribution
     50%   789.00us
     75%     1.06ms
     90%     1.35ms
     99%     3.37ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    102452.36    6635.35  107648.63
  Latency        0.96ms   317.39us     5.42ms
  Latency Distribution
     50%     0.90ms
     75%     1.17ms
     90%     1.46ms
     99%     2.27ms
### Cookie Endpoint (/cookie)
  Reqs/sec    101591.33    8166.20  108404.24
  Latency        0.97ms   274.92us     4.15ms
  Latency Distribution
     50%     0.92ms
     75%     1.23ms
     90%     1.52ms
     99%     2.18ms
### Exception Endpoint (/exc)
  Reqs/sec    123218.06   11977.04  133943.52
  Latency      797.65us   322.23us     5.79ms
  Latency Distribution
     50%   754.00us
     75%     0.94ms
     90%     1.19ms
     99%     2.04ms
### HTML Response (/html)
  Reqs/sec    140142.48   13748.68  155212.78
  Latency      691.60us   371.10us     5.61ms
  Latency Distribution
     50%   661.00us
     75%   840.00us
     90%     1.08ms
     99%     1.80ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     36822.29    8494.29   41314.56
  Latency        2.71ms     1.58ms    18.51ms
  Latency Distribution
     50%     2.47ms
     75%     3.15ms
     90%     3.98ms
     99%     9.40ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     77694.06    4500.94   80562.42
  Latency        1.27ms   390.84us     5.06ms
  Latency Distribution
     50%     1.17ms
     75%     1.53ms
     90%     1.96ms
     99%     3.08ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     17595.66    1577.23   19066.76
  Latency        5.64ms     1.41ms    13.55ms
  Latency Distribution
     50%     5.61ms
     75%     6.73ms
     90%     7.62ms
     99%     9.81ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     16010.56    1130.05   19248.35
  Latency        6.24ms     1.75ms    14.86ms
  Latency Distribution
     50%     6.12ms
     75%     7.62ms
     90%     9.02ms
     99%    11.41ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     86718.11    6621.77   95858.65
  Latency        1.12ms   382.44us     7.59ms
  Latency Distribution
     50%     1.04ms
     75%     1.38ms
     90%     1.77ms
     99%     2.81ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    143725.91    8376.08  152008.76
  Latency      673.20us   290.35us     5.47ms
  Latency Distribution
     50%   623.00us
     75%   787.00us
     90%     0.96ms
     99%     2.11ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     98979.60    9081.84  107107.08
  Latency        0.99ms   360.57us     5.56ms
  Latency Distribution
     50%     0.93ms
     75%     1.22ms
     90%     1.55ms
     99%     2.39ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14650.53    1283.55   18410.60
  Latency        6.84ms     1.91ms    17.31ms
  Latency Distribution
     50%     6.76ms
     75%     8.29ms
     90%     9.57ms
     99%    12.53ms
### Users Full10 (Sync) (/users/sync-full10)
 9099 / 10000 [===========================================>----]  90.99% 11345/s
  Reqs/sec     11484.65    1508.16   14471.97
  Latency        8.70ms     3.25ms    27.14ms
  Latency Distribution
     50%     8.12ms
     75%    10.81ms
     90%    13.64ms
     99%    19.15ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     16368.94    1619.67   17647.03
  Latency        5.97ms     1.97ms    15.14ms
  Latency Distribution
     50%     6.06ms
     75%     7.77ms
     90%     9.02ms
     99%    11.30ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12426.08     941.74   14217.11
  Latency        8.00ms     2.76ms    24.33ms
  Latency Distribution
     50%     7.53ms
     75%     9.64ms
     90%    11.97ms
     99%    17.41ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    108157.27    8137.48  114600.19
  Latency        0.91ms   317.19us     4.21ms
  Latency Distribution
     50%   831.00us
     75%     1.11ms
     90%     1.44ms
     99%     2.28ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    102263.15    8607.15  107895.08
  Latency        0.96ms   368.05us     6.18ms
  Latency Distribution
     50%     0.87ms
     75%     1.23ms
     90%     1.63ms
     99%     2.44ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     68808.40    4923.53   74197.11
  Latency        1.42ms   374.08us     4.53ms
  Latency Distribution
     50%     1.34ms
     75%     1.70ms
     90%     2.07ms
     99%     3.04ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     99132.47    6640.27  106197.76
  Latency        0.99ms   360.73us     5.67ms
  Latency Distribution
     50%     0.90ms
     75%     1.24ms
     90%     1.62ms
     99%     2.55ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     94166.22   11084.00  104504.11
  Latency        1.00ms   331.76us     4.34ms
  Latency Distribution
     50%     0.92ms
     75%     1.26ms
     90%     1.62ms
     99%     2.37ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    118483.62   48675.95  217024.76
  Latency        0.99ms   337.50us     5.21ms
  Latency Distribution
     50%     0.93ms
     75%     1.23ms
     90%     1.57ms
     99%     2.44ms
### CBV Response Types (/cbv-response)
  Reqs/sec    105713.87    7782.32  114161.73
  Latency        0.92ms   270.36us     3.89ms
  Latency Distribution
     50%     0.86ms
     75%     1.13ms
     90%     1.43ms
     99%     2.04ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     17209.52    1238.55   18493.86
  Latency        5.77ms     1.58ms    15.11ms
  Latency Distribution
     50%     5.70ms
     75%     6.97ms
     90%     8.13ms
     99%    10.65ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    115431.77    7039.57  120760.42
  Latency      835.29us   321.55us     6.22ms
  Latency Distribution
     50%   797.00us
     75%     1.03ms
     90%     1.29ms
     99%     1.98ms
### File Upload (POST /upload)
  Reqs/sec    102725.53    7081.90  111584.56
  Latency        0.95ms   306.43us     5.37ms
  Latency Distribution
     50%     0.93ms
     75%     1.13ms
     90%     1.43ms
     99%     2.04ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    100244.04    7247.31  104704.31
  Latency        0.98ms   319.21us     6.42ms
  Latency Distribution
     50%     0.94ms
     75%     1.18ms
     90%     1.45ms
     99%     1.99ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9906.21     942.86   11384.72
  Latency       10.01ms     2.40ms    21.37ms
  Latency Distribution
     50%    10.18ms
     75%    11.86ms
     90%    13.31ms
     99%    17.07ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    134668.16   12736.04  147585.13
  Latency      707.72us   297.75us     5.94ms
  Latency Distribution
     50%   659.00us
     75%     0.87ms
     90%     1.11ms
     99%     1.75ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     99897.84    8967.04  107362.69
  Latency        0.98ms   367.72us     5.14ms
  Latency Distribution
     50%     0.90ms
     75%     1.20ms
     90%     1.51ms
     99%     2.45ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     94389.64    6918.82  100195.30
  Latency        1.05ms   337.94us     4.45ms
  Latency Distribution
     50%     0.97ms
     75%     1.27ms
     90%     1.62ms
     99%     2.46ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    100142.46    6406.95  103727.78
  Latency        0.98ms   296.18us     4.13ms
  Latency Distribution
     50%     0.91ms
     75%     1.20ms
     90%     1.52ms
     99%     2.28ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    106855.18    7165.87  111070.47
  Latency        0.92ms   287.69us     5.01ms
  Latency Distribution
     50%   845.00us
     75%     1.14ms
     90%     1.44ms
     99%     2.23ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    106613.85    7422.14  111156.51
  Latency        0.92ms   300.23us     4.49ms
  Latency Distribution
     50%     0.87ms
     75%     1.11ms
     90%     1.39ms
     99%     2.17ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    161475.90    9543.12  172204.94
  Latency      612.32us   317.70us     5.59ms
  Latency Distribution
     50%   539.00us
     75%   717.00us
     90%     0.96ms
     99%     2.14ms

### Path Parameter - int (/items/12345)
  Reqs/sec    135728.55    4517.47  139662.23
  Latency      716.16us   326.04us     5.73ms
  Latency Distribution
     50%   670.00us
     75%     0.90ms
     90%     1.21ms
     99%     2.16ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    136211.85    6568.79  145470.13
  Latency      719.96us   382.27us     5.11ms
  Latency Distribution
     50%   657.00us
     75%   839.00us
     90%     1.11ms
     99%     3.32ms

### Header Parameter (/header)
  Reqs/sec    104192.51    7841.17  112064.05
  Latency        0.94ms   277.74us     3.32ms
  Latency Distribution
     50%     0.88ms
     75%     1.16ms
     90%     1.49ms
     99%     2.24ms

### Cookie Parameter (/cookie)
  Reqs/sec    104803.28    9139.94  112108.59
  Latency        0.94ms   343.09us     7.10ms
  Latency Distribution
     50%     0.86ms
     75%     1.14ms
     90%     1.43ms
     99%     2.44ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     89661.80    6072.39   93220.94
  Latency        1.10ms   318.22us     5.28ms
  Latency Distribution
     50%     1.04ms
     75%     1.34ms
     90%     1.64ms
     99%     2.40ms
