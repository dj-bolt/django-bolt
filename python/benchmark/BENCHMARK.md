# Django-Bolt Benchmark
Generated: Sun Aug 30 11:18:42 AM PKT 2026
Config: 8 processes × 1 workers | C=100 N=100000

## Root Endpoint Performance
  Reqs/sec    303178.31   16782.91  326182.62
  Latency      326.65us   346.71us     8.12ms
  Latency Distribution
     50%   228.00us
     75%   324.00us
     90%   520.00us
     99%     2.73ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    174581.87   17795.59  196446.10
  Latency      570.90us   317.11us     8.33ms
  Latency Distribution
     50%   478.00us
     75%   636.00us
     90%     0.90ms
     99%     2.52ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    192499.81   16298.09  211304.98
  Latency      516.38us   227.99us     6.71ms
  Latency Distribution
     50%   464.00us
     75%   612.00us
     90%   808.00us
     99%     1.79ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    258076.14   26530.48  294140.09
  Latency      382.00us   230.39us     5.40ms
  Latency Distribution
     50%   316.00us
     75%   412.00us
     90%   602.00us
     99%     1.76ms
### Cookie Endpoint (/cookie)
  Reqs/sec    259742.14   25013.08  287343.98
  Latency      382.39us   270.00us     9.45ms
  Latency Distribution
     50%   308.00us
     75%   424.00us
     90%   602.00us
     99%     1.96ms
### Exception Endpoint (/exc)
  Reqs/sec    241332.31   20788.53  269652.34
  Latency      410.32us   272.33us    11.40ms
  Latency Distribution
     50%   333.00us
     75%   440.00us
     90%   652.00us
     99%     2.09ms
### HTML Response (/html)
  Reqs/sec    287718.80   17529.12  322213.39
  Latency      344.29us   290.46us     7.01ms
  Latency Distribution
     50%   262.00us
     75%   358.00us
     90%   553.00us
     99%     2.15ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     51649.64    5165.92   56708.20
  Latency        1.93ms   533.39us    20.19ms
  Latency Distribution
     50%     1.78ms
     75%     2.30ms
     90%     2.93ms
     99%     4.31ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
 32903 / 100000 [==================================================================================>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------]  32.90% 162295/s
 98498 / 100000 [======================================================================================================================================================================================================================================================>---]  98.50% 162816/s
  Reqs/sec    162983.53    9681.66  176980.56
  Latency      610.76us   274.35us    13.12ms
  Latency Distribution
     50%   589.00us
     75%   683.00us
     90%   833.00us
     99%     1.88ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    172203.57    9519.12  183491.53
  Latency      578.32us   159.42us     6.63ms
  Latency Distribution
     50%   563.00us
     75%   656.00us
     90%   786.00us
     99%     1.33ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     88460.17    8584.95   96269.75
  Latency        1.13ms   485.26us    15.81ms
  Latency Distribution
     50%     1.11ms
     75%     1.31ms
     90%     1.54ms
     99%     2.80ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    187371.38    7993.75  198410.16
  Latency      531.39us   186.82us     5.60ms
  Latency Distribution
     50%   495.00us
     75%   614.00us
     90%   801.00us
     99%     1.50ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    162110.86    6927.45  171729.62
  Latency      614.29us   160.74us     5.57ms
  Latency Distribution
     50%   563.00us
     75%   711.00us
     90%     0.88ms
     99%     1.39ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     85699.71    5921.58   94205.03
  Latency        1.16ms   339.02us    16.04ms
  Latency Distribution
     50%     1.09ms
     75%     1.35ms
     90%     1.76ms
     99%     3.02ms

## Authentication & Authorization Performance
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     40038.76    1472.81   42491.03
  Latency        2.49ms   237.84us     9.14ms
  Latency Distribution
     50%     2.44ms
     75%     2.74ms
     90%     3.60ms
     99%     4.15ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    154967.36    8438.73  170828.00
  Latency      639.01us   169.92us     6.60ms
  Latency Distribution
     50%   607.00us
     75%   757.00us
     90%     0.92ms
     99%     1.50ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    265337.37   14851.48  287506.15
  Latency      374.47us   325.10us     8.15ms
  Latency Distribution
     50%   287.00us
     75%   386.00us
     90%   565.00us
     99%     2.58ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    250639.38   16203.47  265017.79
  Latency      395.24us   294.04us     7.17ms
  Latency Distribution
     50%   327.00us
     75%   420.00us
     90%   586.00us
     99%     2.30ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
 61900 / 100000 [=======================================================================================================================================================>--------------------------------------------------------------------------------------------]  61.90% 20597/s 00m01s
  Reqs/sec     20693.12    1005.78   22003.15
  Latency        4.83ms     1.47ms    57.06ms
  Latency Distribution
     50%     4.75ms
     75%     6.07ms
     90%     7.28ms
     99%     9.14ms
### Users Full10 (Sync) (/users/sync-full10)
 28496 / 100000 [=====================================================================>------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  28.50% 15786/s 00m04s
  Reqs/sec     15306.65    1246.87   19372.46
  Latency        6.53ms     2.42ms    53.47ms
  Latency Distribution
     50%     5.82ms
     75%     7.88ms
     90%    10.52ms
     99%    17.00ms
### Users Mini10 (Async) (/users/mini10)
 91897 / 100000 [======================================================================================================================================================================================================================================>--------------------]  91.90% 25485/s
  Reqs/sec     25512.99    1466.31   27178.33
  Latency        3.92ms     0.95ms    32.86ms
  Latency Distribution
     50%     3.81ms
     75%     4.84ms
     90%     5.81ms
     99%     7.72ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    169814.55   37236.21  235408.78
  Latency      588.99us   437.09us     8.53ms
  Latency Distribution
     50%   416.00us
     75%   628.00us
     90%     1.05ms
     99%     3.47ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    206312.64   16599.29  232447.31
  Latency      482.23us   216.41us     8.59ms
  Latency Distribution
     50%   418.00us
     75%   569.00us
     90%   775.00us
     99%     1.77ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    222154.16   13955.81  246086.61
  Latency      446.72us   190.56us    12.01ms
  Latency Distribution
     50%   383.00us
     75%   544.00us
     90%   706.00us
     99%     1.33ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    196236.76   20176.27  215260.93
  Latency      501.95us   211.55us     6.50ms
  Latency Distribution
     50%   443.00us
     75%   597.00us
     90%   778.00us
     99%     1.64ms

## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    236416.69   15639.92  258437.83
  Latency      421.81us   263.12us     7.03ms
  Latency Distribution
     50%   354.00us
     75%   471.00us
     90%   663.00us
     99%     1.83ms
### File Upload (POST /upload)
  Reqs/sec    186225.83   10614.02  204591.18
  Latency      534.03us   205.83us     7.29ms
  Latency Distribution
     50%   476.00us
     75%   604.00us
     90%   823.00us
     99%     1.66ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    221656.62   19205.12  248451.57
  Latency      449.21us   214.62us    12.25ms
  Latency Distribution
     50%   396.00us
     75%   508.00us
     90%   669.00us
     99%     1.49ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    162659.51    6283.07  169306.76
  Latency      611.99us   134.61us     5.05ms
  Latency Distribution
     50%   578.00us
     75%   750.00us
     90%     0.90ms
     99%     1.42ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9027.32    1556.66   11591.14
  Latency       10.98ms     8.75ms   192.97ms
  Latency Distribution
     50%     9.88ms
     75%    11.06ms
     90%    14.92ms
     99%    20.96ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    255110.28   19290.87  282105.86
  Latency      385.99us   287.75us     7.65ms
  Latency Distribution
     50%   307.00us
     75%   420.00us
     90%   595.00us
     99%     2.23ms
## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Feed of 100 mixed union items (/feed)
  Reqs/sec     84212.84    8170.31   95447.50
  Latency        1.18ms   274.21us     9.79ms
  Latency Distribution
     50%     1.06ms
     75%     1.52ms
     90%     1.86ms
     99%     2.90ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    302845.00   27859.61  329866.80
  Latency      322.09us   257.99us     8.09ms
  Latency Distribution
     50%   231.00us
     75%   359.00us
     90%   560.00us
     99%     1.99ms

### Path Parameter - int (/items/12345)
  Reqs/sec    262794.30   22249.36  294314.36
  Latency      377.96us   302.94us     9.19ms
  Latency Distribution
     50%   286.00us
     75%   407.00us
     90%   620.00us
     99%     2.31ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    244161.24   44419.00  291117.99
  Latency      408.38us   330.60us     9.43ms
  Latency Distribution
     50%   304.00us
     75%   430.00us
     90%   682.00us
     99%     2.68ms

### Header Parameter (/header)
  Reqs/sec    251025.36   22598.82  288803.08
  Latency      393.21us   277.91us    10.39ms
  Latency Distribution
     50%   314.00us
     75%   438.00us
     90%   618.00us
     99%     2.16ms

### Cookie Parameter (/cookie)
  Reqs/sec    262952.95   19600.79  282687.84
  Latency      377.43us   284.84us    10.02ms
  Latency Distribution
     50%   315.00us
     75%   399.00us
     90%   564.00us
     99%     1.91ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    153176.20   12120.46  161786.97
  Latency      648.54us   194.24us     6.51ms
  Latency Distribution
     50%   623.00us
     75%   799.00us
     90%     0.99ms
     99%     1.63ms
