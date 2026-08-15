# Django-Bolt Benchmark
Generated: Sat Aug  8 06:04:31 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=100000

## Root Endpoint Performance
  Reqs/sec    318517.54   26046.88  338008.95
  Latency      311.96us   279.92us     8.38ms
  Latency Distribution
     50%   224.00us
     75%   320.00us
     90%   506.00us
     99%     2.27ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    188072.69   10546.06  206069.17
  Latency      529.69us   227.41us     6.80ms
  Latency Distribution
     50%   507.00us
     75%   654.00us
     90%   796.00us
     99%     1.67ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    205103.46   14485.67  222523.69
  Latency      485.55us   220.83us     7.27ms
  Latency Distribution
     50%   431.00us
     75%   542.00us
     90%   718.00us
     99%     1.50ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    251833.54   34949.64  273840.80
  Latency      394.62us   363.21us    12.48ms
  Latency Distribution
     50%   319.00us
     75%   425.00us
     90%   612.00us
     99%     1.85ms
### Cookie Endpoint (/cookie)
  Reqs/sec    256767.84   24256.60  282758.69
  Latency      380.45us   237.62us     9.02ms
  Latency Distribution
     50%   324.00us
     75%   426.00us
     90%   576.00us
     99%     1.77ms
### Exception Endpoint (/exc)
 51749 / 100000 [=========================================================================================================================================================================>-------------------------------------------------------------------------------------------------------------------------------------------------------------]  51.75% 256174/s
  Reqs/sec    251148.53   12224.85  265140.87
  Latency      395.37us   211.25us     6.95ms
  Latency Distribution
     50%   330.00us
     75%   464.00us
     90%   640.00us
     99%     1.69ms
### HTML Response (/html)
  Reqs/sec    276719.00   43966.45  317600.11
  Latency      361.18us   373.32us    10.18ms
  Latency Distribution
     50%   262.00us
     75%   366.00us
     90%   549.00us
     99%     3.02ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     47977.20    3535.50   52449.99
  Latency        2.08ms   590.67us    19.53ms
  Latency Distribution
     50%     1.85ms
     75%     2.51ms
     90%     3.33ms
     99%     5.35ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    174378.97    7271.16  181653.96
  Latency      570.62us   141.98us     5.43ms
  Latency Distribution
     50%   581.00us
     75%   643.00us
     90%   695.00us
     99%     1.11ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    174495.51    6754.22  183547.18
  Latency      569.86us   118.34us     4.89ms
  Latency Distribution
     50%   588.00us
     75%   704.00us
     90%   783.00us
     99%     1.13ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     95799.47   11197.99  102218.17
  Latency        1.04ms     1.25ms    44.08ms
  Latency Distribution
     50%     0.94ms
     75%     1.16ms
     90%     1.34ms
     99%     2.87ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    195485.44    8202.50  208027.66
  Latency      508.24us   141.39us     5.94ms
  Latency Distribution
     50%   518.00us
     75%   592.00us
     90%   685.00us
     99%     1.15ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    164366.28   11977.94  179888.81
  Latency      599.23us   184.46us     5.66ms
  Latency Distribution
     50%   547.00us
     75%   746.00us
     90%     1.03ms
     99%     1.43ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     91677.74   10536.36  101343.82
  Latency        1.09ms     1.19ms    44.47ms
  Latency Distribution
     50%     0.90ms
     75%     1.32ms
     90%     1.61ms
     99%     3.09ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    303956.62   15421.55  319791.36
  Latency      326.27us   280.63us    10.24ms
  Latency Distribution
     50%   254.00us
     75%   347.00us
     90%   503.00us
     99%     2.11ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    306574.75   17459.13  319958.88
  Latency      323.19us   248.14us     8.20ms
  Latency Distribution
     50%   258.00us
     75%   356.00us
     90%   510.00us
     99%     1.83ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     89921.29    3650.46   95914.14
  Latency        1.11ms   145.80us     6.87ms
  Latency Distribution
     50%     1.06ms
     75%     1.41ms
     90%     1.68ms
     99%     2.25ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     84310.51    3831.52   89782.00
  Latency        1.18ms   260.96us     8.83ms
  Latency Distribution
     50%     1.20ms
     75%     1.39ms
     90%     1.63ms
     99%     2.38ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec    133097.61   19405.07  159881.02
  Latency      738.95us   374.33us    11.41ms
  Latency Distribution
     50%   625.00us
     75%     0.85ms
     90%     1.20ms
     99%     3.19ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
 73747 / 100000 [=================================================================================================================================================================================================================================================>--------------------------------------------------------------------------------------]  73.75% 40922/s
  Reqs/sec     41168.11    1583.55   43301.83
  Latency        2.42ms   243.19us     8.65ms
  Latency Distribution
     50%     2.42ms
     75%     2.87ms
     90%     3.15ms
     99%     3.67ms
### Get User via Dependency (/auth/me-dependency)
 56903 / 100000 [======================================================================================================================================================================================>------------------------------------------------------------------------------------------------------------------------------------------]  56.90% 40517/s 00m01s
  Reqs/sec     40771.47    3899.44   50493.38
  Latency        2.45ms     2.69ms    88.28ms
  Latency Distribution
     50%     2.20ms
     75%     2.89ms
     90%     3.66ms
     99%     5.40ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    150045.57    8273.98  162930.82
  Latency      663.07us   232.60us     5.74ms
  Latency Distribution
     50%   593.00us
     75%   760.00us
     90%     1.07ms
     99%     1.92ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    271773.30   14840.08  295683.68
  Latency      363.51us   332.54us     9.80ms
  Latency Distribution
     50%   277.00us
     75%   378.00us
     90%   542.00us
     99%     2.50ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    254030.88   19200.19  281760.36
  Latency      390.02us   304.57us     7.53ms
  Latency Distribution
     50%   308.00us
     75%   401.00us
     90%   601.00us
     99%     2.30ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
 15989 / 100000 [===================================================>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  15.99% 19958/s 00m04s
 54987 / 100000 [================================================================================================================================================================================>------------------------------------------------------------------------------------------------------------------------------------------------]  54.99% 21115/s 00m02s
  Reqs/sec     21298.62    1291.63   23071.96
  Latency        4.69ms     2.21ms    89.20ms
  Latency Distribution
     50%     4.58ms
     75%     5.43ms
     90%     6.21ms
     99%     8.03ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     15986.28    1310.63   19524.29
  Latency        6.25ms     2.69ms    69.54ms
  Latency Distribution
     50%     5.66ms
     75%     7.92ms
     90%    10.80ms
     99%    16.91ms
### Users Mini10 (Async) (/users/mini10)
 58904 / 100000 [=============================================================================================================================================================================================>-----------------------------------------------------------------------------------------------------------------------------------]  58.90% 26733/s 00m01s
  Reqs/sec     26683.26    1584.95   29050.75
  Latency        3.74ms     1.50ms    57.28ms
  Latency Distribution
     50%     3.65ms
     75%     4.55ms
     90%     5.33ms
     99%     6.93ms
### Users Mini10 (Sync) (/users/sync-mini10)
 80500 / 100000 [==================================================================================================================================================================================================================================================================>--------------------------------------------------------------]  80.50% 18257/s 00m01s
  Reqs/sec     18251.43    1072.34   21627.72
  Latency        5.47ms     2.01ms    70.58ms
  Latency Distribution
     50%     5.09ms
     75%     6.70ms
     90%     8.55ms
     99%    13.10ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    219463.09   12620.40  246204.50
  Latency      453.46us   249.81us     6.10ms
  Latency Distribution
     50%   390.00us
     75%   501.00us
     90%   702.00us
     99%     1.87ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    212517.93   12675.14  237084.41
  Latency      467.53us   241.71us     6.63ms
  Latency Distribution
     50%   397.00us
     75%   514.00us
     90%   743.00us
     99%     2.06ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     95594.48    3309.54  101709.59
  Latency        1.04ms   135.13us     6.47ms
  Latency Distribution
     50%     1.03ms
     75%     1.25ms
     90%     1.49ms
     99%     2.11ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
 84897 / 100000 [=====================================================================================================================================================================================================================================================================================>-------------------------------------------------]  84.90% 211545/s
  Reqs/sec    211126.29   17326.72  247331.89
  Latency      471.68us   238.82us     8.09ms
  Latency Distribution
     50%   386.00us
     75%   554.00us
     90%   781.00us
     99%     1.95ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    191490.39   16250.86  221913.97
  Latency      521.92us   250.99us     7.41ms
  Latency Distribution
     50%   456.00us
     75%   599.00us
     90%   844.00us
     99%     2.07ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
 83901 / 100000 [==================================================================================================================================================================================================================================================================================>----------------------------------------------------]  83.90% 208205/s
  Reqs/sec    209039.10   11387.76  232494.05
  Latency      475.40us   267.65us     8.62ms
  Latency Distribution
     50%   404.00us
     75%   551.00us
     90%   750.00us
     99%     1.96ms
### CBV Response Types (/cbv-response)
  Reqs/sec    233823.89   19284.16  257728.35
  Latency      423.83us   258.08us     6.63ms
  Latency Distribution
     50%   334.00us
     75%   512.00us
     90%   715.00us
     99%     1.89ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     20655.90    2706.96   22833.19
  Latency        4.82ms     3.48ms   118.19ms
  Latency Distribution
     50%     4.59ms
     75%     5.68ms
     90%     6.79ms
     99%     9.10ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    225422.13   17372.45  256560.60
  Latency      442.36us   276.50us     7.09ms
  Latency Distribution
     50%   360.00us
     75%   480.00us
     90%   699.00us
     99%     2.24ms
### File Upload (POST /upload)
  Reqs/sec    181205.73   14190.66  201742.40
  Latency      550.00us   215.52us     6.78ms
  Latency Distribution
     50%   510.00us
     75%   640.00us
     90%   832.00us
     99%     1.81ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    179752.02    9673.99  191700.21
  Latency      553.61us   181.83us     4.97ms
  Latency Distribution
     50%   501.00us
     75%   641.00us
     90%   830.00us
     99%     1.56ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    202435.53   14179.23  233345.46
  Latency      492.26us   223.37us     6.40ms
  Latency Distribution
     50%   425.00us
     75%   619.00us
     90%   797.00us
     99%     1.87ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    157005.23   10235.72  168812.83
  Latency      634.05us   171.43us     6.08ms
  Latency Distribution
     50%   590.00us
     75%   740.00us
     90%     0.95ms
     99%     1.61ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
 34989 / 100000 [================================================================================================================>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  34.99% 8728/s 00m07s
 36749 / 100000 [======================================================================================================================>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  36.75% 8731/s 00m07s
 99990 / 100000 [=========================================================================================================================================================================================================================================================================================================================================]  99.99% 8599/s
  Reqs/sec      8537.73    1435.82   11606.51
  Latency       11.57ms    10.57ms   217.48ms
  Latency Distribution
     50%    10.54ms
     75%    12.99ms
     90%    16.21ms
     99%    25.23ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    244014.67   19859.04  268129.35
  Latency      406.22us   314.30us     7.92ms
  Latency Distribution
     50%   307.00us
     75%   421.00us
     90%   667.00us
     99%     2.33ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    209057.53   18132.20  248757.76
  Latency      474.80us   366.45us     7.61ms
  Latency Distribution
     50%   344.00us
     75%   494.00us
     90%   837.00us
     99%     3.04ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec    149016.70   15693.23  174660.61
  Latency      665.78us   341.80us     7.48ms
  Latency Distribution
     50%   530.00us
     75%   782.00us
     90%     1.12ms
     99%     2.90ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    210489.88   22432.73  245369.76
  Latency      473.11us   390.08us    10.14ms
  Latency Distribution
     50%   332.00us
     75%   488.00us
     90%   836.00us
     99%     3.06ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
 30752 / 100000 [====================================================================================================>----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  30.75% 153300/s
 59990 / 100000 [====================================================================================================================================================================================================>----------------------------------------------------------------------------------------------------------------------------------]  59.99% 149405/s
  Reqs/sec    166203.16   29852.01  220113.92
  Latency      604.23us   417.01us     8.71ms
  Latency Distribution
     50%   443.00us
     75%   679.00us
     90%     1.12ms
     99%     3.31ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    168324.83   13762.66  193916.80
  Latency      591.09us   405.39us     8.67ms
  Latency Distribution
     50%   431.00us
     75%   660.00us
     90%     1.06ms
     99%     3.34ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    205950.35   24315.99  248473.51
  Latency      483.35us   425.91us     8.73ms
  Latency Distribution
     50%   319.00us
     75%   501.00us
     90%     0.90ms
     99%     3.35ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    214987.77   21885.47  244783.74
  Latency      460.01us   379.96us     7.56ms
  Latency Distribution
     50%   317.00us
     75%   484.00us
     90%   828.00us
     99%     3.14ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    239034.82   25639.27  281954.98
  Latency      414.41us   363.61us     7.08ms
  Latency Distribution
     50%   292.00us
     75%   413.00us
     90%   713.00us
     99%     2.87ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     83384.24    5174.33   95233.69
  Latency        1.20ms   250.45us     6.65ms
  Latency Distribution
     50%     1.12ms
     75%     1.42ms
     90%     1.82ms
     99%     2.86ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    262564.27   25703.77  309726.51
  Latency      378.16us   359.33us     6.62ms
  Latency Distribution
     50%   237.00us
     75%   367.00us
     90%   728.00us
     99%     2.90ms

### Path Parameter - int (/items/12345)
  Reqs/sec    232635.96   28790.84  268797.90
  Latency      429.18us   396.01us     9.73ms
  Latency Distribution
     50%   301.00us
     75%   440.00us
     90%   751.00us
     99%     3.06ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    238339.88   19549.67  267924.53
  Latency      416.65us   344.14us     8.43ms
  Latency Distribution
     50%   315.00us
     75%   441.00us
     90%   680.00us
     99%     2.83ms

### Header Parameter (/header)
  Reqs/sec    223968.32   19388.88  254607.45
  Latency      444.48us   364.92us     7.58ms
  Latency Distribution
     50%   328.00us
     75%   464.00us
     90%   711.00us
     99%     3.33ms

### Cookie Parameter (/cookie)
  Reqs/sec    221308.10   15791.18  248100.47
  Latency      448.84us   358.74us     9.79ms
  Latency Distribution
     50%   336.00us
     75%   473.00us
     90%   779.00us
     99%     2.60ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    141542.69   10010.13  161269.94
  Latency      705.20us   294.21us     7.59ms
  Latency Distribution
     50%   616.00us
     75%   789.00us
     90%     1.10ms
     99%     2.42ms
