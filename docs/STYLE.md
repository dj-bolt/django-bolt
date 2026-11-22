# Documentation style guide

This guide is for people who write the Django-Bolt docs in `docs/src`.
Read it before you add or change a page.

The docs serve one reader: a Django developer who wants to build an API.
The reader knows Python and Django. The reader does not know Rust, PyO3 or our internals.

## 1. Kinds of page

Each page is one kind. Do not mix kinds on one page.
The kinds follow [Diátaxis](https://diataxis.fr/).

| Kind | Directory | The reader wants to... | The page gives... |
| --- | --- | --- | --- |
| Tutorial | `tutorial/` | learn by doing | one path, start to end, that always works |
| Guide | `topics/` | do a task or understand a feature | the task, then the options, then the edge cases |
| Reference | `ref/` | look up a fact | every argument, type, default and error, in a fixed order |
| Explanation | root (`architecture.md`, `benchmarks.md`) | understand why | the design, the trade-offs, the numbers |

### Tutorial rules

- Build one project from the start to the end. Each step adds to the previous step.
- Each step ends with a command and the exact output the reader sees.
- Do not explain every option. Link to the guide.
- Do not give choices. Give one path.

### Guide rules

- Open with one or two sentences: what the feature does and when to use it.
- Show the most common case first, as a complete example.
- Then go from simple to advanced. Each section has a task as its heading, for example "Validate a JSON body".
- End with "See also" links to the reference page and to related guides.

### Reference rules

- Use the same order on each entry: signature, description, parameters, return value, errors, example.
- List each public argument. Give its type and its default.
- Do not teach. Link to the guide.

## 2. Voice

Write in [ASD-STE100 Simplified Technical English](https://www.asd-ste100.org/), as `CLAUDE.md` asks.

- Use short sentences. An instruction has 20 words or fewer. A description has 25 words or fewer.
- Put one instruction in one sentence.
- Use the active voice and simple tenses.
- Start an instruction with a verb: "Add `django_bolt` to `INSTALLED_APPS`."
- Write "you" for the reader. Do not write "we" for the project, except on the benchmark and comparison pages.
- Use one word for one meaning. A "handler" is the function that serves a route. Do not also call it a "view function" or an "endpoint function".
- Do not use more than three nouns in a row.

## 3. Words and patterns to remove

These words and patterns make text longer and say nothing. Delete them or replace them with a fact.

| Do not write | Write |
| --- | --- |
| simply, just, easily, quickly, seamlessly, effortlessly | (delete the word) |
| powerful, robust, blazing, lightning-fast, cutting-edge, modern, elegant | a number or a fact |
| leverage, utilize | use |
| comprehensive, full-featured | (list the features) |
| "It's important to note that", "Note that", "Please note" | (delete; state the fact) |
| "Let's", "Now we will", "In this section, you will learn" | (delete; start with the content) |
| "under the hood", "out of the box", "magic" | the mechanism, in one sentence |
| "This is the headline", "Here's the thing", "the best part" | (delete) |
| exclamation marks | a period |
| emoji, ✅ ❌ in tables | "Yes", "No", or a short fact |
| a bold phrase in every paragraph | bold only for a term at its definition, or for a warning |
| three adjectives in a row ("fast, simple and flexible") | one fact |
| a summary at the end that repeats the page | "See also" links |

Also do not:

- Pre-announce content. Do not write "This page explains X." The title says that.
- Make a claim without proof. "Fast" needs a number and a link to [Benchmarks](src/benchmarks.md).
- Use an em dash (—) to join two thoughts. Use two sentences.

## 4. Code examples

The code examples are the most important part of a page. Readers copy them.

- Each example is complete. A reader can paste it into `api.py` and run it.
- Each example runs. Run it with `TestClient` before you commit it.
- Show the imports in the first example of a page. Later examples on the same page can omit imports that the page already showed.
- Give a block a file name when the file matters: ` ```python title="myapp/api.py" `.
- Show the request and the response next to the handler:

  ```bash
  curl localhost:8000/items/3
  ```

  ```json
  {"id": 3, "name": "Lamp"}
  ```

- Use real names: `Article`, `Author`, `Order`. Do not use `foo`, `bar` or themed names, for example space missions.
- Put the line `<!-- fragment -->` before a block that is not complete code, for example a signature. `just docs-check` skips such a block.
- Use type hints in each handler.
- Use `async def` for a handler, except when the example shows a sync handler.

`just docs-check` parses each Python block and checks that each import from `django_bolt` and `bolt_mcp` exists.
It does not run the code. You run it.

## 5. Page format

Each page starts with front matter that has a `description`. Search engines show it.

```markdown
---
description: Read path, query, header, cookie, form and body parameters from a typed handler signature.
---

# Requests

Bolt reads the arguments of a handler from the request. The type hint of each argument sets the source and the type.
```

- Use one `#` heading: the page title. Use sentence case: "Class-based views", not "Class-Based Views".
- Use `##` for tasks and `###` for sub-tasks. Do not go deeper than `###`.
- Use an admonition only for something the reader must not miss:
  - `!!! warning` for data loss, a security risk or a breaking change.
  - `!!! note` for a fact that is easy to miss.
  - Use not more than two admonitions on a page.
- Use a table for a list of options with the same fields. Use a list for anything else.
- Link to a page with a relative path to the `.md` file: `[Routing](routing.md)`.

## 6. Facts

- Each name, argument, default and status code on a page must match the source in `python/django_bolt/` or `crates/`.
- Supported versions come from the `pyproject.toml` classifiers. Do not copy them by hand into other pages. Link to [Installation](src/intro/install.md).
- A benchmark number always has its conditions: machine, processes, concurrency, date. It comes from `python/benchmark/BENCHMARK.md` or from the cross-framework harness.
- Do not document internals that a user cannot use or change. Put them in `architecture.md`, or leave them out.
