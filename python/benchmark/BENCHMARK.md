# Django-Bolt Benchmark
Generated: Sun Aug 30 12:15:47 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=100000

## Root Endpoint Performance
  Reqs/sec    305093.39   17253.02  331543.24
  Latency      324.20us   321.31us     8.13ms
  Latency Distribution
     50%   217.00us
     75%   325.00us
     90%   556.00us
     99%     2.41ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    178952.02   12020.05  198459.72
  Latency      555.94us   197.61us     5.18ms
  Latency Distribution
     50%   495.00us
     75%   697.00us
     90%     0.90ms
     99%     1.84ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    194160.96   14421.15  211551.98
  Latency      512.51us   226.40us     6.17ms
  Latency Distribution
     50%   445.00us
     75%   595.00us
     90%   804.00us
     99%     1.80ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    257936.74   13436.33  274661.68
  Latency      385.77us   259.70us    11.21ms
  Latency Distribution
     50%   318.00us
     75%   430.00us
     90%   610.00us
     99%     2.03ms
### Cookie Endpoint (/cookie)
  Reqs/sec    251418.48   17878.94  271807.46
  Latency      394.22us   305.15us     9.39ms
  Latency Distribution
     50%   308.00us
     75%   403.00us
     90%   603.00us
     99%     2.45ms
### Exception Endpoint (/exc)
  Reqs/sec    231693.21   33350.88  272630.99
  Latency      428.78us   296.62us     6.74ms
  Latency Distribution
     50%   346.00us
     75%   467.00us
     90%   703.00us
     99%     2.30ms
### HTML Response (/html)
  Reqs/sec    243276.66   32740.99  312561.12
  Latency      404.91us   380.86us    11.70ms
  Latency Distribution
     50%   268.00us
     75%   413.00us
     90%   735.00us
     99%     2.89ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     49931.02    4934.46   55154.82
  Latency        2.00ms   617.29us    22.88ms
  Latency Distribution
     50%     1.79ms
     75%     2.44ms
     90%     3.19ms
     99%     5.10ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    162403.15    9446.71  177123.59
  Latency      612.99us   271.52us     7.64ms
  Latency Distribution
     50%   571.00us
     75%   674.00us
     90%   796.00us
     99%     2.01ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    170415.61    8295.72  180903.24
  Latency      582.87us   198.27us     5.69ms
  Latency Distribution
     50%   564.00us
     75%   736.00us
     90%   801.00us
     99%     1.33ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     91770.07    8107.84  100405.62
  Latency        1.08ms     1.10ms    42.55ms
  Latency Distribution
     50%     1.00ms
     75%     1.21ms
     90%     1.44ms
     99%     3.52ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    182859.11   14026.69  204207.88
  Latency      544.66us   228.66us     5.93ms
  Latency Distribution
     50%   466.00us
     75%   635.00us
     90%     0.86ms
     99%     1.78ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    163958.25    5889.13  175896.60
  Latency      606.86us   168.38us     5.98ms
  Latency Distribution
     50%   591.00us
     75%   703.00us
     90%     0.86ms
     99%     1.46ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     90302.96    8496.95  100653.54
  Latency        1.10ms     1.24ms    44.20ms
  Latency Distribution
     50%     0.98ms
     75%     1.25ms
     90%     1.64ms
     99%     3.33ms

## Authentication & Authorization Performance
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
 46994 / 100000 [==================================================================================================================>---------------------------------------------------------------------------------------------------------------------------------]  46.99% 39115/s 00m01s
  Reqs/sec     39577.27    2191.97   42321.99
  Latency        2.52ms   264.68us    10.09ms
  Latency Distribution
     50%     2.45ms
     75%     2.97ms
     90%     3.25ms
     99%     3.92ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    156199.46    7026.33  163935.96
  Latency      637.00us   248.10us     8.62ms
  Latency Distribution
     50%   599.00us
     75%   726.00us
     90%     0.90ms
     99%     1.73ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    238081.70   46352.66  297408.60
  Latency      415.44us   344.03us     8.89ms
  Latency Distribution
     50%   297.00us
     75%   432.00us
     90%   731.00us
     99%     2.71ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    247246.47   17647.61  270041.41
  Latency      403.06us   283.25us     6.15ms
  Latency Distribution
     50%   295.00us
     75%   494.00us
     90%   661.00us
     99%     2.22ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
 65754 / 100000 [================================================================================================================================================================>-----------------------------------------------------------------------------------]  65.75% 20507/s 00m01s
  Reqs/sec     20665.89    1125.66   22327.69
  Latency        4.83ms     1.45ms    55.68ms
  Latency Distribution
     50%     4.73ms
     75%     5.67ms
     90%     6.65ms
     99%     8.78ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     16075.44    1450.65   22738.37
  Latency        6.22ms     2.37ms    65.01ms
  Latency Distribution
     50%     5.62ms
     75%     7.49ms
     90%     9.83ms
     99%    15.08ms
### Users Mini10 (Async) (/users/mini10)
 67902 / 100000 [=====================================================================================================================================================================>------------------------------------------------------------------------------]  67.90% 26068/s 00m01s
  Reqs/sec     25718.44    1291.64   27414.98
  Latency        3.88ms     1.03ms    34.74ms
  Latency Distribution
     50%     3.61ms
     75%     4.73ms
     90%     5.93ms
     99%     8.03ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    235013.56   13800.37  255427.24
  Latency      423.61us   178.19us     8.44ms
  Latency Distribution
     50%   372.00us
     75%   510.00us
     90%   665.00us
     99%     1.23ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    184470.99   18333.53  213477.33
  Latency      541.64us   338.50us    11.72ms
  Latency Distribution
     50%   439.00us
     75%   598.00us
     90%     0.92ms
     99%     2.76ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    213618.71   15528.04  235717.72
  Latency      465.96us   213.68us     7.24ms
  Latency Distribution
     50%   413.00us
     75%   533.00us
     90%   723.00us
     99%     1.67ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    196356.94   17014.36  220337.52
  Latency      502.87us   220.00us     9.22ms
  Latency Distribution
     50%   427.00us
     75%   584.00us
     90%   791.00us
     99%     1.74ms

## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    235979.03   15555.32  264690.05
  Latency      422.56us   210.88us     6.95ms
  Latency Distribution
     50%   362.00us
     75%   469.00us
     90%   665.00us
     99%     1.85ms
### File Upload (POST /upload)
  Reqs/sec    196612.96   10496.83  208053.47
  Latency      504.95us   184.88us     9.50ms
  Latency Distribution
     50%   454.00us
     75%   612.00us
     90%   774.00us
     99%     1.33ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    221957.18   18668.26  252123.27
  Latency      447.83us   203.54us     7.95ms
  Latency Distribution
     50%   388.00us
     75%   507.00us
     90%   656.00us
     99%     1.49ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    159941.91    6744.63  169013.91
  Latency      620.85us   174.94us     6.72ms
  Latency Distribution
     50%   588.00us
     75%   739.00us
     90%     0.94ms
     99%     1.58ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
 16902 / 100000 [=========================================>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  16.90% 9361/s 00m08s
 95897 / 100000 [=================================================================================================================================================================================================================================================>----------]  95.90% 9198/s
  Reqs/sec      9200.00    1116.41   11541.23
  Latency       10.87ms     8.37ms   185.01ms
  Latency Distribution
     50%     9.72ms
     75%    12.12ms
     90%    14.19ms
     99%    19.91ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    255931.31   16540.81  278787.17
  Latency      387.06us   253.15us     9.99ms
  Latency Distribution
     50%   321.00us
     75%   417.00us
     90%   595.00us
     99%     1.94ms
## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Feed of 100 mixed union items (/feed)
  Reqs/sec     88411.56    4092.51   94369.71
  Latency        1.13ms   178.36us     5.71ms
  Latency Distribution
     50%     1.14ms
     75%     1.43ms
     90%     1.69ms
     99%     2.48ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    310820.47   18192.40  343893.28
  Latency      320.88us   290.11us     6.55ms
  Latency Distribution
     50%   221.00us
     75%   331.00us
     90%   554.00us
     99%     2.22ms

### Path Parameter - int (/items/12345)
  Reqs/sec    258360.90   18811.63  281122.63
  Latency      384.14us   296.58us     7.56ms
  Latency Distribution
     50%   293.00us
     75%   414.00us
     90%   632.00us
     99%     2.43ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    272170.36   19118.22  295080.38
  Latency      366.30us   277.33us     7.66ms
  Latency Distribution
     50%   283.00us
     75%   392.00us
     90%   584.00us
     99%     2.26ms

### Header Parameter (/header)
  Reqs/sec    266569.86   14178.46  282091.70
  Latency      372.95us   234.58us    12.55ms
  Latency Distribution
     50%   309.00us
     75%   406.00us
     90%   584.00us
     99%     1.75ms

### Cookie Parameter (/cookie)
  Reqs/sec    256720.63   17881.64  278326.91
  Latency      383.14us   191.79us     6.84ms
  Latency Distribution
     50%   346.00us
     75%   443.00us
     90%   588.00us
     99%     1.52ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    154256.87    9684.50  164949.94
  Latency      641.17us   146.09us     6.30ms
  Latency Distribution
     50%   606.00us
     75%   766.00us
     90%     0.95ms
     99%     1.49ms
