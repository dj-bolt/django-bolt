# Django-Bolt Benchmark
Generated: Sun Jul 19 02:49:02 AM PKT 2026
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    206474.30   16765.41  225772.54
  Latency      460.45us   203.56us     4.30ms
  Latency Distribution
     50%   437.00us
     75%   537.00us
     90%   644.00us
     99%     1.33ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    130503.64   15780.05  143303.77
  Latency      754.68us   319.04us     6.69ms
  Latency Distribution
     50%   735.00us
     75%     0.87ms
     90%     1.07ms
     99%     2.38ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    134476.09   14057.07  146599.78
  Latency      732.31us   375.96us     5.77ms
  Latency Distribution
     50%   673.00us
     75%   835.00us
     90%     1.08ms
     99%     2.44ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    144782.15   31818.59  169176.49
  Latency      609.77us   233.19us     4.58ms
  Latency Distribution
     50%   543.00us
     75%   724.00us
     90%     0.92ms
     99%     1.81ms
### Cookie Endpoint (/cookie)
  Reqs/sec    159630.72   14037.86  168500.96
  Latency      622.21us   359.88us     6.03ms
  Latency Distribution
     50%   566.00us
     75%   711.00us
     90%     0.93ms
     99%     2.49ms
### Exception Endpoint (/exc)
  Reqs/sec    162908.24   20109.41  191023.83
  Latency      629.02us   229.90us     4.83ms
  Latency Distribution
     50%   586.00us
     75%   718.00us
     90%     0.95ms
     99%     1.56ms
### HTML Response (/html)
  Reqs/sec    191417.13   22603.42  205984.31
  Latency      512.81us   216.40us     5.64ms
  Latency Distribution
     50%   461.00us
     75%   631.00us
     90%   784.00us
     99%     1.38ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     42223.68    8680.78   54533.85
  Latency        2.41ms     1.30ms    16.93ms
  Latency Distribution
     50%     2.05ms
     75%     2.90ms
     90%     3.93ms
     99%     7.44ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    157067.08   21931.13  176203.69
  Latency      609.95us   291.87us     4.21ms
  Latency Distribution
     50%   582.00us
     75%   712.00us
     90%     0.86ms
     99%     2.17ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    175753.27   13509.95  184577.63
  Latency      558.15us   254.93us     4.29ms
  Latency Distribution
     50%   545.00us
     75%   588.00us
     90%   725.00us
     99%     1.62ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     84233.79   18771.17   98126.76
  Latency        1.09ms     3.31ms    42.79ms
  Latency Distribution
     50%   745.00us
     75%     1.02ms
     90%     1.27ms
     99%     4.42ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    198424.65   20010.14  213724.70
  Latency      493.25us   228.39us     4.26ms
  Latency Distribution
     50%   448.00us
     75%   570.00us
     90%   687.00us
     99%     1.40ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    170840.65   18625.61  181757.18
  Latency      571.96us   246.81us     4.98ms
  Latency Distribution
     50%   524.00us
     75%   688.00us
     90%   736.00us
     99%     1.41ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     61326.89   40433.36   95999.73
  Latency        1.15ms     2.78ms    44.41ms
  Latency Distribution
     50%   848.00us
     75%     1.14ms
     90%     1.51ms
     99%     5.40ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    199492.71   20721.57  218951.70
  Latency      483.13us   268.29us    10.49ms
  Latency Distribution
     50%   458.00us
     75%   573.00us
     90%   662.00us
     99%     1.36ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    203877.63   16791.08  218034.27
  Latency      474.07us   170.78us     4.15ms
  Latency Distribution
     50%   422.00us
     75%   496.00us
     90%   828.00us
     99%     1.30ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     77161.47    4862.98   80441.70
  Latency        1.28ms   320.07us     5.08ms
  Latency Distribution
     50%     1.26ms
     75%     1.45ms
     90%     1.71ms
     99%     2.59ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     73890.59    4634.62   77499.91
  Latency        1.33ms   390.62us     5.80ms
  Latency Distribution
     50%     1.36ms
     75%     1.55ms
     90%     1.73ms
     99%     2.74ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec    137105.15   17964.10  146839.44
  Latency      720.56us   550.76us    10.36ms
  Latency Distribution
     50%   647.00us
     75%   841.00us
     90%     1.04ms
     99%     1.65ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     20201.13    1370.93   20863.20
  Latency        4.91ms     0.85ms    11.44ms
  Latency Distribution
     50%     5.05ms
     75%     5.87ms
     90%     6.35ms
     99%     7.27ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     16530.27    2652.41   19645.83
  Latency        6.01ms     6.47ms    77.26ms
  Latency Distribution
     50%     5.11ms
     75%     6.71ms
     90%     8.07ms
     99%    13.06ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    139372.23   10612.65  150008.22
  Latency      699.11us   231.54us     3.87ms
  Latency Distribution
     50%   642.00us
     75%     0.85ms
     90%     1.10ms
     99%     1.71ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    186147.77   13989.37  196609.13
  Latency      524.76us   151.95us     3.74ms
  Latency Distribution
     50%   565.00us
     75%   650.00us
     90%   776.00us
     99%     1.10ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    170735.25   17962.97  181358.29
  Latency      572.25us   214.74us     6.05ms
  Latency Distribution
     50%   571.00us
     75%   666.00us
     90%   763.00us
     99%     1.57ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     11547.95    1929.96   14976.24
  Latency        8.65ms     7.38ms    90.45ms
  Latency Distribution
     50%     7.11ms
     75%    10.36ms
     90%    14.67ms
     99%    24.97ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     11758.09     916.54   13852.77
  Latency        8.47ms     5.54ms    91.13ms
  Latency Distribution
     50%     6.81ms
     75%    10.64ms
     90%    15.88ms
     99%    24.54ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     16737.55    1213.17   20645.20
  Latency        5.98ms     2.24ms    19.39ms
  Latency Distribution
     50%     5.65ms
     75%     7.38ms
     90%     9.33ms
     99%    13.58ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     16734.92    1645.90   24568.33
  Latency        6.02ms     2.04ms    17.99ms
  Latency Distribution
     50%     5.72ms
     75%     7.34ms
     90%     9.11ms
     99%    12.81ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    188698.07   19448.56  201393.96
  Latency      521.32us   168.17us     2.54ms
  Latency Distribution
     50%   473.00us
     75%   631.00us
     90%   814.00us
     99%     1.36ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    182756.70   16223.31  194247.97
  Latency      536.79us   162.71us     2.09ms
  Latency Distribution
     50%   481.00us
     75%   669.00us
     90%     0.89ms
     99%     1.38ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     93228.94    5907.86   96876.72
  Latency        1.06ms   314.46us     4.27ms
  Latency Distribution
     50%     1.03ms
     75%     1.30ms
     90%     1.64ms
     99%     2.45ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    186775.73   15533.20  198300.06
  Latency      527.44us   152.19us     2.29ms
  Latency Distribution
     50%   482.00us
     75%   650.00us
     90%   826.00us
     99%     1.29ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    180163.93   17277.98  191857.27
  Latency      542.33us   203.54us     3.34ms
  Latency Distribution
     50%   493.00us
     75%   658.00us
     90%   824.00us
     99%     1.45ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    179843.32   17104.33  189897.59
  Latency      542.39us   247.63us     4.52ms
  Latency Distribution
     50%   492.00us
     75%   647.00us
     90%   824.00us
     99%     1.35ms
### CBV Response Types (/cbv-response)
  Reqs/sec    197406.60   20193.18  211008.63
  Latency      502.29us   150.61us     2.88ms
  Latency Distribution
     50%   462.00us
     75%   610.00us
     90%   776.00us
     99%     1.25ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
 3850 / 10000 [===============================================================================================================================>----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  38.50% 19211/s
  Reqs/sec     18183.10    2494.02   20775.25
  Latency        5.48ms     5.94ms    72.69ms
  Latency Distribution
     50%     4.73ms
     75%     6.06ms
     90%     7.53ms
     99%    11.96ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    153655.41   10025.35  160015.66
  Latency      642.26us   218.49us     3.32ms
  Latency Distribution
     50%   603.00us
     75%   739.00us
     90%     0.92ms
     99%     1.65ms
### File Upload (POST /upload)
  Reqs/sec    127880.97   13883.78  137785.93
  Latency      766.85us   214.67us     3.28ms
  Latency Distribution
     50%   736.00us
     75%     0.92ms
     90%     1.17ms
     99%     1.73ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    107012.05   35156.78  129209.82
  Latency      803.29us   313.08us     5.81ms
  Latency Distribution
     50%   742.00us
     75%     0.97ms
     90%     1.20ms
     99%     1.84ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    142505.87   17423.27  154465.05
  Latency      694.38us   264.58us     4.34ms
  Latency Distribution
     50%   667.00us
     75%   805.00us
     90%     1.03ms
     99%     1.57ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    113873.75    9318.33  119738.91
  Latency        0.86ms   298.04us     5.22ms
  Latency Distribution
     50%   822.00us
     75%     1.03ms
     90%     1.26ms
     99%     1.94ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9520.69    2577.05   25261.30
  Latency       10.78ms     7.15ms    85.36ms
  Latency Distribution
     50%    10.08ms
     75%    12.22ms
     90%    14.66ms
     99%    22.43ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    165279.51   17198.31  179758.14
  Latency      589.16us   259.10us     6.18ms
  Latency Distribution
     50%   533.00us
     75%   685.00us
     90%     0.88ms
     99%     1.97ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    162053.03    7600.55  167298.39
  Latency      596.11us   291.18us     5.86ms
  Latency Distribution
     50%   540.00us
     75%   687.00us
     90%     0.89ms
     99%     1.75ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec    161284.17   18765.55  174971.86
  Latency      622.74us   212.34us     2.74ms
  Latency Distribution
     50%   561.00us
     75%   758.00us
     90%     1.00ms
     99%     1.70ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    167462.36   18704.24  178472.76
  Latency      585.43us   283.87us     5.10ms
  Latency Distribution
     50%   536.00us
     75%   644.00us
     90%   790.00us
     99%     1.88ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    185373.22   17806.44  196389.47
  Latency      530.83us   179.26us     2.73ms
  Latency Distribution
     50%   472.00us
     75%   647.00us
     90%   846.00us
     99%     1.40ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    183886.12   20097.60  195599.57
  Latency      535.10us   187.44us     2.62ms
  Latency Distribution
     50%   484.00us
     75%   640.00us
     90%   830.00us
     99%     1.51ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    173219.92   16186.32  183372.52
  Latency      559.78us   266.98us     4.11ms
  Latency Distribution
     50%   482.00us
     75%   682.00us
     90%     0.88ms
     99%     2.24ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    168206.80   19479.12  181084.06
  Latency      581.59us   289.56us     7.41ms
  Latency Distribution
     50%   531.00us
     75%   693.00us
     90%     0.86ms
     99%     1.57ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    171495.37   16584.27  181880.99
  Latency      568.49us   268.61us     6.99ms
  Latency Distribution
     50%   538.00us
     75%   638.00us
     90%   800.00us
     99%     1.54ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     77138.81    3844.83   81745.43
  Latency        1.28ms   340.93us     8.61ms
  Latency Distribution
     50%     1.24ms
     75%     1.61ms
     90%     1.81ms
     99%     2.53ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    203793.25   19520.10  222664.19
  Latency      482.81us   254.86us     6.61ms
  Latency Distribution
     50%   452.00us
     75%   527.00us
     90%   662.00us
     99%     1.76ms

### Path Parameter - int (/items/12345)
  Reqs/sec    175694.65   17398.73  186548.68
  Latency      557.31us   187.72us     4.38ms
  Latency Distribution
     50%   507.00us
     75%   654.00us
     90%   832.00us
     99%     1.26ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    169344.24   13626.02  177942.75
  Latency      576.46us   229.90us     3.69ms
  Latency Distribution
     50%   537.00us
     75%   693.00us
     90%     0.87ms
     99%     1.82ms

### Header Parameter (/header)
  Reqs/sec    161586.69   15529.34  172115.48
  Latency      602.14us   265.59us     7.39ms
  Latency Distribution
     50%   547.00us
     75%   755.00us
     90%     0.92ms
     99%     1.68ms

### Cookie Parameter (/cookie)
  Reqs/sec    145022.83   30537.82  169521.21
  Latency      612.00us   232.70us     5.04ms
  Latency Distribution
     50%   562.00us
     75%   717.00us
     90%     0.91ms
     99%     1.58ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    137911.58   10132.01  147641.74
  Latency      716.82us   295.91us     4.33ms
  Latency Distribution
     50%   648.00us
     75%     0.86ms
     90%     1.10ms
     99%     2.08ms
