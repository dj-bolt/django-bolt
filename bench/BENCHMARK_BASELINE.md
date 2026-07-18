# Django-Bolt Benchmark
Generated: Sun Jul 12 06:39:26 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    193938.49    9567.84  203890.69
  Latency      496.80us   274.15us     4.86ms
  Latency Distribution
     50%   481.00us
     75%   570.00us
     90%   674.00us
     99%     1.54ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    126558.13   11902.99  134453.92
  Latency      774.13us   282.54us     5.62ms
  Latency Distribution
     50%   694.00us
     75%     0.93ms
     90%     1.19ms
     99%     2.13ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    123625.68   20875.40  137276.12
  Latency      795.48us   423.34us     7.60ms
  Latency Distribution
     50%   737.00us
     75%     0.96ms
     90%     1.17ms
     99%     3.21ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    116530.46    8970.96  123930.11
  Latency      847.73us   273.66us     3.56ms
  Latency Distribution
     50%   783.00us
     75%     1.03ms
     90%     1.32ms
     99%     2.10ms
### Cookie Endpoint (/cookie)
  Reqs/sec    115526.78   10462.11  124896.27
  Latency      836.99us   224.12us     3.05ms
  Latency Distribution
     50%   776.00us
     75%     1.03ms
     90%     1.29ms
     99%     1.88ms
### Exception Endpoint (/exc)
  Reqs/sec    155112.28   11985.91  165030.79
  Latency      639.11us   189.01us     4.18ms
  Latency Distribution
     50%   609.00us
     75%   760.00us
     90%     0.95ms
     99%     1.52ms
### HTML Response (/html)
  Reqs/sec    185345.89   15243.21  195557.65
  Latency      526.35us   228.96us     4.34ms
  Latency Distribution
     50%   497.00us
     75%   602.00us
     90%   712.00us
     99%     1.61ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     41151.34    8075.01   46611.50
  Latency        2.42ms     1.35ms    15.28ms
  Latency Distribution
     50%     2.08ms
     75%     2.78ms
     90%     3.78ms
     99%     7.92ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    161011.41   18925.48  175054.97
  Latency      604.77us   285.76us     4.54ms
  Latency Distribution
     50%   539.00us
     75%   708.00us
     90%   817.00us
     99%     2.27ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    167791.11   14335.80  176926.42
  Latency      578.12us   195.31us     5.03ms
  Latency Distribution
     50%   528.00us
     75%   729.00us
     90%   803.00us
     99%     1.13ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     70985.43   38158.89   99301.74
  Latency        1.18ms     3.22ms    42.66ms
  Latency Distribution
     50%   767.00us
     75%     0.95ms
     90%     1.17ms
     99%    12.35ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    195640.08   19960.63  209521.80
  Latency      499.41us   239.53us     4.20ms
  Latency Distribution
     50%   472.00us
     75%   540.00us
     90%   702.00us
     99%     1.39ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    154647.66   17699.91  173785.18
  Latency      604.82us   259.61us     4.44ms
  Latency Distribution
     50%   558.00us
     75%   703.00us
     90%     0.99ms
     99%     2.23ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     61430.43   43259.35   98690.97
  Latency        1.18ms     3.38ms    43.24ms
  Latency Distribution
     50%   728.00us
     75%     0.91ms
     90%     1.16ms
     99%    12.55ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    186204.98   17735.14  197804.69
  Latency      514.27us   181.32us     4.68ms
  Latency Distribution
     50%   451.00us
     75%   643.00us
     90%   743.00us
     99%     1.24ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    184075.61   20318.18  197735.40
  Latency      532.22us   290.73us     5.02ms
  Latency Distribution
     50%   528.00us
     75%   620.00us
     90%   690.00us
     99%     1.65ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     74344.97    4477.82   76898.86
  Latency        1.33ms   352.05us     4.96ms
  Latency Distribution
     50%     1.31ms
     75%     1.51ms
     90%     1.73ms
     99%     2.81ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     63612.15   20321.04   76175.50
  Latency        1.40ms   454.24us     6.21ms
  Latency Distribution
     50%     1.41ms
     75%     1.66ms
     90%     2.12ms
     99%     3.28ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     84159.12    5392.06   88318.61
  Latency        1.17ms   392.27us     4.10ms
  Latency Distribution
     50%     1.05ms
     75%     1.44ms
     90%     1.93ms
     99%     2.94ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
 3690 / 10000 [========================================================>------------------------------------------------------------------------------------------------]  36.90% 18404/s
  Reqs/sec     18976.17    1508.06   21057.26
  Latency        5.25ms     1.48ms    13.34ms
  Latency Distribution
     50%     5.21ms
     75%     6.54ms
     90%     7.41ms
     99%     9.84ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15208.05    3933.91   18044.69
  Latency        6.25ms     5.50ms    77.84ms
  Latency Distribution
     50%     5.64ms
     75%     6.77ms
     90%     7.88ms
     99%    11.00ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     90229.89    6806.89  100936.70
  Latency        1.09ms   375.56us     4.05ms
  Latency Distribution
     50%     0.99ms
     75%     1.32ms
     90%     1.74ms
     99%     2.84ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    162773.60   20577.22  175295.28
  Latency      600.42us   202.45us     3.84ms
  Latency Distribution
     50%   590.00us
     75%   705.00us
     90%   816.00us
     99%     1.51ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    151477.12   15410.46  166269.33
  Latency      634.09us   334.58us     6.05ms
  Latency Distribution
     50%   575.00us
     75%   733.00us
     90%     0.97ms
     99%     1.75ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     13741.78    1058.52   16150.27
  Latency        7.26ms     3.05ms    73.15ms
  Latency Distribution
     50%     7.24ms
     75%     8.30ms
     90%     9.55ms
     99%    11.77ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     12012.53    9724.62   77220.08
  Latency        9.36ms     7.43ms    99.88ms
  Latency Distribution
     50%     7.98ms
     75%    11.08ms
     90%    14.81ms
     99%    27.76ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     17053.28     655.30   18072.37
  Latency        5.82ms     1.51ms    12.68ms
  Latency Distribution
     50%     5.66ms
     75%     6.89ms
     90%     8.37ms
     99%    10.44ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     14554.17    1053.44   18375.22
  Latency        6.87ms     3.33ms    74.20ms
  Latency Distribution
     50%     6.55ms
     75%     8.31ms
     90%    10.17ms
     99%    14.21ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    115012.32   11583.81  125319.43
  Latency        0.86ms   285.41us     4.40ms
  Latency Distribution
     50%   791.00us
     75%     1.05ms
     90%     1.35ms
     99%     2.24ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    117397.25    9833.19  124100.78
  Latency      846.15us   238.94us     4.33ms
  Latency Distribution
     50%   800.00us
     75%     1.05ms
     90%     1.30ms
     99%     1.95ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     71850.70    5521.92   78374.72
  Latency        1.38ms   367.93us     4.32ms
  Latency Distribution
     50%     1.30ms
     75%     1.63ms
     90%     2.08ms
     99%     2.99ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    111319.15    9384.23  122133.45
  Latency        0.87ms   278.42us     4.00ms
  Latency Distribution
     50%   797.00us
     75%     1.06ms
     90%     1.37ms
     99%     2.12ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    109767.91    7076.40  115970.73
  Latency        0.90ms   247.46us     3.30ms
  Latency Distribution
     50%   832.00us
     75%     1.10ms
     90%     1.41ms
     99%     2.02ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    111384.15   12737.59  123157.56
  Latency        0.88ms   317.04us     5.10ms
  Latency Distribution
     50%   805.00us
     75%     1.09ms
     90%     1.37ms
     99%     2.18ms
### CBV Response Types (/cbv-response)
  Reqs/sec    122693.94    7153.51  128446.32
  Latency      799.58us   235.42us     3.27ms
  Latency Distribution
     50%   741.00us
     75%     0.96ms
     90%     1.25ms
     99%     1.95ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     17388.16    1678.57   19214.67
  Latency        5.66ms     3.84ms    65.50ms
  Latency Distribution
     50%     5.53ms
     75%     7.07ms
     90%     8.77ms
     99%    12.05ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    142402.60   13004.47  149928.67
  Latency      693.25us   340.63us     7.55ms
  Latency Distribution
     50%   638.00us
     75%   799.00us
     90%     1.01ms
     99%     1.85ms
### File Upload (POST /upload)
  Reqs/sec    120273.00    9798.92  126323.44
  Latency      814.45us   289.95us     4.27ms
  Latency Distribution
     50%   747.00us
     75%     1.00ms
     90%     1.29ms
     99%     1.98ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    115476.91    9370.82  121878.89
  Latency        0.85ms   438.76us     7.12ms
  Latency Distribution
     50%   803.00us
     75%     1.01ms
     90%     1.32ms
     99%     2.17ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    131107.85   11063.64  137812.49
  Latency      747.61us   300.17us     4.64ms
  Latency Distribution
     50%   667.00us
     75%     0.93ms
     90%     1.12ms
     99%     1.80ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec     96621.47   25521.88  110578.02
  Latency        1.03ms     0.88ms    12.89ms
  Latency Distribution
     50%     0.88ms
     75%     1.23ms
     90%     1.51ms
     99%     3.05ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9119.93    2344.05   22953.93
  Latency       11.20ms     6.93ms    84.98ms
  Latency Distribution
     50%    10.09ms
     75%    12.46ms
     90%    14.02ms
     99%    27.82ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    150744.94   12277.70  158028.89
  Latency      652.68us   257.31us     4.58ms
  Latency Distribution
     50%   585.00us
     75%   748.00us
     90%     0.98ms
     99%     1.89ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    150759.62   19399.95  164264.93
  Latency      651.71us   288.38us     5.63ms
  Latency Distribution
     50%   594.00us
     75%   745.00us
     90%     0.97ms
     99%     2.17ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec    103625.12    7469.44  108825.90
  Latency        0.95ms   282.62us     3.31ms
  Latency Distribution
     50%     0.90ms
     75%     1.19ms
     90%     1.49ms
     99%     2.29ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    152827.23   12266.61  164770.55
  Latency      632.06us   233.04us     5.76ms
  Latency Distribution
     50%   578.00us
     75%   755.00us
     90%     0.91ms
     99%     1.67ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    117500.22    9502.71  123856.32
  Latency      840.69us   235.13us     3.42ms
  Latency Distribution
     50%   784.00us
     75%     1.03ms
     90%     1.31ms
     99%     1.93ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    117620.37    9104.23  126221.37
  Latency      846.14us   271.44us     4.90ms
  Latency Distribution
     50%   784.00us
     75%     1.05ms
     90%     1.30ms
     99%     1.96ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    110295.08    7153.81  115778.84
  Latency        0.89ms   237.58us     2.77ms
  Latency Distribution
     50%   834.00us
     75%     1.09ms
     90%     1.37ms
     99%     2.00ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    107892.17    7838.44  115807.01
  Latency        0.92ms   260.29us     3.38ms
  Latency Distribution
     50%   845.00us
     75%     1.13ms
     90%     1.43ms
     99%     2.03ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    110576.88    7345.79  116180.99
  Latency        0.89ms   235.36us     3.38ms
  Latency Distribution
     50%   825.00us
     75%     1.07ms
     90%     1.33ms
     99%     1.99ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     75291.84    3982.51   78727.58
  Latency        1.31ms   317.01us     4.14ms
  Latency Distribution
     50%     1.27ms
     75%     1.50ms
     90%     1.81ms
     99%     2.56ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    196733.36   11252.57  204958.77
  Latency      496.72us   207.66us     5.18ms
  Latency Distribution
     50%   458.00us
     75%   572.00us
     90%   711.00us
     99%     1.21ms

### Path Parameter - int (/items/12345)
  Reqs/sec    164722.10   19314.81  176910.17
  Latency      591.57us   233.99us     4.37ms
  Latency Distribution
     50%   596.00us
     75%   697.00us
     90%     0.86ms
     99%     1.72ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    164128.65   25260.33  188303.58
  Latency      623.81us   307.63us     4.78ms
  Latency Distribution
     50%   554.00us
     75%   729.00us
     90%     0.96ms
     99%     2.31ms

### Header Parameter (/header)
  Reqs/sec    121026.41   10510.93  131937.75
  Latency      825.18us   253.69us     4.41ms
  Latency Distribution
     50%   761.00us
     75%     1.00ms
     90%     1.29ms
     99%     1.97ms

### Cookie Parameter (/cookie)
  Reqs/sec    113381.09    9496.89  121536.88
  Latency        0.88ms   299.36us     6.81ms
  Latency Distribution
     50%   804.00us
     75%     1.10ms
     90%     1.45ms
     99%     2.20ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     94219.93    7804.75  102163.59
  Latency        1.04ms   332.45us     3.94ms
  Latency Distribution
     50%     0.95ms
     75%     1.28ms
     90%     1.64ms
     99%     2.53ms
