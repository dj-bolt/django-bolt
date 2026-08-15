# Django-Bolt Benchmark
Generated: Sun Aug 16 04:31:38 AM PKT 2026
Config: 8 processes × 1 workers | C=100 N=100000

## Root Endpoint Performance
  Reqs/sec    305065.13   29913.58  336129.13
  Latency      325.99us   323.72us     7.58ms
  Latency Distribution
     50%   230.00us
     75%   332.00us
     90%   542.00us
     99%     2.29ms

## 10kb JSON Response Performance
### 10kb JSON (Async) (/10k-json)
 36499 / 100000 [==================================================================================>----------------------------------------------------------------------------------------------------------------------------------------------]  36.50% 182018/s
  Reqs/sec    172734.27   14835.21  196457.53
  Latency      576.26us   284.01us     6.12ms
  Latency Distribution
     50%   486.00us
     75%   665.00us
     90%     0.93ms
     99%     2.49ms
### 10kb JSON (Sync) (/sync-10k-json)
  Reqs/sec    174159.35   14123.84  198488.60
  Latency      571.39us   324.29us     7.70ms
  Latency Distribution
     50%   496.00us
     75%   666.00us
     90%     0.93ms
     99%     2.65ms

## Response Type Endpoints
### Header Endpoint (/header)
  Reqs/sec    253600.58   31778.72  355918.60
  Latency      399.17us   261.07us     6.20ms
  Latency Distribution
     50%   318.00us
     75%   432.00us
     90%   658.00us
     99%     1.95ms
### Cookie Endpoint (/cookie)
  Reqs/sec    228166.19   32793.54  267521.82
  Latency      435.58us   300.58us    11.13ms
  Latency Distribution
     50%   341.00us
     75%   486.00us
     90%   745.00us
     99%     2.37ms
### Exception Endpoint (/exc)
  Reqs/sec    252865.74   16164.36  274102.52
  Latency      391.28us   195.34us     5.89ms
  Latency Distribution
     50%   329.00us
     75%   437.00us
     90%   638.00us
     99%     1.56ms
### HTML Response (/html)
  Reqs/sec    291496.20   18752.92  314721.42
  Latency      341.78us   271.64us     7.22ms
  Latency Distribution
     50%   266.00us
     75%   375.00us
     90%   552.00us
     99%     1.97ms
### Redirect Response (/redirect)
### File Static via FileResponse (/file-static)
  Reqs/sec     48365.74    4811.25   55075.00
  Latency        2.07ms   516.92us    15.41ms
  Latency Distribution
     50%     1.90ms
     75%     2.47ms
     90%     3.19ms
     99%     4.80ms

## Native Static & Media File Serving
### Static 1KB CSS (GET /static/bench/asset_1k.css)
  Reqs/sec    162100.42   13861.38  176561.96
  Latency      613.87us   259.80us     9.77ms
  Latency Distribution
     50%   567.00us
     75%   696.00us
     90%   847.00us
     99%     1.96ms
### Static 1KB CSS (HEAD /static/bench/asset_1k.css)
  Reqs/sec    169052.01    7207.12  182591.72
  Latency      587.66us   184.45us     7.90ms
  Latency Distribution
     50%   549.00us
     75%   643.00us
     90%   757.00us
     99%     1.59ms
### Static 100KB JS (GET /static/bench/asset_100k.js)
 17751 / 100000 [========================================>-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------]  17.75% 88450/s
  Reqs/sec     88068.67    7908.65  100067.23
  Latency        1.13ms     1.19ms    42.88ms
  Latency Distribution
     50%     1.04ms
     75%     1.29ms
     90%     1.67ms
     99%     3.44ms
### Static 404 miss (GET /static/bench/missing.css)
  Reqs/sec    185854.03   14837.20  204664.70
  Latency      535.61us   198.28us     6.08ms
  Latency Distribution
     50%   471.00us
     75%   620.00us
     90%   789.00us
     99%     1.53ms
### Media 1KB (GET /media/bench/upload_1k.bin)
  Reqs/sec    159012.30    9068.00  172523.97
  Latency      622.56us   166.74us     4.49ms
  Latency Distribution
     50%   562.00us
     75%   745.00us
     90%     0.93ms
     99%     1.56ms
### Media 100KB (GET /media/bench/upload_100k.bin)
 68903 / 100000 [===========================================================================================================================================================>----------------------------------------------------------------------]  68.90% 85797/s
  Reqs/sec     87825.06    8400.51   99699.23
  Latency        1.13ms     1.10ms    46.20ms
  Latency Distribution
     50%     1.03ms
     75%     1.34ms
     90%     1.76ms
     99%     3.38ms

## Authentication & Authorization Performance
### Get Authenticated User (/auth/me) - accesses request.user, triggers DB query
  Reqs/sec     39465.09    2591.55   41295.89
  Latency        2.53ms   368.93us    12.52ms
  Latency Distribution
     50%     2.48ms
     75%     3.18ms
     90%     3.47ms
     99%     4.40ms
### Get Auth Context (/auth/context) validated jwt no db
  Reqs/sec    151215.37    7275.07  165388.80
  Latency      657.56us   212.68us     5.98ms
  Latency Distribution
     50%   645.00us
     75%   802.00us
     90%     0.97ms
     99%     1.77ms

## Items GET Performance (/items/1?q=hello)
  Reqs/sec    231157.67   41556.04  290363.40
  Latency      429.04us   514.72us    12.45ms
  Latency Distribution
     50%   291.00us
     75%   413.00us
     90%   714.00us
     99%     3.13ms

## Items PUT JSON Performance (/items/1)
  Reqs/sec    234188.47   21590.78  269564.71
  Latency      420.79us   327.01us     8.07ms
  Latency Distribution
     50%   325.00us
     75%   444.00us
     90%   675.00us
     99%     2.63ms

## ORM Performance
Seeding 1000 users for benchmark...
Successfully seeded users
Validated: 10 users exist in database
### Users Full10 (Async) (/users/full10)
  Reqs/sec     20722.28    1477.75   23829.00
  Latency        4.82ms     2.33ms    94.61ms
  Latency Distribution
     50%     4.71ms
     75%     5.41ms
     90%     6.11ms
     99%     7.90ms
### Users Full10 (Sync) (/users/sync-full10)
 43995 / 100000 [================================================================================================>--------------------------------------------------------------------------------------------------------------------------]  43.99% 15689/s 00m03s
  Reqs/sec     15395.04    1195.93   19392.87
  Latency        6.49ms     2.79ms    83.97ms
  Latency Distribution
     50%     5.79ms
     75%     7.90ms
     90%    10.68ms
     99%    16.71ms
### Users Mini10 (Async) (/users/mini10)
  Reqs/sec     26323.56    1393.94   28542.68
  Latency        3.80ms     1.42ms    65.74ms
  Latency Distribution
     50%     3.73ms
     75%     4.59ms
     90%     5.32ms
     99%     6.71ms
Cleaning up test users...

## Class-Based Views (CBV) Performance
### Simple APIView GET (/cbv-simple)
  Reqs/sec    205941.21   18624.31  234423.29
  Latency      479.39us   279.42us     8.02ms
  Latency Distribution
     50%   401.00us
     75%   532.00us
     90%   771.00us
     99%     2.10ms
### Simple APIView POST (/cbv-simple)
  Reqs/sec    205027.85   16287.76  234909.79
  Latency      485.09us   224.53us     5.95ms
  Latency Distribution
     50%   443.00us
     75%   563.00us
     90%   763.00us
     99%     1.78ms

## CBV Items - Basic Operations
### CBV Items GET (Retrieve) (/cbv-items/1)
  Reqs/sec    210782.88   21187.92  253089.53
  Latency      469.37us   235.36us     9.02ms
  Latency Distribution
     50%   406.00us
     75%   546.00us
     90%   741.00us
     99%     1.81ms
### CBV Items PUT (Update) (/cbv-items/1)
  Reqs/sec    192707.38   13367.51  212797.51
  Latency      515.44us   257.32us     6.17ms
  Latency Distribution
     50%   446.00us
     75%   574.00us
     90%   811.00us
     99%     1.92ms

## Form and File Upload Performance
### Form Data (POST /form)
  Reqs/sec    208627.54   26822.00  253753.92
  Latency      475.89us   281.86us     7.97ms
  Latency Distribution
     50%   387.00us
     75%   529.00us
     90%   810.00us
     99%     2.28ms
### File Upload (POST /upload)
  Reqs/sec    175890.87   15671.69  199768.20
  Latency      565.42us   249.90us     6.38ms
  Latency Distribution
     50%   498.00us
     75%   678.00us
     90%     0.90ms
     99%     2.07ms
### Form Repeated Keys urlencoded (POST /form-list)
  Reqs/sec    194831.64   19497.15  226346.99
  Latency      511.34us   236.75us     7.02ms
  Latency Distribution
     50%   440.00us
     75%   585.00us
     90%   834.00us
     99%     2.01ms
### Form Repeated Keys multipart (POST /form-list)
  Reqs/sec    150624.04   10189.56  167896.14
  Latency      660.63us   204.21us     6.05ms
  Latency Distribution
     50%   602.00us
     75%   782.00us
     90%     1.03ms
     99%     1.83ms

## Django Middleware Performance
### Django Middleware + Messages Framework (/middleware/demo)
Tests: SessionMiddleware, AuthenticationMiddleware, MessageMiddleware, custom middleware, template rendering
