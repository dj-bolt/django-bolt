# Django-Bolt Benchmark
Generated: Mon Feb 23 02:00:55 AM PKT 2026
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    112144.98    9016.62  118508.56
  Latency        0.87ms   282.75us     3.87ms
  Latency Distribution
     50%   794.00us
     75%     1.07ms
     90%     1.41ms
     99%     2.17ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec     88249.48    5826.86   92170.00
  Latency        1.12ms   326.74us     4.93ms
  Latency Distribution
     50%     1.04ms
     75%     1.35ms
     90%     1.65ms
     99%     2.53ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec     88098.32    6828.16   94019.25
  Latency        1.11ms   280.76us     6.31ms
  Latency Distribution
     50%     1.06ms
     75%     1.35ms
     90%     1.68ms
     99%     2.24ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    103888.31    6100.95  108030.68
  Latency        0.94ms   259.26us     3.79ms
  Latency Distribution
     50%     0.90ms
     75%     1.20ms
     90%     1.50ms
     99%     2.06ms
### Cookie Endpoint (/cookie)
  Reqs/sec    102333.68    7074.41  107900.39
  Latency        0.95ms   357.56us     4.23ms
  Latency Distribution
     50%   849.00us
     75%     1.20ms
     90%     1.59ms
     99%     2.60ms
### Exception Endpoint (/exc)
  Reqs/sec     99351.60    7737.20  105091.94
  Latency        0.99ms   307.81us     5.11ms
  Latency Distribution
     50%     0.92ms
     75%     1.20ms
     90%     1.49ms
     99%     2.21ms
### HTML Response (/html)
  Reqs/sec    108980.77    8112.88  113751.43
  Latency        0.90ms   295.05us     5.05ms
  Latency Distribution
     50%   826.00us
     75%     1.09ms
     90%     1.41ms
     99%     2.15ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     33708.25    8212.14   40687.24
  Latency        2.97ms     1.74ms    35.44ms
  Latency Distribution
     50%     2.60ms
     75%     3.54ms
     90%     4.62ms
     99%    11.57ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     77972.59    5613.64   82458.56
  Latency        1.27ms   358.18us     4.37ms
  Latency Distribution
     50%     1.19ms
     75%     1.52ms
     90%     1.93ms
     99%     2.82ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     17511.75    1393.41   18532.28
  Latency        5.68ms     1.69ms    14.46ms
  Latency Distribution
     50%     5.50ms
     75%     6.62ms
     90%     8.44ms
     99%    11.54ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15569.85    1139.58   20589.06
  Latency        6.43ms     1.53ms    13.34ms
  Latency Distribution
     50%     6.22ms
     75%     7.75ms
     90%     8.98ms
     99%    11.04ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     86842.98    5541.44   91211.72
  Latency        1.13ms   343.04us     5.13ms
  Latency Distribution
     50%     1.08ms
     75%     1.38ms
     90%     1.73ms
     99%     2.64ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    101488.73    7315.57  106699.49
  Latency        0.97ms   331.17us     4.94ms
  Latency Distribution
     50%     0.89ms
     75%     1.22ms
     90%     1.57ms
     99%     2.27ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     97680.36    5658.54  101759.07
  Latency        1.01ms   339.40us     4.64ms
  Latency Distribution
     50%     0.93ms
     75%     1.24ms
     90%     1.58ms
     99%     2.56ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     13844.76     902.58   14958.46
  Latency        7.19ms     1.32ms    16.56ms
  Latency Distribution
     50%     7.52ms
     75%     8.49ms
     90%     9.21ms
     99%    10.60ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec      9927.08    1003.47   11851.32
  Latency       10.04ms     4.07ms    31.86ms
  Latency Distribution
     50%     9.19ms
     75%    12.53ms
     90%    16.26ms
     99%    23.40ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     16092.62     992.43   20252.63
  Latency        6.22ms     1.19ms    12.94ms
  Latency Distribution
     50%     6.01ms
     75%     7.08ms
     90%     8.29ms
     99%    10.01ms
### Users Mini10 (Sync) (/users/sync-mini10)
 0 / 10000 [-----------------------------------------------------------]   0.00% 2338 / 10000 [===========>------------------------------------]  23.38% 11659/s 4731 / 10000 [======================>-------------------------]  47.31% 11807/s 7150 / 10000 [==================================>-------------]  71.50% 11900/s 9558 / 10000 [=============================================>--]  95.58% 11932/s 10000 / 10000 [================================================] 100.00% 9979/s 10000 / 10000 [=============================================] 100.00% 9978/s 1s
  Reqs/sec     11999.84     760.05   13376.31
  Latency        8.31ms     3.42ms    26.52ms
  Latency Distribution
     50%     7.67ms
     75%    10.45ms
     90%    13.55ms
     99%    19.22ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    111187.37    8949.06  116534.28
  Latency        0.89ms   281.05us     4.47ms
  Latency Distribution
     50%   829.00us
     75%     1.08ms
     90%     1.38ms
     99%     2.04ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    103094.50    8118.37  111297.82
  Latency        0.95ms   352.02us     5.21ms
  Latency Distribution
     50%     0.86ms
     75%     1.18ms
     90%     1.55ms
     99%     2.54ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     67766.91    3556.12   70493.33
  Latency        1.46ms   411.55us     4.93ms
  Latency Distribution
     50%     1.38ms
     75%     1.78ms
     90%     2.22ms
     99%     3.27ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     99936.84    6812.70  105483.58
  Latency        0.98ms   301.54us     4.35ms
  Latency Distribution
     50%     0.92ms
     75%     1.23ms
     90%     1.56ms
     99%     2.30ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     97727.48    6764.45  102495.83
  Latency        1.01ms   317.60us     5.64ms
  Latency Distribution
     50%     0.94ms
     75%     1.25ms
     90%     1.55ms
     99%     2.19ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    101990.10    6505.92  105951.50
  Latency        0.97ms   287.52us     5.25ms
  Latency Distribution
     50%     0.91ms
     75%     1.20ms
     90%     1.47ms
     99%     2.13ms
### CBV Response Types (/cbv-response)
  Reqs/sec    103946.47    8908.98  113131.96
  Latency        0.95ms   312.26us     3.88ms
  Latency Distribution
     50%     0.87ms
     75%     1.17ms
     90%     1.51ms
     99%     2.39ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16408.28     969.02   17167.62
  Latency        6.04ms     2.36ms    16.42ms
  Latency Distribution
     50%     5.59ms
     75%     7.41ms
     90%    10.40ms
     99%    12.75ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    102223.18   13418.61  125534.94
  Latency        1.00ms   338.85us     4.68ms
  Latency Distribution
     50%     0.93ms
     75%     1.22ms
     90%     1.55ms
     99%     2.59ms
### File Upload (POST /upload)
  Reqs/sec     86449.07    5633.40   91822.98
  Latency        1.14ms   404.09us     4.51ms
  Latency Distribution
     50%     1.07ms
     75%     1.45ms
     90%     1.82ms
     99%     2.94ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec     86267.78    6182.67   89913.28
  Latency        1.14ms   345.40us     5.86ms
  Latency Distribution
     50%     1.08ms
     75%     1.46ms
     90%     1.81ms
     99%     2.62ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9419.75    1206.39   14049.87
  Latency       10.65ms     2.71ms    24.79ms
  Latency Distribution
     50%    10.56ms
     75%    12.70ms
     90%    14.61ms
     99%    18.99ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    101814.14    7504.35  106688.64
  Latency        0.96ms   305.52us     4.58ms
  Latency Distribution
     50%     0.90ms
     75%     1.19ms
     90%     1.52ms
     99%     2.31ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     99361.07    8861.16  111524.01
  Latency        1.01ms   320.18us     5.25ms
  Latency Distribution
     50%     0.94ms
     75%     1.25ms
     90%     1.58ms
     99%     2.34ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     88437.93    6063.33   92122.94
  Latency        1.12ms   339.24us     4.56ms
  Latency Distribution
     50%     1.04ms
     75%     1.36ms
     90%     1.69ms
     99%     2.69ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     98190.38    6155.70  104172.17
  Latency        1.01ms   327.68us     4.77ms
  Latency Distribution
     50%     0.93ms
     75%     1.22ms
     90%     1.57ms
     99%     2.32ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    114397.34    8405.63  120515.69
  Latency        0.86ms   284.00us     5.32ms
  Latency Distribution
     50%   808.00us
     75%     1.04ms
     90%     1.28ms
     99%     1.92ms

### Path Parameter - int (/items/12345)
  Reqs/sec    104300.94    7606.98  108638.54
  Latency        0.95ms   312.79us     4.31ms
  Latency Distribution
     50%     0.88ms
     75%     1.18ms
     90%     1.53ms
     99%     2.23ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    103967.61    8228.78  109940.76
  Latency        0.95ms   313.03us     4.02ms
  Latency Distribution
     50%     0.88ms
     75%     1.17ms
     90%     1.50ms
     99%     2.31ms

### Header Parameter (/header)
  Reqs/sec    104274.85    7533.35  110399.29
  Latency        0.94ms   292.18us     4.97ms
  Latency Distribution
     50%     0.86ms
     75%     1.16ms
     90%     1.49ms
     99%     2.29ms

### Cookie Parameter (/cookie)
  Reqs/sec    105404.51    6950.82  110734.61
  Latency        0.93ms   261.61us     4.31ms
  Latency Distribution
     50%     0.88ms
     75%     1.15ms
     90%     1.43ms
     99%     2.05ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     86947.48    5347.70   90566.23
  Latency        1.13ms   335.71us     6.07ms
  Latency Distribution
     50%     1.07ms
     75%     1.35ms
     90%     1.67ms
     99%     2.39ms
