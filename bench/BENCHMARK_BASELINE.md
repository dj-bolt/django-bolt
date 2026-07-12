# Django-Bolt Benchmark
Generated: Thu 02 Jul 2026 02:30:40 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    185702.47   23573.92  199741.37
  Latency      534.73us   317.58us     4.47ms
  Latency Distribution
     50%   464.00us
     75%   609.00us
     90%   805.00us
     99%     2.09ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    119561.64   13058.99  128884.58
  Latency      829.31us   395.81us     6.47ms
  Latency Distribution
     50%   749.00us
     75%     0.99ms
     90%     1.27ms
     99%     2.88ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    124424.75   12717.67  134418.62
  Latency      788.82us   364.30us     4.89ms
  Latency Distribution
     50%   720.00us
     75%     0.90ms
     90%     1.27ms
     99%     2.67ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    108563.12    9519.99  118053.94
  Latency        0.91ms   352.72us     5.41ms
  Latency Distribution
     50%   815.00us
     75%     1.11ms
     90%     1.44ms
     99%     2.44ms
### Cookie Endpoint (/cookie)
  Reqs/sec    113082.65   11480.29  125597.58
  Latency        0.88ms   266.15us     3.43ms
  Latency Distribution
     50%   816.00us
     75%     1.08ms
     90%     1.39ms
     99%     2.11ms
### Exception Endpoint (/exc)
  Reqs/sec    153272.92    7150.44  157782.49
  Latency      639.36us   226.62us     5.04ms
  Latency Distribution
     50%   601.00us
     75%   759.00us
     90%     0.89ms
     99%     1.55ms
### HTML Response (/html)
  Reqs/sec    168048.22   15979.16  177986.43
  Latency      583.34us   236.81us     3.83ms
  Latency Distribution
     50%   526.00us
     75%   696.00us
     90%     0.87ms
     99%     1.64ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     36734.39    7439.58   41337.29
  Latency        2.72ms     1.42ms    16.35ms
  Latency Distribution
     50%     2.42ms
     75%     3.14ms
     90%     4.24ms
     99%     8.61ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    130014.88   16975.36  147961.89
  Latency      761.80us   508.60us     7.11ms
  Latency Distribution
     50%   677.00us
     75%   817.00us
     90%     1.03ms
     99%     3.55ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    140456.10   11901.62  148249.95
  Latency      690.96us   333.90us     7.38ms
  Latency Distribution
     50%   627.00us
     75%   802.00us
     90%     1.00ms
     99%     2.20ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     60166.49   30648.52   84113.11
  Latency        1.31ms     2.58ms    42.78ms
  Latency Distribution
     50%     0.97ms
     75%     1.30ms
     90%     1.87ms
     99%     6.57ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    173621.30   20949.33  186126.47
  Latency      563.32us   277.45us     4.59ms
  Latency Distribution
     50%   522.00us
     75%   656.00us
     90%   846.00us
     99%     1.97ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    146834.15   14578.21  155843.45
  Latency      671.03us   312.52us     5.20ms
  Latency Distribution
     50%   615.00us
     75%   762.00us
     90%   843.00us
     99%     1.84ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     55607.56   40233.39   92919.16
  Latency        1.26ms     3.32ms    44.31ms
  Latency Distribution
     50%   847.00us
     75%     1.05ms
     90%     1.43ms
     99%    12.55ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    183180.21   19162.50  195673.23
  Latency      535.21us   234.90us     5.69ms
  Latency Distribution
     50%   568.00us
     75%   675.00us
     90%   756.00us
     99%     1.43ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    163224.75   18354.19  178199.99
  Latency      596.34us   389.04us     4.60ms
  Latency Distribution
     50%   509.00us
     75%   681.00us
     90%     0.96ms
     99%     3.20ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     72646.59    4270.69   77506.33
  Latency        1.36ms   326.29us     5.19ms
  Latency Distribution
     50%     1.34ms
     75%     1.63ms
     90%     1.80ms
     99%     2.69ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     69465.02    4729.83   74631.81
  Latency        1.40ms   327.95us     4.75ms
  Latency Distribution
     50%     1.33ms
     75%     1.70ms
     90%     1.87ms
     99%     2.71ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     82861.99    5982.94   86940.10
  Latency        1.19ms   367.73us     4.74ms
  Latency Distribution
     50%     1.11ms
     75%     1.46ms
     90%     1.82ms
     99%     2.85ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     18867.28    1808.99   22705.12
  Latency        5.30ms     1.29ms    12.83ms
  Latency Distribution
     50%     5.17ms
     75%     6.07ms
     90%     7.14ms
     99%    10.16ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15586.21    3033.07   17466.81
  Latency        6.16ms     4.17ms    68.59ms
  Latency Distribution
     50%     5.55ms
     75%     7.21ms
     90%     8.94ms
     99%    13.82ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     89098.83    5395.12   92670.21
  Latency        1.11ms   347.68us     4.80ms
  Latency Distribution
     50%     1.03ms
     75%     1.38ms
     90%     1.76ms
     99%     2.53ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    165037.45   17829.75  176193.08
  Latency      586.25us   235.42us     5.71ms
  Latency Distribution
     50%   524.00us
     75%   615.00us
     90%     0.92ms
     99%     1.66ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    152146.75   14122.17  160626.40
  Latency      644.55us   267.78us     5.02ms
  Latency Distribution
     50%   607.00us
     75%   683.00us
     90%     0.90ms
     99%     1.92ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     16142.98    1694.70   21581.38
  Latency        6.21ms     3.48ms    71.67ms
  Latency Distribution
     50%     6.01ms
     75%     7.16ms
     90%     9.47ms
     99%    12.49ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     12271.88    2159.46   15102.70
  Latency        8.13ms     7.08ms    81.93ms
  Latency Distribution
     50%     6.82ms
     75%     9.38ms
     90%    13.07ms
     99%    27.97ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     19468.96    1436.38   21461.99
  Latency        5.10ms     2.10ms    59.60ms
  Latency Distribution
     50%     4.94ms
     75%     6.07ms
     90%     7.26ms
     99%     9.23ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     15244.13     931.62   16641.20
  Latency        6.50ms     2.05ms    18.08ms
  Latency Distribution
     50%     6.16ms
     75%     7.79ms
     90%     9.61ms
     99%    13.32ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    110903.51    6984.54  116356.25
  Latency        0.89ms   268.97us     3.24ms
  Latency Distribution
     50%   822.00us
     75%     1.09ms
     90%     1.41ms
     99%     2.11ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    110779.96    7833.22  115444.17
  Latency        0.89ms   293.96us     4.07ms
  Latency Distribution
     50%   832.00us
     75%     1.11ms
     90%     1.39ms
     99%     2.20ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     68771.59    4768.04   73810.74
  Latency        1.44ms   472.13us     4.80ms
  Latency Distribution
     50%     1.29ms
     75%     1.78ms
     90%     2.28ms
     99%     3.49ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    110670.63    6939.29  117088.61
  Latency        0.89ms   293.63us     4.15ms
  Latency Distribution
     50%   814.00us
     75%     1.08ms
     90%     1.41ms
     99%     2.24ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    106700.03    6390.22  111063.10
  Latency        0.92ms   280.63us     3.13ms
  Latency Distribution
     50%   846.00us
     75%     1.15ms
     90%     1.48ms
     99%     2.25ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    108893.20    6700.57  114598.68
  Latency        0.90ms   280.00us     4.88ms
  Latency Distribution
     50%   836.00us
     75%     1.11ms
     90%     1.41ms
     99%     2.15ms
### CBV Response Types (/cbv-response)
  Reqs/sec    111622.40    6094.53  119199.85
  Latency        0.87ms   274.35us     3.18ms
  Latency Distribution
     50%   792.00us
     75%     1.07ms
     90%     1.41ms
     99%     2.20ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     17700.93    1363.77   18921.09
  Latency        5.61ms     2.56ms    62.88ms
  Latency Distribution
     50%     5.39ms
     75%     6.61ms
     90%     7.60ms
     99%     9.59ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    137854.40   12023.63  148563.57
  Latency      709.94us   293.45us     4.81ms
  Latency Distribution
     50%   642.00us
     75%   846.00us
     90%     1.04ms
     99%     1.85ms
### File Upload (POST /upload)
  Reqs/sec    119336.10   10885.62  128706.23
  Latency      836.13us   359.81us     6.62ms
  Latency Distribution
     50%   769.00us
     75%     1.00ms
     90%     1.26ms
     99%     1.96ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    112318.27   10225.94  121279.65
  Latency        0.88ms   314.96us     5.32ms
  Latency Distribution
     50%   820.00us
     75%     1.02ms
     90%     1.29ms
     99%     2.12ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    130222.81   13334.06  139272.44
  Latency      752.75us   344.07us     5.70ms
  Latency Distribution
     50%   724.00us
     75%     0.92ms
     90%     1.06ms
     99%     1.91ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    105184.20    5307.44  107903.76
  Latency        0.93ms   279.32us     4.42ms
  Latency Distribution
     50%     0.91ms
     75%     1.20ms
     90%     1.51ms
     99%     2.13ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      8396.97    1479.84   15083.28
  Latency       11.99ms     7.03ms    84.03ms
  Latency Distribution
     50%    11.51ms
     75%    13.00ms
     90%    16.37ms
     99%    25.20ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    145891.37   14133.89  156525.31
  Latency      666.69us   266.62us     4.35ms
  Latency Distribution
     50%   596.00us
     75%   830.00us
     90%     1.07ms
     99%     1.90ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    153182.45   10006.13  159822.23
  Latency      644.42us   293.50us     4.89ms
  Latency Distribution
     50%   580.00us
     75%   745.00us
     90%     0.93ms
     99%     2.06ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     90606.15    9475.14  101804.31
  Latency        1.09ms   428.89us     6.82ms
  Latency Distribution
     50%     0.99ms
     75%     1.32ms
     90%     1.71ms
     99%     3.19ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    146583.21   12371.29  160101.14
  Latency      663.51us   264.61us     4.59ms
  Latency Distribution
     50%   627.00us
     75%   786.00us
     90%     1.01ms
     99%     1.85ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    114514.32    9004.46  120252.69
  Latency        0.87ms   250.16us     2.83ms
  Latency Distribution
     50%   805.00us
     75%     1.05ms
     90%     1.32ms
     99%     2.12ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    112771.14    8357.32  119383.34
  Latency        0.87ms   215.54us     2.56ms
  Latency Distribution
     50%   821.00us
     75%     1.08ms
     90%     1.33ms
     99%     1.91ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    108086.12    7911.05  112200.09
  Latency        0.91ms   255.66us     3.70ms
  Latency Distribution
     50%   846.00us
     75%     1.11ms
     90%     1.42ms
     99%     2.04ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    107282.18    6251.17  112143.29
  Latency        0.92ms   238.08us     3.21ms
  Latency Distribution
     50%     0.85ms
     75%     1.12ms
     90%     1.41ms
     99%     2.04ms

### Single union item — Like branch (/feed/2)
  Reqs/sec    105836.84    7794.18  111491.91
  Latency        0.93ms   264.69us     3.47ms
  Latency Distribution
     50%     0.87ms
     75%     1.15ms
     90%     1.47ms
     99%     2.18ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     72953.78    3701.70   75073.40
  Latency        1.35ms   357.74us     5.55ms
  Latency Distribution
     50%     1.31ms
     75%     1.63ms
     90%     1.88ms
     99%     2.80ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    178947.48   17245.25  194591.58
  Latency      553.68us   301.43us     4.22ms
  Latency Distribution
     50%   505.00us
     75%   608.00us
     90%   818.00us
     99%     2.59ms

### Path Parameter - int (/items/12345)
  Reqs/sec    155036.89   17077.03  170270.58
  Latency      606.35us   259.37us     5.10ms
  Latency Distribution
     50%   566.00us
     75%   707.00us
     90%     0.89ms
     99%     1.69ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    148434.15   15836.83  161392.78
  Latency      629.19us   327.58us     5.92ms
  Latency Distribution
     50%   556.00us
     75%   753.00us
     90%     1.02ms
     99%     1.87ms

### Header Parameter (/header)
  Reqs/sec    111853.19   11179.85  120281.03
  Latency        0.88ms   277.95us     3.87ms
  Latency Distribution
     50%   804.00us
     75%     1.06ms
     90%     1.39ms
     99%     2.22ms

### Cookie Parameter (/cookie)
  Reqs/sec    112569.93    8362.68  118358.41
  Latency        0.88ms   267.84us     3.67ms
  Latency Distribution
     50%   816.00us
     75%     1.09ms
     90%     1.41ms
     99%     2.17ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     92626.61    4573.47   96216.48
  Latency        1.06ms   319.66us     4.90ms
  Latency Distribution
     50%     1.00ms
     75%     1.30ms
     90%     1.60ms
     99%     2.40ms
