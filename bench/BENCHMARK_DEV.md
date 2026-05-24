# Django-Bolt Benchmark
Generated: Mon 25 May 2026 12:16:02 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    165692.56   17512.49  180970.98
  Latency      585.41us   294.70us     4.32ms
  Latency Distribution
     50%   532.00us
     75%   671.00us
     90%     0.91ms
     99%     2.06ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    108255.92    6111.60  114139.55
  Latency        0.89ms   401.55us     6.74ms
  Latency Distribution
     50%   791.00us
     75%     1.10ms
     90%     1.50ms
     99%     2.61ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    107979.31    8919.21  118582.81
  Latency        0.89ms   372.98us     6.21ms
  Latency Distribution
     50%   819.00us
     75%     1.12ms
     90%     1.54ms
     99%     2.48ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec     97961.88    9951.39  112863.28
  Latency        1.03ms   380.88us     5.14ms
  Latency Distribution
     50%     0.95ms
     75%     1.27ms
     90%     1.64ms
     99%     2.63ms
### Cookie Endpoint (/cookie)
  Reqs/sec     95212.73    6200.34  102497.10
  Latency        1.05ms   371.70us     5.93ms
  Latency Distribution
     50%     0.96ms
     75%     1.30ms
     90%     1.65ms
     99%     2.71ms
### Exception Endpoint (/exc)
  Reqs/sec    127491.61   16755.43  136775.35
  Latency      762.21us   389.56us     6.64ms
  Latency Distribution
     50%   679.00us
     75%     0.94ms
     90%     1.25ms
     99%     2.32ms
### HTML Response (/html)
  Reqs/sec    156960.71   16978.50  166999.54
  Latency      624.40us   338.38us     5.19ms
  Latency Distribution
     50%   536.00us
     75%   741.00us
     90%     0.99ms
     99%     2.22ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     35904.93    6169.02   41524.07
  Latency        2.80ms     1.35ms    20.68ms
  Latency Distribution
     50%     2.57ms
     75%     3.41ms
     90%     4.45ms
     99%     7.75ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    144356.55   30432.00  173671.08
  Latency      611.33us   349.07us     6.49ms
  Latency Distribution
     50%   549.00us
     75%   678.00us
     90%     0.88ms
     99%     2.52ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    165125.28   21660.37  183224.91
  Latency      587.07us   451.54us    10.18ms
  Latency Distribution
     50%   523.00us
     75%   629.00us
     90%   771.00us
     99%     2.62ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     71054.10    5340.10   74664.50
  Latency        1.39ms   421.47us     6.14ms
  Latency Distribution
     50%     1.36ms
     75%     1.72ms
     90%     2.00ms
     99%     3.11ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     69024.89    4222.86   72604.79
  Latency        1.42ms   343.88us     5.75ms
  Latency Distribution
     50%     1.40ms
     75%     1.60ms
     90%     1.89ms
     99%     2.76ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     77142.85    4463.45   82295.66
  Latency        1.28ms   379.13us     5.84ms
  Latency Distribution
     50%     1.19ms
     75%     1.58ms
     90%     1.98ms
     99%     2.92ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     16470.17    1253.08   17436.23
  Latency        6.01ms     1.74ms    17.30ms
  Latency Distribution
     50%     5.78ms
     75%     7.46ms
     90%     8.81ms
     99%    11.30ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14827.16     852.62   16394.42
  Latency        6.71ms     1.62ms    15.18ms
  Latency Distribution
     50%     6.56ms
     75%     7.89ms
     90%     9.17ms
     99%    11.81ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     83290.54    5327.17   86124.01
  Latency        1.18ms   386.92us     5.49ms
  Latency Distribution
     50%     1.11ms
     75%     1.48ms
     90%     1.86ms
     99%     2.81ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    130371.52   34896.99  182278.53
  Latency      825.82us   562.25us     8.32ms
  Latency Distribution
     50%   647.00us
     75%     0.99ms
     90%     1.52ms
     99%     3.71ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    135395.82   18787.53  146648.73
  Latency      726.23us   386.69us     6.53ms
  Latency Distribution
     50%   691.00us
     75%   838.00us
     90%     1.07ms
     99%     2.48ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14235.19    1499.21   19292.11
  Latency        7.04ms     1.60ms    21.93ms
  Latency Distribution
     50%     7.14ms
     75%     8.25ms
     90%     9.32ms
     99%    11.62ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10038.93     991.87   12028.37
  Latency        9.89ms     4.71ms    33.10ms
  Latency Distribution
     50%     9.25ms
     75%    13.07ms
     90%    17.03ms
     99%    23.92ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     16859.35    1184.73   18834.75
  Latency        5.90ms     2.03ms    17.51ms
  Latency Distribution
     50%     5.45ms
     75%     8.02ms
     90%     9.39ms
     99%    11.60ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     11704.05     787.19   13749.68
  Latency        8.51ms     4.18ms    33.03ms
  Latency Distribution
     50%     7.58ms
     75%    10.59ms
     90%    14.43ms
     99%    22.92ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     93441.09    7627.12  101041.24
  Latency        1.03ms   330.15us     5.47ms
  Latency Distribution
     50%     0.96ms
     75%     1.28ms
     90%     1.60ms
     99%     2.40ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     98100.46    8061.06  104723.68
  Latency        1.00ms   315.77us     4.87ms
  Latency Distribution
     50%     0.93ms
     75%     1.22ms
     90%     1.55ms
     99%     2.39ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     64418.76    4612.80   69151.25
  Latency        1.54ms   533.78us     6.37ms
  Latency Distribution
     50%     1.39ms
     75%     1.84ms
     90%     2.43ms
     99%     3.65ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     99570.98    8395.90  105969.87
  Latency        0.98ms   332.67us     5.17ms
  Latency Distribution
     50%     0.91ms
     75%     1.21ms
     90%     1.54ms
     99%     2.27ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     84067.04   10426.54   91660.96
  Latency        1.17ms   451.09us     5.67ms
  Latency Distribution
     50%     1.07ms
     75%     1.49ms
     90%     1.96ms
     99%     3.25ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     89861.94   18553.88  103911.40
  Latency        1.00ms   312.02us     5.63ms
  Latency Distribution
     50%     0.95ms
     75%     1.24ms
     90%     1.56ms
     99%     2.22ms
### CBV Response Types (/cbv-response)
  Reqs/sec    102278.78    5408.84  108793.80
  Latency        0.96ms   305.34us     4.82ms
  Latency Distribution
     50%     0.89ms
     75%     1.18ms
     90%     1.47ms
     99%     2.25ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     15713.02    1422.41   17467.66
  Latency        6.34ms     1.53ms    17.31ms
  Latency Distribution
     50%     6.32ms
     75%     7.53ms
     90%     8.52ms
     99%    10.77ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    135672.11   11436.00  144229.50
  Latency      723.11us   327.63us     5.68ms
  Latency Distribution
     50%   637.00us
     75%   824.00us
     90%     1.09ms
     99%     2.42ms
### File Upload (POST /upload)
  Reqs/sec    117508.48    7559.08  122693.53
  Latency      821.18us   293.31us     7.11ms
  Latency Distribution
     50%   721.00us
     75%     1.05ms
     90%     1.38ms
     99%     2.00ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    111113.38   10065.39  117576.76
  Latency        0.88ms   303.71us     5.92ms
  Latency Distribution
     50%   843.00us
     75%     1.09ms
     90%     1.32ms
     99%     2.15ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec     65774.31    4405.95   71200.80
  Latency        1.50ms   520.80us     5.55ms
  Latency Distribution
     50%     1.43ms
     75%     1.86ms
     90%     2.26ms
     99%     3.94ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    101848.28    7448.04  106909.07
  Latency        0.96ms   314.72us     6.22ms
  Latency Distribution
     50%     0.89ms
     75%     1.18ms
     90%     1.46ms
     99%     2.27ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9456.26     884.70   11702.16
  Latency       10.55ms     3.01ms    25.91ms
  Latency Distribution
     50%     9.75ms
     75%    11.81ms
     90%    16.04ms
     99%    20.60ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    147359.87   14677.73  159900.32
  Latency      659.24us   425.83us     6.21ms
  Latency Distribution
     50%   563.00us
     75%   700.00us
     90%     0.99ms
     99%     2.93ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    151116.51   18604.26  160669.02
  Latency      648.65us   319.91us     5.30ms
  Latency Distribution
     50%   592.00us
     75%   705.00us
     90%     0.92ms
     99%     2.07ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     88684.00    7925.55   92592.88
  Latency        1.11ms   433.60us     6.11ms
  Latency Distribution
     50%     1.03ms
     75%     1.36ms
     90%     1.69ms
     99%     2.82ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    140552.22   13767.05  153524.38
  Latency      692.36us   305.99us     7.53ms
  Latency Distribution
     50%   612.00us
     75%   839.00us
     90%     1.10ms
     99%     2.12ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    107605.33    9147.26  113296.13
  Latency        0.92ms   291.31us     4.75ms
  Latency Distribution
     50%     0.86ms
     75%     1.11ms
     90%     1.40ms
     99%     2.06ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    103853.36    8346.61  112822.32
  Latency        0.95ms   331.98us     5.59ms
  Latency Distribution
     50%     0.88ms
     75%     1.18ms
     90%     1.48ms
     99%     2.29ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec     99855.99    7284.54  109749.33
  Latency        1.00ms   327.97us     5.14ms
  Latency Distribution
     50%     0.93ms
     75%     1.23ms
     90%     1.56ms
     99%     2.37ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec     96280.17    7537.87  103417.03
  Latency        1.00ms   364.09us     4.47ms
  Latency Distribution
     50%     0.90ms
     75%     1.22ms
     90%     1.67ms
     99%     2.61ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    100449.36    7658.12  106797.63
  Latency        0.98ms   274.49us     4.78ms
  Latency Distribution
     50%     0.93ms
     75%     1.20ms
     90%     1.48ms
     99%     2.15ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     69913.66    5047.74   73789.43
  Latency        1.41ms   544.06us     8.45ms
  Latency Distribution
     50%     1.34ms
     75%     1.73ms
     90%     2.05ms
     99%     3.05ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    177403.82   20742.49  193421.24
  Latency      544.00us   359.66us     9.37ms
  Latency Distribution
     50%   459.00us
     75%   627.00us
     90%   840.00us
     99%     2.29ms

### Path Parameter - int (/items/12345)
  Reqs/sec    147437.78   14010.49  161753.15
  Latency      662.10us   341.62us     5.89ms
  Latency Distribution
     50%   570.00us
     75%   758.00us
     90%     1.07ms
     99%     2.18ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    148740.81    9164.14  157227.86
  Latency      645.19us   332.92us     4.97ms
  Latency Distribution
     50%   557.00us
     75%   742.00us
     90%     1.00ms
     99%     2.31ms

### Header Parameter (/header)
  Reqs/sec    103956.27   10812.84  114436.37
  Latency        0.95ms   313.68us     5.15ms
  Latency Distribution
     50%     0.89ms
     75%     1.17ms
     90%     1.46ms
     99%     2.23ms

### Cookie Parameter (/cookie)
  Reqs/sec     98792.83    6151.97  105197.33
  Latency        0.99ms   310.46us     4.67ms
  Latency Distribution
     50%     0.93ms
     75%     1.23ms
     90%     1.54ms
     99%     2.41ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     82701.72    6019.19   86147.83
  Latency        1.19ms   366.44us     5.48ms
  Latency Distribution
     50%     1.11ms
     75%     1.46ms
     90%     1.83ms
     99%     2.69ms
