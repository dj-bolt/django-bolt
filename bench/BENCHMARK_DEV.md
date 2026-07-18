# Django-Bolt Benchmark
Generated: Sun Jul 19 02:49:33 AM PKT 2026
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    205791.03   12675.93  216223.69
  Latency      474.01us   198.08us     4.34ms
  Latency Distribution
     50%   444.00us
     75%   547.00us
     90%   695.00us
     99%     1.30ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    134143.53   14543.66  143181.36
  Latency      731.10us   284.05us     4.40ms
  Latency Distribution
     50%   635.00us
     75%     0.90ms
     90%     1.14ms
     99%     2.03ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    140939.89    9265.60  147345.30
  Latency      696.57us   216.95us     3.80ms
  Latency Distribution
     50%   655.00us
     75%   787.00us
     90%     1.02ms
     99%     1.63ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    164713.40   11643.65  173539.93
  Latency      593.35us   187.02us     3.69ms
  Latency Distribution
     50%   544.00us
     75%   705.00us
     90%     0.88ms
     99%     1.36ms
### Cookie Endpoint (/cookie)
  Reqs/sec    108109.70   25889.35  127269.48
  Latency      817.30us   546.51us     8.31ms
  Latency Distribution
     50%   656.00us
     75%     0.98ms
     90%     1.45ms
     99%     3.78ms
### Exception Endpoint (/exc)
  Reqs/sec    159876.46   11003.85  170033.57
  Latency      611.05us   202.58us     4.05ms
  Latency Distribution
     50%   587.00us
     75%   721.00us
     90%     0.92ms
     99%     1.46ms
### HTML Response (/html)
  Reqs/sec    182515.21    9802.78  192387.61
  Latency      530.81us   244.67us     4.27ms
  Latency Distribution
     50%   484.00us
     75%   662.00us
     90%   802.00us
     99%     1.89ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     43761.21   12142.84   73830.92
  Latency        2.40ms     1.33ms    15.92ms
  Latency Distribution
     50%     2.01ms
     75%     2.78ms
     90%     3.99ms
     99%     7.94ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    172839.35   17264.58  184475.68
  Latency      566.64us   237.51us     4.78ms
  Latency Distribution
     50%   545.00us
     75%   627.00us
     90%   686.00us
     99%     1.30ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    175073.56   15551.01  184740.47
  Latency      558.54us   172.62us     3.88ms
  Latency Distribution
     50%   562.00us
     75%   662.00us
     90%   712.00us
     99%     1.24ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     67516.88   42888.98  100574.82
  Latency        1.07ms     3.33ms    44.06ms
  Latency Distribution
     50%   682.00us
     75%     0.95ms
     90%     1.35ms
     99%     5.47ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    197202.58   20474.24  216077.95
  Latency      484.23us   221.81us     6.64ms
  Latency Distribution
     50%   474.00us
     75%   570.00us
     90%   650.00us
     99%     1.43ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    166827.53   17849.56  177316.78
  Latency      585.94us   284.41us     5.67ms
  Latency Distribution
     50%   582.00us
     75%   705.00us
     90%   820.00us
     99%     1.96ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     67150.27   46797.82  104165.84
  Latency        1.04ms     3.18ms    43.20ms
  Latency Distribution
     50%   678.00us
     75%     0.91ms
     90%     1.19ms
     99%     5.04ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    206670.42   20108.08  219685.58
  Latency      475.68us   280.79us     5.98ms
  Latency Distribution
     50%   444.00us
     75%   502.00us
     90%   602.00us
     99%     1.50ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    209803.03   22325.01  225737.14
  Latency      467.51us   184.27us     4.54ms
  Latency Distribution
     50%   463.00us
     75%   514.00us
     90%   573.00us
     99%     1.49ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     76967.65    5198.39   82547.54
  Latency        1.27ms   314.19us     4.62ms
  Latency Distribution
     50%     1.24ms
     75%     1.47ms
     90%     1.94ms
     99%     2.58ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     73893.50    4611.86   78300.58
  Latency        1.34ms   294.93us     4.76ms
  Latency Distribution
     50%     1.36ms
     75%     1.50ms
     90%     1.85ms
     99%     2.54ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec    136883.52   12401.55  144926.69
  Latency      720.84us   239.13us     3.69ms
  Latency Distribution
     50%   653.00us
     75%     0.88ms
     90%     1.12ms
     99%     1.79ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     20011.75    1180.87   21044.67
  Latency        4.96ms     1.66ms    15.52ms
  Latency Distribution
     50%     4.66ms
     75%     6.81ms
     90%     7.94ms
     99%     9.20ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     16605.06    2771.21   22515.35
  Latency        6.04ms     6.56ms    76.50ms
  Latency Distribution
     50%     5.32ms
     75%     6.32ms
     90%     7.31ms
     99%    12.48ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    144890.94   12984.39  156169.30
  Latency      676.73us   222.38us     3.83ms
  Latency Distribution
     50%   617.00us
     75%   808.00us
     90%     1.02ms
     99%     1.61ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    183240.92   16750.69  194934.91
  Latency      534.40us   200.14us     3.55ms
  Latency Distribution
     50%   471.00us
     75%   657.00us
     90%   780.00us
     99%     1.45ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    170464.36   16985.86  184349.12
  Latency      571.94us   208.62us     3.86ms
  Latency Distribution
     50%   574.00us
     75%   697.00us
     90%   767.00us
     99%     1.48ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
 2590 / 10000 [=====================================================================================>----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  25.90% 12883/s
  Reqs/sec     11788.07    2498.91   15206.69
  Latency        8.46ms     7.30ms    89.62ms
  Latency Distribution
     50%     6.96ms
     75%     9.60ms
     90%    12.97ms
     99%    29.03ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     12006.70     899.21   13972.81
  Latency        8.30ms     4.76ms    82.09ms
  Latency Distribution
     50%     7.06ms
     75%    10.51ms
     90%    14.55ms
     99%    22.19ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     16716.39    1481.57   23522.11
  Latency        6.01ms     2.47ms    20.98ms
  Latency Distribution
     50%     5.49ms
     75%     7.33ms
     90%     9.56ms
     99%    15.32ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     16856.74    1657.18   24247.26
  Latency        5.98ms     2.57ms    28.29ms
  Latency Distribution
     50%     5.31ms
     75%     7.29ms
     90%     9.78ms
     99%    15.29ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    185969.88   13299.78  195246.44
  Latency      529.02us   179.35us     2.85ms
  Latency Distribution
     50%   486.00us
     75%   647.00us
     90%   833.00us
     99%     1.36ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    182134.66   18238.38  192828.51
  Latency      537.66us   184.47us     2.74ms
  Latency Distribution
     50%   481.00us
     75%   656.00us
     90%     0.86ms
     99%     1.47ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     92270.64    4297.90   94617.09
  Latency        1.07ms   259.00us     3.70ms
  Latency Distribution
     50%     1.01ms
     75%     1.24ms
     90%     1.52ms
     99%     2.26ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    179885.31   14221.53  190238.13
  Latency      547.07us   177.94us     2.74ms
  Latency Distribution
     50%   490.00us
     75%   680.00us
     90%     0.89ms
     99%     1.44ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    176626.49   13758.15  185376.40
  Latency      554.40us   162.55us     2.92ms
  Latency Distribution
     50%   508.00us
     75%   675.00us
     90%     0.89ms
     99%     1.36ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    179905.18   18913.86  192640.23
  Latency      545.20us   240.20us     4.14ms
  Latency Distribution
     50%   484.00us
     75%   657.00us
     90%   843.00us
     99%     1.66ms
### CBV Response Types (/cbv-response)
  Reqs/sec    193523.83   17938.75  205722.74
  Latency      504.29us   158.56us     3.26ms
  Latency Distribution
     50%   458.00us
     75%   611.00us
     90%   790.00us
     99%     1.29ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     18398.09    3181.67   23065.39
  Latency        5.44ms     6.23ms    76.81ms
  Latency Distribution
     50%     5.12ms
     75%     6.28ms
     90%     7.14ms
     99%    10.66ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    150604.29   12966.00  161633.17
  Latency      659.99us   266.36us     5.47ms
  Latency Distribution
     50%   597.00us
     75%   768.00us
     90%     1.02ms
     99%     1.71ms
### File Upload (POST /upload)
  Reqs/sec    123145.55   11676.07  132411.63
  Latency      792.65us   325.54us     5.13ms
  Latency Distribution
     50%   769.00us
     75%     0.96ms
     90%     1.20ms
     99%     2.19ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    125323.38    8091.78  131690.35
  Latency      782.77us   260.75us     4.04ms
  Latency Distribution
     50%   734.00us
     75%     0.95ms
     90%     1.24ms
     99%     2.00ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    134542.77    8889.25  139894.38
  Latency      729.93us   297.60us     4.86ms
  Latency Distribution
     50%   674.00us
     75%     0.91ms
     90%     1.12ms
     99%     1.77ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    111132.68    8935.41  122199.27
  Latency        0.88ms   304.92us     5.03ms
  Latency Distribution
     50%   829.00us
     75%     1.12ms
     90%     1.38ms
     99%     2.00ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9142.18    1553.07   11197.12
  Latency       10.89ms     7.35ms    83.29ms
  Latency Distribution
     50%     8.76ms
     75%    14.31ms
     90%    15.64ms
     99%    24.07ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    170107.59   17702.83  183985.05
  Latency      571.09us   228.99us     4.83ms
  Latency Distribution
     50%   519.00us
     75%   665.00us
     90%     0.87ms
     99%     1.60ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    163726.61   15818.40  176767.86
  Latency      606.29us   273.61us     4.32ms
  Latency Distribution
     50%   565.00us
     75%   708.00us
     90%     0.88ms
     99%     1.53ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec    162675.65   17575.70  174645.85
  Latency      614.64us   188.06us     2.98ms
  Latency Distribution
     50%   563.00us
     75%   749.00us
     90%     0.95ms
     99%     1.47ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    146254.75   26737.47  174680.32
  Latency      614.88us   275.73us     5.20ms
  Latency Distribution
     50%   570.00us
     75%   726.00us
     90%     0.92ms
     99%     2.21ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    183707.11   18975.38  196562.82
  Latency      533.12us   222.51us     5.08ms
  Latency Distribution
     50%   478.00us
     75%   639.00us
     90%   813.00us
     99%     1.46ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    181893.62   14209.36  191584.52
  Latency      539.52us   157.68us     2.26ms
  Latency Distribution
     50%   493.00us
     75%   668.00us
     90%     0.86ms
     99%     1.35ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    177232.20   14435.50  185836.08
  Latency      549.64us   250.75us     5.74ms
  Latency Distribution
     50%   507.00us
     75%   620.00us
     90%   775.00us
     99%     1.84ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    163912.99   24185.30  180733.54
  Latency      595.44us   354.41us     6.08ms
  Latency Distribution
     50%   513.00us
     75%   745.00us
     90%     0.90ms
     99%     2.17ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    170782.65   13400.52  179805.89
  Latency      570.19us   219.30us     4.40ms
  Latency Distribution
     50%   513.00us
     75%   702.00us
     90%     0.96ms
     99%     1.51ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     74766.05    6781.06   81007.31
  Latency        1.32ms   487.43us     7.23ms
  Latency Distribution
     50%     1.29ms
     75%     1.62ms
     90%     2.03ms
     99%     3.25ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    206680.08   15009.47  223678.91
  Latency      480.25us   212.24us     5.14ms
  Latency Distribution
     50%   447.00us
     75%   542.00us
     90%   692.00us
     99%     1.40ms

### Path Parameter - int (/items/12345)
  Reqs/sec    175956.29   16731.10  187396.07
  Latency      556.33us   211.37us     6.19ms
  Latency Distribution
     50%   518.00us
     75%   677.00us
     90%   845.00us
     99%     1.46ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    175276.09   17524.21  186689.70
  Latency      557.25us   220.41us     5.19ms
  Latency Distribution
     50%   512.00us
     75%   662.00us
     90%   806.00us
     99%     1.54ms

### Header Parameter (/header)
  Reqs/sec    163980.42   11570.28  171238.29
  Latency      594.98us   214.34us     5.00ms
  Latency Distribution
     50%   540.00us
     75%   693.00us
     90%     0.87ms
     99%     1.41ms

### Cookie Parameter (/cookie)
  Reqs/sec    162793.00   16311.43  171931.10
  Latency      607.96us   281.20us     7.96ms
  Latency Distribution
     50%   572.00us
     75%   708.00us
     90%     0.89ms
     99%     1.55ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    143462.20   11782.34  151606.78
  Latency      688.79us   244.23us     4.12ms
  Latency Distribution
     50%   627.00us
     75%   841.00us
     90%     1.06ms
     99%     1.76ms
