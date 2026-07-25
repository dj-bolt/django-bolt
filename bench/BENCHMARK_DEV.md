# Django-Bolt Benchmark
Generated: Sat Jul 25 11:22:50 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=100000

## Root Endpoint Performance
  Reqs/sec    273817.51   44639.95  333549.37
  Latency      364.47us   389.70us    12.21ms
  Latency Distribution
     50%   231.00us
     75%   358.00us
     90%   678.00us
     99%     2.79ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    174150.55   12855.79  204588.27
  Latency      571.84us   307.55us     6.15ms
  Latency Distribution
     50%   468.00us
     75%   673.00us
     90%     0.90ms
     99%     2.69ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    189578.12   22520.44  212549.34
  Latency      523.57us   292.24us     6.92ms
  Latency Distribution
     50%   438.00us
     75%   609.00us
     90%   818.00us
     99%     2.24ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    257783.54   15667.51  284849.87
  Latency      386.76us   274.97us    11.02ms
  Latency Distribution
     50%   318.00us
     75%   420.00us
     90%   581.00us
     99%     2.10ms
### Cookie Endpoint (/cookie)
  Reqs/sec    257970.62   17689.06  279226.61
  Latency      383.48us   254.46us     7.88ms
  Latency Distribution
     50%   310.00us
     75%   412.00us
     90%   589.00us
     99%     2.09ms
### Exception Endpoint (/exc)
  Reqs/sec    263787.93   14101.61  294264.21
  Latency      375.69us   176.07us     6.09ms
  Latency Distribution
     50%   322.00us
     75%   425.00us
     90%   604.00us
     99%     1.41ms
### HTML Response (/html)
  Reqs/sec    302833.13   24232.79  340594.33
  Latency      325.73us   260.45us     7.17ms
  Latency Distribution
     50%   249.00us
     75%   406.00us
     90%   555.00us
     99%     1.75ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     46105.74    5543.40   53569.67
  Latency        2.17ms   633.97us    16.72ms
  Latency Distribution
     50%     1.96ms
     75%     2.63ms
     90%     3.47ms
     99%     5.52ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    172233.17    6031.09  179936.76
  Latency      577.78us   176.17us     5.85ms
  Latency Distribution
     50%   550.00us
     75%   646.00us
     90%   713.00us
     99%     1.13ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    172739.09    5499.15  177981.79
  Latency      575.65us   147.55us     7.27ms
  Latency Distribution
     50%   539.00us
     75%   758.00us
     90%     0.89ms
     99%     1.27ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     94484.00    5000.28   99249.30
  Latency        1.05ms     1.10ms    42.98ms
  Latency Distribution
     50%     0.99ms
     75%     1.15ms
     90%     1.35ms
     99%     2.98ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    196496.71    7911.30  207612.13
  Latency      505.97us   128.60us     8.21ms
  Latency Distribution
     50%   492.00us
     75%   673.00us
     90%   779.00us
     99%     1.13ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    163116.44    7900.86  173208.40
  Latency      610.15us   155.18us     5.16ms
  Latency Distribution
     50%   547.00us
     75%   803.00us
     90%     0.96ms
     99%     1.49ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     87992.44    6649.92   99855.97
  Latency        1.13ms     1.08ms    43.28ms
  Latency Distribution
     50%     1.00ms
     75%     1.30ms
     90%     1.78ms
     99%     3.48ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    295889.99   16109.98  319603.18
  Latency      335.22us   272.55us     7.67ms
  Latency Distribution
     50%   254.00us
     75%   376.00us
     90%   551.00us
     99%     2.07ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    300612.65   19169.21  325044.95
  Latency      330.53us   241.47us     5.10ms
  Latency Distribution
     50%   252.00us
     75%   372.00us
     90%   561.00us
     99%     1.91ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     88730.57    3786.24   94557.13
  Latency        1.12ms   160.89us     5.85ms
  Latency Distribution
     50%     1.10ms
     75%     1.32ms
     90%     1.58ms
     99%     2.24ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     82510.39    7448.04   89763.39
  Latency        1.21ms   262.88us     8.12ms
  Latency Distribution
     50%     1.09ms
     75%     1.37ms
     90%     1.95ms
     99%     2.84ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec    156694.32    8468.40  164990.22
  Latency      635.27us   200.99us     7.29ms
  Latency Distribution
     50%   590.00us
     75%   766.00us
     90%     0.93ms
     99%     1.65ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
 15503 / 100000 [==============================================>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  15.50% 38686/s 00m02s
 31898 / 100000 [===============================================================================================>----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  31.90% 39811/s 00m01s
  Reqs/sec     40315.90    1558.53   41779.65
  Latency        2.48ms   220.25us     8.42ms
  Latency Distribution
     50%     2.48ms
     75%     2.95ms
     90%     3.30ms
     99%     3.80ms
### Get User via Dependency (/auth/me-dependency)
 7904 / 100000 [=======================>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]   7.90% 39377/s 00m02s
 22748 / 100000 [===================================================================>--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  22.75% 37831/s 00m02s
  Reqs/sec     39429.19    3525.68   46848.99
  Latency        2.53ms     2.28ms    77.53ms
  Latency Distribution
     50%     2.33ms
     75%     2.94ms
     90%     3.61ms
     99%     5.23ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    151414.97   15828.53  165758.13
  Latency      657.36us   250.89us     7.31ms
  Latency Distribution
     50%   608.00us
     75%   698.00us
     90%     0.91ms
     99%     2.08ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    290380.40   16774.78  311227.04
  Latency      342.53us   269.49us     8.70ms
  Latency Distribution
     50%   271.00us
     75%   365.00us
     90%   523.00us
     99%     2.12ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    254691.88   19768.84  284320.81
  Latency      387.59us   260.97us     5.97ms
  Latency Distribution
     50%   307.00us
     75%   404.00us
     90%   624.00us
     99%     2.15ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     20460.64    1188.82   23512.14
  Latency        4.88ms     2.11ms    79.14ms
  Latency Distribution
     50%     4.63ms
     75%     5.82ms
     90%     7.29ms
     99%     9.85ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     16000.88    1260.42   19434.82
  Latency        6.24ms     2.70ms    94.67ms
  Latency Distribution
     50%     5.58ms
     75%     7.74ms
     90%    10.41ms
     99%    16.62ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     26407.25    1070.18   27866.51
  Latency        3.78ms     1.24ms    53.32ms
  Latency Distribution
     50%     3.64ms
     75%     4.50ms
     90%     5.43ms
     99%     7.14ms
### Users Mini10 (Sync) (/users/sync-mini10)
 3502 / 100000 [==========>------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]   3.50% 17460/s 00m05s
 98897 / 100000 [=============================================================================================================================================================================================================================================================================================================>---]  98.90% 17632/s
  Reqs/sec     17631.31     836.45   19712.84
  Latency        5.67ms     1.96ms    58.06ms
  Latency Distribution
     50%     5.11ms
     75%     6.89ms
     90%     8.94ms
     99%    14.46ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    209812.30   13067.24  240229.36
  Latency      473.72us   260.38us     8.56ms
  Latency Distribution
     50%   393.00us
     75%   531.00us
     90%   774.00us
     99%     1.99ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    209277.67   14686.23  239492.72
  Latency      475.48us   222.31us     6.24ms
  Latency Distribution
     50%   415.00us
     75%   546.00us
     90%   761.00us
     99%     1.82ms
### Items100 ViewSet GET (/cbv-items100)
 94746 / 100000 [================================================================================================================================================================================================================================================================================================>----------------]  94.75% 94515/s
  Reqs/sec     94733.07    3883.63  100507.74
  Latency        1.05ms   147.90us     6.07ms
  Latency Distribution
     50%     1.02ms
     75%     1.22ms
     90%     1.52ms
     99%     2.16ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    206526.15   17688.25  250021.49
  Latency      479.59us   252.67us     9.01ms
  Latency Distribution
     50%   415.00us
     75%   548.00us
     90%   764.00us
     99%     2.09ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    192511.29   15982.03  214191.34
  Latency      516.76us   240.48us     8.03ms
  Latency Distribution
     50%   444.00us
     75%   602.00us
     90%   843.00us
     99%     1.87ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    206213.98   14565.33  237406.73
  Latency      482.14us   231.85us     5.96ms
  Latency Distribution
     50%   422.00us
     75%   566.00us
     90%   758.00us
     99%     1.76ms
### CBV Response Types (/cbv-response)
  Reqs/sec    213862.20   13423.49  235239.63
  Latency      465.29us   258.05us     6.09ms
  Latency Distribution
     50%   385.00us
     75%   526.00us
     90%   780.00us
     99%     2.07ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     21014.81    2049.21   23270.84
  Latency        4.76ms     2.67ms    90.37ms
  Latency Distribution
     50%     4.58ms
     75%     5.45ms
     90%     6.26ms
     99%     8.20ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    221196.17   29464.86  256012.37
  Latency      439.81us   219.95us     5.84ms
  Latency Distribution
     50%   373.00us
     75%   545.00us
     90%   737.00us
     99%     1.77ms
### File Upload (POST /upload)
  Reqs/sec    184857.40   11608.99  200848.98
  Latency      537.42us   204.53us     7.45ms
  Latency Distribution
     50%   469.00us
     75%   643.00us
     90%   838.00us
     99%     1.65ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    176517.38   10858.20  191569.51
  Latency      564.15us   174.93us     9.29ms
  Latency Distribution
     50%   523.00us
     75%   732.00us
     90%     0.89ms
     99%     1.58ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    206353.48   14765.98  228583.56
  Latency      482.04us   221.55us     5.39ms
  Latency Distribution
     50%   419.00us
     75%   552.00us
     90%   762.00us
     99%     1.85ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    157316.82    8186.58  169390.16
  Latency      632.70us   196.11us     7.91ms
  Latency Distribution
     50%   584.00us
     75%   746.00us
     90%     0.95ms
     99%     1.61ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
 1503 / 100000 [====>-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]   1.50% 7497/s 00m13s
 4991 / 100000 [==============>---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]   4.99% 8305/s 00m11s
 55904 / 100000 [=======================================================================================================================================================================>-----------------------------------------------------------------------------------------------------------------------------------]  55.90% 9295/s 00m04s
 82498 / 100000 [======================================================================================================================================================================================================================================================>----------------------------------------------------]  82.50% 9352/s 00m01s
  Reqs/sec      9304.03    1194.41   12119.58
  Latency       10.66ms     8.18ms   170.63ms
  Latency Distribution
     50%    10.02ms
     75%    12.06ms
     90%    16.69ms
     99%    19.52ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    251126.50   12441.54  277740.35
  Latency      395.36us   278.47us     6.68ms
  Latency Distribution
     50%   302.00us
     75%   438.00us
     90%   669.00us
     99%     2.19ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    223197.92   17620.20  251941.28
  Latency      443.42us   333.37us     6.92ms
  Latency Distribution
     50%   323.00us
     75%   473.00us
     90%   767.00us
     99%     2.86ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec    168511.92   14131.63  187318.08
  Latency      590.20us   201.21us     5.62ms
  Latency Distribution
     50%   535.00us
     75%   699.00us
     90%     0.92ms
     99%     1.81ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    242111.60   15446.62  261342.39
  Latency      409.19us   258.81us     7.14ms
  Latency Distribution
     50%   335.00us
     75%   459.00us
     90%   669.00us
     99%     2.05ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    209205.41   18458.86  245891.37
  Latency      475.60us   217.95us     7.08ms
  Latency Distribution
     50%   423.00us
     75%   586.00us
     90%   768.00us
     99%     1.72ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    211280.79   13394.55  235507.90
  Latency      469.90us   219.73us     6.63ms
  Latency Distribution
     50%   394.00us
     75%   560.00us
     90%   775.00us
     99%     1.83ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    266880.38   15626.28  298651.10
  Latency      372.00us   256.33us     6.48ms
  Latency Distribution
     50%   297.00us
     75%   397.00us
     90%   601.00us
     99%     2.08ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    238266.92   30879.47  289372.29
  Latency      416.14us   306.29us     6.87ms
  Latency Distribution
     50%   305.00us
     75%   450.00us
     90%   739.00us
     99%     2.38ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    266297.39   17860.97  298670.67
  Latency      372.45us   261.07us     9.74ms
  Latency Distribution
     50%   304.00us
     75%   413.00us
     90%   575.00us
     99%     2.07ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     87099.17    5556.87   95860.66
  Latency        1.14ms   186.51us     5.77ms
  Latency Distribution
     50%     1.07ms
     75%     1.39ms
     90%     1.71ms
     99%     2.45ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    267313.70   33644.19  329288.77
  Latency      371.72us   354.65us     6.83ms
  Latency Distribution
     50%   229.00us
     75%   362.00us
     90%   684.00us
     99%     2.84ms

### Path Parameter - int (/items/12345)
  Reqs/sec    253242.33   22826.06  292931.36
  Latency      390.79us   299.26us     6.42ms
  Latency Distribution
     50%   291.00us
     75%   408.00us
     90%   662.00us
     99%     2.40ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    250652.40   20883.23  282776.63
  Latency      394.48us   300.44us     6.90ms
  Latency Distribution
     50%   287.00us
     75%   412.00us
     90%   678.00us
     99%     2.51ms

### Header Parameter (/header)
  Reqs/sec    228306.85   20534.75  261124.54
  Latency      434.02us   285.81us     5.75ms
  Latency Distribution
     50%   326.00us
     75%   476.00us
     90%   752.00us
     99%     2.45ms

### Cookie Parameter (/cookie)
  Reqs/sec    233070.81   18038.45  264355.92
  Latency      425.77us   264.67us     6.07ms
  Latency Distribution
     50%   336.00us
     75%   471.00us
     90%   720.00us
     99%     2.17ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    147695.79    8190.19  160539.07
  Latency      673.85us   223.74us     7.72ms
  Latency Distribution
     50%   595.00us
     75%   805.00us
     90%     1.05ms
     99%     2.02ms
