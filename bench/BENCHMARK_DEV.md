# Django-Bolt Benchmark
Generated: Sun Jul 19 09:14:45 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    338300.55       0.00  358560.28
  Latency      290.32us   328.02us     6.30ms
  Latency Distribution
     50%   200.00us
     75%   287.00us
     90%   488.00us
     99%     1.91ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    186934.43   21636.34  211481.09
  Latency      509.25us   328.24us     5.85ms
  Latency Distribution
     50%   435.00us
     75%   576.00us
     90%   772.00us
     99%     2.19ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    195338.62   33306.66  217746.47
  Latency      509.54us   425.47us     5.08ms
  Latency Distribution
     50%   425.00us
     75%   522.00us
     90%   682.00us
     99%     3.15ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    264956.27       0.00  269302.49
  Latency      357.02us   317.02us     4.94ms
  Latency Distribution
     50%   300.00us
     75%   385.00us
     90%   508.00us
     99%     1.93ms
### Cookie Endpoint (/cookie)
  Reqs/sec    255005.88       0.00  279630.74
  Latency      373.56us   342.20us     7.49ms
  Latency Distribution
     50%   292.00us
     75%   404.00us
     90%   538.00us
     99%     2.39ms
### Exception Endpoint (/exc)
  Reqs/sec    247305.23       0.00  265396.35
  Latency      391.80us   266.74us     4.41ms
  Latency Distribution
     50%   324.00us
     75%   418.00us
     90%   609.00us
     99%     1.81ms
### HTML Response (/html)
  Reqs/sec    313301.48       0.00  340734.02
  Latency      314.85us   355.27us     5.20ms
  Latency Distribution
     50%   233.00us
     75%   321.00us
     90%   479.00us
     99%     2.51ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     42444.80   10934.60   52832.04
  Latency        2.35ms     1.59ms    18.32ms
  Latency Distribution
     50%     2.00ms
     75%     2.89ms
     90%     3.90ms
     99%     8.15ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    172770.51   13163.68  183449.11
  Latency      567.40us   241.48us     4.76ms
  Latency Distribution
     50%   517.00us
     75%   634.00us
     90%     0.86ms
     99%     1.49ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    167381.92   27525.82  185336.78
  Latency      588.10us   435.91us     6.78ms
  Latency Distribution
     50%   511.00us
     75%   647.00us
     90%   732.00us
     99%     3.51ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     78398.06   37685.92  101880.62
  Latency        1.08ms     3.03ms    43.44ms
  Latency Distribution
     50%   733.00us
     75%     0.93ms
     90%     1.28ms
     99%     8.39ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    197354.46   17149.67  211527.31
  Latency      497.83us   305.09us     5.30ms
  Latency Distribution
     50%   464.00us
     75%   542.00us
     90%   623.00us
     99%     2.25ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    166764.85   13598.60  175865.51
  Latency      588.40us   237.81us     3.68ms
  Latency Distribution
     50%   610.00us
     75%   734.00us
     90%     0.89ms
     99%     1.83ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     68997.05   41727.81  103524.67
  Latency        1.08ms     3.17ms    43.03ms
  Latency Distribution
     50%   724.00us
     75%     0.91ms
     90%     1.29ms
     99%     6.83ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    295801.05       0.00  311564.33
  Latency      324.61us   345.29us     4.23ms
  Latency Distribution
     50%   232.00us
     75%   339.00us
     90%   519.00us
     99%     2.35ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    294034.66       0.00  329890.39
  Latency      334.70us   298.81us     4.42ms
  Latency Distribution
     50%   245.00us
     75%   391.00us
     90%   579.00us
     99%     2.00ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     91972.34    6529.72   96820.28
  Latency        1.06ms   333.47us     4.79ms
  Latency Distribution
     50%     0.97ms
     75%     1.29ms
     90%     1.61ms
     99%     2.45ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     88271.86    6544.33   94390.55
  Latency        1.12ms   334.89us     5.36ms
  Latency Distribution
     50%     1.08ms
     75%     1.38ms
     90%     1.62ms
     99%     2.42ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec    132994.91   10988.39  144882.64
  Latency      733.31us   263.49us     3.54ms
  Latency Distribution
     50%   675.00us
     75%     0.88ms
     90%     1.13ms
     99%     2.06ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     18961.18    1830.48   20847.06
  Latency        5.17ms     1.35ms    13.16ms
  Latency Distribution
     50%     4.68ms
     75%     5.63ms
     90%     8.09ms
     99%     9.89ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     16206.96    4198.06   19776.70
  Latency        6.14ms     7.19ms    86.33ms
  Latency Distribution
     50%     5.23ms
     75%     6.36ms
     90%     7.69ms
     99%    13.41ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    144325.26   15390.02  153462.30
  Latency      681.01us   236.22us     4.33ms
  Latency Distribution
     50%   627.00us
     75%   830.00us
     90%     1.05ms
     99%     1.68ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    290744.03       0.00  323194.49
  Latency      337.87us   385.04us     5.45ms
  Latency Distribution
     50%   250.00us
     75%   322.00us
     90%   465.00us
     99%     2.66ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    270181.26       0.00  296951.70
  Latency      362.18us   299.60us     7.89ms
  Latency Distribution
     50%   308.00us
     75%   426.00us
     90%   552.00us
     99%     1.81ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     19191.39    1504.85   20469.87
  Latency        5.18ms     1.51ms    15.84ms
  Latency Distribution
     50%     5.23ms
     75%     6.37ms
     90%     7.27ms
     99%     9.64ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     13783.21    2091.63   18938.32
  Latency        7.26ms     6.94ms    88.18ms
  Latency Distribution
     50%     6.14ms
     75%     8.73ms
     90%    11.88ms
     99%    22.21ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     23001.57    2798.03   25033.41
  Latency        4.32ms     2.88ms    69.90ms
  Latency Distribution
     50%     3.91ms
     75%     5.04ms
     90%     6.49ms
     99%     9.74ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     16760.54    1022.18   17920.37
  Latency        5.95ms     3.64ms    30.18ms
  Latency Distribution
     50%     4.86ms
     75%     6.75ms
     90%    10.28ms
     99%    21.43ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    194061.84   15317.44  204540.20
  Latency      505.79us   165.68us     2.45ms
  Latency Distribution
     50%   457.00us
     75%   620.00us
     90%   807.00us
     99%     1.30ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    199402.17   19007.99  213105.86
  Latency      492.77us   171.07us     2.64ms
  Latency Distribution
     50%   442.00us
     75%   597.00us
     90%   768.00us
     99%     1.38ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     97207.65    6954.12  104125.23
  Latency        1.02ms   233.97us     4.17ms
  Latency Distribution
     50%     0.97ms
     75%     1.18ms
     90%     1.47ms
     99%     2.09ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    189938.16   19164.18  201340.64
  Latency      517.16us   209.84us     4.10ms
  Latency Distribution
     50%   470.00us
     75%   620.00us
     90%   800.00us
     99%     1.39ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    188465.69   20110.94  201653.44
  Latency      523.12us   158.81us     2.67ms
  Latency Distribution
     50%   472.00us
     75%   641.00us
     90%   813.00us
     99%     1.28ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    193568.82   25378.06  209742.65
  Latency      513.12us   310.19us     5.32ms
  Latency Distribution
     50%   451.00us
     75%   612.00us
     90%   768.00us
     99%     1.29ms
### CBV Response Types (/cbv-response)
  Reqs/sec    205496.83   25118.63  221199.09
  Latency      480.11us   165.79us     2.42ms
  Latency Distribution
     50%   437.00us
     75%   579.00us
     90%   756.00us
     99%     1.34ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     17303.79    3152.63   22893.33
  Latency        5.81ms     6.39ms    79.77ms
  Latency Distribution
     50%     4.94ms
     75%     6.35ms
     90%     7.83ms
     99%    13.02ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    242222.66       0.00  258612.78
  Latency      399.64us   217.61us     3.65ms
  Latency Distribution
     50%   332.00us
     75%   476.00us
     90%   661.00us
     99%     1.63ms
### File Upload (POST /upload)
  Reqs/sec    199344.07   18576.83  215888.77
  Latency      497.51us   322.18us     5.54ms
  Latency Distribution
     50%   442.00us
     75%   576.00us
     90%   702.00us
     99%     1.76ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    180304.55   37156.43  205995.26
  Latency      547.37us   216.91us     3.92ms
  Latency Distribution
     50%   495.00us
     75%   652.00us
     90%     0.85ms
     99%     1.57ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    226773.55   33983.75  246448.34
  Latency      439.10us   352.85us     5.75ms
  Latency Distribution
     50%   381.00us
     75%   484.00us
     90%   631.00us
     99%     2.24ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    163294.60   17144.79  174231.34
  Latency      592.41us   201.21us     3.44ms
  Latency Distribution
     50%   543.00us
     75%   722.00us
     90%     0.90ms
     99%     1.53ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9003.42    1489.58   11026.15
  Latency       11.09ms     7.38ms    88.30ms
  Latency Distribution
     50%    10.42ms
     75%    12.89ms
     90%    14.33ms
     99%    24.82ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    273769.22       0.00  288956.22
  Latency      352.91us   386.74us     4.32ms
  Latency Distribution
     50%   250.00us
     75%   379.00us
     90%   499.00us
     99%     3.15ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    267200.38       0.00  291089.28
  Latency      360.35us   356.96us     5.67ms
  Latency Distribution
     50%   305.00us
     75%   382.00us
     90%   495.00us
     99%     2.40ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec    164254.46   14898.64  176285.12
  Latency      594.29us   182.99us     4.89ms
  Latency Distribution
     50%   538.00us
     75%   726.00us
     90%     0.94ms
     99%     1.45ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    259057.68       0.00  288972.82
  Latency      380.62us   373.59us     4.13ms
  Latency Distribution
     50%   296.00us
     75%   408.00us
     90%   581.00us
     99%     2.62ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    194473.60   22940.12  209123.00
  Latency      509.04us   163.55us     2.46ms
  Latency Distribution
     50%   460.00us
     75%   619.00us
     90%   805.00us
     99%     1.36ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    186053.94   19121.12  200693.51
  Latency      526.92us   188.94us     3.14ms
  Latency Distribution
     50%   480.00us
     75%   635.00us
     90%   809.00us
     99%     1.39ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    263190.49       0.00  307799.73
  Latency      377.29us   389.29us     6.16ms
  Latency Distribution
     50%   257.00us
     75%   367.00us
     90%   634.00us
     99%     2.64ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    246495.97       0.00  262774.42
  Latency      389.23us   511.56us     6.92ms
  Latency Distribution
     50%   264.00us
     75%   382.00us
     90%   638.00us
     99%     3.18ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    250577.82       0.00  267327.40
  Latency      386.17us   404.35us     6.93ms
  Latency Distribution
     50%   293.00us
     75%   394.00us
     90%   635.00us
     99%     2.69ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     89883.36    8992.66  100088.16
  Latency        1.10ms   344.92us     6.85ms
  Latency Distribution
     50%     1.07ms
     75%     1.31ms
     90%     1.62ms
     99%     2.51ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    332173.81       0.00  345499.34
  Latency      294.53us   388.58us     7.46ms
  Latency Distribution
     50%   217.00us
     75%   301.00us
     90%   470.00us
     99%     1.70ms

### Path Parameter - int (/items/12345)
  Reqs/sec    304670.66       0.00  316502.19
  Latency      317.80us   328.74us     4.72ms
  Latency Distribution
     50%   253.00us
     75%   334.00us
     90%   455.00us
     99%     1.97ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    301580.79       0.00  329887.54
  Latency      318.89us   340.24us     7.02ms
  Latency Distribution
     50%   244.00us
     75%   316.00us
     90%   446.00us
     99%     2.25ms

### Header Parameter (/header)
  Reqs/sec    252102.66       0.00  272823.85
  Latency      384.66us   357.04us     4.33ms
  Latency Distribution
     50%   290.00us
     75%   401.00us
     90%   597.00us
     99%     2.37ms

### Cookie Parameter (/cookie)
  Reqs/sec    159536.03  139857.58  261014.24
  Latency      404.38us   400.79us     5.20ms
  Latency Distribution
     50%   290.00us
     75%   423.00us
     90%   681.00us
     99%     2.58ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec    132533.36   16294.63  152893.85
  Latency      746.34us   310.69us     4.76ms
  Latency Distribution
     50%   658.00us
     75%     0.93ms
     90%     1.23ms
     99%     2.15ms
