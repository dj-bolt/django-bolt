# Django-Bolt Benchmark
Generated: Sat Jul 25 10:31:00 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=100000

## Root Endpoint Performance
  Reqs/sec    328174.59   24915.64  353938.69
  Latency      303.12us   279.37us     5.19ms
  Latency Distribution
     50%   219.00us
     75%   310.00us
     90%   462.00us
     99%     2.58ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    185532.85   11218.91  199580.44
  Latency      536.62us   218.77us     5.76ms
  Latency Distribution
     50%   495.00us
     75%   625.00us
     90%   796.00us
     99%     1.67ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    200005.62   12494.81  212294.17
  Latency      497.00us   243.40us     6.47ms
  Latency Distribution
     50%   393.00us
     75%   599.00us
     90%     0.92ms
     99%     1.55ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    260500.54   14939.75  275354.07
  Latency      380.42us   219.75us     7.21ms
  Latency Distribution
     50%   330.00us
     75%   432.00us
     90%   578.00us
     99%     1.57ms
### Cookie Endpoint (/cookie)
  Reqs/sec    263409.59   17895.97  281678.89
  Latency      375.23us   273.05us     8.62ms
  Latency Distribution
     50%   306.00us
     75%   406.00us
     90%   548.00us
     99%     2.22ms
### Exception Endpoint (/exc)
  Reqs/sec    255036.68   14204.60  271731.62
  Latency      389.63us   199.03us     7.03ms
  Latency Distribution
     50%   346.00us
     75%   451.00us
     90%   615.00us
     99%     1.51ms
### HTML Response (/html)
  Reqs/sec    296464.15   25287.63  328846.20
  Latency      335.13us   301.96us     9.66ms
  Latency Distribution
     50%   252.00us
     75%   353.00us
     90%   525.00us
     99%     2.39ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     47637.20    3709.18   53201.92
  Latency        2.09ms   630.25us    13.46ms
  Latency Distribution
     50%     1.84ms
     75%     2.53ms
     90%     3.38ms
     99%     5.68ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    169808.75    9234.35  181096.51
  Latency      582.81us   157.97us     6.96ms
  Latency Distribution
     50%   576.00us
     75%   673.00us
     90%   749.00us
     99%     1.27ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    170411.59    7258.75  180589.05
  Latency      581.75us   203.22us    13.66ms
  Latency Distribution
     50%   546.00us
     75%   689.00us
     90%     0.91ms
     99%     1.34ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
 50753 / 100000 [==========================================================================================================================================================>------------------------------------------------------------------------------------------------------------------------------------------------------]  50.75% 83988/s
  Reqs/sec     85765.13    8059.26   96171.80
  Latency        1.16ms     1.05ms    43.91ms
  Latency Distribution
     50%     1.02ms
     75%     1.36ms
     90%     1.83ms
     99%     3.62ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    194960.06    6945.05  205134.60
  Latency      509.80us   133.53us     5.57ms
  Latency Distribution
     50%   512.00us
     75%   623.00us
     90%   715.00us
     99%     1.12ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    174064.91    8780.72  185047.99
  Latency      570.87us   172.66us     7.82ms
  Latency Distribution
     50%   545.00us
     75%   635.00us
     90%   746.00us
     99%     1.33ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     83018.70    7830.79   96255.92
  Latency        1.20ms     0.94ms    44.25ms
  Latency Distribution
     50%     0.99ms
     75%     1.41ms
     90%     2.06ms
     99%     4.14ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    263787.22   39647.26  309078.69
  Latency      379.24us   319.52us    14.50ms
  Latency Distribution
     50%   262.00us
     75%   400.00us
     90%   684.00us
     99%     2.38ms
### Single struct via tagged union (/bench/union-single)
 55753 / 100000 [=========================================================================================================================================================================>--------------------------------------------------------------------------------------------------------------------------------------]  55.75% 277996/s
  Reqs/sec    279059.09   19693.58  313076.69
  Latency      355.89us   297.56us     9.74ms
  Latency Distribution
     50%   250.00us
     75%   373.00us
     90%   612.00us
     99%     2.49ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     87140.38    3611.53   94825.39
  Latency        1.14ms   170.06us     5.18ms
  Latency Distribution
     50%     1.09ms
     75%     1.36ms
     90%     1.74ms
     99%     2.40ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     75996.01    9867.55   89683.00
  Latency        1.31ms   338.98us    10.04ms
  Latency Distribution
     50%     1.23ms
     75%     1.55ms
     90%     2.01ms
     99%     3.38ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec    153505.52    9079.77  170145.86
  Latency      648.23us   220.52us     5.04ms
  Latency Distribution
     50%   593.00us
     75%   732.00us
     90%     1.00ms
     99%     1.91ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     20023.94    1092.47   21232.45
  Latency        4.98ms   840.64us    21.46ms
  Latency Distribution
     50%     5.37ms
     75%     6.80ms
     90%     8.34ms
     99%     9.59ms
### Get User via Dependency (/auth/me-dependency)
 13747 / 100000 [========================================>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  13.75% 17160/s 00m05s
 40899 / 100000 [=========================================================================================================================>--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  40.90% 18540/s 00m03s
  Reqs/sec     18831.64    2087.69   21013.91
  Latency        5.30ms     3.04ms    94.22ms
  Latency Distribution
     50%     4.88ms
     75%     6.04ms
     90%     7.45ms
     99%    10.26ms
### Get Auth Context (/auth/context) validated jwt no db
 89749 / 100000 [================================================================================================================================================================================================================================================================================>-------------------------------]  89.75% 149289/s
  Reqs/sec    149604.93   10227.34  162032.71
  Latency      665.17us   229.81us     6.38ms
  Latency Distribution
     50%   582.00us
     75%   807.00us
     90%     1.03ms
     99%     1.99ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    252286.16   20237.09  285944.55
  Latency      392.71us   334.05us     9.73ms
  Latency Distribution
     50%   284.00us
     75%   408.00us
     90%   666.00us
     99%     2.62ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    244872.94   27811.93  278702.19
  Latency      404.37us   278.22us    10.78ms
  Latency Distribution
     50%   326.00us
     75%   461.00us
     90%   661.00us
     99%     2.00ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     21130.23    1464.77   22781.29
  Latency        4.72ms     2.10ms    85.19ms
  Latency Distribution
     50%     4.59ms
     75%     5.52ms
     90%     6.21ms
     99%     7.64ms
### Users Full10 (Sync) (/users/sync-full10)
 53903 / 100000 [================================================================================================================================================================>-----------------------------------------------------------------------------------------------------------------------------------------]  53.90% 15821/s 00m02s
 88503 / 100000 [=============================================================================================================================================================================================================================================================================>-----------------------------------]  88.50% 15777/s
  Reqs/sec     15747.54    1171.55   19222.73
  Latency        6.34ms     3.18ms    69.74ms
  Latency Distribution
     50%     5.23ms
     75%     7.62ms
     90%    11.11ms
     99%    20.95ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     26857.68    1124.83   30240.31
  Latency        3.72ms     1.00ms    51.44ms
  Latency Distribution
     50%     3.55ms
     75%     4.54ms
     90%     5.59ms
     99%     6.99ms
### Users Mini10 (Sync) (/users/sync-mini10)
 21502 / 100000 [================================================================>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  21.50% 17862/s 00m04s
  Reqs/sec     17806.47     821.38   19973.77
  Latency        5.61ms     2.10ms    60.82ms
  Latency Distribution
     50%     5.03ms
     75%     6.91ms
     90%     9.06ms
     99%    14.21ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    221926.04   13227.56  236409.13
  Latency      446.77us   192.15us     6.01ms
  Latency Distribution
     50%   393.00us
     75%   564.00us
     90%   704.00us
     99%     1.50ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    187655.08   22567.63  224197.28
  Latency      529.66us   288.19us     7.54ms
  Latency Distribution
     50%   416.00us
     75%   615.00us
     90%     0.92ms
     99%     2.60ms
### Items100 ViewSet GET (/cbv-items100)
 15992 / 100000 [===============================================>----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  15.99% 79425/s 00m01s
 89749 / 100000 [=================================================================================================================================================================================================================================================================================>-------------------------------]  89.75% 89507/s
  Reqs/sec     90076.90    6392.54   97369.41
  Latency        1.11ms   224.84us     6.10ms
  Latency Distribution
     50%     1.11ms
     75%     1.40ms
     90%     1.71ms
     99%     2.52ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    211037.89   18289.61  251487.60
  Latency      470.63us   246.78us     9.67ms
  Latency Distribution
     50%   410.00us
     75%   548.00us
     90%   725.00us
     99%     2.01ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    189048.42   13448.64  209782.44
  Latency      526.39us   241.25us     5.94ms
  Latency Distribution
     50%   430.00us
     75%   659.00us
     90%     0.89ms
     99%     2.08ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    192242.69   16516.20  229174.74
  Latency      516.07us   274.53us     9.32ms
  Latency Distribution
     50%   433.00us
     75%   589.00us
     90%     0.86ms
     99%     2.37ms
### CBV Response Types (/cbv-response)
  Reqs/sec    217102.44   12787.00  237974.51
  Latency      457.88us   273.61us     7.31ms
  Latency Distribution
     50%   372.00us
     75%   516.00us
     90%   755.00us
     99%     2.32ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
 54899 / 100000 [===================================================================================================================================================================>--------------------------------------------------------------------------------------------------------------------------------------]  54.90% 21085/s 00m02s
  Reqs/sec     21057.71    1911.71   23616.31
  Latency        4.75ms     2.62ms    81.63ms
  Latency Distribution
     50%     4.70ms
     75%     5.51ms
     90%     6.26ms
     99%     8.14ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    214627.02   19527.54  253961.56
  Latency      460.86us   349.56us     7.53ms
  Latency Distribution
     50%   354.00us
     75%   483.00us
     90%   765.00us
     99%     2.86ms
### File Upload (POST /upload)
  Reqs/sec    182111.58   16386.41  204079.03
  Latency      545.17us   230.61us     5.50ms
  Latency Distribution
     50%   495.00us
     75%   621.00us
     90%   847.00us
     99%     1.85ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    178015.97   11040.14  193910.37
  Latency      559.26us   197.82us     4.78ms
  Latency Distribution
     50%   507.00us
     75%   644.00us
     90%     0.86ms
     99%     1.70ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    214392.27   14122.95  236678.42
  Latency      461.38us   205.08us     6.59ms
  Latency Distribution
     50%   396.00us
     75%   547.00us
     90%   734.00us
     99%     1.56ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    160025.59    9922.09  173986.06
  Latency      619.42us   167.14us     7.41ms
  Latency Distribution
     50%   585.00us
     75%   717.00us
     90%     0.91ms
     99%     1.64ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
 4901 / 100000 [==============>---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]   4.90% 8137/s 00m11s
 52895 / 100000 [==============================================================================================================================================================>--------------------------------------------------------------------------------------------------------------------------------------------]  52.90% 9093/s 00m05s
 65987 / 100000 [=====================================================================================================================================================================================================>-----------------------------------------------------------------------------------------------------]  65.99% 9139/s 00m03s
  Reqs/sec      9093.35    1291.83   12079.35
  Latency       10.94ms     8.39ms   176.78ms
  Latency Distribution
     50%     9.44ms
     75%    13.45ms
     90%    15.03ms
     99%    19.95ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    259764.36   12542.59  282334.44
  Latency      383.27us   273.48us     7.49ms
  Latency Distribution
     50%   299.00us
     75%   410.00us
     90%   621.00us
     99%     2.17ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    233333.24   19987.10  269701.10
  Latency      422.94us   328.17us     8.32ms
  Latency Distribution
     50%   309.00us
     75%   467.00us
     90%   710.00us
     99%     2.73ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec    168853.72   11420.05  191451.32
  Latency      590.08us   208.04us     8.68ms
  Latency Distribution
     50%   535.00us
     75%   668.00us
     90%     0.93ms
     99%     1.91ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    241995.91   16117.03  269530.09
  Latency      409.55us   285.54us     6.73ms
  Latency Distribution
     50%   311.00us
     75%   438.00us
     90%   692.00us
     99%     2.39ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
 44500 / 100000 [=======================================================================================================================================>------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  44.50% 222002/s
  Reqs/sec    212132.05   26070.86  246374.74
  Latency      460.69us   236.90us     7.83ms
  Latency Distribution
     50%   389.00us
     75%   542.00us
     90%   731.00us
     99%     1.90ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    216510.11   18818.05  245043.61
  Latency      460.09us   201.28us     6.05ms
  Latency Distribution
     50%   369.00us
     75%   547.00us
     90%   847.00us
     99%     1.69ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    272297.30   18646.37  305482.66
  Latency      363.66us   282.23us     7.43ms
  Latency Distribution
     50%   284.00us
     75%   379.00us
     90%   584.00us
     99%     2.24ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    256962.64   28898.56  290415.28
  Latency      385.42us   285.79us     7.44ms
  Latency Distribution
     50%   289.00us
     75%   419.00us
     90%   667.00us
     99%     2.22ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    242191.79   28005.28  289117.37
  Latency      409.40us   330.28us     8.48ms
  Latency Distribution
     50%   292.00us
     75%   429.00us
     90%   722.00us
     99%     2.66ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     88817.36    3988.14   94393.72
  Latency        1.12ms   174.67us     6.70ms
  Latency Distribution
     50%     1.05ms
     75%     1.35ms
     90%     1.67ms
     99%     2.37ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    299114.59   21572.01  323846.10
  Latency      330.28us   300.41us     7.75ms
  Latency Distribution
     50%   226.00us
     75%   335.00us
     90%   571.00us
     99%     2.42ms

### Path Parameter - int (/items/12345)
  Reqs/sec    258960.12   29359.14  298439.83
  Latency      381.56us   270.38us     6.44ms
  Latency Distribution
     50%   289.00us
     75%   448.00us
     90%   645.00us
     99%     2.08ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    230642.85   41312.89  294303.75
  Latency      430.53us   374.71us    11.14ms
  Latency Distribution
     50%   303.00us
     75%   455.00us
     90%   765.00us
     99%     2.94ms

### Header Parameter (/header)
  Reqs/sec    245727.81   17153.41  271228.76
  Latency      401.30us   292.40us     7.02ms
  Latency Distribution
     50%   313.00us
     75%   425.00us
     90%   658.00us
     99%     2.23ms

### Cookie Parameter (/cookie)
  Reqs/sec    245519.02   21158.96  283922.94
  Latency      400.86us   245.32us     7.74ms
  Latency Distribution
     50%   313.00us
     75%   470.00us
     90%   670.00us
     99%     1.93ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    153307.02    8333.25  167304.12
  Latency      649.22us   195.38us     7.19ms
  Latency Distribution
     50%   595.00us
     75%   759.00us
     90%     1.01ms
     99%     1.80ms
