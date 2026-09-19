# Django-Bolt Benchmark
Generated: Sat Sep 19 06:39:55 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=100000

## Root Endpoint Performance
  Reqs/sec    324493.00   34302.70  363488.30
  Latency      306.05us   365.55us     8.69ms
  Latency Distribution
     50%   201.00us
     75%   298.00us
     90%   487.00us
     99%     2.71ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    164709.40   21942.00  205878.04
  Latency      600.03us   405.41us    15.38ms
  Latency Distribution
     50%   506.00us
     75%   666.00us
     90%     0.94ms
     99%     3.21ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    192587.37   32199.88  233131.09
  Latency      513.13us   297.57us     7.47ms
  Latency Distribution
     50%   423.00us
     75%   564.00us
     90%   791.00us
     99%     2.18ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    264435.76   21007.92  292708.48
  Latency      375.86us   276.70us     6.09ms
  Latency Distribution
     50%   286.00us
     75%   410.00us
     90%   585.00us
     99%     2.06ms
### Cookie Endpoint (/cookie)
  Reqs/sec    260544.57   36952.60  296638.18
  Latency      380.08us   297.16us     8.11ms
  Latency Distribution
     50%   296.00us
     75%   404.00us
     90%   592.00us
     99%     2.29ms
### Exception Endpoint (/exc)
  Reqs/sec    239759.94   36963.08  280769.65
  Latency      415.01us   366.64us     9.34ms
  Latency Distribution
     50%   331.00us
     75%   462.00us
     90%   624.00us
     99%     3.01ms
### HTML Response (/html)
  Reqs/sec    303161.93   38709.32  335934.05
  Latency      322.43us   273.06us     8.00ms
  Latency Distribution
     50%   246.00us
     75%   345.00us
     90%   488.00us
     99%     2.19ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     48690.81    6828.82   55559.04
  Latency        2.05ms   832.40us    20.35ms
  Latency Distribution
     50%     1.80ms
     75%     2.48ms
     90%     3.31ms
     99%     6.14ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    150300.69   19007.16  179701.38
  Latency      662.43us   417.76us    13.06ms
  Latency Distribution
     50%   540.00us
     75%   672.00us
     90%     0.90ms
     99%     3.47ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    153889.49   32237.46  189263.51
  Latency      647.37us   499.00us    15.29ms
  Latency Distribution
     50%   572.00us
     75%   676.00us
     90%     0.85ms
     99%     3.36ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
 48503 / 100000 [====================================================================================================>-----------------------------------------------------------------------------------------------------------]  48.50% 80672/s
  Reqs/sec     82468.27    9444.50   93655.24
  Latency        1.21ms   629.45us    17.05ms
  Latency Distribution
     50%     1.11ms
     75%     1.32ms
     90%     1.87ms
     99%     4.00ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    176598.74   26603.97  203398.50
  Latency      564.58us   331.31us    10.07ms
  Latency Distribution
     50%   497.00us
     75%   688.00us
     90%     0.85ms
     99%     2.21ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    156119.22   10921.59  173756.41
  Latency      637.41us   258.39us     6.90ms
  Latency Distribution
     50%   565.00us
     75%   729.00us
     90%     0.93ms
     99%     2.17ms
### Media 100KB (GET /media/bench/upload_100k.bin)
 80497 / 100000 [=======================================================================================================================================================================>----------------------------------------]  80.50% 80344/s
  Reqs/sec     81590.62   11959.79   93562.13
  Latency        1.22ms   572.27us    16.27ms
  Latency Distribution
     50%     1.11ms
     75%     1.49ms
     90%     1.95ms
     99%     4.07ms

## Authentication & Authorization Performance
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
 30897 / 100000 [==============================================================>------------------------------------------------------------------------------------------------------------------------------------------]  30.90% 38577/s 00m01s
  Reqs/sec     38908.03    4306.99   41588.40
  Latency        2.57ms   629.64us    15.20ms
  Latency Distribution
     50%     2.73ms
     75%     3.00ms
     90%     3.22ms
     99%     6.24ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    155103.61   20161.74  171571.39
  Latency      641.15us   301.90us     8.57ms
  Latency Distribution
     50%   571.00us
     75%   742.00us
     90%     0.97ms
     99%     2.10ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    285129.81   29230.40  311282.19
  Latency      348.39us   343.09us    10.02ms
  Latency Distribution
     50%   267.00us
     75%   359.00us
     90%   486.00us
     99%     2.85ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    259226.14   31278.88  293289.51
  Latency      382.92us   317.62us     7.47ms
  Latency Distribution
     50%   311.00us
     75%   402.00us
     90%   568.00us
     99%     2.24ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     19969.88    2012.12   24101.02
  Latency        5.01ms     1.78ms    53.05ms
  Latency Distribution
     50%     4.53ms
     75%     6.11ms
     90%     8.02ms
     99%    11.89ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     16346.23    1796.30   20003.31
  Latency        6.11ms     2.90ms    62.00ms
  Latency Distribution
     50%     5.18ms
     75%     7.46ms
     90%    10.49ms
     99%    19.10ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     25906.40    2355.10   32239.84
  Latency        3.86ms     1.22ms    41.83ms
  Latency Distribution
     50%     3.60ms
     75%     4.58ms
     90%     5.73ms
     99%     8.55ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    209882.81   28345.55  266115.75
  Latency      478.96us   337.25us     9.68ms
  Latency Distribution
     50%   378.00us
     75%   510.00us
     90%   778.00us
     99%     2.77ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    204228.73   15123.99  237582.07
  Latency      484.82us   278.37us     7.27ms
  Latency Distribution
     50%   393.00us
     75%   530.00us
     90%   785.00us
     99%     2.46ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    215106.96   20996.90  248508.28
  Latency      464.36us   287.61us     8.65ms
  Latency Distribution
     50%   391.00us
     75%   505.00us
     90%   718.00us
     99%     2.03ms
### CBV Items PUT (Update) (/cbv-items/1)
 38497 / 100000 [===============================================================================>-------------------------------------------------------------------------------------------------------------------------------]  38.50% 191990/s
  Reqs/sec    194411.89   16448.14  223297.03
  Latency      512.07us   316.00us    12.12ms
  Latency Distribution
     50%   431.00us
     75%   563.00us
     90%   795.00us
     99%     2.31ms

## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    219259.98   36496.17  271215.53
  Latency      453.31us   368.15us     9.51ms
  Latency Distribution
     50%   332.00us
     75%   499.00us
     90%   746.00us
     99%     2.72ms
### File Upload (POST /upload)
  Reqs/sec    185405.91   23975.35  214923.28
  Latency      535.88us   336.79us     9.40ms
  Latency Distribution
     50%   436.00us
     75%   608.00us
     90%   787.00us
     99%     2.77ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    208056.65   27366.61  243976.35
  Latency      478.43us   333.51us    14.04ms
  Latency Distribution
     50%   397.00us
     75%   528.00us
     90%   722.00us
     99%     2.56ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    156265.24   18447.03  186056.42
  Latency      640.60us   353.26us    11.98ms
  Latency Distribution
     50%   572.00us
     75%   727.00us
     90%     0.94ms
     99%     2.58ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
 5901 / 100000 [===========>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]   5.90% 5873/s 00m16s
 99986 / 100000 [=================================================================================================================================================================================================================]  99.99% 6004/s
  Reqs/sec      5996.24    1042.07    8998.06
  Latency       16.55ms    10.64ms   201.46ms
  Latency Distribution
     50%    16.51ms
     75%    18.73ms
     90%    21.01ms
     99%    50.96ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    233471.75   38364.70  273699.14
  Latency      426.96us   381.52us    10.35ms
  Latency Distribution
     50%   300.00us
     75%   441.00us
     90%   728.00us
     99%     3.06ms
## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Feed of 100 mixed union items (/feed)
  Reqs/sec     86837.12    7426.65   98771.79
  Latency        1.15ms   314.56us    10.06ms
  Latency Distribution
     50%     1.04ms
     75%     1.32ms
     90%     1.73ms
     99%     3.01ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    306509.07   44617.31  356745.24
  Latency      323.95us   327.87us     9.71ms
  Latency Distribution
     50%   219.00us
     75%   326.00us
     90%   560.00us
     99%     2.27ms

### Path Parameter - int (/items/12345)
  Reqs/sec    264639.97   36480.49  320714.62
  Latency      374.44us   329.11us     9.55ms
  Latency Distribution
     50%   276.00us
     75%   394.00us
     90%   626.00us
     99%     2.30ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    256755.49   45731.09  301131.75
  Latency      385.80us   454.16us    17.22ms
  Latency Distribution
     50%   277.00us
     75%   369.00us
     90%   605.00us
     99%     2.94ms

### Header Parameter (/header)
  Reqs/sec    255022.70   29643.32  297741.05
  Latency      387.75us   319.71us    11.13ms
  Latency Distribution
     50%   291.00us
     75%   405.00us
     90%   642.00us
     99%     2.42ms

### Cookie Parameter (/cookie)
  Reqs/sec    246662.80   41417.77  293682.13
  Latency      403.61us   307.48us    13.14ms
  Latency Distribution
     50%   301.00us
     75%   421.00us
     90%   679.00us
     99%     2.44ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    146964.47   18766.29  166466.20
  Latency      676.21us   317.80us     8.17ms
  Latency Distribution
     50%   617.00us
     75%   767.00us
     90%     0.99ms
     99%     2.77ms
