# Django-Bolt Benchmark
Generated: Mon Jul 20 12:55:29 AM PKT 2026
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    310626.46       0.00  337792.10
  Latency      316.72us   284.47us     4.42ms
  Latency Distribution
     50%   241.00us
     75%   346.00us
     90%   519.00us
     99%     1.97ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    191024.99   20644.59  207992.30
  Latency      512.55us   230.92us     4.34ms
  Latency Distribution
     50%   478.00us
     75%   567.00us
     90%   759.00us
     99%     1.69ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    186327.09   17027.82  196362.16
  Latency      526.48us   387.52us     6.24ms
  Latency Distribution
     50%   450.00us
     75%   571.00us
     90%   787.00us
     99%     3.01ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    255459.65       0.00  280939.94
  Latency      380.15us   276.23us     5.63ms
  Latency Distribution
     50%   320.00us
     75%   416.00us
     90%   585.00us
     99%     1.87ms
### Cookie Endpoint (/cookie)
  Reqs/sec    259377.47       0.00  274517.87
  Latency      372.88us   253.19us     5.56ms
  Latency Distribution
     50%   307.00us
     75%   448.00us
     90%   581.00us
     99%     1.57ms
### Exception Endpoint (/exc)
  Reqs/sec    248664.92       0.00  276587.30
  Latency      391.32us   243.05us     7.83ms
  Latency Distribution
     50%   331.00us
     75%   462.00us
     90%   633.00us
     99%     1.63ms
### HTML Response (/html)
  Reqs/sec    284924.72       0.00  306964.62
  Latency      342.14us   342.24us     4.84ms
  Latency Distribution
     50%   280.00us
     75%   372.00us
     90%   488.00us
     99%     2.40ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     41416.89    9386.70   45955.02
  Latency        2.43ms     1.46ms    17.61ms
  Latency Distribution
     50%     2.12ms
     75%     2.82ms
     90%     3.75ms
     99%     7.20ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    165114.90   17619.16  180140.33
  Latency      577.98us   311.08us     6.52ms
  Latency Distribution
     50%   527.00us
     75%   618.00us
     90%   819.00us
     99%     1.98ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    178338.90   16366.37  189525.33
  Latency      546.32us   220.27us     4.71ms
  Latency Distribution
     50%   472.00us
     75%   711.00us
     90%   844.00us
     99%     1.41ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     69590.31   43042.80  103084.10
  Latency        1.05ms     3.38ms    44.06ms
  Latency Distribution
     50%   650.00us
     75%     1.08ms
     90%     1.27ms
     99%     6.43ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    203250.40   19893.66  216343.27
  Latency      482.91us   191.21us     4.17ms
  Latency Distribution
     50%   462.00us
     75%   560.00us
     90%   609.00us
     99%     1.28ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    166961.96   12968.83  175967.29
  Latency      584.58us   226.50us     3.84ms
  Latency Distribution
     50%   544.00us
     75%   769.00us
     90%     0.87ms
     99%     1.49ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     68807.68   48048.21  105764.19
  Latency        1.03ms     3.17ms    42.56ms
  Latency Distribution
     50%   705.00us
     75%     0.88ms
     90%     1.10ms
     99%     5.89ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    318381.83       0.00  355796.42
  Latency      309.33us   293.38us     3.53ms
  Latency Distribution
     50%   231.00us
     75%   318.00us
     90%   496.00us
     99%     2.12ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    309765.18       0.00  336183.99
  Latency      318.91us   286.46us     4.63ms
  Latency Distribution
     50%   246.00us
     75%   336.00us
     90%   490.00us
     99%     1.83ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     89564.12    5285.05   93310.21
  Latency        1.09ms   322.62us     5.82ms
  Latency Distribution
     50%     1.01ms
     75%     1.41ms
     90%     1.87ms
     99%     2.44ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     86116.47    5998.92   91830.44
  Latency        1.15ms   295.32us     3.43ms
  Latency Distribution
     50%     1.10ms
     75%     1.49ms
     90%     1.67ms
     99%     2.39ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec    156016.66   18748.88  173179.25
  Latency      607.16us   250.64us     5.34ms
  Latency Distribution
     50%   544.00us
     75%   748.00us
     90%     0.88ms
     99%     1.84ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     20100.12    1550.70   20897.37
  Latency        4.93ms     1.04ms    12.46ms
  Latency Distribution
     50%     4.53ms
     75%     6.25ms
     90%     7.18ms
     99%     8.02ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     18173.50    2922.23   21783.75
  Latency        5.49ms     6.18ms    77.02ms
  Latency Distribution
     50%     5.04ms
     75%     6.06ms
     90%     6.98ms
     99%     9.69ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    153196.86   20043.10  170204.59
  Latency      613.81us   425.07us    15.27ms
  Latency Distribution
     50%   542.00us
     75%   719.00us
     90%     0.91ms
     99%     2.69ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    280703.45       0.00  310684.98
  Latency      348.73us   269.45us     3.97ms
  Latency Distribution
     50%   285.00us
     75%   386.00us
     90%   544.00us
     99%     1.76ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    262157.03       0.00  284519.47
  Latency      367.76us   342.18us     5.22ms
  Latency Distribution
     50%   282.00us
     75%   389.00us
     90%   514.00us
     99%     2.29ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     20192.80    3287.63   21582.41
  Latency        4.78ms     1.66ms    15.24ms
  Latency Distribution
     50%     4.37ms
     75%     6.25ms
     90%     8.07ms
     99%     9.54ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     14362.68    2460.09   18315.68
  Latency        6.93ms     6.78ms    90.55ms
  Latency Distribution
     50%     5.22ms
     75%     7.87ms
     90%    11.91ms
     99%    25.46ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     25825.62    1641.84   29177.64
  Latency        3.86ms     2.22ms    63.46ms
  Latency Distribution
     50%     3.82ms
     75%     4.36ms
     90%     4.88ms
     99%     6.03ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     17513.51    1265.71   20338.81
  Latency        5.70ms     3.47ms    75.21ms
  Latency Distribution
     50%     5.41ms
     75%     6.99ms
     90%     8.74ms
     99%    13.21ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    226580.04   15513.02  239836.91
  Latency      434.33us   243.86us     5.37ms
  Latency Distribution
     50%   396.00us
     75%   482.00us
     90%   635.00us
     99%     1.89ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    219789.21   11881.79  229279.31
  Latency      446.23us   384.66us     8.42ms
  Latency Distribution
     50%   372.00us
     75%   473.00us
     90%   660.00us
     99%     1.86ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     98059.25   10432.02  114774.58
  Latency        1.03ms   298.71us     4.55ms
  Latency Distribution
     50%     0.96ms
     75%     1.18ms
     90%     1.59ms
     99%     2.27ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    233654.53   22686.37  255749.29
  Latency      415.28us   243.48us     4.92ms
  Latency Distribution
     50%   383.00us
     75%   452.00us
     90%   560.00us
     99%     1.46ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    213120.82   20113.45  227129.14
  Latency      462.23us   261.37us     6.70ms
  Latency Distribution
     50%   406.00us
     75%   545.00us
     90%   706.00us
     99%     1.48ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    223096.28   27806.15  249032.52
  Latency      434.66us   233.30us     4.85ms
  Latency Distribution
     50%   384.00us
     75%   500.00us
     90%   636.00us
     99%     1.47ms
### CBV Response Types (/cbv-response)
  Reqs/sec    228641.07   24544.34  256419.05
  Latency      419.82us   323.10us     4.36ms
  Latency Distribution
     50%   343.00us
     75%   482.00us
     90%   650.00us
     99%     1.88ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     20323.89    1936.41   22098.03
  Latency        4.88ms     5.10ms    70.11ms
  Latency Distribution
     50%     4.09ms
     75%     5.63ms
     90%     7.08ms
     99%     9.27ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    237185.45   32151.47  266341.15
  Latency      414.83us   251.62us     3.60ms
  Latency Distribution
     50%   381.00us
     75%   491.00us
     90%   607.00us
     99%     2.02ms
### File Upload (POST /upload)
  Reqs/sec    182144.93   24486.42  200256.72
  Latency      540.82us   319.42us     4.89ms
  Latency Distribution
     50%   478.00us
     75%   635.00us
     90%   800.00us
     99%     2.20ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    178538.01   18564.88  191195.68
  Latency      548.99us   268.04us     4.46ms
  Latency Distribution
     50%   486.00us
     75%   627.00us
     90%     0.86ms
     99%     1.91ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    212027.13   24782.81  229661.81
  Latency      466.15us   311.50us     4.44ms
  Latency Distribution
     50%   418.00us
     75%   571.00us
     90%   734.00us
     99%     2.03ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    163339.44   20418.48  187339.42
  Latency      619.44us   248.63us     4.50ms
  Latency Distribution
     50%   580.00us
     75%   770.00us
     90%     0.95ms
     99%     1.81ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9285.81    1890.77   11082.20
  Latency       10.72ms     6.81ms    86.77ms
  Latency Distribution
     50%    10.23ms
     75%    11.64ms
     90%    12.61ms
     99%    23.50ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    274895.23       0.00  297594.92
  Latency      355.59us   292.58us     4.38ms
  Latency Distribution
     50%   290.00us
     75%   356.00us
     90%   499.00us
     99%     2.16ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    263710.50       0.00  292949.80
  Latency      368.55us   401.74us     5.45ms
  Latency Distribution
     50%   288.00us
     75%   373.00us
     90%   488.00us
     99%     3.25ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec    176630.98   12626.50  189230.33
  Latency      552.19us   204.20us     4.46ms
  Latency Distribution
     50%   487.00us
     75%   643.00us
     90%   795.00us
     99%     1.54ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    257319.20       0.00  274401.52
  Latency      376.87us   330.68us     4.83ms
  Latency Distribution
     50%   316.00us
     75%   408.00us
     90%   555.00us
     99%     2.29ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    229487.96   23169.50  247491.59
  Latency      426.78us   269.95us     3.73ms
  Latency Distribution
     50%   352.00us
     75%   490.00us
     90%   641.00us
     99%     2.04ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    234207.29   31887.52  257891.73
  Latency      420.99us   230.28us     5.05ms
  Latency Distribution
     50%   365.00us
     75%   482.00us
     90%   649.00us
     99%     1.39ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    295921.36       0.00  320325.98
  Latency      328.35us   356.02us     8.36ms
  Latency Distribution
     50%   263.00us
     75%   341.00us
     90%   465.00us
     99%     2.44ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    279141.55       0.00  298257.07
  Latency      345.99us   370.23us     4.93ms
  Latency Distribution
     50%   265.00us
     75%   359.00us
     90%   500.00us
     99%     2.79ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    266997.24       0.00  296777.29
  Latency      359.44us   392.61us     6.70ms
  Latency Distribution
     50%   285.00us
     75%   353.00us
     90%   480.00us
     99%     2.94ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     90397.73    8092.48   96863.66
  Latency        1.09ms   368.96us     6.23ms
  Latency Distribution
     50%     1.03ms
     75%     1.32ms
     90%     1.55ms
     99%     2.47ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    314313.41       0.00  320592.60
  Latency      296.46us   367.08us     6.17ms
  Latency Distribution
     50%   205.00us
     75%   286.00us
     90%   476.00us
     99%     2.56ms

### Path Parameter - int (/items/12345)
  Reqs/sec    240886.14       0.00  268702.55
  Latency      398.61us   401.81us     4.22ms
  Latency Distribution
     50%   280.00us
     75%   415.00us
     90%   677.00us
     99%     2.95ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    268355.04       0.00  286782.74
  Latency      360.41us   365.10us     8.00ms
  Latency Distribution
     50%   270.00us
     75%   375.00us
     90%   599.00us
     99%     2.02ms

### Header Parameter (/header)
  Reqs/sec    255565.94       0.00  274027.14
  Latency      378.32us   361.25us     5.04ms
  Latency Distribution
     50%   297.00us
     75%   391.00us
     90%   529.00us
     99%     2.58ms

### Cookie Parameter (/cookie)
  Reqs/sec    253457.19       0.00  277425.54
  Latency      382.39us   263.16us     4.00ms
  Latency Distribution
     50%   317.00us
     75%   432.00us
     90%   586.00us
     99%     1.73ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    151821.13   11269.92  161414.29
  Latency      642.52us   263.90us     5.69ms
  Latency Distribution
     50%   599.00us
     75%   754.00us
     90%     0.92ms
     99%     1.62ms
