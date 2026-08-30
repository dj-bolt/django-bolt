# Django-Bolt Benchmark
Generated: Sun Aug 30 03:04:59 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=100000

## Root Endpoint Performance
  Reqs/sec    313721.85   14184.59  334779.26
  Latency      315.49us   245.09us     6.83ms
  Latency Distribution
     50%   241.00us
     75%   331.00us
     90%   536.00us
     99%     1.82ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    188919.85   15321.39  205172.94
  Latency      522.98us   180.22us     7.62ms
  Latency Distribution
     50%   487.00us
     75%   604.00us
     90%   766.00us
     99%     1.53ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    203725.47   11915.39  220427.31
  Latency      489.30us   247.32us    13.00ms
  Latency Distribution
     50%   432.00us
     75%   571.00us
     90%   744.00us
     99%     1.47ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    265104.83   14150.37  277679.31
  Latency      374.04us   221.28us     6.03ms
  Latency Distribution
     50%   301.00us
     75%   415.00us
     90%   592.00us
     99%     1.84ms
### Cookie Endpoint (/cookie)
  Reqs/sec    251704.00   46900.42  280440.65
  Latency      380.06us   237.60us     9.04ms
  Latency Distribution
     50%   312.00us
     75%   415.00us
     90%   610.00us
     99%     1.87ms
### Exception Endpoint (/exc)
  Reqs/sec    254373.14   11236.93  268919.56
  Latency      389.96us   176.39us     6.28ms
  Latency Distribution
     50%   327.00us
     75%   471.00us
     90%   642.00us
     99%     1.55ms
### HTML Response (/html)
  Reqs/sec    290993.37   26457.23  320809.19
  Latency      341.88us   269.94us     6.45ms
  Latency Distribution
     50%   252.00us
     75%   381.00us
     90%   589.00us
     99%     2.13ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
 50751 / 100000 [===============================================================================================================================>---------------------------------------------------------------------------------------------------------------------------]  50.75% 50527/s
  Reqs/sec     51177.15    4367.29   56578.92
  Latency        1.95ms   521.01us    18.05ms
  Latency Distribution
     50%     1.79ms
     75%     2.34ms
     90%     2.98ms
     99%     4.41ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    173675.44    7976.55  185326.99
  Latency      573.44us   157.13us     7.77ms
  Latency Distribution
     50%   541.00us
     75%   621.00us
     90%   698.00us
     99%     1.12ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    175030.95    6452.36  184699.04
  Latency      566.94us   183.69us    12.03ms
  Latency Distribution
     50%   553.00us
     75%   601.00us
     90%   660.00us
     99%     1.11ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     88651.09    4806.13   94705.56
  Latency        1.12ms   260.18us    10.50ms
  Latency Distribution
     50%     1.15ms
     75%     1.31ms
     90%     1.54ms
     99%     2.61ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    195465.08    8591.66  209588.23
  Latency      508.67us   155.78us     5.39ms
  Latency Distribution
     50%   455.00us
     75%   581.00us
     90%   811.00us
     99%     1.24ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    165732.76    4989.01  172289.00
  Latency      599.82us   126.40us     6.84ms
  Latency Distribution
     50%   596.00us
     75%   696.00us
     90%   807.00us
     99%     1.31ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     86735.18    5477.44   93231.54
  Latency        1.15ms   298.02us    11.08ms
  Latency Distribution
     50%     1.10ms
     75%     1.37ms
     90%     1.72ms
     99%     2.82ms

## Authentication & Authorization Performance
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
 40502 / 100000 [==================================================================================================>-------------------------------------------------------------------------------------------------------------------------------------------------]  40.50% 40452/s 00m01s
  Reqs/sec     40539.69    1305.69   42360.11
  Latency        2.46ms   185.75us     9.14ms
  Latency Distribution
     50%     2.36ms
     75%     2.78ms
     90%     3.24ms
     99%     3.71ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    156772.46    5211.13  170565.79
  Latency      634.21us   119.98us     4.26ms
  Latency Distribution
     50%   609.00us
     75%   748.00us
     90%     0.96ms
     99%     1.42ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    241280.40   28420.54  297045.57
  Latency      408.85us   300.18us     7.18ms
  Latency Distribution
     50%   304.00us
     75%   464.00us
     90%   722.00us
     99%     2.31ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    240122.84   22189.74  277063.49
  Latency      413.41us   302.65us     7.12ms
  Latency Distribution
     50%   317.00us
     75%   443.00us
     90%   687.00us
     99%     2.39ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
 28754 / 100000 [======================================================================>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  28.75% 20505/s 00m03s
  Reqs/sec     20691.83     852.08   22174.80
  Latency        4.83ms     1.20ms    48.45ms
  Latency Distribution
     50%     4.56ms
     75%     5.71ms
     90%     6.65ms
     99%     8.34ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     15498.35    1314.59   19158.25
  Latency        6.43ms     2.52ms    61.62ms
  Latency Distribution
     50%     5.73ms
     75%     7.81ms
     90%    10.41ms
     99%    17.00ms
### Users Mini10 (Async) (/users/mini10)
 4897 / 100000 [===========>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]   4.90% 24408/s 00m03s
  Reqs/sec     25948.27    1006.13   28046.70
  Latency        3.85ms   816.54us    32.36ms
  Latency Distribution
     50%     3.77ms
     75%     4.55ms
     90%     5.37ms
     99%     6.98ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    212015.24   18420.87  237683.82
  Latency      468.48us   252.47us     6.61ms
  Latency Distribution
     50%   392.00us
     75%   535.00us
     90%   752.00us
     99%     1.93ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    205850.29   18526.39  245620.57
  Latency      483.60us   258.00us     6.82ms
  Latency Distribution
     50%   404.00us
     75%   544.00us
     90%   781.00us
     99%     2.21ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    219101.22   19399.52  248726.17
  Latency      452.34us   215.70us     8.78ms
  Latency Distribution
     50%   400.00us
     75%   519.00us
     90%   699.00us
     99%     1.65ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    196678.95   13877.90  220635.39
  Latency      502.67us   242.20us     9.02ms
  Latency Distribution
     50%   421.00us
     75%   594.00us
     90%   811.00us
     99%     1.86ms

## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    233788.76   19971.96  259318.39
  Latency      423.58us   233.00us     6.62ms
  Latency Distribution
     50%   364.00us
     75%   470.00us
     90%   661.00us
     99%     1.77ms
### File Upload (POST /upload)
  Reqs/sec    166894.53   28630.28  201002.13
  Latency      594.49us   342.86us     8.56ms
  Latency Distribution
     50%   512.00us
     75%   659.00us
     90%     0.95ms
     99%     2.79ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    213857.50   12964.72  229148.84
  Latency      465.40us   178.54us     5.59ms
  Latency Distribution
     50%   408.00us
     75%   586.00us
     90%   740.00us
     99%     1.41ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    157401.72    8654.80  168583.65
  Latency      632.54us   179.06us     4.75ms
  Latency Distribution
     50%   611.00us
     75%   753.00us
     90%     0.94ms
     99%     1.66ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
 14904 / 100000 [====================================>----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  14.90% 9286/s 00m09s
 20505 / 100000 [==================================================>--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  20.50% 9294/s 00m08s
  Reqs/sec      9144.00    1193.29   11694.93
  Latency       10.84ms     7.88ms   176.74ms
  Latency Distribution
     50%    10.50ms
     75%    12.22ms
     90%    13.51ms
     99%    19.28ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    257116.40   19088.82  280111.09
  Latency      382.96us   259.03us     6.05ms
  Latency Distribution
     50%   317.00us
     75%   419.00us
     90%   591.00us
     99%     2.03ms
## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Feed of 100 mixed union items (/feed)
  Reqs/sec     87893.19    8663.72   96329.99
  Latency        1.13ms   241.97us     8.04ms
  Latency Distribution
     50%     1.11ms
     75%     1.36ms
     90%     1.71ms
     99%     2.65ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    301076.66   26526.21  334887.56
  Latency      326.70us   294.99us     7.62ms
  Latency Distribution
     50%   221.00us
     75%   340.00us
     90%   584.00us
     99%     2.32ms

### Path Parameter - int (/items/12345)
  Reqs/sec    268611.41   14015.26  295266.45
  Latency      368.59us   263.13us     6.60ms
  Latency Distribution
     50%   294.00us
     75%   394.00us
     90%   599.00us
     99%     2.03ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    268029.25   24727.16  297390.20
  Latency      368.98us   250.05us     6.53ms
  Latency Distribution
     50%   281.00us
     75%   432.00us
     90%   607.00us
     99%     1.95ms

### Header Parameter (/header)
  Reqs/sec    241188.30   18509.44  263133.72
  Latency      411.91us   299.53us     6.67ms
  Latency Distribution
     50%   313.00us
     75%   444.00us
     90%   710.00us
     99%     2.30ms

### Cookie Parameter (/cookie)
  Reqs/sec    257372.83   17469.78  277675.12
  Latency      385.21us   266.96us     8.43ms
  Latency Distribution
     50%   308.00us
     75%   407.00us
     90%   615.00us
     99%     2.09ms

### Auth Context - JWT validated, no DB (/auth/context)
 90755 / 100000 [==================================================================================================================================================================================================================================>-----------------------]  90.75% 150808/s
  Reqs/sec    151262.41    9111.88  162949.57
  Latency      658.05us   210.21us     7.75ms
  Latency Distribution
     50%   611.00us
     75%   817.00us
     90%     1.02ms
     99%     1.75ms
