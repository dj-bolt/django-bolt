# Django-Bolt Benchmark
Generated: Mon 25 May 2026 12:08:44 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    153989.39   26004.68  174708.85
  Latency      655.48us   481.38us     5.20ms
  Latency Distribution
     50%   509.00us
     75%   732.00us
     90%     1.12ms
     99%     3.27ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    116518.13   13885.76  127542.21
  Latency      824.96us   412.49us     6.46ms
  Latency Distribution
     50%   744.00us
     75%     0.98ms
     90%     1.27ms
     99%     2.54ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    115464.84   10232.24  126042.69
  Latency      830.33us   491.00us     6.33ms
  Latency Distribution
     50%   675.00us
     75%     1.01ms
     90%     1.44ms
     99%     3.02ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    100486.38   12900.64  115858.65
  Latency        1.01ms   333.66us     5.30ms
  Latency Distribution
     50%     0.94ms
     75%     1.25ms
     90%     1.57ms
     99%     2.50ms
### Cookie Endpoint (/cookie)
  Reqs/sec     95960.69    5885.11  103038.12
  Latency        1.01ms   341.69us     4.74ms
  Latency Distribution
     50%     0.95ms
     75%     1.25ms
     90%     1.62ms
     99%     2.45ms
### Exception Endpoint (/exc)
  Reqs/sec    129482.65   16055.80  138678.48
  Latency      753.01us   354.64us     6.33ms
  Latency Distribution
     50%   664.00us
     75%     0.90ms
     90%     1.22ms
     99%     2.28ms
### HTML Response (/html)
  Reqs/sec    157793.26   14680.39  169614.05
  Latency      626.91us   299.58us     6.49ms
  Latency Distribution
     50%   541.00us
     75%   715.00us
     90%     1.05ms
     99%     2.09ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     33329.81    5544.38   36104.40
  Latency        2.99ms     1.30ms    15.88ms
  Latency Distribution
     50%     2.77ms
     75%     3.70ms
     90%     4.75ms
     99%     7.97ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    171493.68   19243.31  185484.53
  Latency      563.71us   518.72us    10.34ms
  Latency Distribution
     50%   447.00us
     75%   603.00us
     90%     0.86ms
     99%     3.58ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    157678.30   15109.27  174121.68
  Latency      603.62us   365.54us     8.25ms
  Latency Distribution
     50%   551.00us
     75%   665.00us
     90%   801.00us
     99%     2.37ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     70893.76    4934.02   73630.65
  Latency        1.39ms   422.56us     7.15ms
  Latency Distribution
     50%     1.39ms
     75%     1.61ms
     90%     2.06ms
     99%     2.86ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     67519.78    4311.48   71033.87
  Latency        1.44ms   394.35us     5.84ms
  Latency Distribution
     50%     1.44ms
     75%     1.77ms
     90%     2.05ms
     99%     2.88ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     78076.97    5587.32   81668.21
  Latency        1.25ms   281.70us     5.37ms
  Latency Distribution
     50%     1.20ms
     75%     1.49ms
     90%     1.79ms
     99%     2.42ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
 3275 / 10000 [==========================>-------------------------------------------------------]  32.75% 16308/s
  Reqs/sec     16865.22    1249.42   17834.32
  Latency        5.90ms     2.08ms    15.54ms
  Latency Distribution
     50%     5.86ms
     75%     7.83ms
     90%     9.20ms
     99%    12.22ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14814.05     701.42   15755.37
  Latency        6.70ms     1.52ms    16.13ms
  Latency Distribution
     50%     6.53ms
     75%     7.87ms
     90%     9.18ms
     99%    11.33ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     77394.38    3833.73   82280.63
  Latency        1.26ms   479.72us     6.32ms
  Latency Distribution
     50%     1.15ms
     75%     1.72ms
     90%     2.18ms
     99%     3.25ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    115303.73   20449.03  137748.02
  Latency        0.88ms   752.77us     8.32ms
  Latency Distribution
     50%   614.00us
     75%     0.97ms
     90%     1.75ms
     99%     4.59ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    111068.13    7980.02  117352.15
  Latency        0.87ms   559.56us     7.18ms
  Latency Distribution
     50%   720.00us
     75%     1.04ms
     90%     1.54ms
     99%     3.46ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
 5575 / 10000 [=============================================>------------------------------------]  55.75% 13915/s
  Reqs/sec     14323.69    1749.18   21563.94
  Latency        7.04ms     1.44ms    20.99ms
  Latency Distribution
     50%     7.20ms
     75%     8.22ms
     90%     8.78ms
     99%     9.99ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10288.11     891.16   12129.66
  Latency        9.66ms     4.93ms    38.51ms
  Latency Distribution
     50%     8.18ms
     75%    11.83ms
     90%    17.19ms
     99%    27.05ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     17743.65    1661.08   22257.25
  Latency        5.64ms     1.77ms    15.32ms
  Latency Distribution
     50%     5.48ms
     75%     6.92ms
     90%     8.43ms
     99%    11.12ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     11784.72     814.42   13801.95
  Latency        8.44ms     4.14ms    36.48ms
  Latency Distribution
     50%     7.21ms
     75%    10.24ms
     90%    14.43ms
     99%    23.64ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     91404.02    6470.82   98101.20
  Latency        1.07ms   372.71us     5.39ms
  Latency Distribution
     50%     0.99ms
     75%     1.31ms
     90%     1.67ms
     99%     2.61ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    106079.53    7391.21  112236.24
  Latency        0.93ms   300.12us     4.97ms
  Latency Distribution
     50%     0.86ms
     75%     1.13ms
     90%     1.44ms
     99%     2.17ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     63314.28    3719.53   66190.32
  Latency        1.56ms   448.21us     5.20ms
  Latency Distribution
     50%     1.51ms
     75%     1.91ms
     90%     2.38ms
     99%     3.33ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     85976.21   11040.23   96094.64
  Latency        1.15ms   555.45us     7.08ms
  Latency Distribution
     50%     1.01ms
     75%     1.45ms
     90%     1.92ms
     99%     3.32ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     99007.28    7017.41  103016.80
  Latency        0.99ms   298.73us     6.23ms
  Latency Distribution
     50%     0.93ms
     75%     1.21ms
     90%     1.51ms
     99%     2.24ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    101173.38    7691.12  105478.66
  Latency        0.97ms   364.04us     5.01ms
  Latency Distribution
     50%     0.88ms
     75%     1.19ms
     90%     1.60ms
     99%     2.54ms
### CBV Response Types (/cbv-response)
  Reqs/sec    104743.00    7292.28  110673.92
  Latency        0.93ms   303.88us     5.45ms
  Latency Distribution
     50%     0.86ms
     75%     1.15ms
     90%     1.48ms
     99%     2.31ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     15602.53    1206.31   17046.57
  Latency        6.37ms     1.91ms    20.32ms
  Latency Distribution
     50%     6.27ms
     75%     7.51ms
     90%     9.09ms
     99%    12.32ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    128344.24   11316.14  136343.55
  Latency      759.17us   354.44us     5.36ms
  Latency Distribution
     50%   681.00us
     75%     0.90ms
     90%     1.20ms
     99%     2.49ms
### File Upload (POST /upload)
  Reqs/sec    118435.62   10143.50  127214.71
  Latency      815.81us   302.65us     5.16ms
  Latency Distribution
     50%   751.00us
     75%     0.94ms
     90%     1.14ms
     99%     2.00ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    114098.75   11919.88  125515.26
  Latency        0.87ms   391.54us     5.86ms
  Latency Distribution
     50%   809.00us
     75%     1.02ms
     90%     1.28ms
     99%     2.65ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9535.66    1035.94   13203.69
  Latency       10.49ms     2.73ms    23.44ms
  Latency Distribution
     50%    10.58ms
     75%    12.74ms
     90%    14.17ms
     99%    17.34ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    130314.66   11068.75  141328.71
  Latency      743.37us   401.43us     7.12ms
  Latency Distribution
     50%   633.00us
     75%     0.87ms
     90%     1.25ms
     99%     2.54ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    134470.95   11746.69  144702.07
  Latency      729.84us   337.11us     6.29ms
  Latency Distribution
     50%   657.00us
     75%     0.86ms
     90%     1.20ms
     99%     2.23ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     80517.68    5167.00   86893.67
  Latency        1.22ms   504.21us     6.29ms
  Latency Distribution
     50%     1.10ms
     75%     1.49ms
     90%     1.92ms
     99%     3.65ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    134009.60    9447.49  140524.65
  Latency      727.75us   355.18us     5.98ms
  Latency Distribution
     50%   660.00us
     75%     0.87ms
     90%     1.15ms
     99%     1.97ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    104171.65   10088.40  110384.71
  Latency        0.94ms   300.19us     5.75ms
  Latency Distribution
     50%     0.88ms
     75%     1.15ms
     90%     1.47ms
     99%     2.18ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec     99135.98    7612.52  105781.28
  Latency        0.99ms   358.87us     5.31ms
  Latency Distribution
     50%     0.91ms
     75%     1.22ms
     90%     1.59ms
     99%     2.47ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec     89433.94   18712.17  101948.71
  Latency        1.01ms   300.83us     5.36ms
  Latency Distribution
     50%     0.96ms
     75%     1.23ms
     90%     1.53ms
     99%     2.13ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec     92178.57    5485.24   98226.68
  Latency        1.07ms   371.85us     5.32ms
  Latency Distribution
     50%     0.99ms
     75%     1.32ms
     90%     1.66ms
     99%     2.62ms

### Single union item — Like branch (/feed/2)
  Reqs/sec     95429.71    8149.46  102795.26
  Latency        1.02ms   322.09us     5.63ms
  Latency Distribution
     50%     0.95ms
     75%     1.24ms
     90%     1.57ms
     99%     2.30ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     63237.03   19887.39   72894.97
  Latency        1.40ms   353.05us     6.66ms
  Latency Distribution
     50%     1.37ms
     75%     1.70ms
     90%     2.02ms
     99%     2.79ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    176860.01   19143.67  188933.78
  Latency      555.04us   303.70us     4.80ms
  Latency Distribution
     50%   521.00us
     75%   672.00us
     90%   832.00us
     99%     1.83ms

### Path Parameter - int (/items/12345)
  Reqs/sec    151729.07    9232.62  160919.74
  Latency      639.85us   330.83us     6.03ms
  Latency Distribution
     50%   580.00us
     75%   746.00us
     90%     0.94ms
     99%     1.84ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    153281.86   11939.64  165033.26
  Latency      636.51us   364.62us     5.57ms
  Latency Distribution
     50%   556.00us
     75%   718.00us
     90%     0.95ms
     99%     2.38ms

### Header Parameter (/header)
  Reqs/sec    101942.75    7744.54  108029.71
  Latency        0.96ms   317.31us     5.21ms
  Latency Distribution
     50%     0.91ms
     75%     1.18ms
     90%     1.49ms
     99%     2.23ms

### Cookie Parameter (/cookie)
  Reqs/sec    104309.84    6631.72  108368.23
  Latency        0.94ms   289.65us     4.90ms
  Latency Distribution
     50%     0.89ms
     75%     1.15ms
     90%     1.41ms
     99%     2.07ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     80432.93    5370.22   86946.80
  Latency        1.21ms   396.42us     5.35ms
  Latency Distribution
     50%     1.12ms
     75%     1.49ms
     90%     1.92ms
     99%     2.96ms
