# Django-Bolt Benchmark
Generated: Tue Jul 28 02:48:58 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=100000

## Root Endpoint Performance
  Reqs/sec    311270.46   15229.41  334966.93
  Latency      318.35us   298.72us     8.02ms
  Latency Distribution
     50%   234.00us
     75%   322.00us
     90%   514.00us
     99%     2.20ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    184059.46   11896.56  206846.29
  Latency      541.06us   278.71us     9.29ms
  Latency Distribution
     50%   475.00us
     75%   622.00us
     90%   815.00us
     99%     1.78ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    187186.07   11985.51  201472.45
  Latency      531.18us   286.54us     6.78ms
  Latency Distribution
     50%   473.00us
     75%   628.00us
     90%   839.00us
     99%     2.17ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    255290.37   14995.84  279805.62
  Latency      389.40us   268.62us     6.31ms
  Latency Distribution
     50%   308.00us
     75%   437.00us
     90%   640.00us
     99%     1.91ms
### Cookie Endpoint (/cookie)
  Reqs/sec    254722.28   18674.98  274427.85
  Latency      388.63us   281.81us     9.43ms
  Latency Distribution
     50%   318.00us
     75%   420.00us
     90%   610.00us
     99%     1.95ms
### Exception Endpoint (/exc)
  Reqs/sec    247087.02   18844.94  270916.21
  Latency      399.90us   272.38us     7.50ms
  Latency Distribution
     50%   324.00us
     75%   440.00us
     90%   635.00us
     99%     1.95ms
### HTML Response (/html)
  Reqs/sec    285927.47   23311.51  323832.21
  Latency      346.67us   292.99us    10.03ms
  Latency Distribution
     50%   271.00us
     75%   361.00us
     90%   540.00us
     99%     2.13ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     47176.72    4583.32   53545.22
  Latency        2.12ms   651.27us    20.37ms
  Latency Distribution
     50%     1.93ms
     75%     2.54ms
     90%     3.32ms
     99%     5.14ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    159113.65   17928.43  180562.98
  Latency      626.42us   410.11us     9.66ms
  Latency Distribution
     50%   579.00us
     75%   721.00us
     90%   812.00us
     99%     2.94ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    169846.96    9159.42  178023.17
  Latency      586.31us   185.73us     8.35ms
  Latency Distribution
     50%   577.00us
     75%   694.00us
     90%   771.00us
     99%     1.31ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     94226.19    7083.96  102539.14
  Latency        1.06ms     1.24ms    43.24ms
  Latency Distribution
     50%     0.95ms
     75%     1.18ms
     90%     1.44ms
     99%     3.27ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    190423.35   10944.12  200879.11
  Latency      519.55us   175.24us     8.80ms
  Latency Distribution
     50%   459.00us
     75%   631.00us
     90%   805.00us
     99%     1.32ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    163667.67    8266.09  175003.60
  Latency      606.94us   166.78us     5.33ms
  Latency Distribution
     50%   557.00us
     75%   700.00us
     90%     0.92ms
     99%     1.45ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     91804.70    6989.17   99528.82
  Latency        1.08ms     1.11ms    42.91ms
  Latency Distribution
     50%     1.02ms
     75%     1.26ms
     90%     1.54ms
     99%     2.85ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    288528.01   24920.90  324376.57
  Latency      340.54us   267.74us     6.67ms
  Latency Distribution
     50%   272.00us
     75%   371.00us
     90%   528.00us
     99%     2.02ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    282390.84   19746.19  312275.85
  Latency      350.96us   261.53us     7.21ms
  Latency Distribution
     50%   274.00us
     75%   373.00us
     90%   582.00us
     99%     1.99ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     89229.38    3136.08   93048.97
  Latency        1.12ms   144.27us     4.73ms
  Latency Distribution
     50%     1.09ms
     75%     1.35ms
     90%     1.58ms
     99%     2.21ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     84308.02    3434.61   89970.80
  Latency        1.18ms   174.32us     7.05ms
  Latency Distribution
     50%     1.16ms
     75%     1.39ms
     90%     1.62ms
     99%     2.35ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec    160577.77    8252.86  171215.59
  Latency      620.07us   186.81us     5.73ms
  Latency Distribution
     50%   587.00us
     75%   724.00us
     90%     0.91ms
     99%     1.60ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
 55748 / 100000 [======================================================================================================================================================================>-----------------------------------------------------------------------------------------------------------------------------------]  55.75% 39752/s 00m01s
  Reqs/sec     40312.25    1917.56   44072.58
  Latency        2.48ms   363.30us    19.43ms
  Latency Distribution
     50%     2.31ms
     75%     2.55ms
     90%     3.75ms
     99%     4.38ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     37846.66    3383.33   44313.69
  Latency        2.64ms     2.68ms    88.75ms
  Latency Distribution
     50%     2.47ms
     75%     3.17ms
     90%     3.95ms
     99%     5.78ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    158059.52    8089.82  168260.55
  Latency      629.48us   191.51us     5.38ms
  Latency Distribution
     50%   606.00us
     75%   730.00us
     90%     0.92ms
     99%     1.61ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    264151.90   16370.58  290438.30
  Latency      376.08us   327.38us     8.47ms
  Latency Distribution
     50%   280.00us
     75%   426.00us
     90%   604.00us
     99%     2.54ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    256945.46   22501.00  282146.17
  Latency      382.60us   281.84us     8.23ms
  Latency Distribution
     50%   307.00us
     75%   405.00us
     90%   586.00us
     99%     2.15ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
 57902 / 100000 [============================================================================================================================================================================>-----------------------------------------------------------------------------------------------------------------------------]  57.90% 20643/s 00m02s
  Reqs/sec     20962.62    1697.63   22885.01
  Latency        4.77ms     2.37ms    93.40ms
  Latency Distribution
     50%     4.59ms
     75%     5.37ms
     90%     6.12ms
     99%     8.06ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     15767.24    1310.04   19188.32
  Latency        6.34ms     2.83ms    70.79ms
  Latency Distribution
     50%     5.50ms
     75%     7.84ms
     90%    10.92ms
     99%    18.06ms
### Users Mini10 (Async) (/users/mini10)
 14988 / 100000 [============================================>-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  14.99% 24946/s 00m03s
  Reqs/sec     26693.72    1169.36   28535.68
  Latency        3.74ms     1.22ms    54.56ms
  Latency Distribution
     50%     3.55ms
     75%     4.60ms
     90%     5.47ms
     99%     6.96ms
### Users Mini10 (Sync) (/users/sync-mini10)
 35896 / 100000 [==========================================================================================================>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  35.90% 17917/s 00m03s
  Reqs/sec     17899.39     978.07   20019.33
  Latency        5.58ms     2.19ms    67.57ms
  Latency Distribution
     50%     5.02ms
     75%     6.63ms
     90%     8.69ms
     99%    14.12ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    219366.82   14289.53  242382.39
  Latency      453.17us   231.76us     6.46ms
  Latency Distribution
     50%   383.00us
     75%   518.00us
     90%   721.00us
     99%     1.75ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    213514.52   19340.55  246457.04
  Latency      465.52us   228.14us     9.40ms
  Latency Distribution
     50%   407.00us
     75%   539.00us
     90%   740.00us
     99%     1.82ms
### Items100 ViewSet GET (/cbv-items100)
 18755 / 100000 [=========================================================>-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  18.75% 93240/s
  Reqs/sec     91774.51    8426.64  100204.90
  Latency        1.09ms   228.55us     8.83ms
  Latency Distribution
     50%     1.01ms
     75%     1.34ms
     90%     1.65ms
     99%     2.51ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    210608.80   12534.24  245922.90
  Latency      472.76us   264.32us     6.10ms
  Latency Distribution
     50%   410.00us
     75%   540.00us
     90%   739.00us
     99%     2.12ms
### CBV Items PUT (Update) (/cbv-items/1)
 39749 / 100000 [========================================================================================================================>---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  39.75% 198109/s
  Reqs/sec    202790.46   17221.08  227402.84
  Latency      491.42us   207.86us     6.67ms
  Latency Distribution
     50%   418.00us
     75%   568.00us
     90%   763.00us
     99%     1.76ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
 84504 / 100000 [================================================================================================================================================================================================================================================================>-----------------------------------------------]  84.50% 210613/s
  Reqs/sec    209375.89   14001.72  229862.58
  Latency      475.00us   222.23us     5.61ms
  Latency Distribution
     50%   403.00us
     75%   573.00us
     90%   760.00us
     99%     1.71ms
### CBV Response Types (/cbv-response)
  Reqs/sec    231510.91   17216.84  252384.11
  Latency      428.90us   230.68us     5.74ms
  Latency Distribution
     50%   363.00us
     75%   506.00us
     90%   689.00us
     99%     1.76ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     21188.93    1910.64   23222.70
  Latency        4.72ms     2.87ms    90.54ms
  Latency Distribution
     50%     4.52ms
     75%     5.39ms
     90%     6.22ms
     99%     7.87ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    217731.01   18560.03  253932.15
  Latency      455.60us   294.42us     6.95ms
  Latency Distribution
     50%   366.00us
     75%   495.00us
     90%   746.00us
     99%     2.40ms
### File Upload (POST /upload)
  Reqs/sec    178364.22   15555.33  207019.90
  Latency      557.51us   264.22us    12.47ms
  Latency Distribution
     50%   489.00us
     75%   651.00us
     90%     0.90ms
     99%     2.00ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    174261.41   11653.53  193104.69
  Latency      571.89us   210.50us     5.34ms
  Latency Distribution
     50%   500.00us
     75%   677.00us
     90%     0.90ms
     99%     1.80ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    208262.82   13914.47  236731.53
  Latency      477.28us   215.14us     5.22ms
  Latency Distribution
     50%   413.00us
     75%   616.00us
     90%   781.00us
     99%     1.60ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    158155.58    7089.02  168746.96
  Latency      629.42us   199.33us     8.12ms
  Latency Distribution
     50%   586.00us
     75%   761.00us
     90%     0.94ms
     99%     1.56ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
 4746 / 100000 [==============>---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]   4.75% 7888/s 00m12s
 10496 / 100000 [===============================>---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  10.50% 8719/s 00m10s
 58503 / 100000 [==============================================================================================================================================================================>----------------------------------------------------------------------------------------------------------------------------]  58.50% 8837/s 00m04s
 86753 / 100000 [===================================================================================================================================================================================================================================================================>---------------------------------------]  86.75% 8825/s 00m01s
 99899 / 100000 [==================================================================================================================================================================================================================================================================================================================]  99.90% 8737/s
  Reqs/sec      8743.76    1239.86   11349.00
  Latency       11.43ms     9.88ms   231.32ms
  Latency Distribution
     50%    10.62ms
     75%    12.31ms
     90%    13.67ms
     99%    21.20ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    251425.98   11471.55  268668.61
  Latency      394.83us   256.58us     6.93ms
  Latency Distribution
     50%   324.00us
     75%   428.00us
     90%   618.00us
     99%     2.01ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    231710.11   21657.43  263841.18
  Latency      428.89us   328.57us     9.96ms
  Latency Distribution
     50%   319.00us
     75%   447.00us
     90%   724.00us
     99%     2.71ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec    167885.88   10739.35  189020.24
  Latency      591.35us   236.61us     5.77ms
  Latency Distribution
     50%   523.00us
     75%   650.00us
     90%     0.92ms
     99%     1.92ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    230464.81   22417.83  263853.51
  Latency      428.75us   354.87us    10.03ms
  Latency Distribution
     50%   317.00us
     75%   431.00us
     90%   719.00us
     99%     2.84ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    205038.92   18137.35  236683.07
  Latency      485.65us   253.26us     6.00ms
  Latency Distribution
     50%   398.00us
     75%   570.00us
     90%   796.00us
     99%     2.03ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    210871.97   13380.97  241411.17
  Latency      471.10us   251.54us     8.95ms
  Latency Distribution
     50%   393.00us
     75%   524.00us
     90%   759.00us
     99%     2.00ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
 51749 / 100000 [=============================================================================================================================================================>--------------------------------------------------------------------------------------------------------------------------------------------------]  51.75% 258033/s
  Reqs/sec    253085.69   21964.77  286270.02
  Latency      391.98us   278.05us     9.98ms
  Latency Distribution
     50%   297.00us
     75%   421.00us
     90%   642.00us
     99%     2.33ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    245998.80   22745.38  285569.07
  Latency      403.97us   313.67us     8.72ms
  Latency Distribution
     50%   302.00us
     75%   421.00us
     90%   685.00us
     99%     2.50ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    246987.75   22863.55  279190.32
  Latency      402.75us   285.69us    10.20ms
  Latency Distribution
     50%   332.00us
     75%   439.00us
     90%   667.00us
     99%     2.15ms

### Feed of 100 mixed union items (/feed)
 16992 / 100000 [===================================================>-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  16.99% 84719/s
 68495 / 100000 [================================================================================================================================================================================================================>------------------------------------------------------------------------------------------------]  68.50% 85326/s
  Reqs/sec     86234.07    4376.31   93421.63
  Latency        1.16ms   208.48us     8.34ms
  Latency Distribution
     50%     1.11ms
     75%     1.41ms
     90%     1.71ms
     99%     2.68ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    292599.65   21640.04  331102.65
  Latency      338.78us   342.47us    13.37ms
  Latency Distribution
     50%   224.00us
     75%   328.00us
     90%   596.00us
     99%     2.49ms

### Path Parameter - int (/items/12345)
  Reqs/sec    256956.53   28024.45  293667.20
  Latency      380.74us   332.26us     7.27ms
  Latency Distribution
     50%   299.00us
     75%   403.00us
     90%   603.00us
     99%     2.39ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    240501.49   19888.25  279971.14
  Latency      411.68us   301.65us     9.22ms
  Latency Distribution
     50%   298.00us
     75%   441.00us
     90%   748.00us
     99%     2.44ms

### Header Parameter (/header)
  Reqs/sec    245686.68   16746.71  274217.91
  Latency      403.17us   306.68us     9.18ms
  Latency Distribution
     50%   311.00us
     75%   416.00us
     90%   654.00us
     99%     2.38ms

### Cookie Parameter (/cookie)
  Reqs/sec    231149.82   15711.60  258491.37
  Latency      428.31us   280.37us     7.75ms
  Latency Distribution
     50%   352.00us
     75%   494.00us
     90%   718.00us
     99%     2.15ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    148424.31    7815.76  160279.30
  Latency      669.71us   224.19us     9.12ms
  Latency Distribution
     50%   604.00us
     75%   834.00us
     90%     1.04ms
     99%     2.00ms
