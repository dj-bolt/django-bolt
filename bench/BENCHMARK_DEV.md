# Django-Bolt Benchmark
Generated: Sun 01 Mar 2026 05:44:34 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    109910.41   11927.52  121207.91
  Latency        0.91ms   363.44us     5.04ms
  Latency Distribution
     50%   818.00us
     75%     1.08ms
     90%     1.42ms
     99%     2.56ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec     86846.44    6567.15   93145.99
  Latency        1.14ms   409.24us     5.69ms
  Latency Distribution
     50%     1.05ms
     75%     1.38ms
     90%     1.77ms
     99%     2.95ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec     86429.04    9365.67   93337.09
  Latency        1.13ms   410.58us     5.05ms
  Latency Distribution
     50%     1.04ms
     75%     1.37ms
     90%     1.78ms
     99%     3.13ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    102449.35    4876.07  107085.46
  Latency        0.96ms   351.61us     4.75ms
  Latency Distribution
     50%     0.87ms
     75%     1.17ms
     90%     1.57ms
     99%     2.62ms
### Cookie Endpoint (/cookie)
  Reqs/sec     91469.59   10803.53  100540.59
  Latency        1.04ms   421.14us     5.00ms
  Latency Distribution
     50%     0.94ms
     75%     1.28ms
     90%     1.65ms
     99%     3.16ms
### Exception Endpoint (/exc)
  Reqs/sec    100002.32    6091.77  106244.78
  Latency        0.98ms   354.27us     4.96ms
  Latency Distribution
     50%     0.89ms
     75%     1.22ms
     90%     1.62ms
     99%     2.53ms
### HTML Response (/html)
  Reqs/sec    105155.80   10290.33  118473.37
  Latency        0.93ms   386.91us     5.79ms
  Latency Distribution
     50%     0.86ms
     75%     1.12ms
     90%     1.43ms
     99%     2.91ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     25615.12   11103.38   43188.11
  Latency        4.01ms     3.86ms    64.42ms
  Latency Distribution
     50%     3.04ms
     75%     4.47ms
     90%     6.58ms
     99%    23.54ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     65675.56   12288.17   78620.03
  Latency        1.49ms   658.28us     7.17ms
  Latency Distribution
     50%     1.34ms
     75%     1.77ms
     90%     2.35ms
     99%     4.69ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     16507.27    1642.13   19610.34
  Latency        6.05ms     1.89ms    15.23ms
  Latency Distribution
     50%     5.94ms
     75%     7.42ms
     90%     8.81ms
     99%    12.62ms
### Get User via Dependency (/auth/me-dependency)
 0 / 10000 [---------------------------------------------------------------------------------------------------------------------------------]   0.00% 2975 / 10000 [===================================>----------------------------------------------------------------------------------]  29.75% 14821/s 6075 / 10000 [=======================================================================>----------------------------------------------]  60.75% 15152/s 9142 / 10000 [===========================================================================================================>----------]  91.42% 15152/s 10000 / 10000 [=====================================================================================================================] 100.00% 12427/s 10000 / 10000 [==================================================================================================================] 100.00% 12424/s 0s
  Reqs/sec     15257.11    1478.68   18687.13
  Latency        6.53ms     2.05ms    19.19ms
  Latency Distribution
     50%     6.41ms
     75%     7.96ms
     90%     9.55ms
     99%    12.57ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     81470.33    5945.93   89110.46
  Latency        1.22ms   412.08us     4.87ms
  Latency Distribution
     50%     1.13ms
     75%     1.54ms
     90%     1.99ms
     99%     3.02ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec     96519.83    9380.52  109143.41
  Latency        1.04ms   401.95us     4.49ms
  Latency Distribution
     50%     0.93ms
     75%     1.26ms
     90%     1.69ms
     99%     2.81ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     89605.76   10817.93   99140.52
  Latency        1.09ms   438.31us     5.67ms
  Latency Distribution
     50%     0.97ms
     75%     1.32ms
     90%     1.73ms
     99%     3.14ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     13948.35    1103.41   15572.93
  Latency        7.11ms     2.63ms    19.82ms
  Latency Distribution
     50%     6.65ms
     75%     8.79ms
     90%    11.29ms
     99%    15.29ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     11197.49    1368.76   14134.75
  Latency        8.87ms     4.38ms    33.12ms
  Latency Distribution
     50%     7.98ms
     75%    11.70ms
     90%    15.62ms
     99%    22.52ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     15741.50    1953.78   17998.72
  Latency        6.32ms     2.19ms    23.92ms
  Latency Distribution
     50%     5.88ms
     75%     7.63ms
     90%     9.67ms
     99%    13.76ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     13082.00    1565.06   16077.71
  Latency        7.61ms     3.10ms    28.83ms
  Latency Distribution
     50%     6.78ms
     75%     9.24ms
     90%    12.02ms
     99%    18.67ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    103248.39    9042.98  109765.71
  Latency        0.95ms   418.89us     5.07ms
  Latency Distribution
     50%   826.00us
     75%     1.16ms
     90%     1.55ms
     99%     3.04ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     99241.13    7663.13  108474.01
  Latency        0.99ms   365.16us     4.40ms
  Latency Distribution
     50%     0.89ms
     75%     1.20ms
     90%     1.59ms
     99%     2.74ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     54127.31   10752.36   71808.71
  Latency        1.88ms     1.00ms    15.72ms
  Latency Distribution
     50%     1.64ms
     75%     2.18ms
     90%     3.02ms
     99%     5.79ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     84628.48    5558.43   89737.26
  Latency        1.16ms   448.94us     7.25ms
  Latency Distribution
     50%     1.09ms
     75%     1.48ms
     90%     1.88ms
     99%     3.15ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     84888.92    7869.82   94016.05
  Latency        1.15ms   463.06us     4.90ms
  Latency Distribution
     50%     1.05ms
     75%     1.43ms
     90%     1.86ms
     99%     3.42ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     87245.58   10997.31   94055.15
  Latency        1.13ms   533.09us     9.17ms
  Latency Distribution
     50%     1.01ms
     75%     1.38ms
     90%     1.83ms
     99%     3.30ms
### CBV Response Types (/cbv-response)
  Reqs/sec     93438.63   10076.22  103706.21
  Latency        1.05ms   448.00us     4.94ms
  Latency Distribution
     50%     0.93ms
     75%     1.34ms
     90%     1.79ms
     99%     2.98ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16816.27    2444.64   26156.32
  Latency        6.02ms     1.99ms    16.95ms
  Latency Distribution
     50%     5.71ms
     75%     7.39ms
     90%     9.19ms
     99%    12.41ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec     96397.94    5672.08  101325.99
  Latency        1.03ms   411.21us     5.79ms
  Latency Distribution
     50%     0.94ms
     75%     1.27ms
     90%     1.63ms
     99%     3.00ms
### File Upload (POST /upload)
  Reqs/sec     81819.46    7806.18   92449.71
  Latency        1.20ms   430.83us     4.81ms
  Latency Distribution
     50%     1.09ms
     75%     1.49ms
     90%     1.93ms
     99%     3.19ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec     75746.29   13876.52   90808.27
  Latency        1.27ms   477.65us     5.64ms
  Latency Distribution
     50%     1.17ms
     75%     1.61ms
     90%     2.09ms
     99%     3.32ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9513.36    1137.63   11609.18
  Latency       10.50ms     2.83ms    28.87ms
  Latency Distribution
     50%    10.55ms
     75%    12.46ms
     90%    14.16ms
     99%    18.96ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec     98415.41   10065.15  108671.14
  Latency        1.02ms   426.42us     5.67ms
  Latency Distribution
     50%     0.92ms
     75%     1.25ms
     90%     1.61ms
     99%     3.17ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     89645.10    6989.06  101079.96
  Latency        1.09ms   540.54us     6.95ms
  Latency Distribution
     50%     0.93ms
     75%     1.32ms
     90%     1.85ms
     99%     3.62ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     80577.42    8238.47   94721.13
  Latency        1.22ms   506.47us     5.98ms
  Latency Distribution
     50%     1.09ms
     75%     1.48ms
     90%     2.04ms
     99%     3.62ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     86023.50    8186.61   95090.10
  Latency        1.13ms   435.97us     6.88ms
  Latency Distribution
     50%     1.04ms
     75%     1.39ms
     90%     1.82ms
     99%     3.15ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    100018.38    4908.58  105921.42
  Latency        0.98ms   451.70us     5.23ms
  Latency Distribution
     50%     0.88ms
     75%     1.20ms
     90%     1.56ms
     99%     3.53ms

### Path Parameter - int (/items/12345)
  Reqs/sec     92503.17   13682.83  112043.99
  Latency        1.02ms   396.25us     4.73ms
  Latency Distribution
     50%     0.93ms
     75%     1.24ms
     90%     1.61ms
     99%     2.95ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec     95445.30    8051.36  107118.90
  Latency        1.00ms   420.16us     4.82ms
  Latency Distribution
     50%     0.91ms
     75%     1.22ms
     90%     1.60ms
     99%     3.13ms

### Header Parameter (/header)
  Reqs/sec     89525.67   19673.00  101970.21
  Latency        1.01ms   384.44us     4.55ms
  Latency Distribution
     50%     0.93ms
     75%     1.25ms
     90%     1.63ms
     99%     2.81ms

### Cookie Parameter (/cookie)
  Reqs/sec     75179.15   20509.38  102969.85
  Latency        1.32ms   751.19us    12.36ms
  Latency Distribution
     50%     1.12ms
     75%     1.61ms
     90%     2.31ms
     99%     4.30ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     77799.06    8422.62   87311.11
  Latency        1.27ms   505.23us     6.24ms
  Latency Distribution
     50%     1.14ms
     75%     1.58ms
     90%     2.04ms
     99%     3.54ms
