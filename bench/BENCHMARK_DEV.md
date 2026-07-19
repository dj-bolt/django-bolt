# Django-Bolt Benchmark
Generated: Mon Jul 20 02:06:21 AM PKT 2026
Config: 8 processes × 1 workers | C=100 N=100000

## Root Endpoint Performance
  Reqs/sec    349715.86   20091.02  370453.71
  Latency      284.34us   280.11us     6.29ms
  Latency Distribution
     50%   198.00us
     75%   279.00us
     90%   463.00us
     99%     2.16ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    193438.50   11628.13  208239.91
  Latency      514.35us   235.62us     9.29ms
  Latency Distribution
     50%   474.00us
     75%   573.00us
     90%   758.00us
     99%     1.64ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    210752.29   15853.21  239819.69
  Latency      473.21us   236.14us    12.53ms
  Latency Distribution
     50%   419.00us
     75%   541.00us
     90%   705.00us
     99%     1.50ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    265251.04   47211.63  291063.32
  Latency      360.53us   274.62us     6.50ms
  Latency Distribution
     50%   281.00us
     75%   404.00us
     90%   566.00us
     99%     2.05ms
### Cookie Endpoint (/cookie)
  Reqs/sec    276614.88   17558.37  298313.66
  Latency      358.07us   224.18us     6.44ms
  Latency Distribution
     50%   300.00us
     75%   432.00us
     90%   550.00us
     99%     1.53ms
### Exception Endpoint (/exc)
  Reqs/sec    269708.68   14355.57  290915.10
  Latency      367.09us   205.80us     6.42ms
  Latency Distribution
     50%   316.00us
     75%   424.00us
     90%   581.00us
     99%     1.58ms
### HTML Response (/html)
  Reqs/sec    315751.84   24689.37  341517.95
  Latency      314.11us   247.48us     6.45ms
  Latency Distribution
     50%   247.00us
     75%   335.00us
     90%   491.00us
     99%     1.83ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     46982.45    4518.11   53021.55
  Latency        2.13ms   555.25us    22.61ms
  Latency Distribution
     50%     1.96ms
     75%     2.56ms
     90%     3.27ms
     99%     4.92ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    168434.26   18266.01  185938.98
  Latency      590.72us   287.99us     8.49ms
  Latency Distribution
     50%   546.00us
     75%   629.00us
     90%   766.00us
     99%     2.00ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    183091.30   10092.30  202097.03
  Latency      542.85us   180.22us     9.28ms
  Latency Distribution
     50%   513.00us
     75%   581.00us
     90%   655.00us
     99%     1.12ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
 96897 / 100000 [=============================================================================================================================================================================================================================================================================================================================>----------]  96.90% 96391/s
  Reqs/sec     96463.13    6684.76  104334.24
  Latency        1.03ms     1.14ms    43.92ms
  Latency Distribution
     50%     0.96ms
     75%     1.11ms
     90%     1.46ms
     99%     2.58ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    196227.02   12243.30  212843.58
  Latency      506.17us   253.46us    10.20ms
  Latency Distribution
     50%   440.00us
     75%   618.00us
     90%   754.00us
     99%     1.49ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    172205.05   12271.39  189232.37
  Latency      578.36us   226.96us     5.76ms
  Latency Distribution
     50%   550.00us
     75%   656.00us
     90%   823.00us
     99%     1.56ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     93394.97    7176.60  100200.74
  Latency        1.07ms     0.86ms    44.24ms
  Latency Distribution
     50%     0.97ms
     75%     1.24ms
     90%     1.59ms
     99%     2.78ms

## Union Response Overhead
### Single struct, no union (/bench/single)
 60990 / 100000 [=======================================================================================================================================================================================================>-------------------------------------------------------------------------------------------------------------------------------]  60.99% 303979/s
  Reqs/sec    312531.69   24698.28  351146.98
  Latency      319.71us   277.87us     8.20ms
  Latency Distribution
     50%   244.00us
     75%   348.00us
     90%   524.00us
     99%     1.99ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    324787.64   17705.41  345999.18
  Latency      305.86us   244.96us     6.05ms
  Latency Distribution
     50%   234.00us
     75%   327.00us
     90%   499.00us
     99%     1.87ms
### List of 100 structs, no union (/bench/list)
 35751 / 100000 [=====================================================================================================================>------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  35.75% 89222/s
  Reqs/sec     90799.29    3549.36   99861.26
  Latency        1.10ms   143.84us     4.85ms
  Latency Distribution
     50%     1.07ms
     75%     1.31ms
     90%     1.61ms
     99%     2.22ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     86429.97    3442.18   92472.47
  Latency        1.15ms   157.09us     4.95ms
  Latency Distribution
     50%     1.07ms
     75%     1.37ms
     90%     1.74ms
     99%     2.36ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec    158045.88    9274.65  167799.40
  Latency      629.93us   175.15us     5.04ms
  Latency Distribution
     50%   544.00us
     75%   795.00us
     90%     1.03ms
     99%     1.63ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
 7904 / 100000 [=========================>--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]   7.90% 19722/s 00m04s
 60895 / 100000 [===================================================================================================================================================================================================>-----------------------------------------------------------------------------------------------------------------------------]  60.90% 20276/s 00m01s
 68752 / 100000 [============================================================================================================================================================================================================================>----------------------------------------------------------------------------------------------------]  68.75% 20199/s 00m01s
  Reqs/sec     20031.34    1076.49   20936.98
  Latency        4.99ms   474.29us    16.91ms
  Latency Distribution
     50%     5.13ms
     75%     6.04ms
     90%     6.44ms
     99%     7.58ms
### Get User via Dependency (/auth/me-dependency)
 49986 / 100000 [================================================================================================================================================================>----------------------------------------------------------------------------------------------------------------------------------------------------------------]  49.99% 19188/s 00m02s
  Reqs/sec     19234.28    1324.77   20745.32
  Latency        5.19ms     2.67ms    81.56ms
  Latency Distribution
     50%     4.87ms
     75%     6.63ms
     90%     7.89ms
     99%     9.90ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    153582.94    6805.90  168379.59
  Latency      647.91us   163.95us     5.20ms
  Latency Distribution
     50%   576.00us
     75%   794.00us
     90%     1.04ms
     99%     1.63ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    294725.87   25063.38  320590.06
  Latency      336.52us   297.94us     7.99ms
  Latency Distribution
     50%   249.00us
     75%   345.00us
     90%   521.00us
     99%     2.35ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    271638.25   17442.00  291518.32
  Latency      364.46us   268.44us     9.33ms
  Latency Distribution
     50%   298.00us
     75%   389.00us
     90%   564.00us
     99%     1.77ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     20766.73    1170.51   22281.26
  Latency        4.81ms     1.97ms    75.87ms
  Latency Distribution
     50%     4.71ms
     75%     5.61ms
     90%     6.51ms
     99%     8.46ms
### Users Full10 (Sync) (/users/sync-full10)
 9993 / 100000 [================================>-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]   9.99% 16595/s 00m05s
  Reqs/sec     15777.85    1190.29   19578.57
  Latency        6.34ms     2.45ms    68.56ms
  Latency Distribution
     50%     5.62ms
     75%     7.78ms
     90%    10.49ms
     99%    16.26ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     26868.01     778.25   28109.35
  Latency        3.72ms     0.88ms    52.08ms
  Latency Distribution
     50%     3.79ms
     75%     4.47ms
     90%     5.10ms
     99%     6.22ms
### Users Mini10 (Sync) (/users/sync-mini10)
 3502 / 100000 [===========>----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]   3.50% 17471/s 00m05s
 28745 / 100000 [============================================================================================>------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  28.75% 17937/s 00m03s
 39501 / 100000 [==============================================================================================================================>--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  39.50% 17922/s 00m03s
  Reqs/sec     17948.17     931.46   21162.52
  Latency        5.57ms     1.77ms    54.63ms
  Latency Distribution
     50%     5.18ms
     75%     6.67ms
     90%     8.38ms
     99%    12.43ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    239104.90   15462.40  261432.82
  Latency      415.86us   196.65us     6.83ms
  Latency Distribution
     50%   369.00us
     75%   478.00us
     90%   633.00us
     99%     1.48ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    246963.44   14194.39  267085.09
  Latency      402.64us   144.20us     7.11ms
  Latency Distribution
     50%   353.00us
     75%   497.00us
     90%   634.00us
     99%     1.11ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     97466.25    2982.33  102506.82
  Latency        1.02ms   164.20us     5.61ms
  Latency Distribution
     50%     1.00ms
     75%     1.23ms
     90%     1.60ms
     99%     2.17ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    249681.27   14242.05  265867.12
  Latency      397.27us   175.17us     6.62ms
  Latency Distribution
     50%   366.00us
     75%   450.00us
     90%   572.00us
     99%     1.15ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    229886.82    9906.21  244033.00
  Latency      432.06us   136.40us     4.89ms
  Latency Distribution
     50%   415.00us
     75%   509.00us
     90%   614.00us
     99%     1.10ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    235052.01   12483.96  245033.39
  Latency      420.23us   153.38us     5.03ms
  Latency Distribution
     50%   372.00us
     75%   533.00us
     90%   669.00us
     99%     1.16ms
### CBV Response Types (/cbv-response)
  Reqs/sec    262271.21   15163.78  286055.44
  Latency      378.90us   181.34us     5.08ms
  Latency Distribution
     50%   321.00us
     75%   439.00us
     90%   600.00us
     99%     1.43ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
 7754 / 100000 [========================>---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]   7.75% 19340/s 00m04s
  Reqs/sec     21369.62    1652.80   23102.32
  Latency        4.67ms     2.46ms    79.84ms
  Latency Distribution
     50%     4.53ms
     75%     5.47ms
     90%     6.45ms
     99%     8.06ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    262517.44   15545.07  277688.32
  Latency      377.99us   148.41us     4.88ms
  Latency Distribution
     50%   337.00us
     75%   438.00us
     90%   593.00us
     99%     1.29ms
### File Upload (POST /upload)
  Reqs/sec    209323.38    9800.67  225169.74
  Latency      475.34us   190.36us    13.02ms
  Latency Distribution
     50%   441.00us
     75%   525.00us
     90%   634.00us
     99%     1.11ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    197193.15    9648.27  207085.54
  Latency      503.83us   110.86us     5.69ms
  Latency Distribution
     50%   458.00us
     75%   666.00us
     90%   798.00us
     99%     1.21ms
### Form Repeated Keys urlencoded (POST /form-list)
 48499 / 100000 [==============================================================================================================================================================>------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  48.50% 241840/s
  Reqs/sec    242225.67   13605.28  257270.56
  Latency      410.63us   164.54us     6.97ms
  Latency Distribution
     50%   373.00us
     75%   473.00us
     90%   600.00us
     99%     1.11ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    172426.69    7195.20  180505.22
  Latency      577.31us   127.64us     6.36ms
  Latency Distribution
     50%   566.00us
     75%   691.00us
     90%   825.00us
     99%     1.25ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
 14898 / 100000 [===============================================>----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  14.90% 9291/s 00m09s
 48994 / 100000 [=============================================================================================================================================================>--------------------------------------------------------------------------------------------------------------------------------------------------------------------]  48.99% 9398/s 00m05s
  Reqs/sec      9429.84    1194.77   11711.38
  Latency       10.60ms     8.05ms   169.35ms
  Latency Distribution
     50%    10.66ms
     75%    11.62ms
     90%    12.53ms
     99%    16.01ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    292791.66   11091.69  306141.77
  Latency      338.82us   212.85us     7.32ms
  Latency Distribution
     50%   294.00us
     75%   405.00us
     90%   527.00us
     99%     1.65ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    279099.41   12949.58  292688.02
  Latency      355.81us   236.25us     6.23ms
  Latency Distribution
     50%   293.00us
     75%   392.00us
     90%   539.00us
     99%     1.80ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec    188589.53   18893.72  199818.49
  Latency      518.41us   131.87us     5.08ms
  Latency Distribution
     50%   502.00us
     75%   600.00us
     90%   717.00us
     99%     1.20ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    288520.23   16809.57  309594.78
  Latency      343.30us   226.57us     6.53ms
  Latency Distribution
     50%   285.00us
     75%   373.00us
     90%   512.00us
     99%     1.69ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    253997.01   13636.27  267171.55
  Latency      390.15us   165.17us     6.86ms
  Latency Distribution
     50%   363.00us
     75%   451.00us
     90%   555.00us
     99%     1.04ms

### Multi-response bare dict (/bench/multi/dict)
 48992 / 100000 [================================================================================================================================================================>----------------------------------------------------------------------------------------------------------------------------------------------------------------------]  48.99% 244428/s
  Reqs/sec    245967.35   16931.91  270864.55
  Latency      402.76us   140.06us     6.25ms
  Latency Distribution
     50%   373.00us
     75%   492.00us
     90%   601.00us
     99%     1.12ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    313573.10   13559.71  333910.43
  Latency      316.60us   275.86us     7.99ms
  Latency Distribution
     50%   255.00us
     75%   333.00us
     90%   464.00us
     99%     1.98ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    305546.88   13275.57  321190.97
  Latency      324.76us   240.30us    11.21ms
  Latency Distribution
     50%   266.00us
     75%   361.00us
     90%   498.00us
     99%     1.85ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    303792.26   16427.09  326043.91
  Latency      326.19us   237.77us     8.18ms
  Latency Distribution
     50%   268.00us
     75%   354.00us
     90%   497.00us
     99%     1.69ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     92826.29    3007.36   99395.84
  Latency        1.07ms   134.85us     4.97ms
  Latency Distribution
     50%     1.08ms
     75%     1.30ms
     90%     1.63ms
     99%     2.19ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    357129.10   15934.64  369380.79
  Latency      277.63us   243.74us     7.12ms
  Latency Distribution
     50%   209.00us
     75%   300.00us
     90%   456.00us
     99%     1.59ms

### Path Parameter - int (/items/12345)
  Reqs/sec    318639.28   14253.12  335840.06
  Latency      311.33us   280.57us     8.12ms
  Latency Distribution
     50%   244.00us
     75%   357.00us
     90%   482.00us
     99%     2.08ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    311919.80   15854.80  333762.69
  Latency      318.39us   245.91us     7.15ms
  Latency Distribution
     50%   269.00us
     75%   366.00us
     90%   475.00us
     99%     1.54ms

### Header Parameter (/header)
  Reqs/sec    279358.80   20491.12  306530.65
  Latency      355.36us   241.28us    10.54ms
  Latency Distribution
     50%   284.00us
     75%   409.00us
     90%   568.00us
     99%     1.68ms

### Cookie Parameter (/cookie)
  Reqs/sec    283401.59   18653.31  304432.23
  Latency      350.97us   227.88us     6.61ms
  Latency Distribution
     50%   292.00us
     75%   384.00us
     90%   535.00us
     99%     1.70ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    159204.22    6974.01  166206.83
  Latency      625.58us   140.94us     6.23ms
  Latency Distribution
     50%   618.00us
     75%   727.00us
     90%     0.89ms
     99%     1.36ms
