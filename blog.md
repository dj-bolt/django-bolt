---
title: 'How I Built the Fastest Python Web Framework (Faster Than FastAPI) aka Django-Bolt'
slug: 'building-fastest-python-webframework'
description: ''
date: '2025-09-11'
tags:
  [
    'gsoc',
    'gsoc-2025',
    'django',
    'open-source',
    'django-template-partials',
    'django-6.0',
    'django-template-system'
  ]
published: false
---


My journey started with Django in 2019. I learned it as my first web framework, so it has a special place in my heart. I contributed to Django as a Google Summer of Code contributor in 2025, and before that, I contributed to djangopackages.org through Djangonaut Space. Through these interactions, the fear of the alien nature of big open-source projects faded away, and I thought I could build something too. AI coding also helped, because in the old days (2019 for me), having to search everything on the internet was a lot of work. Now I have a "Senior Engineer" at my fingertips. Senior Engineer is in quotes, because this senior engineer has quirks (foreshadowing).


I started Django Bolt as an experiment: keep Django’s developer experience, move request handling to Rust, and push performance as far as possible. Why Rust? because of pyo3, pyo3 makes it so easy to integerate rust into python.There are also a lot of examples of this in the community too (Robyn, Pydantic).

Robyn proved the concepts that I was guessing at. So the end goal was kind of clear: handle HTTP requests in Rust and call Python when needed. I was not thinking of it as "I'll build a usable web framework," but the community liked the idea, and now I have spent about 6 months of my time on this project. 


## So It Begins

My first public commit was on September 19, 2025. I was trying to build a single-file Django API web framework, something like Django Nano. But pretty early on I decided against it because I was fighting two things at once: Django and the Rust side.

## Django Bootstrap

For the ORM to work, Django needs to be bootstrapped — its environment configured and initialized. I decided to use a management command to run my Django Bolt server, which handles bootstrapping automatically. It turned out to be a lucky choice. I didn't have to think about it at the time, and it was only later, when I paid closer attention, then I realized why the ORM was just working without any explicit setup.


## Sync vs Async
{{Actually read code for this}}

First of all, I just wanted to prove that this thing could work. So I told Claude to build a kind of an proof-of-concept of the framework in which it used sync calls — it just called the Python function from the Actix layer in full sync mode.

At this point, I was getting about 27,000 requests per second using four workers. That proved the concept, and it was much faster than simple Django views.

But for database endpoints, sync does not perform as well. Initially I was testing with just simple JSON endpoints, which is why it was performing very well. But for database endpoints, it does not perform very well.

So the mission was: I wanted to have async functionality — async calling of the views — but that proved to be a very big refactor of the first proof of concept. The issue was that for handling async, I had to handle Tokio on the Rust side and figure out how to call Python asynchronously. It was not the first design, but what I landed on was: I should have a Python event loop that I store at registration time and use to call the Python functions from the Rust side.

The async event loop was not the first solution I explored. I explored subinterpreters, using threads, and other approaches. I liked subinterpreters, butthe PyO3 support was not there yet. For some reason I was very afraid of the GIL, because in my mind the GIL was a killer of performance. So I decided to use an event loop and then use process forking in Rust, which would give me scalability across different workers, each having their own event loop, resulting in less GIL contention. At this point, I was making the mistake of having more than one Actix thread with one python event loop handling both of those Actix threads. But then I realized the bottleneck will always be the Python code, not the Rust code, so we are not getting any help by increasing the Actix threads for a given worker. So after some time, I just fixed that to one Actix thread per one Python event loop.

### Learning
Python is the bottleneck in most cases (foreshadowing).

### Py-Spy Flame Graphs
I was getting a high number of RPS. I wanted more. So what I did was use Py-Spy and other tools to see where we were spending our time. What I saw was that we were creating a new event loop on every new request. So I changed that to have one event loop created at registration time and then just reuse it.
### Learning 
Use flamegraphs or other performance tools they work. 

### Learning #1
What I learned at that point was: I should do the maximum amount of work at registration time instead of on the hot path ie. the path through code that is followed by every request.


During my testing, I realized that most of the time, my API spends its time on serialization or the query itself. So I wanted to optimize the serialization time. I did not test a lot of libraries at the time. I only knew of Pydantic and msgspec (and stdlib JSON), but I decided to go with msgspec just because of performance reasons — and also because Litestar used msgspec, so there were examples of using it in a web framework. That serialization library alone gave me a lot of performance boost against other frameworks. Building the serialization layer on top of msgspec is another story that we will discuss later.

### Learning #2
The learning from this experience was: use fast libraries like msgspec. Applying this learning, I then used the python-multipart library for form parsing, which I later replaced with Rust-side form parsing, but that comes much later.

At this moment, I was getting about 44,435 requests per second with four workers. Remember, this was without any proper framework functionality. This was just calling the view function from Rust. We did not have any middlewares or any overhead of the framework.


## Benchmarking
Because my goal was to have an experimental project, benchmarking was part of it to show its potential. So I benchmarked at every little change to see what was affected and what was not. For benchmarking, I first used Apache Bench (ab). I created example views for JSON, database queries, and stuff like that, and I made it a one-command thing. I run one command and I get the before and after results, so I know if my performance is increasing or decreasing.

### Learning #3
Before optimizing, we should always measure what we are optimizing. Without measuring, we don't know what part needs optimization, and we are just blindly guessing.

## Streaming and the Limits of AI Coding

From the start of this framework, my goal was to have a proper way to stream AI responses, like Server-Sent Events, or proper WebSockets. I was most excited about this feature, and it was the most excruciatingly difficult part for me. I spent weeks discussing with AI on how to implement this. This also showed me the limitations of AI. AI can be like a world-class programmer if you prompt it well, but it has limits — if it hypothesizes something and you kind of know in your heart that it is wrong, it will get stuck. I spent weeks on this problem. I tested batching and studied other codebases to see what they do. I spent a lot of time on this.

### Learning
Measure the correct thing. Python is not always the bottleneck.

### The Bridge
So what was the problem? In a normal view, we have one full cycle: we call a Python function, we await it, and we get the response. But in streaming, the cycle breaks. What happens is when we call a view, it returns a generator. The generator sends a response back — for example, if we are returning from a while loop, it will return that response after one second or so. So it is not just awaiting a simple Python function. We have to send that response to the client, and then also await the next response. This actually amplifies the problem of the Python-Rust bridge handled by PyO3. Every time a new response comes, we have to cross that bridge every time. The other stupid thing I was doing was measuring requests per second for streaming responses. That was not the right thing to measure for streaming responses. {{Solution from code}}


### Vibe Coder vs Software Engineer
- Problem

After solving the streaming part, the other problem I identified when I was comparing Litestar, FastAPI, and Django Bolt was that for simple 1KB JSON, Django Bolt was outperforming Litestar and FastAPI. But when the response size increased — like for a 10KB JSON response — Django Bolt was stuck at about 40,000 requests per second, no matter how many workers I added. Meanwhile, Litestar was getting more than 60,000. So I knew in my heart that I was doing something wrong here. But when I asked AI for help, it just told me this is the price of the FFI bridge, that it is adding latency for large responses, and this is a limitation of the framework. "We cannot fix that."

- Process

This is where a normal vibe coder and a software engineer differentiate themselves. I knew in my heart that this could not be the real problem. But for actually finding the problem, I had to spend a lot of time talking with AI, and that talk went all in vain because it just kept saying that this is a limitation of the framework and we cannot fix it. So I did what a software engineer should do. I precisely measured everything: time for creating a promise, converting that promise from Python to Rust, awaiting that promise, Python-side processing, how the response is sent from Rust to Python. I measured everything and tested everything that I could.

- Solution

What I found was that the response transfer path between Python and Rust was spending too much time in GIL-bound extraction and conversion for larger payloads. The data is still copied at the Python-Rust boundary, but moving that hot-path work out of the GIL-heavy path fixed the bottleneck. Now I get about 90,000 requests per second for the same number of workers, where I was previously stuck under 35,000 requests per second.


## Testing

Another problem this created was the testing part, because some of our code lives inside Rust. For example, common middlewares like compression and CORS are in Rust, some specific authentication also happens in Rust, and our dispatch code is in Python. Because we were using routing as a OnceCell-like memory type in Rust, we can only set it once. For testing, we need to create a new router for every test, and we cannot reset the router in Rust. I know there may be a better solution for this, but I do not know it yet. If someone knows a better solution, please tell me about it.

So what I had to do was recreate most of the hot path code for the Rust part in the testing module. I was copying a lot of code, and that was actually slowing down development. I still have not fully figured out the solution. Now I mostly try to create shared functionality wherever I can. Every new feature I add has to be added inside the Rust testing part as well, and that was really killing my productivity.

It is not fully fixed yet, but how I have gotten around it is by using the Actix testing library that Actix provides and wrapping over it, so I don't have to do the whole routing part. But that still required me to have testing data separately because, as I discussed before, the route and other state is a global variable. So I had to have separate code for some of the features.

### Django ORM Does Not Like My Testing Solution
Because we are using a thread inside our testing, whenever we update something in the database in our test, we cannot see those changes in the test assertions. That is why I have added the use of transactions, which slow down the tests. This part is still unsolved. I decided to go forward with making the framework more mature and come back to solve it later.


## Django Views Compatibility

Django Bolt is a separate framework that still wants to have Django admin. My first solution was to have an ASGI bridge. I created a simple ASGI bridge to handle Django admin pages so that users can use Django admin. To handle static files, I initially added a kind of proxy to handle static files in the HTML pages, but that was a trick I used at first.

Now I have fixed it. I have full static file handling inside Rust. If debug mode is on, it falls back to Django to find the static files. I cannot make that function work in production because Django also advises against it, and it is not fully secure. So what I recommend is to run `collectstatic` and have a static folder in settings so my Rust side can serve those static files, instead of depending on Django to find static files for Django admin.

Now I have full ASGI mounting functionality in Django Bolt that can handle full ASGI. You can mount FastAPI or Django applications. Django admin also uses that. This way, you can also use separate packages like Django Allauth or other packages that have views or URLs that they register through Django. So now users can have their views in Django and expose them through the Django Bolt server, same as Granian.


## Middleware

I wanted to implement simple middleware inside Rust. So what I did was implement CORS on the Rust side. But I wanted to have the ability to override it. So I implemented it inside my dispatch function in Rust. That was a very bad decision because it caused a lot of pain afterwards. In a framework, there are many return paths for a function, so I had to go and handle CORS on every one of them. The learning from this was: just use middlewares.

### Learning
Just f***ing use middlewares. Don't reinvent the wheel.


Django middleware compatibility was an important aspect because many packages use Django middleware. There are also security middlewares that I did not want to re-implement inside Django Bolt because it would have been a lot of work. So I decided to build a compatibility layer between Django middlewares and Django Bolt. Now if a user wants to use a Django middleware inside their Django Bolt view, they can just pass a Django middleware, and our adapter layer handles it natively.

For this, I had to implement a compatibility layer for the Django Bolt request, which was barebone before. I had to implement different properties that Django middlewares expect on the request. But this way, adding more properties was getting heavy on the request side. So I used lazy getters and setters: we have a state dictionary that stores all the data, the setters store data in that dictionary, and the getters read keys from the dictionary. What we achieve from this is that we don't have the overhead for all Django Bolt views — we only have some overhead for the views that use Django middleware, which should be a small number of them.

Another problem with these middlewares was that I could not use the default middlewares provided by Actix, because I wanted to override them per-function using decorators. So I had to implement new middlewares for these functionalities so that I could override them. Compression was a great example. I wanted to override it for streaming. What was happening was that the compression middleware was buffering my streaming responses, and I was not getting the streamed responses in real time. I may have spent a few days on this bug. I could not figure out why streaming was not working. AI was not helping either, because the streaming code is in one place and the middleware code is in another. It had no idea that it should read the middleware code, or that we were using a middleware when I was talking about streaming. What actually helped was adding console logs inside the functions to see what works and what does not, and why something was not returning properly. That also helped AI figure it out.

### Learning
What you don't know is what you don't know. AI cannot help you with everything.



## Authentication

Honestly speaking, I don't like the way Django Bolt's authentication works today. What it does now is that it is mostly JWT-heavy. It only works well with JWT tokens, not other kinds of authentication, because a JWT token is like your passport — it works without your framework needing to do anything. What Rust can do is decode the JWT token and see if it is a valid login or not. So now what users do is, when they have an authenticated endpoint, the JWT token gets validated: check if it is valid, decode it, check if it is expired. It is very fast compared to a Python solution because it does not even touch Python at all. But it has limitations because we are not actually checking the database. So it is a starting point, and I want to improve it further.

For session authentication, I just defer to Django's session middleware. I tell the user to use that. This works because we have the Django middleware compatibility layer, so it just works fine.


## Serialization (Pydantic vs Litestar)

I like Pydantic's syntax very much. I wanted to use it in Django Bolt. But naively, I thought its performance was too bad for Django Bolt — and it wasn't. This decision punished me. I had to build a full msgspec-based syntax that has Django model support and that can act as a serialization layer for a web framework. I spent a lot of days working on this, and it is still not finalized. It still needs a lot of work. The performance improvement that it gives was not worth the work it took to build all this. But in the end, it was kind of worth it because even the unoptimized code that I built for serialization still outperforms Pydantic's Rust-powered version for most operations — not all. If I had to choose again, maybe I would still choose msgspec :).

Msgspec is built for pure performance. It has fewer features because of that. For example, it has a fail-fast mode, so it will only tell you one field is wrong. That does not work with a web framework because web frameworks need to tell the user which fields are missing, not just one field. So I had to build on top of it, learning from Litestar.

- Type Safety?

I had to decide between type safety and development productivity. With a serialization layer like Django REST Framework, when you use a model serializer, it creates fields dynamically. When fields are created dynamically, the IDE does not know about them and cannot provide static type safety. So I decided against that, and I created a way to define fields manually. That is more work, but it is more statically type-safe. I decided this way because I think AI is going to write a lot of code, and writing a lot of code will not be a problem — the actual type safety will be the problem.

Another feature I like very much that I built from this learning was having a subset of the parent serializer. In Django REST Framework, you have to define an admin serializer for all the fields and then a normal serializer that is a subset of those fields. What I decided instead was to have one class, and in the config we just define two levels of fields and use one class to have two serializers, instead of defining those two serializers separately. I had to fight a lot of msgspec's design to build this serialization layer, and it is still in development.

{{Code example}}

### Learning
Choose libraries carefully.




## Do Things in Rust

I don't have to tell anybody to rewrite their code in Rust because people are already rewriting their code in Rust. The learning was: I should do the maximum amount of work inside Rust. I moved my form processing to Rust, which gave me a lot of performance boost. I added type coercion in Rust, cookie parsing, header parsing, and other things like that.

### Learning

Do things in Rust.




## Signals

This is one of the problems that is not fully solved yet. Django signals are great and help with a lot of things. But what happens on every request in Django? Django fires a request-started signal and a request-ended signal. Those signals actually clean up old database connections. The amount of time spent on these signals is not very much compared to actual query time, so it is negligible for database-heavy functions. But if you are not querying anything, that overhead becomes significant.

For now, I have added optional signal support so users can enable signals if they want. FastAPI and other libraries handle this using dependency injection, but Django handles database connections at the request level. So that is another overhead. Functions that don't even use database queries still have to pay the price of those signals, because the signal function is called twice on every view. It also has the overhead of the signals engine itself.

When I run without Django signals and just call the functions directly, I get about 17,000 requests per second on one worker. But when I pass it through the Django signals engine, I get about 5,000 to 6,000 requests per second. I have not fully figured out the solution yet, but I think there is some work needed in Django core too.



## What has come out of it?
