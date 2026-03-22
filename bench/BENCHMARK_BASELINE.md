# Django-Bolt Benchmark
Generated: Sun 22 Mar 2026 01:19:59 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    147118.32   19568.20  166937.10
  Latency      617.64us   474.10us     5.78ms
  Latency Distribution
     50%   498.00us
     75%   684.00us
     90%     0.98ms
     99%     3.00ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    102291.94    8196.77  108606.54
  Latency        0.95ms   576.77us     9.63ms
  Latency Distribution
     50%   822.00us
     75%     1.16ms
     90%     1.57ms
     99%     3.72ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    110718.14   18763.49  129518.04
  Latency        0.91ms   713.68us    11.59ms
  Latency Distribution
     50%   735.00us
     75%     1.12ms
     90%     1.57ms
     99%     4.75ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec     84163.89   23867.91  101054.43
  Latency        1.08ms   532.52us     7.64ms
  Latency Distribution
     50%     0.98ms
     75%     1.30ms
     90%     1.68ms
     99%     3.75ms
### Cookie Endpoint (/cookie)
  Reqs/sec     91622.95    8742.39  100786.06
  Latency        1.08ms   326.46us     4.69ms
  Latency Distribution
     50%     1.01ms
     75%     1.38ms
     90%     1.74ms
     99%     2.45ms
### Exception Endpoint (/exc)
  Reqs/sec    113706.24   13319.05  126423.13
  Latency      827.48us   476.01us     6.64ms
  Latency Distribution
     50%   729.00us
     75%     0.96ms
     90%     1.31ms
     99%     3.31ms
### HTML Response (/html)
  Reqs/sec    140353.56    7784.15  145876.11
  Latency      698.10us   424.55us     5.66ms
  Latency Distribution
     50%   605.00us
     75%   823.00us
     90%     1.17ms
     99%     2.68ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
 6899 / 10000 [===================================>---------------]  68.99% 34330/s
  Reqs/sec     34941.14    7044.86   38174.66
  Latency        2.80ms     1.32ms    19.83ms
  Latency Distribution
     50%     2.56ms
     75%     3.28ms
     90%     4.29ms
     99%     8.82ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     72066.86    4145.68   75716.10
  Latency        1.37ms   372.63us     4.75ms
  Latency Distribution
     50%     1.29ms
     75%     1.66ms
     90%     2.06ms
     99%     2.93ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     16103.80    2312.19   19131.58
  Latency        6.19ms     2.04ms    24.27ms
  Latency Distribution
     50%     5.59ms
     75%     7.36ms
     90%     8.61ms
     99%    14.37ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15370.33    1203.44   16602.38
  Latency        6.41ms     2.12ms    19.67ms
  Latency Distribution
     50%     6.41ms
     75%     8.03ms
     90%     9.54ms
     99%    12.45ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     70103.32    5127.34   75817.47
  Latency        1.40ms   653.34us     7.34ms
  Latency Distribution
     50%     1.26ms
     75%     1.73ms
     90%     2.24ms
     99%     4.57ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    124085.47   14402.10  141585.93
  Latency      781.88us   497.25us     6.12ms
  Latency Distribution
     50%   648.00us
     75%     0.94ms
     90%     1.33ms
     99%     3.69ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    123485.33   16314.33  135445.36
  Latency      780.55us   532.46us     6.97ms
  Latency Distribution
     50%   641.00us
     75%     0.89ms
     90%     1.21ms
     99%     3.55ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     15517.20    1760.89   19250.13
  Latency        6.45ms     1.95ms    17.08ms
  Latency Distribution
     50%     6.25ms
     75%     7.91ms
     90%     9.44ms
     99%    12.66ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     12852.80    1769.17   20356.01
  Latency        7.85ms     2.77ms    27.50ms
  Latency Distribution
     50%     7.38ms
     75%     9.69ms
     90%    11.96ms
     99%    16.88ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     19106.60    1601.94   23631.07
  Latency        5.24ms     1.67ms    16.48ms
  Latency Distribution
     50%     4.83ms
     75%     6.23ms
     90%     7.91ms
     99%    11.06ms
### Users Mini10 (Sync) (/users/sync-mini10)
 8275 / 10000 [==========================================>--------]  82.75% 13735/s
  Reqs/sec     13870.80    1368.76   16847.69
  Latency        7.17ms     2.63ms    24.57ms
  Latency Distribution
     50%     6.80ms
     75%     8.85ms
     90%    11.06ms
     99%    15.83ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     92310.69   14277.32  104345.71
  Latency        1.00ms   340.41us     4.14ms
  Latency Distribution
     50%     0.93ms
     75%     1.24ms
     90%     1.60ms
     99%     2.58ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     72131.48   19169.63  101371.28
  Latency        1.35ms     0.91ms     8.31ms
  Latency Distribution
     50%     1.10ms
     75%     1.56ms
     90%     2.31ms
     99%     6.57ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     66668.23    6110.13   71695.56
  Latency        1.49ms   451.01us     6.32ms
  Latency Distribution
     50%     1.41ms
     75%     1.79ms
     90%     2.23ms
     99%     3.37ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     92650.62    6002.47  101878.10
  Latency        1.08ms   383.16us     5.10ms
  Latency Distribution
     50%     0.98ms
     75%     1.38ms
     90%     1.76ms
     99%     2.72ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     90013.67    6479.83   94518.29
  Latency        1.09ms   395.45us     5.14ms
  Latency Distribution
     50%     0.97ms
     75%     1.36ms
     90%     1.85ms
     99%     2.79ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     95438.00    7207.17  102394.79
  Latency        1.02ms   354.85us     4.94ms
  Latency Distribution
     50%     0.95ms
     75%     1.27ms
     90%     1.57ms
     99%     2.36ms
### CBV Response Types (/cbv-response)
  Reqs/sec     93704.15    9228.75  103023.47
  Latency        1.02ms   367.22us     5.30ms
  Latency Distribution
     50%     0.95ms
     75%     1.28ms
     90%     1.68ms
     99%     2.58ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16905.98    1636.92   18848.39
  Latency        5.89ms     1.64ms    17.38ms
  Latency Distribution
     50%     5.90ms
     75%     7.15ms
     90%     8.33ms
     99%    10.72ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    114680.38   15065.34  128678.15
  Latency      804.93us   462.95us     7.71ms
  Latency Distribution
     50%   703.00us
     75%     0.99ms
     90%     1.31ms
     99%     3.51ms
### File Upload (POST /upload)
  Reqs/sec    104696.39    5778.66  109133.56
  Latency        0.93ms   434.20us     6.29ms
  Latency Distribution
     50%     0.85ms
     75%     1.12ms
     90%     1.46ms
     99%     2.89ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    104128.29   10154.49  114326.25
  Latency        0.95ms   408.78us     6.53ms
  Latency Distribution
     50%     0.86ms
     75%     1.15ms
     90%     1.50ms
     99%     2.63ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9553.18    1584.58   16464.62
  Latency       10.58ms     2.99ms    26.70ms
  Latency Distribution
     50%    10.00ms
     75%    12.24ms
     90%    14.84ms
     99%    20.92ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    129808.63   28328.38  165072.61
  Latency      806.02us   708.12us     9.15ms
  Latency Distribution
     50%   671.00us
     75%     0.91ms
     90%     1.21ms
     99%     4.42ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     69247.28   16472.63   98466.39
  Latency        1.39ms   847.99us     7.78ms
  Latency Distribution
     50%     1.13ms
     75%     1.64ms
     90%     2.54ms
     99%     5.29ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     86864.16    8338.80   94090.35
  Latency        1.13ms   385.53us     5.97ms
  Latency Distribution
     50%     1.06ms
     75%     1.36ms
     90%     1.71ms
     99%     2.59ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     87989.65    7878.50   92939.05
  Latency        1.12ms   325.79us     4.65ms
  Latency Distribution
     50%     1.08ms
     75%     1.42ms
     90%     1.74ms
     99%     2.50ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec     91778.39    7706.94   98140.12
  Latency        1.04ms   334.29us     4.31ms
  Latency Distribution
     50%     0.96ms
     75%     1.34ms
     90%     1.71ms
     99%     2.48ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec     91968.39    9583.76  101127.68
  Latency        1.04ms   362.19us     4.87ms
  Latency Distribution
     50%     0.98ms
     75%     1.30ms
     90%     1.65ms
     99%     2.60ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    152636.33   13152.47  164830.62
  Latency      613.60us   390.21us     5.26ms
  Latency Distribution
     50%   495.00us
     75%   718.00us
     90%     1.10ms
     99%     2.52ms

### Path Parameter - int (/items/12345)
  Reqs/sec    128905.59   10815.82  137422.63
  Latency      745.16us   483.66us     6.09ms
  Latency Distribution
     50%   580.00us
     75%     0.91ms
     90%     1.25ms
     99%     3.47ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    130578.69   13551.02  145391.71
  Latency      743.45us   451.40us     5.56ms
  Latency Distribution
     50%   618.00us
     75%     0.89ms
     90%     1.22ms
     99%     3.07ms

### Header Parameter (/header)
  Reqs/sec     90830.66    6649.49   96468.18
  Latency        1.08ms   427.79us     6.03ms
  Latency Distribution
     50%     1.01ms
     75%     1.36ms
     90%     1.73ms
     99%     2.81ms

### Cookie Parameter (/cookie)
  Reqs/sec     94721.33    5551.56  101864.00
  Latency        1.05ms   362.28us     4.65ms
  Latency Distribution
     50%     0.97ms
     75%     1.30ms
     90%     1.69ms
     99%     2.65ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     71843.61    7269.09   81224.98
  Latency        1.36ms   659.39us     9.90ms
  Latency Distribution
     50%     1.19ms
     75%     1.69ms
     90%     2.30ms
     99%     4.21ms
