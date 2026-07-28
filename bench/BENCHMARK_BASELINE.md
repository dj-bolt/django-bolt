# Django-Bolt Benchmark
Generated: Tue Jul 28 02:17:13 PM PKT 2026
Config: 8 processes × 1 workers | C=100 N=100000

## Root Endpoint Performance
  Reqs/sec    319158.51   25951.74  344192.83
  Latency      310.30us   315.10us    11.30ms
  Latency Distribution
     50%   238.00us
     75%   314.00us
     90%   468.00us
     99%     2.26ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
  Reqs/sec    180052.16   14220.58  200224.23
  Latency      552.12us   246.85us     7.32ms
  Latency Distribution
     50%   480.00us
     75%   607.00us
     90%     0.86ms
     99%     1.92ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    196639.99   11336.10  214870.10
  Latency      504.41us   209.32us     6.07ms
  Latency Distribution
     50%   472.00us
     75%   623.00us
     90%   779.00us
     99%     1.56ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    239274.38   18100.74  271639.96
  Latency      414.22us   287.85us     9.17ms
  Latency Distribution
     50%   330.00us
     75%   460.00us
     90%   695.00us
     99%     2.18ms
### Cookie Endpoint (/cookie)
  Reqs/sec    244947.98   16798.01  268575.25
  Latency      404.44us   276.21us     9.08ms
  Latency Distribution
     50%   329.00us
     75%   470.00us
     90%   656.00us
     99%     1.93ms
### Exception Endpoint (/exc)
  Reqs/sec    251648.34   12476.16  275576.69
  Latency      394.39us   198.23us     6.64ms
  Latency Distribution
     50%   343.00us
     75%   444.00us
     90%   618.00us
     99%     1.56ms
### HTML Response (/html)
  Reqs/sec    287052.34   15857.01  311289.80
  Latency      344.36us   243.38us     5.92ms
  Latency Distribution
     50%   270.00us
     75%   385.00us
     90%   578.00us
     99%     1.84ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     47884.63    4532.06   51720.70
  Latency        2.08ms   544.61us    16.66ms
  Latency Distribution
     50%     1.92ms
     75%     2.46ms
     90%     3.18ms
     99%     4.82ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    170319.01    7821.68  175756.93
  Latency      584.68us   157.73us     8.66ms
  Latency Distribution
     50%   594.00us
     75%   722.00us
     90%   785.00us
     99%     1.10ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    173539.51    7507.04  179733.80
  Latency      573.55us   137.05us     6.84ms
  Latency Distribution
     50%   571.00us
     75%   686.00us
     90%   761.00us
     99%     1.08ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
  Reqs/sec     91073.51   12616.38  100896.00
  Latency        1.10ms     1.14ms    45.65ms
  Latency Distribution
     50%     0.97ms
     75%     1.36ms
     90%     1.55ms
     99%     3.22ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    196585.42    8086.25  208444.82
  Latency      506.35us   132.52us     9.22ms
  Latency Distribution
     50%   479.00us
     75%   580.00us
     90%   704.00us
     99%     1.07ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    166649.56    5451.84  179920.74
  Latency      597.35us   127.66us     6.38ms
  Latency Distribution
     50%   576.00us
     75%   740.00us
     90%     0.87ms
     99%     1.33ms
### Media 100KB (GET /media/bench/upload_100k.bin)
 55903 / 100000 [==========================================================================================================================================================================>--------------------------------------------------------------------------------------------------------------------------------------]  55.90% 92893/s
  Reqs/sec     95141.07    8141.49  102754.38
  Latency        1.05ms     1.01ms    43.09ms
  Latency Distribution
     50%     0.93ms
     75%     1.19ms
     90%     1.44ms
     99%     2.98ms

## Union Response Overhead
### Single struct, no union (/bench/single)
  Reqs/sec    303739.79   17765.30  325315.99
  Latency      324.66us   300.70us     7.35ms
  Latency Distribution
     50%   254.00us
     75%   339.00us
     90%   493.00us
     99%     2.16ms
### Single struct via tagged union (/bench/union-single)
  Reqs/sec    306383.58   14846.03  334128.84
  Latency      323.98us   263.01us     6.31ms
  Latency Distribution
     50%   259.00us
     75%   357.00us
     90%   512.00us
     99%     1.96ms
### List of 100 structs, no union (/bench/list)
 90751 / 100000 [====================================================================================================================================================================================================================================================================================>----------------------------]  90.75% 90499/s
  Reqs/sec     90617.69    3443.31   95520.87
  Latency        1.10ms   136.22us     4.99ms
  Latency Distribution
     50%     1.05ms
     75%     1.29ms
     90%     1.50ms
     99%     2.11ms
### List of 100 structs via tagged union (/bench/union-list)
 16989 / 100000 [===================================================>-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  16.99% 84647/s
  Reqs/sec     85766.74    3333.04   91162.21
  Latency        1.16ms   159.28us     5.72ms
  Latency Distribution
     50%     1.08ms
     75%     1.41ms
     90%     1.75ms
     99%     2.30ms

## Authentication & Authorization Performance
### Auth NO User Access (/auth/no-user-access) - lazy loading, no DB query
  Reqs/sec    162280.49    7168.52  175517.69
  Latency      612.79us   156.43us     5.57ms
  Latency Distribution
     50%   553.00us
     75%   714.00us
     90%     0.94ms
     99%     1.42ms
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     41133.92    1304.14   41915.57
  Latency        2.43ms   221.52us     9.35ms
  Latency Distribution
     50%     2.42ms
     75%     2.91ms
     90%     3.24ms
     99%     3.66ms
### Get User via Dependency (/auth/me-dependency)
  Reqs/sec     40516.30    3721.18   45853.97
  Latency        2.46ms     2.36ms    85.22ms
  Latency Distribution
     50%     2.27ms
     75%     2.82ms
     90%     3.46ms
     99%     5.04ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    158572.00    6945.25  170595.88
  Latency      627.69us   152.09us     5.58ms
  Latency Distribution
     50%   610.00us
     75%   713.00us
     90%     0.85ms
     99%     1.42ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    247408.41   17703.51  284715.70
  Latency      399.65us   313.59us     7.54ms
  Latency Distribution
     50%   297.00us
     75%   429.00us
     90%   670.00us
     99%     2.56ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    242309.36   14708.51  267563.34
  Latency      409.83us   257.69us     6.34ms
  Latency Distribution
     50%   334.00us
     75%   469.00us
     90%   662.00us
     99%     2.13ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     21088.04    1297.41   22627.13
  Latency        4.74ms     2.20ms    95.08ms
  Latency Distribution
     50%     4.77ms
     75%     5.58ms
     90%     6.21ms
     99%     7.79ms
### Users Full10 (Sync) (/users/sync-full10)
  Reqs/sec     15952.83    1151.01   19032.09
  Latency        6.26ms     2.90ms    98.41ms
  Latency Distribution
     50%     5.50ms
     75%     7.76ms
     90%    10.51ms
     99%    17.90ms
### Users Mini10 (Async) (/users/mini10)
 4748 / 100000 [=====>-----------------------------------------------------------------------------------------------------------------------]   4.75% 23676/s 00m04s
 67504 / 100000 [===================================================================================>----------------------------------------]  67.50% 25915/s 00m01s
 83498 / 100000 [=============================================================================================================>---------------------]  83.50% 26049/s
 98993 / 100000 [=================================================================================================================================>-]  98.99% 26005/s
  Reqs/sec     25984.49    2039.51   28461.18
  Latency        3.84ms     1.50ms    63.93ms
  Latency Distribution
     50%     3.73ms
     75%     4.50ms
     90%     5.26ms
     99%     7.10ms
### Users Mini10 (Sync) (/users/sync-mini10)
 99497 / 100000 [===================================================================================================================================]  99.50% 17726/s
  Reqs/sec     17735.85    1034.07   20079.63
  Latency        5.63ms     1.84ms    69.19ms
  Latency Distribution
     50%     5.23ms
     75%     6.84ms
     90%     8.66ms
     99%    12.96ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    203566.72   19685.99  234211.43
  Latency      488.34us   265.87us     8.17ms
  Latency Distribution
     50%   399.00us
     75%   571.00us
     90%   810.00us
     99%     2.22ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    201422.18   14713.57  226763.75
  Latency      493.10us   280.36us     9.09ms
  Latency Distribution
     50%   403.00us
     75%   568.00us
     90%   822.00us
     99%     2.23ms
### Items100 ViewSet GET (/cbv-items100)
  Reqs/sec     92480.50    4004.19  100179.31
  Latency        1.08ms   201.41us     6.00ms
  Latency Distribution
     50%     1.03ms
     75%     1.27ms
     90%     1.62ms
     99%     2.34ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    205934.95   17756.70  233410.71
  Latency      481.91us   263.91us     6.72ms
  Latency Distribution
     50%   410.00us
     75%   531.00us
     90%   768.00us
     99%     2.08ms
### CBV Items PUT (Update) (/cbv-items/1)
 79497 / 100000 [=======================================================================================================>--------------------------]  79.50% 198239/s
  Reqs/sec    196806.39   16372.98  222593.39
  Latency      504.72us   256.36us     7.07ms
  Latency Distribution
     50%   439.00us
     75%   564.00us
     90%   798.00us
     99%     1.96ms

## CBV Additional Benchmarks
### CBV Bench Parse (POST /cbv-bench-parse)
  Reqs/sec    197263.96   19267.57  233326.69
  Latency      504.81us   265.73us     6.65ms
  Latency Distribution
     50%   418.00us
     75%   558.00us
     90%   837.00us
     99%     2.33ms
### CBV Response Types (/cbv-response)
  Reqs/sec    213409.17   19609.04  245541.91
  Latency      461.58us   281.47us     6.04ms
  Latency Distribution
     50%   369.00us
     75%   537.00us
     90%   773.00us
     99%     2.34ms

## ORM Performance with CBV
Seeding 1000 users for CBV benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users CBV Mini10 (List) (/users/cbv-mini10)
  Reqs/sec     20687.83    2297.22   23408.14
  Latency        4.83ms     3.09ms   103.39ms
  Latency Distribution
     50%     4.62ms
     75%     5.49ms
     90%     6.35ms
     99%     8.36ms
Cleaning up test users...
