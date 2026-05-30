# Django-Bolt Benchmark
Generated: Sat 30 May 2026 10:21:10 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    141004.55    9229.50  151789.34
  Latency      688.93us   379.00us     6.05ms
  Latency Distribution
     50%   561.00us
     75%     0.86ms
     90%     1.26ms
     99%     2.49ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    103847.98   10143.41  113646.71
  Latency        0.93ms   441.26us     5.70ms
  Latency Distribution
     50%   808.00us
     75%     1.17ms
     90%     1.64ms
     99%     2.99ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    113342.73   12894.59  126159.15
  Latency      833.96us   378.63us     4.75ms
  Latency Distribution
     50%   726.00us
     75%     1.00ms
     90%     1.39ms
     99%     2.55ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec     99403.51    8630.20  108845.77
  Latency        0.99ms   342.61us     5.06ms
  Latency Distribution
     50%     0.92ms
     75%     1.21ms
     90%     1.51ms
     99%     2.39ms
### Cookie Endpoint (/cookie)
  Reqs/sec     93509.28    7535.36  102132.35
  Latency        1.05ms   355.47us     6.25ms
  Latency Distribution
     50%     0.98ms
     75%     1.31ms
     90%     1.67ms
     99%     2.46ms
### Exception Endpoint (/exc)
  Reqs/sec    136118.84   14116.79  147276.28
  Latency      715.25us   319.24us     5.67ms
  Latency Distribution
     50%   644.00us
     75%   819.00us
     90%     1.14ms
     99%     2.06ms
### HTML Response (/html)
  Reqs/sec    145088.90   17314.32  158345.69
  Latency      675.74us   407.72us     6.79ms
  Latency Distribution
     50%   557.00us
     75%   807.00us
     90%     1.15ms
     99%     2.35ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     28364.78    5422.58   35394.09
  Latency        3.49ms     1.57ms    16.52ms
  Latency Distribution
     50%     3.09ms
     75%     4.23ms
     90%     5.76ms
     99%    10.20ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    125234.85   13863.84  138181.42
  Latency      779.20us   463.49us     7.19ms
  Latency Distribution
     50%   658.00us
     75%   846.00us
     90%     1.32ms
     99%     3.07ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    136276.69   11627.54  144675.87
  Latency      711.28us   323.00us     5.60ms
  Latency Distribution
     50%   642.00us
     75%     0.85ms
     90%     1.00ms
     99%     2.29ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
 9999 / 10000 [=========================================================================================================================================================================================================================================]  99.99% 49628/s
  Reqs/sec     41260.33   24134.50   65297.62
  Latency        1.86ms     2.47ms    44.76ms
  Latency Distribution
     50%     1.39ms
     75%     2.08ms
     90%     3.37ms
     99%     7.51ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    107264.48    7934.04  113838.06
  Latency        0.90ms   618.52us     7.06ms
  Latency Distribution
     50%   708.00us
     75%     1.09ms
     90%     1.71ms
     99%     3.87ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    103027.22    5008.36  109428.07
  Latency        0.94ms   610.77us     7.33ms
  Latency Distribution
     50%   731.00us
     75%     1.15ms
     90%     1.81ms
     99%     4.02ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     62245.32   30566.88  146892.88
  Latency        1.86ms     2.21ms    43.45ms
  Latency Distribution
     50%     1.37ms
     75%     2.18ms
     90%     3.45ms
     99%     7.32ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    124612.07   12692.54  131176.05
  Latency      779.11us   585.07us     6.33ms
  Latency Distribution
     50%   536.00us
     75%     0.91ms
     90%     1.60ms
     99%     3.56ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    128178.30   15940.57  142991.65
  Latency      761.48us   587.12us    10.95ms
  Latency Distribution
     50%   573.00us
     75%     0.87ms
     90%     1.49ms
     99%     3.81ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     55839.53    4655.45   61614.87
  Latency        1.76ms   770.19us     7.62ms
  Latency Distribution
     50%     1.58ms
     75%     2.25ms
     90%     3.09ms
     99%     4.86ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     56287.29    9124.02   79379.01
  Latency        1.83ms   812.78us     8.75ms
  Latency Distribution
     50%     1.67ms
     75%     2.33ms
     90%     3.07ms
     99%     5.15ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     63355.82    4728.66   69218.03
  Latency        1.54ms   552.87us     9.38ms
  Latency Distribution
     50%     1.42ms
     75%     1.93ms
     90%     2.47ms
     99%     3.68ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     13730.50    2871.05   16572.97
  Latency        7.26ms     2.79ms    36.16ms
  Latency Distribution
     50%     6.84ms
     75%     8.34ms
     90%    10.12ms
     99%    20.38ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     13949.06    1080.94   15546.86
  Latency        7.13ms     2.80ms    22.23ms
  Latency Distribution
     50%     6.43ms
     75%     8.28ms
     90%    11.71ms
     99%    16.83ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     69093.72    8583.34   81057.19
  Latency        1.44ms   540.75us     6.42ms
  Latency Distribution
     50%     1.32ms
     75%     1.81ms
     90%     2.36ms
     99%     3.79ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    132210.89   10629.57  142527.61
  Latency      736.90us   392.02us     5.93ms
  Latency Distribution
     50%   655.00us
     75%     0.89ms
     90%     1.25ms
     99%     2.66ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    123984.45   19356.69  144201.68
  Latency      781.18us   503.66us     9.00ms
  Latency Distribution
     50%   647.00us
     75%     0.89ms
     90%     1.34ms
     99%     3.17ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14244.74    1779.82   19236.73
  Latency        7.03ms     2.27ms    23.70ms
  Latency Distribution
     50%     6.97ms
     75%     8.63ms
     90%    10.10ms
     99%    13.82ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     11030.62    1317.96   13447.15
  Latency        9.05ms     3.61ms    31.93ms
  Latency Distribution
     50%     8.29ms
     75%    10.95ms
     90%    14.22ms
     99%    22.08ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     17174.92    1654.10   21631.34
  Latency        5.83ms     1.84ms    19.09ms
  Latency Distribution
     50%     5.51ms
     75%     7.24ms
     90%     8.72ms
     99%    11.81ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12342.42     906.37   14067.77
  Latency        8.06ms     2.87ms    25.73ms
  Latency Distribution
     50%     7.55ms
     75%     9.63ms
     90%    12.21ms
     99%    18.25ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     89815.12    7578.72   98479.05
  Latency        1.08ms   387.44us     5.81ms
  Latency Distribution
     50%     0.99ms
     75%     1.35ms
     90%     1.74ms
     99%     2.69ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     90405.36    5093.60   95768.35
  Latency        1.08ms   343.07us     4.29ms
  Latency Distribution
     50%     1.01ms
     75%     1.34ms
     90%     1.73ms
     99%     2.51ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     56175.55    7879.21   65784.91
  Latency        1.76ms   644.76us     8.84ms
  Latency Distribution
     50%     1.60ms
     75%     2.12ms
     90%     2.75ms
     99%     4.62ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     84561.72   10690.64   93177.38
  Latency        1.17ms   459.62us     6.51ms
  Latency Distribution
     50%     1.05ms
     75%     1.47ms
     90%     1.88ms
     99%     3.19ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     90531.41    6893.15   96557.06
  Latency        1.08ms   364.15us     4.88ms
  Latency Distribution
     50%     0.99ms
     75%     1.34ms
     90%     1.73ms
     99%     2.73ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     95532.03    8539.09  105135.26
  Latency        1.05ms   339.84us     4.28ms
  Latency Distribution
     50%     0.98ms
     75%     1.29ms
     90%     1.64ms
     99%     2.56ms
### CBV Response Types (/cbv-response)
  Reqs/sec     96431.33    8133.29  105686.88
  Latency        1.04ms   379.61us     4.97ms
  Latency Distribution
     50%     0.95ms
     75%     1.29ms
     90%     1.66ms
     99%     2.95ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     14901.77    1909.48   17323.95
  Latency        6.69ms     2.42ms    20.66ms
  Latency Distribution
     50%     6.18ms
     75%     8.18ms
     90%    10.47ms
     99%    14.75ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    112440.24   11569.94  121427.03
  Latency        0.86ms   443.53us     5.80ms
  Latency Distribution
     50%   758.00us
     75%     1.08ms
     90%     1.46ms
     99%     2.94ms
### File Upload (POST /upload)
  Reqs/sec    102469.89    6854.36  109871.78
  Latency        0.95ms   405.80us     5.64ms
  Latency Distribution
     50%     0.87ms
     75%     1.16ms
     90%     1.57ms
     99%     2.76ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec     97224.63   10351.60  109536.97
  Latency        1.01ms   394.66us     5.07ms
  Latency Distribution
     50%     0.96ms
     75%     1.26ms
     90%     1.64ms
     99%     2.78ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
 1699 / 10000 [=======================================>--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  16.99% 8450/s
  Reqs/sec      8630.46    1379.61   10638.57
  Latency       11.54ms     3.64ms    37.67ms
  Latency Distribution
     50%    10.73ms
     75%    13.63ms
     90%    16.17ms
     99%    26.52ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    128745.55   10326.56  139572.21
  Latency      757.04us   404.96us     7.35ms
  Latency Distribution
     50%   650.00us
     75%     0.91ms
     90%     1.23ms
     99%     2.71ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    128586.11    9394.27  137912.30
  Latency      745.35us   384.70us     8.09ms
  Latency Distribution
     50%   643.00us
     75%     0.88ms
     90%     1.27ms
     99%     2.55ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     81139.62    8370.56   89796.44
  Latency        1.21ms   462.29us     6.65ms
  Latency Distribution
     50%     1.13ms
     75%     1.46ms
     90%     1.86ms
     99%     3.08ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    116266.13   14952.83  136217.04
  Latency      801.67us   462.16us     5.62ms
  Latency Distribution
     50%   674.00us
     75%     0.96ms
     90%     1.38ms
     99%     2.74ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec     95246.12   10791.01  102900.55
  Latency        1.00ms   312.67us     4.53ms
  Latency Distribution
     50%     0.94ms
     75%     1.23ms
     90%     1.55ms
     99%     2.42ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec     75890.66   24067.63   94520.18
  Latency        1.30ms   759.11us    13.50ms
  Latency Distribution
     50%     1.08ms
     75%     1.55ms
     90%     2.28ms
     99%     4.61ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec     87045.84   10045.22   95061.35
  Latency        1.13ms   411.91us     5.18ms
  Latency Distribution
     50%     1.02ms
     75%     1.45ms
     90%     1.85ms
     99%     2.96ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec     86997.20   16327.54  115234.81
  Latency        1.19ms   479.25us     6.73ms
  Latency Distribution
     50%     1.09ms
     75%     1.46ms
     90%     1.91ms
     99%     3.24ms

### Single union item — Like branch (/feed/2)
  Reqs/sec     82731.75    7521.00   91669.03
  Latency        1.19ms   418.76us     5.17ms
  Latency Distribution
     50%     1.09ms
     75%     1.50ms
     90%     1.93ms
     99%     2.96ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     64073.60    5370.43   72578.96
  Latency        1.52ms   459.14us     6.99ms
  Latency Distribution
     50%     1.48ms
     75%     1.82ms
     90%     2.24ms
     99%     3.37ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    149600.15   13170.37  161916.11
  Latency      652.09us   393.61us     7.35ms
  Latency Distribution
     50%   551.00us
     75%   768.00us
     90%     1.15ms
     99%     2.41ms

### Path Parameter - int (/items/12345)
  Reqs/sec    132158.78    9372.04  143056.86
  Latency      728.43us   347.09us     7.00ms
  Latency Distribution
     50%   642.00us
     75%     0.89ms
     90%     1.27ms
     99%     2.26ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    131224.70   18815.09  143749.42
  Latency      745.43us   433.22us     6.64ms
  Latency Distribution
     50%   636.00us
     75%     0.89ms
     90%     1.22ms
     99%     2.76ms

### Header Parameter (/header)
  Reqs/sec     82137.44    9054.86   92204.22
  Latency        1.20ms   491.87us     6.02ms
  Latency Distribution
     50%     1.07ms
     75%     1.49ms
     90%     1.96ms
     99%     3.50ms

### Cookie Parameter (/cookie)
  Reqs/sec     79334.68   10996.73   89678.36
  Latency        1.26ms   538.36us     6.12ms
  Latency Distribution
     50%     1.14ms
     75%     1.56ms
     90%     2.05ms
     99%     3.79ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     71392.62   10810.28   84950.67
  Latency        1.38ms   535.81us     8.06ms
  Latency Distribution
     50%     1.27ms
     75%     1.71ms
     90%     2.21ms
     99%     3.56ms
