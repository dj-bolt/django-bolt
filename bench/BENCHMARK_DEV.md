# Django-Bolt Benchmark
Generated: Mon 25 May 2026 09:39:14 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    146046.19   22491.60  174286.09
  Latency      686.79us   374.36us     7.97ms
  Latency Distribution
     50%   577.00us
     75%     0.87ms
     90%     1.20ms
     99%     2.45ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    108727.93    7812.01  118108.51
  Latency        0.89ms   427.37us     7.29ms
  Latency Distribution
     50%   779.00us
     75%     1.06ms
     90%     1.49ms
     99%     2.67ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    115335.54    8896.05  124464.39
  Latency      838.47us   435.83us     6.02ms
  Latency Distribution
     50%   703.00us
     75%     1.02ms
     90%     1.40ms
     99%     2.72ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec     91723.14    5481.50   99856.93
  Latency        1.07ms   414.22us     5.56ms
  Latency Distribution
     50%     0.97ms
     75%     1.34ms
     90%     1.72ms
     99%     2.97ms
### Cookie Endpoint (/cookie)
  Reqs/sec    101941.31   14175.22  127581.45
  Latency        1.01ms   388.45us     5.28ms
  Latency Distribution
     50%     0.92ms
     75%     1.26ms
     90%     1.68ms
     99%     2.81ms
### Exception Endpoint (/exc)
  Reqs/sec    122946.89    7706.67  133288.85
  Latency      780.99us   425.39us     6.83ms
  Latency Distribution
     50%   685.00us
     75%     0.89ms
     90%     1.25ms
     99%     3.00ms
### HTML Response (/html)
  Reqs/sec    142737.57   10902.68  158655.43
  Latency      672.83us   446.89us     8.17ms
  Latency Distribution
     50%   553.00us
     75%   779.00us
     90%     1.13ms
     99%     2.70ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     30549.82    4718.46   35308.48
  Latency        3.26ms     1.48ms    17.84ms
  Latency Distribution
     50%     2.93ms
     75%     3.96ms
     90%     5.32ms
     99%     9.29ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    133969.89   13018.67  145143.49
  Latency      725.00us   439.19us     7.32ms
  Latency Distribution
     50%   602.00us
     75%     0.85ms
     90%     1.30ms
     99%     2.85ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    148020.97   20168.13  168791.24
  Latency      633.81us   481.93us     6.73ms
  Latency Distribution
     50%   529.00us
     75%   684.00us
     90%     0.89ms
     99%     3.41ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     64513.83    4485.83   70564.26
  Latency        1.52ms   584.97us     6.76ms
  Latency Distribution
     50%     1.42ms
     75%     1.81ms
     90%     2.28ms
     99%     4.27ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     58252.49    8847.81   67131.08
  Latency        1.69ms     0.86ms    12.85ms
  Latency Distribution
     50%     1.42ms
     75%     2.09ms
     90%     2.88ms
     99%     5.34ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     67286.15    2018.43   70204.82
  Latency        1.46ms   556.73us     8.78ms
  Latency Distribution
     50%     1.37ms
     75%     1.83ms
     90%     2.34ms
     99%     3.86ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     16412.49    1506.43   18498.63
  Latency        6.07ms     1.85ms    16.98ms
  Latency Distribution
     50%     5.67ms
     75%     7.54ms
     90%     9.03ms
     99%    11.64ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14400.33    1148.01   15922.58
  Latency        6.92ms     2.02ms    23.86ms
  Latency Distribution
     50%     6.64ms
     75%     8.39ms
     90%     9.97ms
     99%    12.94ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     80454.29    9013.82   85496.73
  Latency        1.21ms   407.17us     7.52ms
  Latency Distribution
     50%     1.14ms
     75%     1.48ms
     90%     1.87ms
     99%     2.85ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    125401.69   17277.78  153225.60
  Latency      823.06us   669.52us    12.64ms
  Latency Distribution
     50%   661.00us
     75%     0.90ms
     90%     1.39ms
     99%     4.25ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    114700.60   11749.99  122017.05
  Latency        0.86ms   669.87us     6.42ms
  Latency Distribution
     50%   664.00us
     75%     0.94ms
     90%     1.54ms
     99%     4.45ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
 5799 / 10000 [================================================================================================================================================================================>-------------------------------------------------------------------------------------------------------------------------------]  57.99% 14465/s
  Reqs/sec     14771.55    1374.79   17006.10
  Latency        6.75ms     2.17ms    18.64ms
  Latency Distribution
     50%     6.63ms
     75%     8.41ms
     90%     9.98ms
     99%    13.40ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10455.30     995.08   13042.75
  Latency        9.51ms     4.16ms    28.20ms
  Latency Distribution
     50%     8.58ms
     75%    12.55ms
     90%    16.13ms
     99%    21.71ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     18346.83    1840.33   25264.35
  Latency        5.46ms     1.47ms    13.78ms
  Latency Distribution
     50%     5.31ms
     75%     6.67ms
     90%     7.83ms
     99%     9.91ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12171.02     890.46   14171.90
  Latency        8.19ms     4.03ms    33.78ms
  Latency Distribution
     50%     6.98ms
     75%    10.22ms
     90%    14.37ms
     99%    22.61ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     94669.74    7064.27  102866.58
  Latency        1.03ms   389.18us     6.87ms
  Latency Distribution
     50%     0.92ms
     75%     1.30ms
     90%     1.68ms
     99%     2.67ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     83995.23    9307.69   93761.52
  Latency        1.17ms   494.11us     7.07ms
  Latency Distribution
     50%     1.08ms
     75%     1.45ms
     90%     1.88ms
     99%     3.04ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     59158.45    7091.55   65043.14
  Latency        1.67ms   560.49us     6.27ms
  Latency Distribution
     50%     1.53ms
     75%     2.00ms
     90%     2.56ms
     99%     4.02ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     86573.15    5633.66   92253.86
  Latency        1.14ms   395.06us     5.21ms
  Latency Distribution
     50%     1.04ms
     75%     1.42ms
     90%     1.87ms
     99%     2.89ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     89165.41   10646.47   99907.55
  Latency        1.10ms   403.23us     7.21ms
  Latency Distribution
     50%     1.01ms
     75%     1.36ms
     90%     1.81ms
     99%     2.84ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     99736.74    8268.81  105403.99
  Latency        0.98ms   353.30us     5.85ms
  Latency Distribution
     50%     0.89ms
     75%     1.20ms
     90%     1.56ms
     99%     2.41ms
### CBV Response Types (/cbv-response)
  Reqs/sec    106140.28    5988.37  112050.37
  Latency        0.92ms   288.13us     4.36ms
  Latency Distribution
     50%     0.87ms
     75%     1.14ms
     90%     1.45ms
     99%     2.14ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16182.20    1264.19   17265.47
  Latency        6.14ms     2.14ms    20.51ms
  Latency Distribution
     50%     5.70ms
     75%     7.55ms
     90%     9.86ms
     99%    12.57ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    130933.07   14701.08  140677.50
  Latency      738.87us   408.33us     7.48ms
  Latency Distribution
     50%   693.00us
     75%     0.87ms
     90%     1.09ms
     99%     2.03ms
### File Upload (POST /upload)
  Reqs/sec    112116.10    8876.43  118974.93
  Latency        0.88ms   332.30us     5.25ms
  Latency Distribution
     50%   800.00us
     75%     1.08ms
     90%     1.33ms
     99%     2.23ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    109489.02    7462.59  115592.95
  Latency        0.89ms   329.85us     5.66ms
  Latency Distribution
     50%     0.85ms
     75%     1.05ms
     90%     1.36ms
     99%     2.14ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    114239.57   16891.57  127951.34
  Latency      797.63us   290.12us     5.29ms
  Latency Distribution
     50%   751.00us
     75%     0.97ms
     90%     1.23ms
     99%     2.00ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    102324.09    8627.81  108341.76
  Latency        0.95ms   355.71us     6.24ms
  Latency Distribution
     50%     0.89ms
     75%     1.14ms
     90%     1.41ms
     99%     2.28ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9470.20    1029.72   11116.90
  Latency       10.52ms     2.43ms    23.35ms
  Latency Distribution
     50%    10.21ms
     75%    11.92ms
     90%    14.12ms
     99%    18.52ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    133169.48   13560.21  145596.64
  Latency      731.48us   415.29us     6.38ms
  Latency Distribution
     50%   649.00us
     75%     0.86ms
     90%     1.15ms
     99%     2.64ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    113334.37    5749.27  120447.39
  Latency        0.85ms   495.29us    10.70ms
  Latency Distribution
     50%   725.00us
     75%     1.06ms
     90%     1.49ms
     99%     3.34ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     63123.47   34550.63  148408.64
  Latency        1.85ms     1.15ms    16.52ms
  Latency Distribution
     50%     1.56ms
     75%     2.27ms
     90%     3.21ms
     99%     6.75ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    131664.52   14402.96  141689.98
  Latency      740.43us   331.78us     5.45ms
  Latency Distribution
     50%   649.00us
     75%     0.91ms
     90%     1.25ms
     99%     2.41ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec     85022.60    8723.45   94517.66
  Latency        1.16ms   484.31us     6.67ms
  Latency Distribution
     50%     1.06ms
     75%     1.43ms
     90%     1.86ms
     99%     3.60ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec     86650.96    3920.35   89322.47
  Latency        1.14ms   428.49us     5.49ms
  Latency Distribution
     50%     1.05ms
     75%     1.43ms
     90%     1.83ms
     99%     3.05ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec     93879.00    7181.84   98932.90
  Latency        1.03ms   341.13us     5.26ms
  Latency Distribution
     50%     0.95ms
     75%     1.28ms
     90%     1.64ms
     99%     2.61ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec     99213.10    5383.26  104254.16
  Latency        0.99ms   288.76us     5.57ms
  Latency Distribution
     50%     0.93ms
     75%     1.23ms
     90%     1.54ms
     99%     2.18ms

### Single union item — Like branch (/feed/2)
  Reqs/sec     90964.89    8931.99  100972.70
  Latency        1.06ms   395.84us     5.38ms
  Latency Distribution
     50%     0.97ms
     75%     1.33ms
     90%     1.71ms
     99%     2.79ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     68867.80    3978.04   71848.51
  Latency        1.43ms   424.12us     5.17ms
  Latency Distribution
     50%     1.33ms
     75%     1.75ms
     90%     2.13ms
     99%     3.19ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    134570.90    9610.27  145964.31
  Latency      722.76us   438.56us     5.22ms
  Latency Distribution
     50%   579.00us
     75%     0.89ms
     90%     1.33ms
     99%     3.15ms

### Path Parameter - int (/items/12345)
  Reqs/sec    151439.61   14485.94  164978.99
  Latency      653.04us   300.56us     6.05ms
  Latency Distribution
     50%   606.00us
     75%   761.00us
     90%     1.01ms
     99%     1.99ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    135662.99   37367.89  164410.45
  Latency      759.89us   592.65us     9.19ms
  Latency Distribution
     50%   595.00us
     75%   796.00us
     90%     1.29ms
     99%     3.94ms

### Header Parameter (/header)
  Reqs/sec     97028.76   11430.17  111490.50
  Latency        1.03ms   383.69us     6.48ms
  Latency Distribution
     50%     0.95ms
     75%     1.28ms
     90%     1.63ms
     99%     2.60ms

### Cookie Parameter (/cookie)
  Reqs/sec     90051.64    8943.30  101066.50
  Latency        1.08ms   513.11us     7.34ms
  Latency Distribution
     50%     0.97ms
     75%     1.36ms
     90%     1.73ms
     99%     3.41ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     72112.51    4872.35   77772.28
  Latency        1.36ms   428.07us     5.82ms
  Latency Distribution
     50%     1.28ms
     75%     1.69ms
     90%     2.12ms
     99%     3.01ms
