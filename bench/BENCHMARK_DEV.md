# Django-Bolt Benchmark
Generated: Sun 24 May 2026 11:31:42 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    166529.85   21994.81  180936.83
  Latency      581.56us   368.35us     6.46ms
  Latency Distribution
     50%   483.00us
     75%   649.00us
     90%     1.01ms
     99%     2.29ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    121162.14   12297.11  130843.07
  Latency      802.88us   336.83us     7.54ms
  Latency Distribution
     50%   749.00us
     75%     0.93ms
     90%     1.18ms
     99%     2.10ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    124789.51   10383.11  130164.62
  Latency      777.59us   303.95us     5.68ms
  Latency Distribution
     50%   742.00us
     75%     0.92ms
     90%     1.15ms
     99%     2.00ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    104771.96    7321.29  109841.75
  Latency        0.94ms   324.10us     3.94ms
  Latency Distribution
     50%     0.86ms
     75%     1.13ms
     90%     1.44ms
     99%     2.44ms
### Cookie Endpoint (/cookie)
  Reqs/sec    103458.35    6872.06  108630.31
  Latency        0.95ms   291.00us     4.68ms
  Latency Distribution
     50%     0.89ms
     75%     1.16ms
     90%     1.45ms
     99%     2.22ms
### Exception Endpoint (/exc)
  Reqs/sec    143225.31   12455.22  152584.35
  Latency      681.73us   257.37us     5.41ms
  Latency Distribution
     50%   661.00us
     75%   835.00us
     90%     1.02ms
     99%     1.89ms
### HTML Response (/html)
  Reqs/sec    136816.76   53631.72  174157.83
  Latency      595.38us   290.35us     5.02ms
  Latency Distribution
     50%   551.00us
     75%   674.00us
     90%     0.86ms
     99%     1.84ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     34211.58    5372.34   39222.28
  Latency        2.90ms     1.27ms    14.71ms
  Latency Distribution
     50%     2.60ms
     75%     3.56ms
     90%     4.63ms
     99%     8.33ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    148541.27   17565.21  164695.27
  Latency      625.96us   359.99us     5.38ms
  Latency Distribution
     50%   529.00us
     75%   720.00us
     90%     1.00ms
     99%     2.47ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    160640.55   13724.06  174054.72
  Latency      596.34us   371.75us     6.49ms
  Latency Distribution
     50%   508.00us
     75%   656.00us
     90%     0.92ms
     99%     2.50ms
### List of 100 structs, no union (/bench/list)
  Reqs/sec     70766.31    4565.84   77033.40
  Latency        1.39ms   424.76us     6.37ms
  Latency Distribution
     50%     1.33ms
     75%     1.60ms
     90%     2.05ms
     99%     2.79ms
### List of 100 structs via tagged union (/bench/union-list)
  Reqs/sec     63255.63   17959.02   73011.01
  Latency        1.42ms   350.14us     5.81ms
  Latency Distribution
     50%     1.59ms
     75%     1.79ms
     90%     1.93ms
     99%     2.65ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     78055.52    5874.26   83052.48
  Latency        1.27ms   362.94us     5.60ms
  Latency Distribution
     50%     1.21ms
     75%     1.57ms
     90%     1.95ms
     99%     2.75ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     16680.44    1249.01   17711.34
  Latency        5.96ms     1.86ms    17.01ms
  Latency Distribution
     50%     6.01ms
     75%     7.57ms
     90%     8.72ms
     99%    11.15ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14859.69     769.30   16258.73
  Latency        6.69ms     2.03ms    16.85ms
  Latency Distribution
     50%     6.43ms
     75%     8.44ms
     90%     9.99ms
     99%    12.53ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     84600.12   10468.81  106676.38
  Latency        1.21ms   359.35us     5.17ms
  Latency Distribution
     50%     1.14ms
     75%     1.49ms
     90%     1.87ms
     99%     2.72ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    152879.60   16173.25  164644.33
  Latency      636.59us   377.01us     6.30ms
  Latency Distribution
     50%   561.00us
     75%   685.00us
     90%     0.91ms
     99%     2.20ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    148285.22   16106.60  156671.35
  Latency      664.68us   366.22us     8.97ms
  Latency Distribution
     50%   578.00us
     75%   756.00us
     90%     0.99ms
     99%     2.38ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14187.17    1481.02   18879.46
  Latency        7.06ms     1.65ms    21.76ms
  Latency Distribution
     50%     7.28ms
     75%     8.28ms
     90%     8.96ms
     99%    10.85ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10133.49    1057.42   13224.74
  Latency        9.85ms     5.01ms    34.14ms
  Latency Distribution
     50%     9.03ms
     75%    12.71ms
     90%    17.55ms
     99%    25.43ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     17257.89    1087.11   18903.80
  Latency        5.77ms     1.79ms    14.29ms
  Latency Distribution
     50%     5.49ms
     75%     7.73ms
     90%     8.67ms
     99%    10.56ms
### Users Mini10 (Sync) (/users/sync-mini10)
 7090 / 10000 [==========================================================>-----------------------]  70.90% 11796/s
  Reqs/sec     11899.72     757.44   14026.35
  Latency        8.37ms     3.24ms    23.31ms
  Latency Distribution
     50%     8.03ms
     75%    10.54ms
     90%    13.22ms
     99%    18.25ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    108237.69    9554.15  114864.01
  Latency        0.91ms   310.36us     4.92ms
  Latency Distribution
     50%     0.85ms
     75%     1.12ms
     90%     1.38ms
     99%     2.20ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    105593.03    8389.66  110304.30
  Latency        0.93ms   300.89us     5.07ms
  Latency Distribution
     50%     0.86ms
     75%     1.13ms
     90%     1.41ms
     99%     2.12ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     65701.47    4616.52   69285.73
  Latency        1.50ms   469.49us     5.78ms
  Latency Distribution
     50%     1.39ms
     75%     1.84ms
     90%     2.39ms
     99%     3.43ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    101351.40    7337.77  107195.03
  Latency        0.97ms   374.28us     4.92ms
  Latency Distribution
     50%     0.87ms
     75%     1.19ms
     90%     1.59ms
     99%     2.71ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    101645.64    7227.12  107135.27
  Latency        0.96ms   257.27us     3.91ms
  Latency Distribution
     50%     0.91ms
     75%     1.20ms
     90%     1.50ms
     99%     2.06ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    102316.98    6712.42  107114.79
  Latency        0.95ms   271.90us     4.32ms
  Latency Distribution
     50%     0.88ms
     75%     1.18ms
     90%     1.47ms
     99%     2.12ms
### CBV Response Types (/cbv-response)
  Reqs/sec    109835.64    7086.57  115569.40
  Latency        0.90ms   298.41us     4.22ms
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
  Reqs/sec     15793.69    1248.44   17936.21
  Latency        6.31ms     1.62ms    20.74ms
  Latency Distribution
     50%     6.28ms
     75%     7.31ms
     90%     8.72ms
     99%    11.06ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    136438.25   12714.43  147404.85
  Latency      717.90us   344.64us     6.01ms
  Latency Distribution
     50%   665.00us
     75%     0.85ms
     90%     1.09ms
     99%     1.79ms
### File Upload (POST /upload)
  Reqs/sec    117443.52    8853.31  126350.80
  Latency      825.32us   330.74us     6.31ms
  Latency Distribution
     50%   763.00us
     75%     0.99ms
     90%     1.24ms
     99%     2.07ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    111669.97    8044.98  119310.08
  Latency        0.86ms   315.67us     6.31ms
  Latency Distribution
     50%   759.00us
     75%     1.12ms
     90%     1.36ms
     99%     2.01ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9765.13    1748.39   20931.07
  Latency       10.40ms     2.14ms    22.09ms
  Latency Distribution
     50%    10.16ms
     75%    11.42ms
     90%    13.92ms
     99%    17.29ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    149839.66   13715.92  159941.96
  Latency      653.76us   315.80us     5.63ms
  Latency Distribution
     50%   593.00us
     75%   787.00us
     90%     1.00ms
     99%     1.88ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    145307.03   11750.96  158403.84
  Latency      644.03us   307.20us     6.28ms
  Latency Distribution
     50%   584.00us
     75%   765.00us
     90%     0.93ms
     99%     1.98ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     88102.46    8825.71   92795.38
  Latency        1.12ms   458.91us     6.33ms
  Latency Distribution
     50%     1.02ms
     75%     1.35ms
     90%     1.74ms
     99%     3.19ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec    148539.59   12595.05  157242.97
  Latency      652.37us   296.17us     5.65ms
  Latency Distribution
     50%   615.00us
     75%   717.00us
     90%     0.86ms
     99%     1.94ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec    104454.70    8450.19  110318.87
  Latency        0.94ms   278.65us     4.66ms
  Latency Distribution
     50%     0.88ms
     75%     1.17ms
     90%     1.49ms
     99%     2.20ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    106644.27    7431.47  113889.72
  Latency        0.93ms   291.92us     4.83ms
  Latency Distribution
     50%     0.86ms
     75%     1.13ms
     90%     1.46ms
     99%     2.26ms

## Union Response Performance
Polymorphic feed with tagged msgspec Struct union (PostActivity | CommentActivity | LikeActivity)

### Single union item — Post branch (/feed/0)
  Reqs/sec    101826.34    8579.87  107361.02
  Latency        0.97ms   291.22us     5.38ms
  Latency Distribution
     50%     0.91ms
     75%     1.19ms
     90%     1.47ms
     99%     2.10ms

### Single union item — Comment branch (/feed/1)
  Reqs/sec     94418.41   11695.10  104051.21
  Latency        1.00ms   339.33us     4.68ms
  Latency Distribution
     50%     0.92ms
     75%     1.23ms
     90%     1.59ms
     99%     2.43ms

### Single union item — Like branch (/feed/2)
  Reqs/sec     98644.75    5819.47  103252.87
  Latency        0.99ms   339.04us     3.88ms
  Latency Distribution
     50%     0.90ms
     75%     1.21ms
     90%     1.58ms
     99%     2.55ms

### Feed of 100 mixed union items (/feed)
  Reqs/sec     71174.74    3561.41   73889.57
  Latency        1.38ms   347.64us     6.06ms
  Latency Distribution
     50%     1.42ms
     75%     1.62ms
     90%     1.86ms
     99%     2.65ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    174125.83   21925.73  187584.15
  Latency      559.80us   336.23us     5.44ms
  Latency Distribution
     50%   485.00us
     75%   653.00us
     90%     0.89ms
     99%     2.26ms

### Path Parameter - int (/items/12345)
  Reqs/sec    152630.27   12989.75  162781.90
  Latency      633.70us   326.54us     5.33ms
  Latency Distribution
     50%   551.00us
     75%   702.00us
     90%     0.98ms
     99%     2.17ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    151398.54   13095.73  166040.04
  Latency      629.79us   293.36us     5.01ms
  Latency Distribution
     50%   568.00us
     75%   755.00us
     90%     0.95ms
     99%     2.00ms

### Header Parameter (/header)
  Reqs/sec    103215.50    9427.23  109270.52
  Latency        0.95ms   316.33us     5.26ms
  Latency Distribution
     50%     0.90ms
     75%     1.19ms
     90%     1.50ms
     99%     2.13ms

### Cookie Parameter (/cookie)
  Reqs/sec    103892.66    8628.39  109662.18
  Latency        0.95ms   324.41us     5.58ms
  Latency Distribution
     50%     0.88ms
     75%     1.17ms
     90%     1.46ms
     99%     2.24ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     85518.34    6472.29   90281.15
  Latency        1.16ms   322.45us     4.74ms
  Latency Distribution
     50%     1.10ms
     75%     1.42ms
     90%     1.74ms
     99%     2.50ms
