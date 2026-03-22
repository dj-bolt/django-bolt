# Django-Bolt Benchmark
Generated: Sun 22 Mar 2026 12:16:56 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    158129.77   17279.60  168333.63
  Latency      622.45us   321.55us     3.90ms
  Latency Distribution
     50%   532.00us
     75%   741.00us
     90%     1.07ms
     99%     2.24ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    113056.30    9835.12  120502.46
  Latency        0.86ms   374.54us     5.76ms
  Latency Distribution
     50%   767.00us
     75%     1.04ms
     90%     1.42ms
     99%     2.56ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    119055.42   13011.29  127769.93
  Latency      828.91us   449.76us     6.72ms
  Latency Distribution
     50%   718.00us
     75%     0.98ms
     90%     1.35ms
     99%     2.96ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec     93308.31   11607.31  104205.25
  Latency        1.02ms   342.94us     4.63ms
  Latency Distribution
     50%     0.95ms
     75%     1.27ms
     90%     1.58ms
     99%     2.50ms
### Cookie Endpoint (/cookie)
  Reqs/sec     95092.16    6171.98  100091.65
  Latency        1.03ms   335.03us     5.43ms
  Latency Distribution
     50%     0.97ms
     75%     1.29ms
     90%     1.61ms
     99%     2.48ms
### Exception Endpoint (/exc)
  Reqs/sec    128305.33   10750.16  136087.62
  Latency      762.16us   341.13us     5.82ms
  Latency Distribution
     50%   703.00us
     75%     0.97ms
     90%     1.25ms
     99%     2.16ms
### HTML Response (/html)
  Reqs/sec    143985.48   14281.02  157485.92
  Latency      677.69us   415.35us     6.91ms
  Latency Distribution
     50%   580.00us
     75%   824.00us
     90%     1.17ms
     99%     2.58ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     30340.38    8644.26   39759.38
  Latency        3.20ms     2.02ms    25.02ms
  Latency Distribution
     50%     2.71ms
     75%     3.69ms
     90%     5.14ms
     99%    13.24ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     72306.44    5215.55   75672.27
  Latency        1.36ms   371.14us     5.99ms
  Latency Distribution
     50%     1.31ms
     75%     1.67ms
     90%     2.05ms
     99%     2.87ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     16964.14    1655.24   18078.70
  Latency        5.86ms     1.44ms    15.97ms
  Latency Distribution
     50%     5.61ms
     75%     6.84ms
     90%     7.92ms
     99%    10.97ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15340.84    1075.23   16824.33
  Latency        6.49ms     1.96ms    23.72ms
  Latency Distribution
     50%     6.23ms
     75%     7.72ms
     90%     9.45ms
     99%    13.20ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     73863.95    5707.98   81607.34
  Latency        1.33ms   530.21us     5.56ms
  Latency Distribution
     50%     1.20ms
     75%     1.64ms
     90%     2.31ms
     99%     3.63ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec     88079.96   23131.68  115900.88
  Latency        1.11ms     0.99ms     9.44ms
  Latency Distribution
     50%   680.00us
     75%     1.26ms
     90%     2.63ms
     99%     5.88ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    127937.68   11403.27  136961.64
  Latency      758.02us   444.27us     9.17ms
  Latency Distribution
     50%   633.00us
     75%     0.87ms
     90%     1.30ms
     99%     3.08ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14800.17    2082.19   17546.51
  Latency        6.75ms     2.27ms    23.68ms
  Latency Distribution
     50%     6.38ms
     75%     8.03ms
     90%     9.92ms
     99%    14.83ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     11185.88    1604.34   14239.52
  Latency        8.81ms     4.49ms    38.62ms
  Latency Distribution
     50%     7.72ms
     75%    11.27ms
     90%    15.53ms
     99%    23.68ms
### Users Mini10 (Async) (/users/mini10)
 3590 / 10000 [======>------------]  35.90% 17907/s
  Reqs/sec     18444.37    1084.43   20003.11
  Latency        5.39ms     1.47ms    13.30ms
  Latency Distribution
     50%     5.21ms
     75%     6.50ms
     90%     7.83ms
     99%    10.10ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12554.83    1771.59   21395.18
  Latency        8.04ms     2.88ms    24.56ms
  Latency Distribution
     50%     7.68ms
     75%    10.07ms
     90%    12.38ms
     99%    16.92ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     97561.90    8227.45  103811.06
  Latency        1.02ms   434.52us     5.52ms
  Latency Distribution
     50%     0.92ms
     75%     1.27ms
     90%     1.64ms
     99%     2.86ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     93309.13   11065.44  102735.06
  Latency        1.01ms   334.18us     4.77ms
  Latency Distribution
     50%     0.93ms
     75%     1.25ms
     90%     1.60ms
     99%     2.44ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     66811.53   17628.55  110276.38
  Latency        1.61ms   664.47us     8.85ms
  Latency Distribution
     50%     1.42ms
     75%     1.89ms
     90%     2.54ms
     99%     4.63ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     77896.52   26095.76   97382.97
  Latency        1.30ms     1.01ms    14.27ms
  Latency Distribution
     50%     1.05ms
     75%     1.53ms
     90%     2.09ms
     99%     4.92ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     91218.29    6535.64   99314.82
  Latency        1.08ms   354.80us     5.92ms
  Latency Distribution
     50%     0.99ms
     75%     1.36ms
     90%     1.75ms
     99%     2.59ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     97274.56    9984.34  111185.15
  Latency        1.03ms   326.81us     5.95ms
  Latency Distribution
     50%     0.96ms
     75%     1.29ms
     90%     1.61ms
     99%     2.40ms
### CBV Response Types (/cbv-response)
  Reqs/sec     98949.07    6428.45  104605.61
  Latency        0.99ms   318.57us     4.08ms
  Latency Distribution
     50%     0.92ms
     75%     1.22ms
     90%     1.56ms
     99%     2.39ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16784.52    1379.20   18209.15
  Latency        5.93ms     1.64ms    19.43ms
  Latency Distribution
     50%     5.77ms
     75%     7.04ms
     90%     8.41ms
     99%    11.28ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    122223.85   10782.09  131908.57
  Latency      803.29us   388.55us     5.84ms
  Latency Distribution
     50%   709.00us
     75%     0.99ms
     90%     1.31ms
     99%     2.31ms
### File Upload (POST /upload)
  Reqs/sec    114037.28    8365.96  120718.73
  Latency        0.86ms   455.10us    10.08ms
  Latency Distribution
     50%   779.00us
     75%     0.97ms
     90%     1.28ms
     99%     2.85ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    104768.63    8390.18  109189.91
  Latency        0.93ms   359.91us     6.51ms
  Latency Distribution
     50%     0.89ms
     75%     1.15ms
     90%     1.40ms
     99%     2.25ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9595.82    1051.34   12604.53
  Latency       10.43ms     3.64ms    26.24ms
  Latency Distribution
     50%    10.15ms
     75%    13.29ms
     90%    15.78ms
     99%    20.32ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    135611.62   16060.92  145447.95
  Latency      725.39us   330.50us     4.94ms
  Latency Distribution
     50%   643.00us
     75%     0.88ms
     90%     1.19ms
     99%     2.41ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     93591.40    8121.99  101178.64
  Latency        1.06ms   355.41us     5.67ms
  Latency Distribution
     50%     0.98ms
     75%     1.30ms
     90%     1.66ms
     99%     2.56ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     85856.58    8929.03   92739.52
  Latency        1.15ms   475.54us     7.14ms
  Latency Distribution
     50%     1.06ms
     75%     1.42ms
     90%     1.81ms
     99%     3.19ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     90583.72    5668.46   96788.85
  Latency        1.07ms   368.77us     4.97ms
  Latency Distribution
     50%     0.98ms
     75%     1.33ms
     90%     1.73ms
     99%     2.73ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec     94476.28    7201.18  102073.74
  Latency        1.01ms   303.56us     5.64ms
  Latency Distribution
     50%     0.94ms
     75%     1.24ms
     90%     1.57ms
     99%     2.23ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec     98377.55    6033.80  103755.15
  Latency        1.00ms   331.00us     4.68ms
  Latency Distribution
     50%     0.91ms
     75%     1.25ms
     90%     1.60ms
     99%     2.46ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    149897.11   25787.33  173319.29
  Latency      602.54us   349.43us     5.38ms
  Latency Distribution
     50%   507.00us
     75%   699.00us
     90%     1.02ms
     99%     2.22ms

### Path Parameter - int (/items/12345)
  Reqs/sec    135000.11   12799.10  147741.28
  Latency      723.56us   405.00us     6.66ms
  Latency Distribution
     50%   641.00us
     75%     0.88ms
     90%     1.24ms
     99%     2.15ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    137351.30   11017.72  147907.13
  Latency      704.78us   352.07us     4.73ms
  Latency Distribution
     50%   629.00us
     75%   847.00us
     90%     1.15ms
     99%     2.63ms

### Header Parameter (/header)
  Reqs/sec     95916.77    9923.16  104004.49
  Latency        1.02ms   379.57us     5.46ms
  Latency Distribution
     50%     0.94ms
     75%     1.25ms
     90%     1.61ms
     99%     2.66ms

### Cookie Parameter (/cookie)
  Reqs/sec     94649.11    6706.55   99428.38
  Latency        1.03ms   372.42us     5.11ms
  Latency Distribution
     50%     0.95ms
     75%     1.32ms
     90%     1.68ms
     99%     2.49ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     76032.54    4368.58   82901.42
  Latency        1.27ms   451.79us     5.39ms
  Latency Distribution
     50%     1.17ms
     75%     1.61ms
     90%     2.09ms
     99%     3.15ms
