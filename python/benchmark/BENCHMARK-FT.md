# Django-Bolt Benchmark
Generated: Sat Sep 19 06:44:55 PM PKT 2026
Config: 1 processes × 12 workers | C=100 N=100000

## Root Endpoint Performance
  Reqs/sec    297233.50   19110.16  320989.72
  Latency      333.70us   468.03us    15.82ms
  Latency Distribution
     50%   149.00us
     75%   309.00us
     90%   756.00us
     99%     2.80ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec     48067.54    2775.85   52946.55
  Latency        2.08ms   381.54us    12.47ms
  Latency Distribution
     50%     1.98ms
     75%     2.50ms
     90%     3.06ms
     99%     4.42ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec     48733.40    2459.53   53933.57
  Latency        2.05ms   354.02us     8.03ms
  Latency Distribution
     50%     1.94ms
     75%     2.50ms
     90%     3.11ms
     99%     4.41ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    244466.29   20790.00  269883.52
  Latency      405.65us   526.65us    17.41ms
  Latency Distribution
     50%   213.00us
     75%   327.00us
     90%   794.00us
     99%     3.44ms
### Cookie Endpoint (/cookie)
  Reqs/sec    243317.04   13980.44  267410.52
  Latency      408.19us   510.60us    11.87ms
  Latency Distribution
     50%   216.00us
     75%   324.00us
     90%   722.00us
     99%     3.54ms
### Exception Endpoint (/exc)
  Reqs/sec    233675.00   15896.47  266962.96
  Latency      425.29us   515.53us    17.85ms
  Latency Distribution
     50%   242.00us
     75%   377.00us
     90%   766.00us
     99%     3.35ms
### HTML Response (/html)
  Reqs/sec    274758.30   17369.62  303767.45
  Latency      362.75us   403.15us    10.02ms
  Latency Distribution
     50%   176.00us
     75%   330.00us
     90%   784.00us
     99%     3.06ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     51998.20    6524.68   62868.71
  Latency        1.92ms     1.20ms    30.42ms
  Latency Distribution
     50%     1.55ms
     75%     2.14ms
     90%     3.08ms
     99%     7.79ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    175586.74   12047.58  193471.63
  Latency      565.62us   427.54us     8.35ms
  Latency Distribution
     50%   380.00us
     75%   592.00us
     90%     0.94ms
     99%     3.45ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    176907.38   11527.55  196075.11
  Latency      562.03us   481.27us    14.72ms
  Latency Distribution
     50%   378.00us
     75%   565.00us
     90%     0.94ms
     99%     3.53ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     89187.00   10662.04  103135.70
  Latency        1.11ms   593.21us    14.01ms
  Latency Distribution
     50%   773.00us
     75%     1.39ms
     90%     2.23ms
     99%     4.80ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    202664.65   10905.83  227783.73
  Latency      490.63us   500.25us    11.64ms
  Latency Distribution
     50%   321.00us
     75%   479.00us
     90%   839.00us
     99%     3.43ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    167080.24   32558.63  193617.92
  Latency      577.78us   488.26us    10.53ms
  Latency Distribution
     50%   373.00us
     75%   523.00us
     90%     0.99ms
     99%     3.76ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     88015.16    8915.27  103703.33
  Latency        1.13ms   586.34us    10.65ms
  Latency Distribution
     50%   776.00us
     75%     1.36ms
     90%     2.29ms
     99%     4.82ms

## Authentication & Authorization Performance
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     19819.48    2290.13   24540.80
  Latency        5.04ms   457.95us    14.61ms
  Latency Distribution
     50%     4.80ms
     75%     5.43ms
     90%     6.35ms
     99%     7.64ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    148720.48   11441.13  165489.67
  Latency      668.92us   466.81us    10.08ms
  Latency Distribution
     50%   464.00us
     75%   694.00us
     90%     1.14ms
     99%     3.81ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    253773.72   18323.14  279637.66
  Latency      390.91us   423.50us     9.23ms
  Latency Distribution
     50%   193.00us
     75%   352.00us
     90%     0.86ms
     99%     3.15ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    234491.10   16943.46  267277.47
  Latency      423.96us   390.88us     8.94ms
  Latency Distribution
     50%   229.00us
     75%   438.00us
     90%     0.90ms
     99%     3.11ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec      4677.61     305.81    5308.70
  Latency       21.37ms   781.81us    50.22ms
  Latency Distribution
     50%    21.27ms
     75%    22.16ms
     90%    23.10ms
     99%    24.96ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     20490.85    1802.54   28002.13
  Latency        4.88ms     2.39ms    36.51ms
  Latency Distribution
     50%     3.94ms
     75%     6.25ms
     90%     9.34ms
     99%    16.84ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec      6216.49     390.27    7165.62
  Latency       16.08ms   499.46us    21.87ms
  Latency Distribution
     50%    16.00ms
     75%    16.78ms
     90%    17.61ms
     99%    18.93ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    198906.26   22713.14  226943.74
  Latency      501.11us   697.51us    29.16ms
  Latency Distribution
     50%   298.00us
     75%   489.00us
     90%     0.88ms
     99%     3.40ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    189536.37   15763.16  208856.59
  Latency      519.21us   438.94us     8.82ms
  Latency Distribution
     50%   318.00us
     75%   542.00us
     90%     0.99ms
     99%     3.45ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    190153.67   16814.41  213023.24
  Latency      523.40us   532.00us    16.81ms
  Latency Distribution
     50%   319.00us
     75%   541.00us
     90%     0.94ms
     99%     3.51ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    179993.10   12372.44  199780.56
  Latency      553.34us   460.25us    10.84ms
  Latency Distribution
     50%   348.00us
     75%   556.00us
     90%     1.00ms
     99%     3.58ms

## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    225417.80   19470.79  254072.47
  Latency      438.89us   502.78us    13.79ms
  Latency Distribution
     50%   248.00us
     75%   405.00us
     90%   813.00us
     99%     3.41ms
### File Upload (POST /upload)
  Reqs/sec    181306.54   12479.72  196252.37
  Latency      547.58us   421.99us     8.03ms
  Latency Distribution
     50%   349.00us
     75%   602.00us
     90%     1.02ms
     99%     3.44ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    173596.87   14274.29  198104.96
  Latency      571.68us   511.70us    22.19ms
  Latency Distribution
     50%   368.00us
     75%   642.00us
     90%     1.11ms
     99%     2.97ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    140692.52    9256.34  154559.50
  Latency      703.09us   429.94us    14.82ms
  Latency Distribution
     50%   517.00us
     75%   809.00us
     90%     1.28ms
     99%     3.02ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      3075.33     760.01    5559.05
  Latency       32.53ms    28.71ms   587.35ms
  Latency Distribution
     50%    28.66ms
     75%    32.78ms
     90%    41.77ms
     99%   118.85ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    241517.86   23893.04  276414.51
  Latency      412.18us   469.41us    14.12ms
  Latency Distribution
     50%   219.00us
     75%   419.00us
     90%   844.00us
     99%     3.05ms
## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Feed of 100 mixed union items (/feed)
  Reqs/sec     84261.66    5297.85   92052.98
  Latency        1.19ms   550.37us    17.51ms
  Latency Distribution
     50%     1.02ms
     75%     1.37ms
     90%     1.86ms
     99%     4.13ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    292076.31   25978.04  323905.15
  Latency      340.53us   459.21us    13.62ms
  Latency Distribution
     50%   151.00us
     75%   322.00us
     90%   751.00us
     99%     2.93ms

### Path Parameter - int (/items/12345)
  Reqs/sec    253447.78   18907.77  278090.39
  Latency      391.49us   380.01us     9.81ms
  Latency Distribution
     50%   199.00us
     75%   400.00us
     90%     0.86ms
     99%     2.93ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    217358.19   28501.15  272441.83
  Latency      456.52us   470.12us    12.07ms
  Latency Distribution
     50%   216.00us
     75%   489.00us
     90%     1.03ms
     99%     3.39ms

### Header Parameter (/header)
  Reqs/sec    238560.74   16994.60  265119.85
  Latency      415.54us   414.98us    11.86ms
  Latency Distribution
     50%   218.00us
     75%   412.00us
     90%     0.90ms
     99%     3.14ms

### Cookie Parameter (/cookie)
  Reqs/sec    239654.13   17175.94  279193.50
  Latency      414.70us   401.61us    11.59ms
  Latency Distribution
     50%   223.00us
     75%   436.00us
     90%     0.85ms
     99%     3.15ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    148859.37    8977.10  162388.67
  Latency      668.05us   476.28us    10.26ms
  Latency Distribution
     50%   464.00us
     75%   657.00us
     90%     1.08ms
     99%     3.82ms
