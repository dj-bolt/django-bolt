# Django-Bolt Benchmark
Generated: Sun 01 Mar 2026 06:41:18 AM PKT
Config: 8 processes × 1 workers | C=100 N=10000

## Root Endpoint Performance
  Reqs/sec    108834.74    6640.45  114173.43
  Latency        0.90ms   311.13us     5.33ms
  Latency Distribution
     50%   824.00us
     75%     1.12ms
     90%     1.44ms
     99%     2.10ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec     85821.13    5786.63   91422.64
  Latency        1.14ms   381.02us     6.45ms
  Latency Distribution
     50%     1.05ms
     75%     1.38ms
     90%     1.79ms
     99%     2.84ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec     86890.87    6520.57   90931.46
  Latency        1.13ms   368.35us     4.91ms
  Latency Distribution
     50%     1.06ms
     75%     1.38ms
     90%     1.76ms
     99%     2.66ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec     65615.85   11860.90   79170.00
  Latency        1.44ms     0.87ms     8.77ms
  Latency Distribution
     50%     1.15ms
     75%     1.74ms
     90%     2.67ms
     99%     5.30ms
### Cookie Endpoint (/cookie)
  Reqs/sec     86753.18    7195.87   93326.13
  Latency        1.14ms   424.82us     6.00ms
  Latency Distribution
     50%     1.05ms
     75%     1.39ms
     90%     1.80ms
     99%     2.84ms
### Exception Endpoint (/exc)
  Reqs/sec     83292.44   10432.75   94256.58
  Latency        1.18ms   503.63us     6.18ms
  Latency Distribution
     50%     1.04ms
     75%     1.44ms
     90%     1.95ms
     99%     3.32ms
### HTML Response (/html)
  Reqs/sec    101954.33    8856.97  107910.04
  Latency        0.97ms   389.94us     5.36ms
  Latency Distribution
     50%     0.89ms
     75%     1.21ms
     90%     1.50ms
     99%     2.40ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     28922.16    6298.71   35851.17
  Latency        3.47ms     1.91ms    22.05ms
  Latency Distribution
     50%     2.90ms
     75%     4.08ms
     90%     5.72ms
     99%    11.94ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec     70563.87    6051.54   77003.27
  Latency        1.39ms   498.15us     6.04ms
  Latency Distribution
     50%     1.29ms
     75%     1.68ms
     90%     2.13ms
     99%     3.79ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     16879.14    1561.77   18989.28
  Latency        5.91ms     1.53ms    14.57ms
  Latency Distribution
     50%     5.92ms
     75%     6.98ms
     90%     8.02ms
     99%    10.88ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     14805.09    2166.35   16662.26
  Latency        6.58ms     3.12ms    29.56ms
  Latency Distribution
     50%     6.17ms
     75%     8.32ms
     90%    11.19ms
     99%    16.27ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec     75655.65    3949.60   81476.80
  Latency        1.29ms   460.47us     5.24ms
  Latency Distribution
     50%     1.18ms
     75%     1.62ms
     90%     2.15ms
     99%     3.22ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec     98435.75    7806.76  106724.36
  Latency        1.01ms   386.16us     5.84ms
  Latency Distribution
     50%     0.92ms
     75%     1.24ms
     90%     1.58ms
     99%     2.80ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec     91541.14    7932.20  100964.44
  Latency        1.07ms   341.38us     4.12ms
  Latency Distribution
     50%     0.99ms
     75%     1.35ms
     90%     1.70ms
     99%     2.54ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     14288.81    1222.34   15632.82
  Latency        6.96ms     2.14ms    20.01ms
  Latency Distribution
     50%     6.67ms
     75%     8.37ms
     90%    10.23ms
     99%    14.01ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     11672.10    1551.31   14653.39
  Latency        8.53ms     3.66ms    36.41ms
  Latency Distribution
     50%     7.86ms
     75%    10.50ms
     90%    13.76ms
     99%    21.07ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     15734.32    1419.30   17985.98
  Latency        6.31ms     2.37ms    20.96ms
  Latency Distribution
     50%     5.88ms
     75%     7.89ms
     90%    10.10ms
     99%    14.31ms
### Users Mini10 (Sync) (/users/sync-mini10)
  Reqs/sec     12570.56    1956.22   15129.80
  Latency        7.77ms     3.39ms    28.69ms
  Latency Distribution
     50%     7.10ms
     75%     9.89ms
     90%    12.89ms
     99%    19.33ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec     95920.07   12028.20  115289.96
  Latency        1.02ms   490.31us     5.72ms
  Latency Distribution
     50%     0.88ms
     75%     1.21ms
     90%     1.69ms
     99%     3.48ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec     89570.13    7116.59  100012.00
  Latency        1.08ms   418.22us     6.82ms
  Latency Distribution
     50%     0.97ms
     75%     1.32ms
     90%     1.85ms
     99%     2.99ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     62423.06    4729.40   68118.37
  Latency        1.58ms   585.98us     6.60ms
  Latency Distribution
     50%     1.46ms
     75%     1.89ms
     90%     2.50ms
     99%     4.05ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec     82913.48    9514.12   92263.21
  Latency        1.18ms   478.38us     5.49ms
  Latency Distribution
     50%     1.06ms
     75%     1.49ms
     90%     1.97ms
     99%     3.34ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec     87958.62    8025.68   99956.46
  Latency        1.12ms   420.08us     5.42ms
  Latency Distribution
     50%     1.03ms
     75%     1.37ms
     90%     1.76ms
     99%     3.08ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec     81312.52   10739.06   94149.87
  Latency        1.20ms   565.35us     8.53ms
  Latency Distribution
     50%     1.05ms
     75%     1.47ms
     90%     1.96ms
     99%     3.79ms
### CBV Response Types (/cbv-response)
  Reqs/sec     88303.21   15172.87  106128.09
  Latency        1.08ms   456.84us     5.16ms
  Latency Distribution
     50%     0.97ms
     75%     1.33ms
     90%     1.77ms
     99%     3.30ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     16252.82    2118.83   18900.57
  Latency        6.02ms     2.12ms    18.40ms
  Latency Distribution
     50%     5.76ms
     75%     7.50ms
     90%     9.21ms
     99%    12.76ms
Cleaning up test users...


## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec     91175.59    6287.81   98037.62
  Latency        1.06ms   413.70us     5.04ms
  Latency Distribution
     50%     0.96ms
     75%     1.31ms
     90%     1.74ms
     99%     3.04ms
### File Upload (POST /upload)
  Reqs/sec     79394.29    9516.49   93414.06
  Latency        1.23ms   514.60us     6.02ms
  Latency Distribution
     50%     1.09ms
     75%     1.53ms
     90%     2.13ms
     99%     3.50ms
### Mixed Form with Files (POST /mixed-form)
  Reqs/sec     72049.14    8866.04   82145.16
  Latency        1.35ms   603.63us     5.98ms
  Latency Distribution
     50%     1.21ms
     75%     1.67ms
     90%     2.30ms
     99%     4.14ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
  Reqs/sec      9430.31    1345.41   14278.36
  Latency       10.64ms     3.47ms    26.51ms
  Latency Distribution
     50%    10.67ms
     75%    13.22ms
     90%    15.42ms
     99%    20.58ms

## Django Ninja-style Benchmarks
### JSON Parse/Validate (POST /bench/parse)
  Reqs/sec     95523.13    6272.12  104234.93
  Latency        1.02ms   402.59us     5.76ms
  Latency Distribution
     50%     0.93ms
     75%     1.23ms
     90%     1.61ms
     99%     2.87ms

## Serializer Performance Benchmarks
### Raw msgspec Serializer (POST /bench/serializer-raw)
  Reqs/sec     87528.55   11884.86  100671.03
  Latency        1.12ms   597.80us     7.49ms
  Latency Distribution
     50%     0.96ms
     75%     1.35ms
     90%     1.86ms
     99%     4.04ms
### Django-Bolt Serializer with Validators (POST /bench/serializer-validated)
  Reqs/sec     80202.10    8656.12   94285.61
  Latency        1.21ms   491.08us     5.97ms
  Latency Distribution
     50%     1.09ms
     75%     1.45ms
     90%     1.92ms
     99%     3.56ms
### Users msgspec Serializer (POST /users/bench/msgspec)
  Reqs/sec     83285.32   10465.96   93616.44
  Latency        1.16ms   578.28us     6.52ms
  Latency Distribution
     50%     0.99ms
     75%     1.44ms
     90%     2.05ms
     99%     3.83ms

## Latency Percentile Benchmarks
Measures p50/p75/p90/p99 latency for type coercion overhead analysis

### Baseline - No Parameters (/)
  Reqs/sec    103082.08   10729.74  114982.44
  Latency        0.95ms   431.29us     5.83ms
  Latency Distribution
     50%   839.00us
     75%     1.16ms
     90%     1.49ms
     99%     3.03ms

### Path Parameter - int (/items/12345)
  Reqs/sec     97156.34   11503.03  110344.28
  Latency        1.00ms   414.97us     5.92ms
  Latency Distribution
     50%     0.89ms
     75%     1.20ms
     90%     1.57ms
     99%     3.12ms

### Path + Query Parameters (/items/12345?q=hello)
  Reqs/sec    102900.29   17197.53  135640.60
  Latency        1.01ms   414.83us     5.28ms
  Latency Distribution
     50%     0.92ms
     75%     1.22ms
     90%     1.60ms
     99%     3.00ms

### Header Parameter (/header)
  Reqs/sec     93154.12    9814.68  102436.25
  Latency        1.04ms   388.50us     5.74ms
  Latency Distribution
     50%     0.95ms
     75%     1.29ms
     90%     1.63ms
     99%     2.85ms

### Cookie Parameter (/cookie)
  Reqs/sec     84664.98    9731.97   90607.71
  Latency        1.17ms   513.45us     5.52ms
  Latency Distribution
     50%     1.03ms
     75%     1.48ms
     90%     1.98ms
     99%     3.66ms

### Auth Context - JWT validated, no DB (/auth/context)
  Reqs/sec     75198.17   10035.26   89922.16
  Latency        1.30ms   632.42us     8.71ms
  Latency Distribution
     50%     1.15ms
     75%     1.60ms
     90%     2.12ms
     99%     3.97ms
