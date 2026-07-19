# Django-Bolt Benchmark
Generated: Sun Jul 19 09:08:07 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    319987.37       0.00  384319.46
  Latency      307.74us   341.18us     4.26ms
  Latency Distribution
     50%   205.00us
     75%   310.00us
     90%   540.00us
     99%     2.24ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    199367.59   39962.12  226171.54
  Latency      500.32us   338.22us     4.50ms
  Latency Distribution
     50%   454.00us
     75%   586.00us
     90%   725.00us
     99%     2.50ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    184760.31   38741.27  221876.96
  Latency      534.48us   404.31us     5.87ms
  Latency Distribution
     50%   453.00us
     75%   588.00us
     90%   782.00us
     99%     2.61ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    272231.98       0.00  302024.34
  Latency      357.31us   334.94us     6.25ms
  Latency Distribution
     50%   296.00us
     75%   380.00us
     90%   513.00us
     99%     2.00ms
### Cookie Endpoint (/cookie)
  Reqs/sec    264410.17       0.00  291461.51
  Latency      370.99us   385.92us     6.49ms
  Latency Distribution
     50%   296.00us
     75%   401.00us
     90%   553.00us
     99%     2.52ms
### Exception Endpoint (/exc)
  Reqs/sec    250350.49       0.00  278002.27
  Latency      389.85us   392.22us     5.20ms
  Latency Distribution
     50%   311.00us
     75%   368.00us
     90%   489.00us
     99%     3.17ms
### HTML Response (/html)
  Reqs/sec    316353.31       0.00  336778.53
  Latency      309.33us   387.74us     5.80ms
  Latency Distribution
     50%   240.00us
     75%   317.00us
     90%   429.00us
     99%     2.19ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     45308.29   13512.47   54979.41
  Latency        2.24ms     1.88ms    23.92ms
  Latency Distribution
     50%     1.90ms
     75%     2.59ms
     90%     3.34ms
     99%     9.62ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    176014.04   16665.65  190266.32
  Latency      552.87us   261.78us     4.89ms
  Latency Distribution
     50%   528.00us
     75%   711.00us
     90%   844.00us
     99%     1.24ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    176930.90   16822.63  188304.26
  Latency      552.45us   268.95us     4.85ms
  Latency Distribution
     50%   520.00us
     75%   609.00us
     90%   691.00us
     99%     1.37ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     62226.48   43554.65   99826.11
  Latency        1.20ms     3.24ms    44.91ms
  Latency Distribution
     50%   756.00us
     75%     0.98ms
     90%     1.30ms
     99%    12.82ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    196837.75   16206.29  208856.63
  Latency      499.23us   233.38us     4.11ms
  Latency Distribution
     50%   437.00us
     75%   608.00us
     90%   693.00us
     99%     1.47ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    177656.66   15699.89  188513.74
  Latency      550.20us   290.34us     5.00ms
  Latency Distribution
     50%   496.00us
     75%   543.00us
     90%     0.85ms
     99%     1.22ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     61978.58   44526.10  103488.96
  Latency        1.14ms     3.36ms    42.96ms
  Latency Distribution
     50%   750.00us
     75%     0.93ms
     90%     1.08ms
     99%    12.53ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    311575.81       0.00  343025.38
  Latency      317.27us   287.29us     3.53ms
  Latency Distribution
     50%   226.00us
     75%   350.00us
     90%   549.00us
     99%     1.92ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    321429.66       0.00  344500.71
  Latency      304.09us   395.39us     4.47ms
  Latency Distribution
     50%   226.00us
     75%   294.00us
     90%   393.00us
     99%     3.23ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     92674.20    6722.14   99516.30
  Latency        1.06ms   325.95us     5.55ms
  Latency Distribution
     50%     1.06ms
     75%     1.26ms
     90%     1.54ms
     99%     2.21ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     89030.98    5595.64   93024.78
  Latency        1.11ms   275.99us     4.68ms
  Latency Distribution
     50%     1.11ms
     75%     1.37ms
     90%     1.54ms
     99%     2.19ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec    140813.53   11223.39  148845.85
  Latency      699.97us   253.41us     3.86ms
  Latency Distribution
     50%   639.00us
     75%   834.00us
     90%     1.07ms
     99%     1.85ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     20288.56    1305.49   21021.10
  Latency        4.89ms     0.99ms    12.33ms
  Latency Distribution
     50%     4.95ms
     75%     5.93ms
     90%     6.39ms
     99%     7.25ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     16694.78    3156.61   19869.38
  Latency        5.96ms     6.55ms    79.66ms
  Latency Distribution
     50%     5.27ms
     75%     6.49ms
     90%     7.69ms
     99%    11.75ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    131446.72    9307.84  140939.43
  Latency      745.12us   244.41us     4.38ms
  Latency Distribution
     50%   696.00us
     75%     0.94ms
     90%     1.20ms
     99%     1.71ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    301794.26       0.00  331305.13
  Latency      326.52us   303.85us     4.08ms
  Latency Distribution
     50%   263.00us
     75%   338.00us
     90%   486.00us
     99%     2.06ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    277161.46       0.00  302174.10
  Latency      352.21us   357.09us     6.67ms
  Latency Distribution
     50%   270.00us
     75%   341.00us
     90%   453.00us
     99%     2.28ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     19411.25    1450.92   20760.92
  Latency        5.13ms     1.21ms    15.60ms
  Latency Distribution
     50%     5.00ms
     75%     6.11ms
     90%     6.97ms
     99%     8.88ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     13407.51    2533.13   19226.05
  Latency        7.42ms     6.98ms    84.98ms
  Latency Distribution
     50%     5.89ms
     75%     8.46ms
     90%    12.17ms
     99%    25.14ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     24593.41    1709.77   29259.40
  Latency        4.07ms   797.48us     8.88ms
  Latency Distribution
     50%     4.15ms
     75%     4.76ms
     90%     5.31ms
     99%     6.47ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     16268.90     947.94   18245.61
  Latency        6.08ms     3.80ms    32.55ms
  Latency Distribution
     50%     4.90ms
     75%     7.62ms
     90%    11.96ms
     99%    19.83ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    197602.37   16936.28  211438.14
  Latency      500.19us   147.16us     2.76ms
  Latency Distribution
     50%   457.00us
     75%   611.00us
     90%   772.00us
     99%     1.20ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    190488.24   11432.12  198179.20
  Latency      512.55us   180.14us     2.99ms
  Latency Distribution
     50%   464.00us
     75%   623.00us
     90%   805.00us
     99%     1.43ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     96642.20    8152.09  101959.81
  Latency        1.03ms   276.18us     3.91ms
  Latency Distribution
     50%     1.00ms
     75%     1.25ms
     90%     1.51ms
     99%     2.26ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    196729.83   18990.64  209835.00
  Latency      500.78us   194.86us     3.31ms
  Latency Distribution
     50%   451.00us
     75%   609.00us
     90%   777.00us
     99%     1.43ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    189663.10   14255.14  198515.67
  Latency      514.72us   150.81us     2.69ms
  Latency Distribution
     50%   477.00us
     75%   627.00us
     90%   799.00us
     99%     1.21ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    194965.51   24289.46  211746.64
  Latency      505.53us   211.33us     3.87ms
  Latency Distribution
     50%   457.00us
     75%   616.00us
     90%   771.00us
     99%     1.27ms
### CBV Response Types (/cbv-response)
  Reqs/sec    205843.43   19993.85  221078.65
  Latency      483.83us   204.98us     3.59ms
  Latency Distribution
     50%   434.00us
     75%   591.00us
     90%   767.00us
     99%     1.26ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     18220.58    3750.26   21725.84
  Latency        5.48ms     6.38ms    80.96ms
  Latency Distribution
     50%     4.65ms
     75%     6.25ms
     90%     7.55ms
     99%    11.65ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    204725.72   68503.31  259891.84
  Latency      399.14us   281.08us     4.19ms
  Latency Distribution
     50%   322.00us
     75%   471.00us
     90%   613.00us
     99%     1.83ms
### File Upload (POST /upload)
  Reqs/sec    201099.61   16179.60  212180.86
  Latency      487.94us   279.66us     4.49ms
  Latency Distribution
     50%   436.00us
     75%   630.00us
     90%   759.00us
     99%     1.86ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    195471.00   20451.95  209537.75
  Latency      500.54us   307.78us     5.08ms
  Latency Distribution
     50%   441.00us
     75%   541.00us
     90%   693.00us
     99%     1.38ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    228205.60   23981.52  255896.93
  Latency      410.46us   236.51us     4.05ms
  Latency Distribution
     50%   378.00us
     75%   486.00us
     90%   614.00us
     99%     1.51ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    166489.81   15664.35  178093.38
  Latency      579.01us   195.50us     5.06ms
  Latency Distribution
     50%   537.00us
     75%   681.00us
     90%     0.86ms
     99%     1.48ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9173.02    1629.69   10822.02
  Latency       10.83ms     6.98ms    88.89ms
  Latency Distribution
     50%     9.11ms
     75%    12.51ms
     90%    14.92ms
     99%    29.67ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    266884.61       0.00  288343.78
  Latency      362.47us   399.72us     6.51ms
  Latency Distribution
     50%   299.00us
     75%   363.00us
     90%   500.00us
     99%     2.23ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    270468.08       0.00  303301.89
  Latency      360.88us   315.06us     5.73ms
  Latency Distribution
     50%   280.00us
     75%   412.00us
     90%   561.00us
     99%     1.80ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec    166406.34   17416.41  178534.78
  Latency      587.38us   194.97us     2.83ms
  Latency Distribution
     50%   526.00us
     75%   710.00us
     90%     0.94ms
     99%     1.56ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    265296.11       0.00  297710.12
  Latency      368.42us   353.09us     4.72ms
  Latency Distribution
     50%   300.00us
     75%   402.00us
     90%   541.00us
     99%     2.45ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    195456.18   18767.62  206694.30
  Latency      503.28us   154.25us     2.56ms
  Latency Distribution
     50%   453.00us
     75%   618.00us
     90%   811.00us
     99%     1.28ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    194668.29   18092.05  207269.75
  Latency      506.91us   169.50us     3.22ms
  Latency Distribution
     50%   460.00us
     75%   619.00us
     90%   801.00us
     99%     1.33ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    305458.04       0.00  333022.08
  Latency      319.14us   346.68us     6.18ms
  Latency Distribution
     50%   246.00us
     75%   340.00us
     90%   465.00us
     99%     2.75ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    291746.10       0.00  311790.66
  Latency      332.46us   360.82us     5.44ms
  Latency Distribution
     50%   278.00us
     75%   345.00us
     90%   436.00us
     99%     3.00ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    289752.71       0.00  313732.03
  Latency      333.88us   341.58us     4.28ms
  Latency Distribution
     50%   262.00us
     75%   345.00us
     90%   493.00us
     99%     2.69ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     92681.86    5476.57   96094.46
  Latency        1.06ms   312.94us     5.74ms
  Latency Distribution
     50%     1.04ms
     75%     1.31ms
     90%     1.54ms
     99%     2.25ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    341022.54       0.00  362412.87
  Latency      285.65us   299.92us     6.39ms
  Latency Distribution
     50%   215.00us
     75%   314.00us
     90%   473.00us
     99%     1.58ms

### Path Parameter - int (/items/12345)
  Reqs/sec    300978.58       0.00  328710.44
  Latency      323.63us   375.69us     5.36ms
  Latency Distribution
     50%   247.00us
     75%   329.00us
     90%   446.00us
     99%     2.58ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    306177.03       0.00  325161.09
  Latency      322.98us   333.90us     4.48ms
  Latency Distribution
     50%   266.00us
     75%   333.00us
     90%   445.00us
     99%     2.25ms

### Header Parameter (/header)
  Reqs/sec    272881.66       0.00  295574.57
  Latency      357.68us   353.71us     5.14ms
  Latency Distribution
     50%   286.00us
     75%   357.00us
     90%   475.00us
     99%     2.25ms

### Cookie Parameter (/cookie)
  Reqs/sec    279035.51       0.00  303404.01
  Latency      349.52us   283.15us     5.13ms
  Latency Distribution
     50%   289.00us
     75%   376.00us
     90%   506.00us
     99%     1.96ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    148033.68   14300.52  156405.51
  Latency      669.22us   219.59us     3.62ms
  Latency Distribution
     50%   608.00us
     75%   811.00us
     90%     1.06ms
     99%     1.79ms
