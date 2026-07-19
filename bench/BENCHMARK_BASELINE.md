# Django-Bolt Benchmark
Generated: Mon Jul 20 12:54:17 AM PKT 2026
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    310952.85       0.00  327657.56
  Latency      311.10us   359.48us    11.28ms
  Latency Distribution
     50%   217.00us
     75%   315.00us
     90%   489.00us
     99%     2.29ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    185755.47   22319.65  206728.72
  Latency      524.26us   373.82us     6.52ms
  Latency Distribution
     50%   468.00us
     75%   580.00us
     90%   751.00us
     99%     1.87ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    178390.82   20311.72  199290.87
  Latency      541.81us   323.42us     5.36ms
  Latency Distribution
     50%   464.00us
     75%   594.00us
     90%     0.92ms
     99%     2.05ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    251750.98       0.00  284787.54
  Latency      387.30us   292.93us     4.10ms
  Latency Distribution
     50%   311.00us
     75%   430.00us
     90%   607.00us
     99%     2.01ms
### Cookie Endpoint (/cookie)
  Reqs/sec    254220.62       0.00  275235.18
  Latency      379.00us   279.18us     5.63ms
  Latency Distribution
     50%   312.00us
     75%   407.00us
     90%   601.00us
     99%     1.68ms
### Exception Endpoint (/exc)
  Reqs/sec    252323.41       0.00  281187.20
  Latency      386.71us   319.45us     4.07ms
  Latency Distribution
     50%   302.00us
     75%   422.00us
     90%   582.00us
     99%     2.20ms
### HTML Response (/html)
  Reqs/sec    293565.91       0.00  323466.08
  Latency      334.91us   351.49us     6.26ms
  Latency Distribution
     50%   252.00us
     75%   345.00us
     90%   511.00us
     99%     2.24ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     46629.53    7660.40   50713.61
  Latency        2.14ms     0.96ms    13.79ms
  Latency Distribution
     50%     1.91ms
     75%     2.54ms
     90%     3.17ms
     99%     6.34ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    161626.92   17846.61  172102.99
  Latency      607.82us   484.06us     7.92ms
  Latency Distribution
     50%   520.00us
     75%   619.00us
     90%   771.00us
     99%     3.55ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    167885.05   12341.92  179219.37
  Latency      584.95us   316.39us     5.37ms
  Latency Distribution
     50%   538.00us
     75%   669.00us
     90%   762.00us
     99%     2.26ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     60985.87   43373.92  101537.93
  Latency        1.11ms     3.39ms    42.42ms
  Latency Distribution
     50%   706.00us
     75%     0.94ms
     90%     1.22ms
     99%     5.74ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    197062.73   23050.83  215577.96
  Latency      497.52us   319.35us     4.97ms
  Latency Distribution
     50%   466.00us
     75%   539.00us
     90%   647.00us
     99%     1.66ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    166550.52   14070.10  176179.50
  Latency      594.63us   321.00us     4.64ms
  Latency Distribution
     50%   539.00us
     75%   687.00us
     90%   789.00us
     99%     2.36ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     69052.91   38560.56  100241.89
  Latency        1.10ms     3.41ms    43.15ms
  Latency Distribution
     50%   701.00us
     75%     0.97ms
     90%     1.26ms
     99%     6.69ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    206926.42   17972.28  227301.24
  Latency      475.68us   443.31us     6.64ms
  Latency Distribution
     50%   287.00us
     75%   574.00us
     90%     1.05ms
     99%     2.46ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    276946.73       0.00  308291.59
  Latency      347.17us   373.32us     4.80ms
  Latency Distribution
     50%   238.00us
     75%   329.00us
     90%   577.00us
     99%     2.52ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     87729.93    8494.99   93370.62
  Latency        1.13ms   423.05us     5.49ms
  Latency Distribution
     50%     1.06ms
     75%     1.33ms
     90%     1.76ms
     99%     3.08ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     84364.70    6602.67   88235.85
  Latency        1.17ms   383.12us     7.19ms
  Latency Distribution
     50%     1.13ms
     75%     1.48ms
     90%     1.69ms
     99%     2.56ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec    153809.83   14895.16  170836.22
  Latency      624.84us   317.55us     5.50ms
  Latency Distribution
     50%   585.00us
     75%   811.00us
     90%     0.94ms
     99%     2.13ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     19402.78    1544.36   20865.48
  Latency        5.11ms     1.02ms    15.94ms
  Latency Distribution
     50%     4.99ms
     75%     5.88ms
     90%     7.01ms
     99%     8.60ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     17196.73    4421.84   21599.13
  Latency        5.78ms     6.95ms    80.92ms
  Latency Distribution
     50%     4.99ms
     75%     5.90ms
     90%     6.85ms
     99%    11.27ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    153861.63   13853.49  168503.10
  Latency      634.26us   278.24us     5.29ms
  Latency Distribution
     50%   597.00us
     75%   708.00us
     90%   837.00us
     99%     1.84ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    283558.24       0.00  311917.23
  Latency      345.29us   376.13us     6.25ms
  Latency Distribution
     50%   290.00us
     75%   358.00us
     90%   442.00us
     99%     2.77ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    268625.20       0.00  291908.70
  Latency      356.91us   328.44us     6.14ms
  Latency Distribution
     50%   288.00us
     75%   359.00us
     90%   507.00us
     99%     2.39ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     19850.37    1878.66   21848.86
  Latency        5.02ms     2.80ms    64.39ms
  Latency Distribution
     50%     4.77ms
     75%     5.98ms
     90%     7.30ms
     99%    10.66ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     14426.79    1825.88   17413.22
  Latency        6.89ms     6.98ms    85.60ms
  Latency Distribution
     50%     5.43ms
     75%     8.01ms
     90%    11.33ms
     99%    23.55ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     24611.36    1702.30   26681.03
  Latency        4.00ms     0.92ms     9.75ms
  Latency Distribution
     50%     3.85ms
     75%     4.74ms
     90%     5.62ms
     99%     7.23ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     17612.04     799.92   18677.67
  Latency        5.64ms     2.30ms    20.25ms
  Latency Distribution
     50%     5.17ms
     75%     7.06ms
     90%     9.15ms
     99%    13.88ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    224725.25   24580.85  242981.65
  Latency      438.49us   257.67us     4.51ms
  Latency Distribution
     50%   390.00us
     75%   549.00us
     90%   667.00us
     99%     2.02ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    225608.40   18433.57  243375.52
  Latency      427.36us   220.44us     4.17ms
  Latency Distribution
     50%   374.00us
     75%   517.00us
     90%   652.00us
     99%     1.48ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     95094.19    6058.31  101768.51
  Latency        1.02ms   353.91us     6.40ms
  Latency Distribution
     50%     0.96ms
     75%     1.25ms
     90%     1.47ms
     99%     2.21ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    224423.60   25423.07  248316.21
  Latency      445.43us   302.71us     4.40ms
  Latency Distribution
     50%   394.00us
     75%   501.00us
     90%   662.00us
     99%     2.15ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    201190.58   21068.58  215722.44
  Latency      491.30us   323.25us     4.97ms
  Latency Distribution
     50%   409.00us
     75%   557.00us
     90%   760.00us
     99%     2.31ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    219035.20   34387.36  245339.36
  Latency      460.27us   315.21us     4.58ms
  Latency Distribution
     50%   410.00us
     75%   521.00us
     90%   689.00us
     99%     2.13ms
### CBV Response Types (/cbv-response)
  Reqs/sec    237037.32       0.00  250876.48
  Latency      408.17us   370.42us     5.18ms
  Latency Distribution
     50%   333.00us
     75%   426.00us
     90%   567.00us
     99%     2.42ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     19359.75    5800.12   45052.03
  Latency        5.40ms     6.22ms    78.87ms
  Latency Distribution
     50%     4.67ms
     75%     5.70ms
     90%     6.79ms
     99%    12.02ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    245371.14   39939.28  276260.29
  Latency      419.85us   399.81us     6.82ms
  Latency Distribution
     50%   335.00us
     75%   448.00us
     90%   628.00us
     99%     2.72ms
### File Upload (POST /upload)
  Reqs/sec    190579.96   23678.83  205313.69
  Latency      513.44us   260.28us     5.78ms
  Latency Distribution
     50%   473.00us
     75%   595.00us
     90%   765.00us
     99%     1.81ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    180597.67   25275.06  196231.45
  Latency      543.40us   299.91us     5.99ms
  Latency Distribution
     50%   469.00us
     75%   607.00us
     90%     0.85ms
     99%     2.06ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    201333.06   18462.83  222429.04
  Latency      475.44us   376.12us     5.28ms
  Latency Distribution
     50%   392.00us
     75%   543.00us
     90%   769.00us
     99%     2.56ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    157313.75   16776.33  175786.18
  Latency      618.68us   237.33us     5.34ms
  Latency Distribution
     50%   570.00us
     75%   762.00us
     90%     0.97ms
     99%     1.75ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9146.98    1443.52   11215.33
  Latency       10.91ms     6.79ms    84.40ms
  Latency Distribution
     50%    10.57ms
     75%    12.48ms
     90%    13.34ms
     99%    26.15ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    254365.01   35340.83  287038.28
  Latency      406.01us   347.08us     4.75ms
  Latency Distribution
     50%   307.00us
     75%   428.00us
     90%   685.00us
     99%     2.48ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    260157.51       0.00  286938.52
  Latency      374.38us   327.90us     4.96ms
  Latency Distribution
     50%   317.00us
     75%   393.00us
     90%   500.00us
     99%     2.12ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec    142054.69   39973.21  177182.05
  Latency      610.14us   329.92us     6.23ms
  Latency Distribution
     50%   503.00us
     75%   693.00us
     90%     1.00ms
     99%     2.24ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    253674.33       0.00  283639.09
  Latency      379.96us   266.39us     5.10ms
  Latency Distribution
     50%   322.00us
     75%   424.00us
     90%   614.00us
     99%     1.69ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    221526.81   46129.18  250487.06
  Latency      456.17us   318.12us     4.88ms
  Latency Distribution
     50%   373.00us
     75%   537.00us
     90%   699.00us
     99%     2.35ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    224902.65   28152.83  248728.35
  Latency      451.20us   265.35us     3.76ms
  Latency Distribution
     50%   394.00us
     75%   512.00us
     90%   677.00us
     99%     1.55ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    248575.94       0.00  279519.38
  Latency      389.85us   378.80us     5.01ms
  Latency Distribution
     50%   287.00us
     75%   424.00us
     90%   654.00us
     99%     2.45ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    257876.98   53086.34  305046.94
  Latency      407.74us   446.27us     7.10ms
  Latency Distribution
     50%   283.00us
     75%   384.00us
     90%   660.00us
     99%     3.09ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    233659.19   31373.62  262363.82
  Latency      415.48us   459.32us     6.79ms
  Latency Distribution
     50%   315.00us
     75%   433.00us
     90%   638.00us
     99%     2.75ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     89485.84    8438.28   96011.97
  Latency        1.10ms   328.21us     5.97ms
  Latency Distribution
     50%     1.06ms
     75%     1.31ms
     90%     1.62ms
     99%     2.48ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    307622.78       0.00  322318.98
  Latency      317.58us   454.21us     9.25ms
  Latency Distribution
     50%   215.00us
     75%   290.00us
     90%   452.00us
     99%     2.79ms

### Path Parameter - int (/items/12345)
  Reqs/sec    289494.19       0.00  313954.93
  Latency      338.62us   296.92us     5.17ms
  Latency Distribution
     50%   281.00us
     75%   365.00us
     90%   486.00us
     99%     1.76ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    267795.16       0.00  296394.86
  Latency      363.84us   369.97us     5.55ms
  Latency Distribution
     50%   262.00us
     75%   391.00us
     90%   567.00us
     99%     2.23ms

### Header Parameter (/header)
  Reqs/sec    248714.27       0.00  265641.20
  Latency      387.27us   425.33us     4.69ms
  Latency Distribution
     50%   289.00us
     75%   439.00us
     90%   570.00us
     99%     3.12ms

### Cookie Parameter (/cookie)
  Reqs/sec    225696.26   22381.30  251178.51
  Latency      419.53us   460.01us     6.62ms
  Latency Distribution
     50%   303.00us
     75%   423.00us
     90%   635.00us
     99%     3.20ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    142077.38   17854.79  157286.67
  Latency      701.00us   371.51us     5.46ms
  Latency Distribution
     50%   622.00us
     75%   818.00us
     90%     1.09ms
     99%     3.04ms
