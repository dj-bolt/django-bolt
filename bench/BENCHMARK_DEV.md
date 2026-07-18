# Django-Bolt Benchmark
Generated: Sat Jul 18 11:37:49 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    198741.34    5416.88  204673.96
  Latency      491.05us   333.36us     5.61ms
  Latency Distribution
     50%   428.00us
     75%   548.00us
     90%   742.00us
     99%     1.89ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    130902.34   13453.05  138958.21
  Latency      749.02us   290.89us     5.02ms
  Latency Distribution
     50%   671.00us
     75%     0.89ms
     90%     1.11ms
     99%     1.94ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    131413.89   13534.80  142130.83
  Latency      746.55us   383.02us     5.92ms
  Latency Distribution
     50%   651.00us
     75%     0.89ms
     90%     1.13ms
     99%     2.42ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    162588.54   21479.25  175009.69
  Latency      601.28us   579.55us     5.95ms
  Latency Distribution
     50%   376.00us
     75%   710.00us
     90%     1.27ms
     99%     3.60ms
### Cookie Endpoint (/cookie)
  Reqs/sec    170734.82   11669.36  178931.29
  Latency      570.95us   528.81us     7.60ms
  Latency Distribution
     50%   392.00us
     75%   683.00us
     90%     1.07ms
     99%     3.35ms
### Exception Endpoint (/exc)
  Reqs/sec    170670.27   19010.06  184236.69
  Latency      570.44us   643.41us     8.01ms
  Latency Distribution
     50%   358.00us
     75%   624.00us
     90%     1.06ms
     99%     3.93ms
### HTML Response (/html)
  Reqs/sec    187696.99   20928.84  203109.58
  Latency      518.00us   454.49us     6.18ms
  Latency Distribution
     50%   344.00us
     75%   639.00us
     90%     1.01ms
     99%     2.76ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     43318.35   11491.40   51077.60
  Latency        2.31ms     1.77ms    24.92ms
  Latency Distribution
     50%     1.91ms
     75%     2.69ms
     90%     3.86ms
     99%    11.19ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    168463.65   26106.38  200042.33
  Latency      609.77us   582.40us     8.15ms
  Latency Distribution
     50%   419.00us
     75%   761.00us
     90%     1.21ms
     99%     3.36ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    180757.19   16201.53  194011.84
  Latency      539.66us   565.65us     7.03ms
  Latency Distribution
     50%   337.00us
     75%   592.00us
     90%     1.04ms
     99%     3.56ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     85358.00   16656.91   93875.35
  Latency        1.16ms     2.95ms    45.03ms
  Latency Distribution
     50%   686.00us
     75%     1.21ms
     90%     1.93ms
     99%     8.21ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    197403.02   20445.10  211550.53
  Latency      496.10us   458.35us     7.76ms
  Latency Distribution
     50%   364.00us
     75%   620.00us
     90%     0.92ms
     99%     2.70ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    169378.34   16098.86  179262.01
  Latency      576.93us   425.90us     4.43ms
  Latency Distribution
     50%   443.00us
     75%   730.00us
     90%     1.05ms
     99%     2.88ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     61046.65   41708.97   97135.07
  Latency        1.11ms     2.69ms    44.22ms
  Latency Distribution
     50%   582.00us
     75%     1.11ms
     90%     2.08ms
     99%     6.07ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    192696.88   18959.70  207859.39
  Latency      511.64us   476.27us     5.79ms
  Latency Distribution
     50%   320.00us
     75%   636.00us
     90%     1.00ms
     99%     3.04ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    168525.95   24942.22  185874.45
  Latency      581.06us   573.52us     6.30ms
  Latency Distribution
     50%   331.00us
     75%   726.00us
     90%     1.26ms
     99%     3.37ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     85732.92    6032.18   89663.44
  Latency        1.15ms   782.69us    10.77ms
  Latency Distribution
     50%     0.95ms
     75%     1.35ms
     90%     2.12ms
     99%     4.56ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     84934.86    7251.25   88885.01
  Latency        1.16ms   680.90us     9.65ms
  Latency Distribution
     50%     0.99ms
     75%     1.40ms
     90%     2.09ms
     99%     4.30ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec    102513.97    8723.47  109402.70
  Latency        0.96ms   407.15us     6.21ms
  Latency Distribution
     50%     0.85ms
     75%     1.19ms
     90%     1.60ms
     99%     2.86ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     23317.63    2804.20   25167.93
  Latency        4.24ms     1.90ms    21.45ms
  Latency Distribution
     50%     3.85ms
     75%     5.20ms
     90%     7.06ms
     99%    10.38ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     20807.38    3055.19   23615.11
  Latency        4.66ms     4.16ms    86.49ms
  Latency Distribution
     50%     4.22ms
     75%     5.56ms
     90%     6.93ms
     99%     9.90ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    104046.06    7795.36  110447.51
  Latency        0.94ms   426.24us     5.73ms
  Latency Distribution
     50%   822.00us
     75%     1.17ms
     90%     1.62ms
     99%     2.80ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    184154.59   15656.56  193671.72
  Latency      534.77us   516.22us     7.08ms
  Latency Distribution
     50%   367.00us
     75%   589.00us
     90%     1.03ms
     99%     3.32ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    175880.09   12940.30  188169.43
  Latency      558.56us   568.66us     6.84ms
  Latency Distribution
     50%   371.00us
     75%   642.00us
     90%     1.03ms
     99%     3.66ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     13604.94    1759.86   14542.54
  Latency        7.16ms     2.53ms    20.53ms
  Latency Distribution
     50%     6.95ms
     75%     8.81ms
     90%    11.95ms
     99%    13.72ms
### Users Full10 (Sync) (/users/sync-full10)
 6550 / 10000 [========================================================================================================================================================================================================================>-----------------------------------------------------------------------------------------------------------------]  65.50% 10868/s
  Reqs/sec     10796.10    1559.28   14279.45
  Latency        9.26ms     6.40ms    86.64ms
  Latency Distribution
     50%     8.10ms
     75%    11.10ms
     90%    14.34ms
     99%    22.27ms
### Users Mini10 (Async) (/users/mini10)
 3275 / 10000 [============================================================================================================>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  32.75% 16337/s
  Reqs/sec     16876.51    1097.88   19116.77
  Latency        5.91ms     3.39ms    65.60ms
  Latency Distribution
     50%     5.48ms
     75%     6.72ms
     90%     8.06ms
     99%     9.77ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     14896.41     911.20   17230.81
  Latency        6.69ms     2.22ms    20.94ms
  Latency Distribution
     50%     6.21ms
     75%     7.98ms
     90%     9.95ms
     99%    14.74ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    125918.64   11447.27  133100.92
  Latency      781.18us   259.83us     3.25ms
  Latency Distribution
     50%   714.00us
     75%     0.94ms
     90%     1.23ms
     99%     2.06ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    126453.20   11550.58  132433.13
  Latency      777.23us   223.66us     3.11ms
  Latency Distribution
     50%   730.00us
     75%     0.93ms
     90%     1.18ms
     99%     1.89ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     74225.67    4691.34   77684.05
  Latency        1.34ms   418.69us     4.60ms
  Latency Distribution
     50%     1.24ms
     75%     1.63ms
     90%     2.11ms
     99%     3.22ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    122528.32   10220.02  130825.63
  Latency      800.42us   272.80us     4.51ms
  Latency Distribution
     50%   733.00us
     75%     0.97ms
     90%     1.25ms
     99%     2.00ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    116260.45    9177.02  125155.09
  Latency      828.74us   302.11us     4.52ms
  Latency Distribution
     50%   758.00us
     75%     1.00ms
     90%     1.26ms
     99%     1.96ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    113624.12   18621.28  127793.42
  Latency      806.84us   251.39us     4.19ms
  Latency Distribution
     50%   739.00us
     75%     0.98ms
     90%     1.27ms
     99%     1.92ms
### CBV Response Types (/cbv-response)
  Reqs/sec    129733.23   10368.28  138232.51
  Latency      757.18us   247.57us     3.76ms
  Latency Distribution
     50%   702.00us
     75%     0.92ms
     90%     1.15ms
     99%     1.92ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     18610.53    1118.56   19809.60
  Latency        5.34ms     1.39ms    16.14ms
  Latency Distribution
     50%     5.01ms
     75%     6.75ms
     90%     7.79ms
     99%     9.34ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    159703.47    9252.03  164749.37
  Latency      616.05us   192.71us     3.97ms
  Latency Distribution
     50%   592.00us
     75%   733.00us
     90%     0.87ms
     99%     1.42ms
### File Upload (POST /upload)
  Reqs/sec    130530.16    9108.73  138556.57
  Latency      752.10us   245.59us     4.37ms
  Latency Distribution
     50%   725.00us
     75%     0.92ms
     90%     1.10ms
     99%     1.74ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    125358.12    8638.88  130508.02
  Latency      782.39us   294.33us     4.39ms
  Latency Distribution
     50%   731.00us
     75%     0.99ms
     90%     1.19ms
     99%     1.88ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    140729.28   12488.68  148003.83
  Latency      698.08us   281.11us     5.71ms
  Latency Distribution
     50%   696.00us
     75%   847.00us
     90%     1.02ms
     99%     1.94ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    114593.85    8274.68  118729.76
  Latency        0.86ms   300.12us     5.71ms
  Latency Distribution
     50%     0.85ms
     75%     1.02ms
     90%     1.24ms
     99%     1.90ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      8977.70    1410.27   10599.92
  Latency       11.09ms     6.83ms    81.80ms
  Latency Distribution
     50%     9.76ms
     75%    13.15ms
     90%    14.56ms
     99%    22.99ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    171487.01   18670.98  183265.43
  Latency      573.18us   307.72us     5.08ms
  Latency Distribution
     50%   494.00us
     75%   686.00us
     90%     0.86ms
     99%     1.59ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    167269.95   23783.82  184136.03
  Latency      584.53us   227.77us     4.05ms
  Latency Distribution
     50%   527.00us
     75%   667.00us
     90%   848.00us
     99%     1.78ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec    111607.30   10279.54  122189.29
  Latency        0.89ms   268.16us     3.61ms
  Latency Distribution
     50%   829.00us
     75%     1.09ms
     90%     1.38ms
     99%     2.13ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    162316.71   14947.66  174269.60
  Latency      602.82us   285.87us     5.06ms
  Latency Distribution
     50%   548.00us
     75%   720.00us
     90%     0.86ms
     99%     1.79ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    125422.13    8116.41  129894.87
  Latency      783.38us   248.94us     3.88ms
  Latency Distribution
     50%   732.00us
     75%     0.95ms
     90%     1.19ms
     99%     1.85ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    123986.02    7801.15  129214.17
  Latency      789.76us   216.42us     2.92ms
  Latency Distribution
     50%   732.00us
     75%     0.96ms
     90%     1.23ms
     99%     1.86ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    170992.10   12701.50  179058.87
  Latency      569.94us   260.79us     4.53ms
  Latency Distribution
     50%   525.00us
     75%   683.00us
     90%     0.85ms
     99%     1.67ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    169923.86   16456.46  181297.93
  Latency      573.30us   207.87us     5.09ms
  Latency Distribution
     50%   549.00us
     75%   667.00us
     90%   818.00us
     99%     1.44ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    170919.61   17054.44  183780.98
  Latency      573.73us   214.66us     5.01ms
  Latency Distribution
     50%   562.00us
     75%   661.00us
     90%   809.00us
     99%     1.36ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     78486.74    4614.15   82834.26
  Latency        1.26ms   293.34us     5.16ms
  Latency Distribution
     50%     1.22ms
     75%     1.44ms
     90%     1.66ms
     99%     2.42ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    208187.15   25752.60  227469.15
  Latency      475.54us   210.99us     5.60ms
  Latency Distribution
     50%   444.00us
     75%   542.00us
     90%   693.00us
     99%     1.32ms

### Path Parameter - int (/items/12345)
  Reqs/sec    181191.59   20702.68  195273.73
  Latency      538.97us   178.85us     3.81ms
  Latency Distribution
     50%   521.00us
     75%   634.00us
     90%   746.00us
     99%     1.35ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    177991.09   15603.08  191402.62
  Latency      550.87us   229.40us     4.47ms
  Latency Distribution
     50%   518.00us
     75%   649.00us
     90%   769.00us
     99%     1.59ms

### Header Parameter (/header)
  Reqs/sec    166869.08   13402.76  176523.73
  Latency      585.64us   183.35us     3.28ms
  Latency Distribution
     50%   556.00us
     75%   703.00us
     90%     0.85ms
     99%     1.38ms

### Cookie Parameter (/cookie)
  Reqs/sec    134435.32   56248.54  174550.39
  Latency      607.04us   266.75us     6.64ms
  Latency Distribution
     50%   577.00us
     75%   751.00us
     90%     0.94ms
     99%     1.82ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    101330.27    8440.55  108327.84
  Latency        0.97ms   302.54us     4.43ms
  Latency Distribution
     50%     0.91ms
     75%     1.17ms
     90%     1.45ms
     99%     2.21ms
