# Django-Bolt Benchmark
Generated: Sun 01 Mar 2026 06:43:55 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    111331.74    6419.31  117075.67
  Latency        0.89ms   308.34us     4.04ms
  Latency Distribution
     50%   818.00us
     75%     1.09ms
     90%     1.42ms
     99%     2.33ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec     89403.84    8219.46   96849.94
  Latency        1.10ms   405.14us     6.08ms
  Latency Distribution
     50%     1.01ms
     75%     1.32ms
     90%     1.70ms
     99%     2.60ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec     88306.82    7257.54   96609.79
  Latency        1.12ms   382.71us     6.20ms
  Latency Distribution
     50%     1.03ms
     75%     1.36ms
     90%     1.70ms
     99%     2.69ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    104177.41    7300.07  110401.43
  Latency        0.94ms   288.71us     4.17ms
  Latency Distribution
     50%     0.87ms
     75%     1.18ms
     90%     1.49ms
     99%     2.23ms
### Cookie Endpoint (/cookie)
  Reqs/sec    105808.37    6169.72  112040.71
  Latency        0.93ms   308.78us     5.62ms
  Latency Distribution
     50%     0.87ms
     75%     1.14ms
     90%     1.43ms
     99%     2.18ms
### Exception Endpoint (/exc)
  Reqs/sec    100834.78    7046.78  107470.73
  Latency        0.98ms   274.59us     4.33ms
  Latency Distribution
     50%     0.91ms
     75%     1.18ms
     90%     1.47ms
     99%     2.17ms
### HTML Response (/html)
  Reqs/sec    109120.65   10197.58  120344.47
  Latency        0.89ms   302.10us     5.00ms
  Latency Distribution
     50%   827.00us
     75%     1.09ms
     90%     1.39ms
     99%     2.04ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     32664.81    7736.44   38188.76
  Latency        3.06ms     1.85ms    28.73ms
  Latency Distribution
     50%     2.62ms
     75%     3.49ms
     90%     4.84ms
     99%    10.94ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     77263.66    5133.83   81490.13
  Latency        1.27ms   335.84us     5.59ms
  Latency Distribution
     50%     1.20ms
     75%     1.52ms
     90%     1.90ms
     99%     2.69ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     17001.88    1526.04   18528.49
  Latency        5.83ms     1.80ms    16.69ms
  Latency Distribution
     50%     5.48ms
     75%     6.90ms
     90%     8.82ms
     99%    11.68ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15730.42    1023.08   17137.04
  Latency        6.32ms     1.80ms    15.60ms
  Latency Distribution
     50%     6.04ms
     75%     7.53ms
     90%     9.12ms
     99%    12.12ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     83142.47    6466.11   89430.33
  Latency        1.17ms   360.04us     6.45ms
  Latency Distribution
     50%     1.10ms
     75%     1.46ms
     90%     1.83ms
     99%     2.60ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec     94027.74    8665.20  104683.02
  Latency        1.02ms   374.21us     4.93ms
  Latency Distribution
     50%     0.93ms
     75%     1.26ms
     90%     1.65ms
     99%     2.68ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     75903.54    9292.41   87523.13
  Latency        1.31ms   500.81us     6.43ms
  Latency Distribution
     50%     1.21ms
     75%     1.62ms
     90%     2.12ms
     99%     3.56ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14527.55    1281.37   17177.22
  Latency        6.87ms     1.85ms    19.15ms
  Latency Distribution
     50%     6.67ms
     75%     8.10ms
     90%     9.68ms
     99%    12.86ms
### Users Full10 (Sync) (/users/sync-full10)
 0 / 10000 [--------------------------------------------------------------------------------------------------------------------------------------------------------------]   0.00% 2414 / 10000 [===================================>---------------------------------------------------------------------------------------------------------------]  24.14% 12025/s 4730 / 10000 [=====================================================================>-----------------------------------------------------------------------------]  47.30% 11787/s 6999 / 10000 [======================================================================================================>--------------------------------------------]  69.99% 11632/s 9173 / 10000 [======================================================================================================================================>------------]  91.73% 11430/s 10000 / 10000 [===================================================================================================================================================] 100.00% 9966/s 10000 / 10000 [================================================================================================================================================] 100.00% 9965/s 1s
  Reqs/sec     11443.81    1422.55   14231.52
  Latency        8.72ms     3.33ms    28.08ms
  Latency Distribution
     50%     8.17ms
     75%    10.79ms
     90%    13.60ms
     99%    19.71ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     16321.91    1948.31   18633.39
  Latency        5.99ms     2.02ms    16.47ms
  Latency Distribution
     50%     5.76ms
     75%     7.58ms
     90%     9.15ms
     99%    12.21ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12879.87    1337.08   16991.71
  Latency        7.78ms     3.72ms    31.25ms
  Latency Distribution
     50%     6.81ms
     75%     9.60ms
     90%    13.17ms
     99%    20.97ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    105116.29    8405.65  113100.92
  Latency        0.93ms   342.01us     4.95ms
  Latency Distribution
     50%     0.87ms
     75%     1.14ms
     90%     1.41ms
     99%     2.47ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     94017.56   10938.02  105340.72
  Latency        1.00ms   361.83us     4.92ms
  Latency Distribution
     50%     0.93ms
     75%     1.28ms
     90%     1.64ms
     99%     2.49ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     64712.06    4988.14   71756.19
  Latency        1.50ms   518.28us     5.68ms
  Latency Distribution
     50%     1.35ms
     75%     1.81ms
     90%     2.33ms
     99%     3.99ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     91160.46    6653.27   97810.41
  Latency        1.08ms   395.69us     4.90ms
  Latency Distribution
     50%     1.00ms
     75%     1.34ms
     90%     1.74ms
     99%     2.83ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     89317.12    6498.93   95605.27
  Latency        1.10ms   373.30us     5.68ms
  Latency Distribution
     50%     1.04ms
     75%     1.36ms
     90%     1.68ms
     99%     2.68ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     92703.31    8681.23  101091.88
  Latency        1.04ms   385.20us     6.33ms
  Latency Distribution
     50%     0.96ms
     75%     1.31ms
     90%     1.65ms
     99%     2.63ms
### CBV Response Types (/cbv-response)
  Reqs/sec     97458.42    6839.79  103585.33
  Latency        1.01ms   359.86us     5.22ms
  Latency Distribution
     50%     0.93ms
     75%     1.26ms
     90%     1.64ms
     99%     2.45ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
 0 / 10000 [--------------------------------------------------------------------------------------------------------------------------------------------------------------]   0.00% 3255 / 10000 [===============================================>---------------------------------------------------------------------------------------------------]  32.55% 16118/s 6675 / 10000 [==================================================================================================>------------------------------------------------]  66.75% 16598/s 10000 / 10000 [==================================================================================================================================================] 100.00% 16581/s 10000 / 10000 [===============================================================================================================================================] 100.00% 16577/s 0s
  Reqs/sec     16814.80    1514.06   18768.19
  Latency        5.92ms     1.83ms    16.53ms
  Latency Distribution
     50%     5.64ms
     75%     7.14ms
     90%     8.70ms
     99%    11.94ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec     94427.03    7524.89  101275.58
  Latency        1.05ms   333.93us     4.45ms
  Latency Distribution
     50%     0.98ms
     75%     1.29ms
     90%     1.62ms
     99%     2.55ms
### File Upload (POST /upload)
  Reqs/sec     82418.87    7360.34   92130.49
  Latency        1.19ms   452.70us     5.43ms
  Latency Distribution
     50%     1.08ms
     75%     1.48ms
     90%     1.98ms
     99%     3.17ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec     83988.37    7266.69   91070.81
  Latency        1.16ms   391.43us     4.89ms
  Latency Distribution
     50%     1.08ms
     75%     1.44ms
     90%     1.80ms
     99%     2.81ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9995.90     977.89   12434.54
  Latency        9.98ms     2.11ms    23.96ms
  Latency Distribution
     50%     9.87ms
     75%    11.48ms
     90%    12.97ms
     99%    16.25ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec     98833.38   10412.53  106532.83
  Latency        1.00ms   435.34us     5.26ms
  Latency Distribution
     50%     0.89ms
     75%     1.21ms
     90%     1.56ms
     99%     3.56ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec    106719.81   24887.80  155512.85
  Latency        1.00ms   312.36us     5.69ms
  Latency Distribution
     50%     0.93ms
     75%     1.22ms
     90%     1.51ms
     99%     2.29ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     86722.58    9947.28   95583.77
  Latency        1.14ms   461.16us     6.65ms
  Latency Distribution
     50%     1.02ms
     75%     1.39ms
     90%     1.85ms
     99%     3.28ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     93995.62    5791.45   99184.13
  Latency        1.04ms   381.75us     4.87ms
  Latency Distribution
     50%     0.93ms
     75%     1.31ms
     90%     1.75ms
     99%     2.69ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    108480.71    8911.63  116559.01
  Latency        0.90ms   378.01us     5.69ms
  Latency Distribution
     50%   807.00us
     75%     1.09ms
     90%     1.41ms
     99%     2.72ms

### Path Parameter - int (/items/12345)
  Reqs/sec    101130.23    7874.11  109801.80
  Latency        0.97ms   289.60us     3.83ms
  Latency Distribution
     50%     0.91ms
     75%     1.19ms
     90%     1.49ms
     99%     2.32ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec     94550.80    8145.33  108229.36
  Latency        1.03ms   405.90us     5.32ms
  Latency Distribution
     50%     0.93ms
     75%     1.27ms
     90%     1.66ms
     99%     3.00ms

### Header Parameter (/header)
  Reqs/sec     96016.56   19558.29  110898.85
  Latency        1.01ms   734.35us    11.72ms
  Latency Distribution
     50%     0.89ms
     75%     1.24ms
     90%     1.56ms
     99%     2.84ms

### Cookie Parameter (/cookie)
  Reqs/sec     92737.71    3049.42   95870.76
  Latency        1.06ms   334.91us     4.88ms
  Latency Distribution
     50%     0.97ms
     75%     1.33ms
     90%     1.64ms
     99%     2.42ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     79946.15    8920.16   89495.19
  Latency        1.20ms   423.57us     4.97ms
  Latency Distribution
     50%     1.10ms
     75%     1.48ms
     90%     1.93ms
     99%     3.06ms
