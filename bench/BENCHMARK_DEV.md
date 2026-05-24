# Django-Bolt Benchmark
Generated: Sun 24 May 2026 10:05:45 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    167376.11   24214.53  185516.85
  Latency      578.27us   308.92us     5.88ms
  Latency Distribution
     50%   511.00us
     75%   693.00us
     90%     0.88ms
     99%     2.35ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    124238.07   13416.78  141848.71
  Latency      810.79us   387.73us     5.92ms
  Latency Distribution
     50%   757.00us
     75%     0.96ms
     90%     1.24ms
     99%     2.34ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    119554.73   11266.08  127720.09
  Latency      820.08us   393.20us     5.68ms
  Latency Distribution
     50%   718.00us
     75%     1.02ms
     90%     1.29ms
     99%     2.58ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    105587.33    8826.54  110595.28
  Latency        0.93ms   269.94us     5.50ms
  Latency Distribution
     50%     0.88ms
     75%     1.12ms
     90%     1.40ms
     99%     2.09ms
### Cookie Endpoint (/cookie)
  Reqs/sec    104854.85    8571.20  111476.53
  Latency        0.94ms   328.27us     4.65ms
  Latency Distribution
     50%     0.86ms
     75%     1.15ms
     90%     1.50ms
     99%     2.37ms
### Exception Endpoint (/exc)
  Reqs/sec    139992.65   12142.82  147280.27
  Latency      693.10us   272.92us     4.42ms
  Latency Distribution
     50%   629.00us
     75%     0.85ms
     90%     1.05ms
     99%     1.98ms
### HTML Response (/html)
  Reqs/sec    146619.59   28987.96  172498.74
  Latency      608.68us   256.19us     5.18ms
  Latency Distribution
     50%   575.00us
     75%   736.00us
     90%     0.91ms
     99%     1.69ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     36894.24    6281.57   41216.47
  Latency        2.71ms     1.08ms    15.81ms
  Latency Distribution
     50%     2.50ms
     75%     3.18ms
     90%     3.99ms
     99%     7.82ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    165972.96   12030.42  173074.26
  Latency      583.49us   312.21us     6.60ms
  Latency Distribution
     50%   526.00us
     75%   654.00us
     90%   831.00us
     99%     2.24ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    156068.13   12545.06  167698.29
  Latency      601.77us   304.58us     5.70ms
  Latency Distribution
     50%   547.00us
     75%   731.00us
     90%   846.00us
     99%     1.58ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     71681.68    4504.05   74978.32
  Latency        1.37ms   390.49us     6.05ms
  Latency Distribution
     50%     1.23ms
     75%     1.79ms
     90%     2.16ms
     99%     2.84ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     70049.77    3942.32   72070.59
  Latency        1.40ms   332.09us     7.17ms
  Latency Distribution
     50%     1.42ms
     75%     1.60ms
     90%     1.82ms
     99%     2.60ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     78262.99    5428.03   82562.07
  Latency        1.26ms   375.68us     4.62ms
  Latency Distribution
     50%     1.20ms
     75%     1.60ms
     90%     2.01ms
     99%     2.85ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     16831.65    1348.26   18335.71
  Latency        5.91ms     2.35ms    17.96ms
  Latency Distribution
     50%     5.06ms
     75%     7.36ms
     90%    10.39ms
     99%    13.37ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14938.84     705.77   16069.20
  Latency        6.66ms     1.80ms    16.11ms
  Latency Distribution
     50%     6.38ms
     75%     7.96ms
     90%     9.45ms
     99%    12.66ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     83251.85    7255.86   87222.83
  Latency        1.18ms   372.80us     4.70ms
  Latency Distribution
     50%     1.09ms
     75%     1.46ms
     90%     1.91ms
     99%     2.86ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    151783.77   15119.12  165858.82
  Latency      632.05us   330.91us    10.06ms
  Latency Distribution
     50%   593.00us
     75%   736.00us
     90%   843.00us
     99%     1.84ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    145493.58   11692.40  154770.13
  Latency      664.70us   315.17us     5.67ms
  Latency Distribution
     50%   621.00us
     75%   817.00us
     90%     0.92ms
     99%     1.99ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14354.46    1706.17   20251.81
  Latency        7.01ms     1.34ms    22.81ms
  Latency Distribution
     50%     6.72ms
     75%     7.86ms
     90%     8.87ms
     99%    10.81ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10249.47    1001.88   12478.77
  Latency        9.65ms     5.30ms    37.35ms
  Latency Distribution
     50%     7.81ms
     75%    12.72ms
     90%    18.09ms
     99%    27.75ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     17636.33    2633.27   30413.63
  Latency        5.78ms     1.15ms    13.67ms
  Latency Distribution
     50%     5.62ms
     75%     6.66ms
     90%     7.74ms
     99%     9.35ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12074.81     966.75   16512.24
  Latency        8.27ms     3.35ms    28.37ms
  Latency Distribution
     50%     7.48ms
     75%    10.08ms
     90%    13.43ms
     99%    19.29ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     99812.49    9619.05  105753.16
  Latency        0.98ms   327.91us     4.66ms
  Latency Distribution
     50%     0.92ms
     75%     1.22ms
     90%     1.56ms
     99%     2.40ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    103901.50    7873.99  109382.35
  Latency        0.94ms   290.82us     4.46ms
  Latency Distribution
     50%     0.88ms
     75%     1.15ms
     90%     1.49ms
     99%     2.24ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     63752.25    3700.23   66235.55
  Latency        1.55ms   476.45us     5.76ms
  Latency Distribution
     50%     1.46ms
     75%     1.88ms
     90%     2.33ms
     99%     3.56ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    101059.46    5899.75  104982.46
  Latency        0.97ms   278.29us     4.96ms
  Latency Distribution
     50%     0.90ms
     75%     1.20ms
     90%     1.50ms
     99%     2.14ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     97642.16   10778.85  108364.72
  Latency        1.03ms   385.93us     5.51ms
  Latency Distribution
     50%     0.94ms
     75%     1.27ms
     90%     1.63ms
     99%     2.51ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    101274.66    8238.61  107221.32
  Latency        0.96ms   310.54us     6.00ms
  Latency Distribution
     50%     0.90ms
     75%     1.18ms
     90%     1.47ms
     99%     2.18ms
### CBV Response Types (/cbv-response)
  Reqs/sec    104047.87    5417.94  108854.94
  Latency        0.94ms   297.64us     4.96ms
  Latency Distribution
     50%     0.87ms
     75%     1.14ms
     90%     1.43ms
     99%     2.20ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     15703.86    1350.58   19594.86
  Latency        6.39ms     1.61ms    17.40ms
  Latency Distribution
     50%     6.43ms
     75%     7.78ms
     90%     8.70ms
     99%    11.02ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    112077.07   15923.83  124771.78
  Latency        0.88ms   540.07us     6.51ms
  Latency Distribution
     50%   745.00us
     75%     1.09ms
     90%     1.51ms
     99%     3.64ms
### File Upload (POST /upload)
  Reqs/sec    111154.73    9114.36  116728.43
  Latency        0.87ms   347.56us     5.89ms
  Latency Distribution
     50%   796.00us
     75%     1.05ms
     90%     1.37ms
     99%     2.44ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    107407.28    9315.58  114784.60
  Latency        0.91ms   325.59us     5.26ms
  Latency Distribution
     50%   824.00us
     75%     1.13ms
     90%     1.42ms
     99%     2.31ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9598.82    1002.44   14047.03
  Latency       10.45ms     2.46ms    23.07ms
  Latency Distribution
     50%    10.40ms
     75%    12.51ms
     90%    14.00ms
     99%    16.99ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    142877.84   11949.80  151685.40
  Latency      679.04us   281.21us     4.83ms
  Latency Distribution
     50%   606.00us
     75%   825.00us
     90%     1.17ms
     99%     1.90ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    148504.89   12322.45  156546.68
  Latency      651.28us   265.07us     6.98ms
  Latency Distribution
     50%   580.00us
     75%   776.00us
     90%     1.03ms
     99%     1.67ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     88361.62    8623.83   94434.54
  Latency        1.12ms   494.35us     7.18ms
  Latency Distribution
     50%     1.00ms
     75%     1.34ms
     90%     1.76ms
     99%     3.31ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    143719.01   13015.75  155554.87
  Latency      661.01us   303.22us     5.29ms
  Latency Distribution
     50%   582.00us
     75%   770.00us
     90%     1.01ms
     99%     2.24ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    106067.03    8131.89  110730.09
  Latency        0.92ms   265.73us     5.08ms
  Latency Distribution
     50%     0.87ms
     75%     1.13ms
     90%     1.39ms
     99%     2.00ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    105167.96    5602.60  109375.33
  Latency        0.94ms   322.41us     3.92ms
  Latency Distribution
     50%   849.00us
     75%     1.15ms
     90%     1.49ms
     99%     2.45ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    100551.12    7874.32  105090.09
  Latency        0.98ms   294.16us     4.00ms
  Latency Distribution
     50%     0.91ms
     75%     1.21ms
     90%     1.53ms
     99%     2.31ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec    100710.60    6192.42  107866.47
  Latency        0.97ms   303.60us     4.84ms
  Latency Distribution
     50%     0.91ms
     75%     1.18ms
     90%     1.48ms
     99%     2.08ms

### Single union item — Like branch (/feed/2)
  Reqs/sec     96577.55    6531.69  101962.00
  Latency        1.03ms   326.11us     5.21ms
  Latency Distribution
     50%     0.96ms
     75%     1.25ms
     90%     1.59ms
     99%     2.33ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     67576.41    4676.78   71468.07
  Latency        1.43ms   428.55us     6.63ms
  Latency Distribution
     50%     1.39ms
     75%     1.72ms
     90%     2.23ms
     99%     3.08ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    165696.68   19234.29  177628.47
  Latency      586.69us   342.42us     5.56ms
  Latency Distribution
     50%   507.00us
     75%   687.00us
     90%     0.96ms
     99%     2.08ms

### Path Parameter - int (/items/12345)
  Reqs/sec    143847.67   11003.75  154040.49
  Latency      684.49us   341.61us     5.03ms
  Latency Distribution
     50%   616.00us
     75%   803.00us
     90%     1.08ms
     99%     2.39ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    148611.74   15072.42  164300.53
  Latency      669.43us   299.09us     5.35ms
  Latency Distribution
     50%   603.00us
     75%   796.00us
     90%     1.06ms
     99%     1.88ms

### Header Parameter (/header)
  Reqs/sec    100747.75    8969.73  105810.80
  Latency        0.98ms   331.78us     4.89ms
  Latency Distribution
     50%     0.90ms
     75%     1.19ms
     90%     1.51ms
     99%     2.39ms

### Cookie Parameter (/cookie)
  Reqs/sec     99215.21    7890.12  103651.90
  Latency        0.99ms   338.93us     5.23ms
  Latency Distribution
     50%     0.92ms
     75%     1.22ms
     90%     1.57ms
     99%     2.30ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     83210.45    5535.58   86230.20
  Latency        1.18ms   320.92us     5.11ms
  Latency Distribution
     50%     1.13ms
     75%     1.46ms
     90%     1.76ms
     99%     2.44ms
