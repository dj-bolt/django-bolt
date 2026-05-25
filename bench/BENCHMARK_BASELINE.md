# Django-Bolt Benchmark
Generated: Mon 25 May 2026 09:21:02 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    169888.83   18492.00  186041.32
  Latency      538.25us   282.50us     4.11ms
  Latency Distribution
     50%   479.00us
     75%   607.00us
     90%   839.00us
     99%     2.05ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    122644.32    9491.33  129223.82
  Latency      792.61us   300.07us     5.04ms
  Latency Distribution
     50%   752.00us
     75%     0.96ms
     90%     1.17ms
     99%     2.01ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    126447.48    8535.24  132572.15
  Latency      770.58us   269.82us     4.85ms
  Latency Distribution
     50%   742.00us
     75%     0.87ms
     90%     1.04ms
     99%     1.92ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    102488.88    7952.03  109355.27
  Latency        0.96ms   313.16us     4.44ms
  Latency Distribution
     50%     0.89ms
     75%     1.19ms
     90%     1.54ms
     99%     2.37ms
### Cookie Endpoint (/cookie)
  Reqs/sec    103550.35    7581.57  107823.50
  Latency        0.95ms   298.27us     4.97ms
  Latency Distribution
     50%     0.87ms
     75%     1.17ms
     90%     1.49ms
     99%     2.27ms
### Exception Endpoint (/exc)
  Reqs/sec    137416.03   12653.83  148672.94
  Latency      706.35us   346.49us     5.76ms
  Latency Distribution
     50%   645.00us
     75%   847.00us
     90%     1.09ms
     99%     2.03ms
### HTML Response (/html)
  Reqs/sec    138856.13   37771.15  162553.28
  Latency      726.24us     0.97ms    12.43ms
  Latency Distribution
     50%   539.00us
     75%   762.00us
     90%     1.10ms
     99%     4.96ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     35983.95    7070.51   45486.40
  Latency        2.81ms     1.39ms    17.10ms
  Latency Distribution
     50%     2.47ms
     75%     3.40ms
     90%     4.54ms
     99%     8.39ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    149411.24   11250.32  159640.80
  Latency      625.81us   310.15us     5.55ms
  Latency Distribution
     50%   558.00us
     75%   743.00us
     90%     0.99ms
     99%     2.06ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    158304.84   13979.27  173603.14
  Latency      606.94us   374.86us     9.18ms
  Latency Distribution
     50%   528.00us
     75%   650.00us
     90%     0.90ms
     99%     2.22ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     68211.00    5445.37   72363.12
  Latency        1.42ms   424.12us     6.58ms
  Latency Distribution
     50%     1.37ms
     75%     1.62ms
     90%     1.97ms
     99%     2.92ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     67787.09    4941.50   71733.15
  Latency        1.44ms   407.01us     5.12ms
  Latency Distribution
     50%     1.32ms
     75%     1.73ms
     90%     2.33ms
     99%     3.04ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     73412.42   12368.11  101479.38
  Latency        1.42ms   404.57us     5.53ms
  Latency Distribution
     50%     1.33ms
     75%     1.73ms
     90%     2.15ms
     99%     3.06ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     15600.97    1266.09   17351.59
  Latency        6.38ms     1.50ms    19.22ms
  Latency Distribution
     50%     6.13ms
     75%     7.40ms
     90%     8.54ms
     99%    11.41ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14042.30    1492.86   16011.17
  Latency        7.07ms     2.08ms    30.39ms
  Latency Distribution
     50%     6.79ms
     75%     8.34ms
     90%    10.08ms
     99%    13.62ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     77966.38    4863.82   81735.13
  Latency        1.26ms   506.32us     6.97ms
  Latency Distribution
     50%     1.14ms
     75%     1.58ms
     90%     2.03ms
     99%     3.29ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    133192.47   12163.07  140982.48
  Latency      722.81us   406.57us     5.40ms
  Latency Distribution
     50%   611.00us
     75%   820.00us
     90%     1.17ms
     99%     2.81ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    106015.62   27511.68  134382.43
  Latency      822.55us   383.12us     5.33ms
  Latency Distribution
     50%   748.00us
     75%     0.97ms
     90%     1.37ms
     99%     2.72ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14038.03    1208.50   16737.66
  Latency        7.11ms     1.36ms    20.12ms
  Latency Distribution
     50%     6.82ms
     75%     8.20ms
     90%     9.12ms
     99%    11.37ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10983.03    1309.38   13645.38
  Latency        9.06ms     3.38ms    32.85ms
  Latency Distribution
     50%     8.35ms
     75%    11.08ms
     90%    14.16ms
     99%    20.25ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     17286.64    1505.19   22756.22
  Latency        5.81ms     1.34ms    16.61ms
  Latency Distribution
     50%     5.68ms
     75%     6.74ms
     90%     7.85ms
     99%    10.17ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     11504.43     827.43   13477.70
  Latency        8.62ms     3.26ms    32.32ms
  Latency Distribution
     50%     8.04ms
     75%    10.22ms
     90%    13.19ms
     99%    20.09ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     72453.70   20162.19   91361.75
  Latency        1.37ms   827.41us    11.11ms
  Latency Distribution
     50%     1.15ms
     75%     1.65ms
     90%     2.27ms
     99%     5.08ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     88435.84    9168.83   99962.65
  Latency        1.11ms   453.33us     7.27ms
  Latency Distribution
     50%     1.02ms
     75%     1.36ms
     90%     1.75ms
     99%     2.72ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     59031.82    5337.44   64332.94
  Latency        1.65ms   514.56us     6.27ms
  Latency Distribution
     50%     1.54ms
     75%     1.95ms
     90%     2.42ms
     99%     3.79ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     70557.28   11559.31   82026.54
  Latency        1.41ms   729.61us    12.46ms
  Latency Distribution
     50%     1.22ms
     75%     1.76ms
     90%     2.33ms
     99%     4.89ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     77141.58   12326.73   88693.48
  Latency        1.21ms   453.79us     5.63ms
  Latency Distribution
     50%     1.12ms
     75%     1.52ms
     90%     1.93ms
     99%     3.25ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     52311.92   10342.17   65498.15
  Latency        1.87ms     1.28ms    17.41ms
  Latency Distribution
     50%     1.52ms
     75%     2.34ms
     90%     3.40ms
     99%     6.84ms
### CBV Response Types (/cbv-response)
  Reqs/sec     84480.42    5650.09   90644.85
  Latency        1.16ms   489.10us     5.63ms
  Latency Distribution
     50%     1.03ms
     75%     1.50ms
     90%     1.96ms
     99%     3.32ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     15547.84    1657.42   20303.95
  Latency        6.46ms     1.66ms    18.86ms
  Latency Distribution
     50%     6.29ms
     75%     7.52ms
     90%     8.81ms
     99%    12.03ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    113314.32   10678.17  121824.58
  Latency      835.44us   363.35us     5.76ms
  Latency Distribution
     50%   745.00us
     75%     1.04ms
     90%     1.47ms
     99%     2.53ms
### File Upload (POST /upload)
  Reqs/sec     99309.76    5452.69  105289.34
  Latency        0.98ms   388.25us     5.35ms
  Latency Distribution
     50%     0.91ms
     75%     1.22ms
     90%     1.61ms
     99%     2.70ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec     65806.75   11870.74   77332.00
  Latency        1.50ms     0.95ms    12.49ms
  Latency Distribution
     50%     1.23ms
     75%     1.91ms
     90%     2.93ms
     99%     5.67ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec     58491.61    3370.07   61567.83
  Latency        1.69ms   591.87us     7.95ms
  Latency Distribution
     50%     1.61ms
     75%     2.08ms
     90%     2.58ms
     99%     4.13ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec     84618.84    9000.03   92349.74
  Latency        1.16ms   465.80us     6.49ms
  Latency Distribution
     50%     1.07ms
     75%     1.47ms
     90%     1.97ms
     99%     3.19ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      8089.00    1214.20    9871.29
  Latency       12.29ms     4.14ms    48.27ms
  Latency Distribution
     50%    12.18ms
     75%    14.72ms
     90%    17.60ms
     99%    24.89ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    121565.18   12964.33  141955.15
  Latency      838.22us   534.43us     6.13ms
  Latency Distribution
     50%   672.00us
     75%     1.00ms
     90%     1.50ms
     99%     3.65ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    130145.17    8972.67  136777.40
  Latency      736.60us   325.12us     8.32ms
  Latency Distribution
     50%   643.00us
     75%     0.91ms
     90%     1.26ms
     99%     2.15ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     83682.82    7538.24   88958.30
  Latency        1.18ms   462.00us     6.03ms
  Latency Distribution
     50%     1.06ms
     75%     1.47ms
     90%     1.91ms
     99%     3.02ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    107821.89    8565.13  116892.37
  Latency        0.90ms   490.44us     6.57ms
  Latency Distribution
     50%   776.00us
     75%     1.15ms
     90%     1.61ms
     99%     3.25ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec     89692.13    6620.46   94589.98
  Latency        1.10ms   491.69us     6.72ms
  Latency Distribution
     50%     0.99ms
     75%     1.34ms
     90%     1.75ms
     99%     2.97ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec     94778.33    5333.87  100577.03
  Latency        1.02ms   310.13us     4.97ms
  Latency Distribution
     50%     0.97ms
     75%     1.26ms
     90%     1.57ms
     99%     2.36ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec     82890.74    4979.65   88399.75
  Latency        1.19ms   444.17us     5.41ms
  Latency Distribution
     50%     1.10ms
     75%     1.47ms
     90%     1.90ms
     99%     3.11ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec     93665.51    8423.54  102207.44
  Latency        1.06ms   336.26us     5.53ms
  Latency Distribution
     50%     0.99ms
     75%     1.33ms
     90%     1.69ms
     99%     2.48ms

### Single union item — Like branch (/feed/2)
  Reqs/sec     94015.64    8556.57  101066.67
  Latency        1.06ms   363.30us    10.52ms
  Latency Distribution
     50%     0.99ms
     75%     1.30ms
     90%     1.62ms
     99%     2.43ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     66675.13    5464.77   72678.64
  Latency        1.47ms   439.41us     5.29ms
  Latency Distribution
     50%     1.45ms
     75%     1.80ms
     90%     2.23ms
     99%     3.35ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    145545.76   12856.99  158853.74
  Latency      653.80us   396.45us     5.91ms
  Latency Distribution
     50%   537.00us
     75%   767.00us
     90%     1.15ms
     99%     2.56ms

### Path Parameter - int (/items/12345)
  Reqs/sec    125546.32   14784.41  136624.57
  Latency      780.86us   434.02us     6.40ms
  Latency Distribution
     50%   646.00us
     75%     0.94ms
     90%     1.39ms
     99%     2.82ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    133949.80   19241.66  156093.00
  Latency      734.49us   403.20us     5.82ms
  Latency Distribution
     50%   628.00us
     75%     0.89ms
     90%     1.27ms
     99%     2.60ms

### Header Parameter (/header)
  Reqs/sec    102354.88    8087.60  106864.22
  Latency        0.96ms   311.18us     4.92ms
  Latency Distribution
     50%     0.90ms
     75%     1.17ms
     90%     1.45ms
     99%     2.17ms

### Cookie Parameter (/cookie)
  Reqs/sec    101383.41    9023.70  109298.71
  Latency        0.97ms   320.20us     5.62ms
  Latency Distribution
     50%     0.92ms
     75%     1.17ms
     90%     1.47ms
     99%     2.20ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     71637.66    6902.24   77551.99
  Latency        1.37ms   436.26us     6.12ms
  Latency Distribution
     50%     1.30ms
     75%     1.71ms
     90%     2.17ms
     99%     3.17ms
