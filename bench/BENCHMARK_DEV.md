# Django-Bolt Benchmark
Generated: Tue 24 Feb 2026 10:49:46 PM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    113050.77    8650.61  118187.88
  Latency        0.87ms   321.10us     4.92ms
  Latency Distribution
     50%   808.00us
     75%     1.05ms
     90%     1.30ms
     99%     2.15ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec     88930.48    5887.03   92438.51
  Latency        1.11ms   309.80us     4.90ms
  Latency Distribution
     50%     1.04ms
     75%     1.35ms
     90%     1.64ms
     99%     2.39ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec     88016.56    6299.59   91184.69
  Latency        1.12ms   349.95us     5.37ms
  Latency Distribution
     50%     1.04ms
     75%     1.37ms
     90%     1.70ms
     99%     2.43ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    105627.70    7197.47  109689.64
  Latency        0.93ms   245.88us     4.25ms
  Latency Distribution
     50%     0.88ms
     75%     1.14ms
     90%     1.40ms
     99%     1.95ms
### Cookie Endpoint (/cookie)
  Reqs/sec    104482.38    8348.59  111837.25
  Latency        0.94ms   318.39us     6.22ms
  Latency Distribution
     50%     0.88ms
     75%     1.16ms
     90%     1.46ms
     99%     2.18ms
### Exception Endpoint (/exc)
  Reqs/sec     95422.24    7756.80  101483.22
  Latency        1.01ms   309.47us     4.39ms
  Latency Distribution
     50%     0.96ms
     75%     1.28ms
     90%     1.62ms
     99%     2.33ms
### HTML Response (/html)
  Reqs/sec    102322.53   25201.06  116445.57
  Latency        0.94ms   637.47us    12.64ms
  Latency Distribution
     50%   848.00us
     75%     1.10ms
     90%     1.36ms
     99%     2.37ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     33445.15    6684.91   37149.10
  Latency        2.98ms     1.39ms    19.19ms
  Latency Distribution
     50%     2.64ms
     75%     3.48ms
     90%     4.60ms
     99%     8.16ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     75291.71    5497.41   78599.63
  Latency        1.31ms   381.12us     5.29ms
  Latency Distribution
     50%     1.22ms
     75%     1.58ms
     90%     1.96ms
     99%     2.86ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     17150.72    1579.82   20631.07
  Latency        5.83ms     1.99ms    16.47ms
  Latency Distribution
     50%     5.76ms
     75%     7.90ms
     90%     8.95ms
     99%    11.50ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     15062.95     842.08   17210.23
  Latency        6.62ms     1.99ms    15.80ms
  Latency Distribution
     50%     6.46ms
     75%     8.00ms
     90%     9.70ms
     99%    12.53ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     83224.01    5105.90   87406.13
  Latency        1.18ms   338.93us     4.49ms
  Latency Distribution
     50%     1.11ms
     75%     1.43ms
     90%     1.79ms
     99%     2.67ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec     97941.92    9463.05  107851.07
  Latency        1.00ms   313.80us     4.50ms
  Latency Distribution
     50%     0.93ms
     75%     1.24ms
     90%     1.55ms
     99%     2.34ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     93290.69    6418.32   99177.14
  Latency        1.05ms   324.63us     4.70ms
  Latency Distribution
     50%     0.98ms
     75%     1.30ms
     90%     1.61ms
     99%     2.38ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
 0 / 10000 [--------------------------------------------------------------------------------------------------------------------------------------------------------------------]   0.00% 2636 / 10000 [========================================>----------------------------------------------------------------------------------------------------------------]  26.36% 13141/s 5375 / 10000 [==================================================================================>----------------------------------------------------------------------]  53.75% 13410/s 8129 / 10000 [============================================================================================================================>----------------------------]  81.29% 13522/s 10000 / 10000 [========================================================================================================================================================] 100.00% 12467/s 10000 / 10000 [=====================================================================================================================================================] 100.00% 12465/s 0s
  Reqs/sec     13614.93     846.55   15111.37
  Latency        7.31ms     1.66ms    16.22ms
  Latency Distribution
     50%     7.58ms
     75%     8.80ms
     90%    10.10ms
     99%    11.69ms
### Users Full10 (Sync) (/users/sync-full10)
 0 / 10000 [--------------------------------------------------------------------------------------------------------------------------------------------------------------------]   0.00% 1981 / 10000 [==============================>---------------------------------------------------------------------------------------------------------------------------]  19.81% 9845/s 3924 / 10000 [============================================================>---------------------------------------------------------------------------------------------]  39.24% 9767/s 5850 / 10000 [==========================================================================================>---------------------------------------------------------------]  58.50% 9715/s 7743 / 10000 [=======================================================================================================================>----------------------------------]  77.43% 9650/s 9687 / 10000 [=====================================================================================================================================================>----]  96.87% 9656/s 10000 / 10000 [=========================================================================================================================================================] 100.00% 8303/s 10000 / 10000 [======================================================================================================================================================] 100.00% 8302/s 1s
  Reqs/sec      9677.61     873.99   11818.18
  Latency       10.29ms     5.07ms    41.81ms
  Latency Distribution
     50%     9.18ms
     75%    13.10ms
     90%    17.37ms
     99%    28.83ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     15661.65     731.33   16954.81
  Latency        6.34ms     1.60ms    14.70ms
  Latency Distribution
     50%     5.82ms
     75%     7.38ms
     90%     9.48ms
     99%    11.37ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     11843.44     841.96   13507.99
  Latency        8.41ms     3.87ms    30.11ms
  Latency Distribution
     50%     7.67ms
     75%    10.26ms
     90%    13.98ms
     99%    21.62ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    105545.17    7898.03  111595.46
  Latency        0.93ms   273.81us     5.20ms
  Latency Distribution
     50%     0.88ms
     75%     1.13ms
     90%     1.41ms
     99%     2.04ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     98120.70    6729.68  103324.45
  Latency        1.00ms   314.01us     5.20ms
  Latency Distribution
     50%     0.94ms
     75%     1.23ms
     90%     1.55ms
     99%     2.21ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     65886.88    4988.05   70237.71
  Latency        1.49ms   409.47us     5.85ms
  Latency Distribution
     50%     1.41ms
     75%     1.80ms
     90%     2.26ms
     99%     3.16ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     96673.75    4816.28   99944.27
  Latency        1.02ms   290.23us     4.79ms
  Latency Distribution
     50%     0.97ms
     75%     1.25ms
     90%     1.53ms
     99%     2.21ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     94841.48    5901.47   98211.77
  Latency        1.04ms   309.31us     5.12ms
  Latency Distribution
     50%     0.98ms
     75%     1.27ms
     90%     1.60ms
     99%     2.26ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     93478.63    7787.32  100252.71
  Latency        1.03ms   314.64us     4.85ms
  Latency Distribution
     50%     0.96ms
     75%     1.29ms
     90%     1.67ms
     99%     2.33ms
### CBV Response Types (/cbv-response)
  Reqs/sec    100585.31    7076.99  106666.57
  Latency        0.97ms   318.24us     4.94ms
  Latency Distribution
     50%     0.90ms
     75%     1.20ms
     90%     1.56ms
     99%     2.40ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16238.89    1190.58   17627.94
  Latency        6.12ms     1.36ms    16.56ms
  Latency Distribution
     50%     5.92ms
     75%     7.23ms
     90%     8.46ms
     99%    10.10ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec     93030.41    8376.79  100614.99
  Latency        1.06ms   352.14us     4.70ms
  Latency Distribution
     50%     0.99ms
     75%     1.34ms
     90%     1.69ms
     99%     2.57ms
### File Upload (POST /upload)
  Reqs/sec     82497.85    5444.42   87616.66
  Latency        1.19ms   371.82us     6.10ms
  Latency Distribution
     50%     1.09ms
     75%     1.49ms
     90%     1.92ms
     99%     2.67ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec    106660.29   64142.56  251800.37
  Latency        1.19ms   302.83us     5.88ms
  Latency Distribution
     50%     1.14ms
     75%     1.46ms
     90%     1.76ms
     99%     2.44ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9693.48     897.04   10751.89
  Latency       10.26ms     2.30ms    23.70ms
  Latency Distribution
     50%    10.22ms
     75%    12.03ms
     90%    13.51ms
     99%    16.72ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec    101468.29   10001.15  108295.08
  Latency        0.97ms   356.64us     6.35ms
  Latency Distribution
     50%     0.90ms
     75%     1.18ms
     90%     1.44ms
     99%     2.11ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     96570.94    7314.13  101874.93
  Latency        1.02ms   293.45us     4.58ms
  Latency Distribution
     50%     0.95ms
     75%     1.25ms
     90%     1.53ms
     99%     2.20ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     87166.21    7149.24   91019.66
  Latency        1.13ms   375.30us     4.62ms
  Latency Distribution
     50%     1.04ms
     75%     1.42ms
     90%     1.82ms
     99%     2.64ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     95840.68    6504.23  100254.03
  Latency        1.02ms   323.77us     5.77ms
  Latency Distribution
     50%     0.94ms
     75%     1.25ms
     90%     1.58ms
     99%     2.25ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    109775.78   12418.63  118456.63
  Latency        0.90ms   325.13us     4.59ms
  Latency Distribution
     50%   838.00us
     75%     1.08ms
     90%     1.35ms
     99%     2.21ms

### Path Parameter - int (/items/12345)
  Reqs/sec    104286.68    7468.41  109770.73
  Latency        0.94ms   302.68us     4.58ms
  Latency Distribution
     50%     0.88ms
     75%     1.15ms
     90%     1.45ms
     99%     2.22ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    103801.96    6004.05  108505.11
  Latency        0.95ms   266.63us     4.96ms
  Latency Distribution
     50%     0.90ms
     75%     1.16ms
     90%     1.43ms
     99%     2.02ms

### Header Parameter (/header)
  Reqs/sec    105243.24    6720.06  109380.06
  Latency        0.94ms   262.45us     4.51ms
  Latency Distribution
     50%     0.88ms
     75%     1.16ms
     90%     1.46ms
     99%     2.13ms

### Cookie Parameter (/cookie)
  Reqs/sec    103515.60    7832.67  107933.82
  Latency        0.95ms   279.87us     4.49ms
  Latency Distribution
     50%     0.90ms
     75%     1.16ms
     90%     1.44ms
     99%     2.04ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     84266.17    4904.76   87526.06
  Latency        1.17ms   368.39us     4.57ms
  Latency Distribution
     50%     1.11ms
     75%     1.45ms
     90%     1.82ms
     99%     2.76ms
