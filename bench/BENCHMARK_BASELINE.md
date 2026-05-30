# Django-Bolt Benchmark
Generated: Sat 30 May 2026 08:57:55 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    172699.32   27577.98  196308.64
  Latency      554.77us   357.65us     6.34ms
  Latency Distribution
     50%   466.00us
     75%   665.00us
     90%     0.87ms
     99%     2.18ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    115793.77    8867.72  125174.08
  Latency      842.22us   298.58us     4.45ms
  Latency Distribution
     50%   776.00us
     75%     1.04ms
     90%     1.29ms
     99%     2.25ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    115003.36   12109.06  127872.30
  Latency      817.15us   344.51us     5.29ms
  Latency Distribution
     50%   724.00us
     75%     0.98ms
     90%     1.28ms
     99%     2.40ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec     94010.76    6937.25   99901.51
  Latency        1.05ms   377.50us     4.61ms
  Latency Distribution
     50%     0.97ms
     75%     1.30ms
     90%     1.66ms
     99%     2.68ms
### Cookie Endpoint (/cookie)
  Reqs/sec    101560.72    8190.18  107796.73
  Latency        0.97ms   307.28us     5.46ms
  Latency Distribution
     50%     0.91ms
     75%     1.18ms
     90%     1.46ms
     99%     2.17ms
### Exception Endpoint (/exc)
  Reqs/sec    137735.69   14141.74  147362.85
  Latency      709.03us   360.21us     6.28ms
  Latency Distribution
     50%   611.00us
     75%     0.86ms
     90%     1.13ms
     99%     1.91ms
### HTML Response (/html)
  Reqs/sec    143465.70   30586.94  175371.28
  Latency      610.29us   324.90us     4.73ms
  Latency Distribution
     50%   526.00us
     75%   712.00us
     90%     1.00ms
     99%     2.34ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     35138.34    5614.74   39011.81
  Latency        2.83ms     1.27ms    17.35ms
  Latency Distribution
     50%     2.59ms
     75%     3.40ms
     90%     4.35ms
     99%     7.79ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    106574.41   23840.17  132126.42
  Latency      826.37us   434.63us     5.79ms
  Latency Distribution
     50%   713.00us
     75%     1.00ms
     90%     1.41ms
     99%     2.93ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    121927.57   12477.49  134743.96
  Latency      788.89us   381.03us     6.09ms
  Latency Distribution
     50%   713.00us
     75%     0.93ms
     90%     1.24ms
     99%     2.65ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     55299.05   30853.60   79817.23
  Latency        1.45ms     2.64ms    44.33ms
  Latency Distribution
     50%     1.07ms
     75%     1.49ms
     90%     1.96ms
     99%     9.45ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    137746.80   46259.34  172411.06
  Latency      604.23us   314.38us     5.09ms
  Latency Distribution
     50%   512.00us
     75%   764.00us
     90%     0.95ms
     99%     1.82ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    134933.77   17010.01  148095.69
  Latency      712.95us   357.45us     7.92ms
  Latency Distribution
     50%   748.00us
     75%     0.88ms
     90%     1.03ms
     99%     2.10ms
### Media 100KB (GET /media/bench/upload_100k.bin)
  Reqs/sec     74661.66   11370.82   86656.51
  Latency        1.30ms     2.27ms    44.33ms
  Latency Distribution
     50%     1.06ms
     75%     1.30ms
     90%     1.70ms
     99%     4.50ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    157026.34    8938.73  165942.76
  Latency      616.31us   400.13us     8.88ms
  Latency Distribution
     50%   529.00us
     75%   704.00us
     90%     0.95ms
     99%     2.44ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    158782.00   10055.88  166854.09
  Latency      618.70us   386.96us     5.54ms
  Latency Distribution
     50%   543.00us
     75%   714.00us
     90%     0.90ms
     99%     2.69ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     70859.47    4105.08   73599.80
  Latency        1.39ms   487.40us     6.84ms
  Latency Distribution
     50%     1.32ms
     75%     1.73ms
     90%     2.13ms
     99%     3.12ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     68246.93    2968.98   71028.02
  Latency        1.44ms   408.69us     5.75ms
  Latency Distribution
     50%     1.34ms
     75%     1.82ms
     90%     2.18ms
     99%     3.05ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     79319.75    6930.14   86599.80
  Latency        1.23ms   389.50us     4.96ms
  Latency Distribution
     50%     1.14ms
     75%     1.51ms
     90%     1.91ms
     99%     2.85ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     16621.26    1294.01   17668.49
  Latency        5.97ms     1.69ms    15.53ms
  Latency Distribution
     50%     5.55ms
     75%     6.47ms
     90%     9.15ms
     99%    12.22ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14765.04     724.46   15507.46
  Latency        6.72ms     2.17ms    15.38ms
  Latency Distribution
     50%     6.55ms
     75%     8.35ms
     90%    10.04ms
     99%    12.80ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     84790.15   10879.25  105757.31
  Latency        1.20ms   371.38us     5.53ms
  Latency Distribution
     50%     1.13ms
     75%     1.47ms
     90%     1.83ms
     99%     2.64ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    138911.22   13961.88  147725.31
  Latency      698.39us   338.37us     6.09ms
  Latency Distribution
     50%   606.00us
     75%   839.00us
     90%     1.13ms
     99%     2.34ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    140814.75   15519.13  155714.28
  Latency      684.56us   358.80us     7.69ms
  Latency Distribution
     50%   591.00us
     75%   783.00us
     90%     0.98ms
     99%     2.57ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14425.12    1381.96   16513.88
  Latency        6.92ms     1.73ms    20.35ms
  Latency Distribution
     50%     6.87ms
     75%     8.06ms
     90%     9.29ms
     99%    12.24ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10596.00    1262.12   15333.74
  Latency        9.45ms     4.21ms    31.32ms
  Latency Distribution
     50%     9.06ms
     75%    12.07ms
     90%    15.30ms
     99%    23.30ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     17247.78    1215.40   18853.02
  Latency        5.77ms     1.65ms    16.29ms
  Latency Distribution
     50%     5.47ms
     75%     6.80ms
     90%     8.46ms
     99%    11.11ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12461.27    4083.41   38193.11
  Latency        8.38ms     3.11ms    27.02ms
  Latency Distribution
     50%     7.84ms
     75%    10.48ms
     90%    13.09ms
     99%    18.33ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    104426.92    7905.53  108915.33
  Latency        0.94ms   326.34us     4.47ms
  Latency Distribution
     50%     0.85ms
     75%     1.17ms
     90%     1.52ms
     99%     2.40ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    104556.02    7796.36  110011.60
  Latency        0.94ms   309.87us     5.23ms
  Latency Distribution
     50%     0.89ms
     75%     1.15ms
     90%     1.41ms
     99%     2.12ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     65167.73    4353.79   68700.45
  Latency        1.52ms   442.33us     4.66ms
  Latency Distribution
     50%     1.39ms
     75%     1.81ms
     90%     2.34ms
     99%     3.37ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     98747.61   10347.91  105914.08
  Latency        0.99ms   320.55us     4.00ms
  Latency Distribution
     50%     0.92ms
     75%     1.25ms
     90%     1.57ms
     99%     2.42ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     98528.97    7017.60  103153.58
  Latency        0.99ms   307.76us     4.72ms
  Latency Distribution
     50%     0.93ms
     75%     1.23ms
     90%     1.53ms
     99%     2.18ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     99944.06    7551.58  104204.64
  Latency        0.98ms   334.49us     4.94ms
  Latency Distribution
     50%     0.92ms
     75%     1.21ms
     90%     1.55ms
     99%     2.40ms
### CBV Response Types (/cbv-response)
  Reqs/sec    103752.87    8891.57  110609.20
  Latency        0.95ms   319.25us     4.59ms
  Latency Distribution
     50%     0.88ms
     75%     1.17ms
     90%     1.49ms
     99%     2.26ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     15844.43    1275.34   17596.41
  Latency        6.29ms     1.95ms    18.36ms
  Latency Distribution
     50%     5.94ms
     75%     7.90ms
     90%     9.43ms
     99%    12.12ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    113586.83   11090.40  124793.97
  Latency      837.65us   399.53us     5.86ms
  Latency Distribution
     50%   732.00us
     75%     1.03ms
     90%     1.45ms
     99%     2.54ms
### File Upload (POST /upload)
  Reqs/sec    102478.95    5831.30  108388.89
  Latency        0.95ms   424.67us     6.11ms
  Latency Distribution
     50%     0.86ms
     75%     1.14ms
     90%     1.57ms
     99%     2.92ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec     86038.58   32640.53  108247.67
  Latency        0.98ms   488.85us     7.18ms
  Latency Distribution
     50%     0.86ms
     75%     1.21ms
     90%     1.68ms
     99%     2.99ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec     10058.00    6091.40   54006.37
  Latency       10.78ms     2.66ms    24.14ms
  Latency Distribution
     50%    10.19ms
     75%    12.24ms
     90%    15.05ms
     99%    19.82ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    151741.31   13000.54  160035.95
  Latency      648.92us   303.36us     6.17ms
  Latency Distribution
     50%   609.00us
     75%   733.00us
     90%     0.91ms
     99%     1.85ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    140966.35   13647.12  156634.37
  Latency      681.20us   319.30us     5.56ms
  Latency Distribution
     50%   606.00us
     75%   839.00us
     90%     1.09ms
     99%     2.08ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     84087.25   10388.10   92168.60
  Latency        1.17ms   541.43us     8.96ms
  Latency Distribution
     50%     1.08ms
     75%     1.46ms
     90%     1.85ms
     99%     3.19ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    136717.68   11750.46  145299.04
  Latency      711.89us   358.20us     6.72ms
  Latency Distribution
     50%   611.00us
     75%     0.87ms
     90%     1.20ms
     99%     2.28ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    100901.62    6888.74  105792.04
  Latency        0.97ms   315.17us     4.75ms
  Latency Distribution
     50%     0.91ms
     75%     1.19ms
     90%     1.48ms
     99%     2.30ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    102167.95    8802.03  108399.49
  Latency        0.96ms   297.76us     4.42ms
  Latency Distribution
     50%     0.90ms
     75%     1.19ms
     90%     1.50ms
     99%     2.24ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec     89909.21    5784.94   93857.43
  Latency        1.09ms   388.06us     6.13ms
  Latency Distribution
     50%     1.00ms
     75%     1.36ms
     90%     1.77ms
     99%     2.75ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec     92992.99    6252.81   98501.63
  Latency        1.06ms   324.11us     4.91ms
  Latency Distribution
     50%     1.00ms
     75%     1.33ms
     90%     1.66ms
     99%     2.37ms

### Single union item — Like branch (/feed/2)
  Reqs/sec     90600.07    7296.65  100155.22
  Latency        1.10ms   351.65us     5.03ms
  Latency Distribution
     50%     1.03ms
     75%     1.38ms
     90%     1.73ms
     99%     2.62ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     63082.43    4099.69   69878.20
  Latency        1.56ms   552.89us     5.90ms
  Latency Distribution
     50%     1.47ms
     75%     1.95ms
     90%     2.43ms
     99%     3.87ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    177286.27   16797.40  189907.39
  Latency      546.89us   262.91us     5.16ms
  Latency Distribution
     50%   512.00us
     75%   667.00us
     90%   831.00us
     99%     1.66ms

### Path Parameter - int (/items/12345)
  Reqs/sec    150638.43    9780.16  162928.32
  Latency      632.60us   277.76us     5.47ms
  Latency Distribution
     50%   559.00us
     75%   761.00us
     90%     0.98ms
     99%     1.78ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    138722.40   10774.28  152540.36
  Latency      710.65us   317.23us     4.85ms
  Latency Distribution
     50%   621.00us
     75%   843.00us
     90%     1.20ms
     99%     2.18ms

### Header Parameter (/header)
  Reqs/sec     97492.11    8989.11  103037.79
  Latency        1.01ms   406.62us     5.77ms
  Latency Distribution
     50%     0.92ms
     75%     1.24ms
     90%     1.64ms
     99%     2.60ms

### Cookie Parameter (/cookie)
  Reqs/sec     88635.21    6522.55   97529.60
  Latency        1.10ms   381.89us     4.19ms
  Latency Distribution
     50%     1.02ms
     75%     1.35ms
     90%     1.75ms
     99%     2.87ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     77074.47    4813.92   81026.23
  Latency        1.29ms   441.94us     6.34ms
  Latency Distribution
     50%     1.21ms
     75%     1.58ms
     90%     1.97ms
     99%     3.23ms
