# Django-Bolt Benchmark
Generated: Sat 23 May 2026 10:55:26 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    170948.42   20475.96  182811.97
  Latency      552.83us   263.39us     4.95ms
  Latency Distribution
     50%   452.00us
     75%   750.00us
     90%     0.93ms
     99%     1.71ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    125663.78   12402.67  139773.73
  Latency      794.83us   323.50us     7.33ms
  Latency Distribution
     50%   751.00us
     75%     0.90ms
     90%     1.17ms
     99%     1.95ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    117437.23   16982.72  134457.99
  Latency      811.28us   420.43us     6.51ms
  Latency Distribution
     50%   714.00us
     75%     0.95ms
     90%     1.23ms
     99%     2.49ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec     96098.11    6659.27  100564.48
  Latency        1.03ms   358.11us     5.32ms
  Latency Distribution
     50%     0.95ms
     75%     1.26ms
     90%     1.60ms
     99%     2.51ms
### Cookie Endpoint (/cookie)
  Reqs/sec    103941.04    9924.08  111370.79
  Latency        0.95ms   320.87us     5.40ms
  Latency Distribution
     50%     0.88ms
     75%     1.16ms
     90%     1.43ms
     99%     2.24ms
### Exception Endpoint (/exc)
  Reqs/sec    128528.52    9890.85  137419.64
  Latency      757.47us   370.64us     5.84ms
  Latency Distribution
     50%   668.00us
     75%     0.93ms
     90%     1.25ms
     99%     2.35ms
### HTML Response (/html)
  Reqs/sec    165656.54   11583.25  172399.81
  Latency      583.14us   294.05us     5.59ms
  Latency Distribution
     50%   512.00us
     75%   690.00us
     90%     0.95ms
     99%     2.03ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     37706.22    5723.84   41374.92
  Latency        2.64ms     1.14ms    15.14ms
  Latency Distribution
     50%     2.39ms
     75%     3.26ms
     90%     4.27ms
     99%     7.11ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     75875.60    5185.31   78910.52
  Latency        1.30ms   381.44us     5.27ms
  Latency Distribution
     50%     1.29ms
     75%     1.60ms
     90%     1.93ms
     99%     2.67ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     16510.77    1307.73   17552.79
  Latency        6.01ms     1.65ms    14.64ms
  Latency Distribution
     50%     5.97ms
     75%     7.30ms
     90%     8.54ms
     99%    10.59ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14487.41     888.90   16991.48
  Latency        6.89ms     1.40ms    14.18ms
  Latency Distribution
     50%     6.78ms
     75%     7.96ms
     90%     9.09ms
     99%    11.20ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     82739.14    5770.26   87606.60
  Latency        1.19ms   360.25us     8.57ms
  Latency Distribution
     50%     1.12ms
     75%     1.47ms
     90%     1.84ms
     99%     2.66ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    154842.81   15345.24  164301.95
  Latency      628.09us   224.94us     5.56ms
  Latency Distribution
     50%   610.00us
     75%   727.00us
     90%   843.00us
     99%     1.77ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    150469.37   14299.48  158880.11
  Latency      649.77us   322.98us     5.28ms
  Latency Distribution
     50%   612.00us
     75%   717.00us
     90%     0.85ms
     99%     1.98ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     13944.09    1211.01   15541.62
  Latency        7.14ms     2.04ms    20.52ms
  Latency Distribution
     50%     7.07ms
     75%     8.57ms
     90%    11.00ms
     99%    12.68ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     10421.91     991.58   12195.47
  Latency        9.56ms     3.84ms    33.45ms
  Latency Distribution
     50%     8.89ms
     75%    11.92ms
     90%    15.10ms
     99%    22.17ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     16901.58    1209.13   18998.22
  Latency        5.90ms     1.82ms    19.65ms
  Latency Distribution
     50%     5.43ms
     75%     7.30ms
     90%     8.92ms
     99%    11.51ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     11732.31     888.97   13518.45
  Latency        8.48ms     4.38ms    29.19ms
  Latency Distribution
     50%     7.47ms
     75%    11.80ms
     90%    15.51ms
     99%    21.33ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    102020.36    8969.53  107710.01
  Latency        0.96ms   323.88us     4.65ms
  Latency Distribution
     50%     0.89ms
     75%     1.19ms
     90%     1.52ms
     99%     2.29ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     84668.68   10588.13   94486.92
  Latency        1.16ms   635.53us    11.24ms
  Latency Distribution
     50%     1.00ms
     75%     1.42ms
     90%     1.91ms
     99%     3.60ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     64199.32    3715.75   67551.46
  Latency        1.54ms   421.47us     4.74ms
  Latency Distribution
     50%     1.48ms
     75%     1.87ms
     90%     2.37ms
     99%     3.16ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     90027.02    7542.84   95627.59
  Latency        1.09ms   422.23us     6.32ms
  Latency Distribution
     50%     1.01ms
     75%     1.41ms
     90%     1.80ms
     99%     2.76ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     91550.88    8433.76  101552.90
  Latency        1.08ms   368.74us     4.43ms
  Latency Distribution
     50%     1.00ms
     75%     1.34ms
     90%     1.73ms
     99%     2.64ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     91675.96   12131.61  101054.91
  Latency        1.02ms   317.87us     4.65ms
  Latency Distribution
     50%     0.94ms
     75%     1.27ms
     90%     1.64ms
     99%     2.35ms
### CBV Response Types (/cbv-response)
  Reqs/sec    104799.35    7982.07  111735.55
  Latency        0.94ms   343.56us     6.13ms
  Latency Distribution
     50%     0.85ms
     75%     1.16ms
     90%     1.49ms
     99%     2.26ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     15391.88    1290.30   16217.46
  Latency        6.46ms     1.85ms    16.53ms
  Latency Distribution
     50%     6.36ms
     75%     7.97ms
     90%     9.42ms
     99%    11.74ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    127907.80   12001.35  142108.88
  Latency      758.64us   331.17us     5.05ms
  Latency Distribution
     50%   683.00us
     75%     0.94ms
     90%     1.24ms
     99%     2.06ms
### File Upload (POST /upload)
  Reqs/sec    115391.05    8004.80  122927.54
  Latency        0.85ms   343.86us     5.42ms
  Latency Distribution
     50%   797.00us
     75%     1.01ms
     90%     1.24ms
     99%     2.38ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    114080.63   11427.08  122153.23
  Latency        0.87ms   321.76us     6.22ms
  Latency Distribution
     50%   813.00us
     75%     0.99ms
     90%     1.27ms
     99%     1.96ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      8489.35    1042.35   10970.14
  Latency       11.76ms     2.60ms    32.89ms
  Latency Distribution
     50%    11.32ms
     75%    13.16ms
     90%    15.25ms
     99%    20.66ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    148267.19   14592.14  160462.08
  Latency      661.73us   333.53us     5.78ms
  Latency Distribution
     50%   605.00us
     75%   786.00us
     90%     0.97ms
     99%     1.97ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     89771.17    6587.28   95082.46
  Latency        1.10ms   436.14us     6.17ms
  Latency Distribution
     50%     1.01ms
     75%     1.37ms
     90%     1.75ms
     99%     3.22ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     81270.80    7134.97   89545.43
  Latency        1.20ms   481.80us     6.90ms
  Latency Distribution
     50%     1.10ms
     75%     1.45ms
     90%     1.84ms
     99%     3.20ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     89985.55    5264.61   95937.26
  Latency        1.10ms   394.58us     6.38ms
  Latency Distribution
     50%     0.99ms
     75%     1.35ms
     90%     1.78ms
     99%     2.75ms

## Multi-Response Performance

### Multi-response tuple return (/bench/multi/tuple)
  Reqs/sec     96759.37    8243.79  108440.63
  Latency        1.04ms   357.82us     4.97ms
  Latency Distribution
     50%     0.96ms
     75%     1.29ms
     90%     1.67ms
     99%     2.62ms

### Multi-response bare dict (/bench/multi/dict)
  Reqs/sec    102949.07    9740.63  112937.34
  Latency        0.96ms   310.76us     5.25ms
  Latency Distribution
     50%     0.89ms
     75%     1.16ms
     90%     1.45ms
     99%     2.27ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    131919.45   60658.79  173298.54
  Latency      603.84us   382.88us     5.63ms
  Latency Distribution
     50%   529.00us
     75%   713.00us
     90%     0.99ms
     99%     2.12ms

### Path Parameter - int (/items/12345)
  Reqs/sec    145157.59   14749.31  161406.08
  Latency      685.73us   370.36us     4.54ms
  Latency Distribution
     50%   586.00us
     75%   785.00us
     90%     1.12ms
     99%     2.62ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    111284.97   21110.87  129508.02
  Latency      827.31us   428.19us     5.41ms
  Latency Distribution
     50%   724.00us
     75%     1.04ms
     90%     1.47ms
     99%     2.86ms

### Header Parameter (/header)
  Reqs/sec     95175.97    9327.41  109292.25
  Latency        1.06ms   401.59us     5.73ms
  Latency Distribution
     50%     0.97ms
     75%     1.30ms
     90%     1.69ms
     99%     2.71ms

### Cookie Parameter (/cookie)
  Reqs/sec    100619.72    8150.15  105339.83
  Latency        0.98ms   349.03us     4.85ms
  Latency Distribution
     50%     0.90ms
     75%     1.22ms
     90%     1.57ms
     99%     2.33ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     83439.01    8880.19   98358.19
  Latency        1.20ms   370.98us     6.00ms
  Latency Distribution
     50%     1.13ms
     75%     1.49ms
     90%     1.87ms
     99%     2.62ms
