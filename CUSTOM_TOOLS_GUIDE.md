# ZeroGraph Custom Tools Guide

Add your own vulnerability detectors and code-quality checks by:

1. Writing a Scala query template in `zerograph/templates/`.
2. Adding a Python tool function in `zerograph/handlers/extension_handlers.py`.
3. Restarting the server.

The server auto-registers everything in `extension_handlers.py` on start.

---

## Project Structure

```
zerograph/
├── handlers/
│   ├── registry.py              ← Loads extension handlers on start
│   └── extension_handlers.py    ← Your custom MCP tools (edit this)
└── templates/
    ├── command_injection_sinks.scala   ← Example query (bundled)
    └── your_query_name.scala           ← Your new query goes here
```

---

## Step 1 — Write a Query Template

Create `zerograph/templates/your_query_name.scala`.

Every query file is a Scala block.  Template variables use `{{double_braces}}`
and are substituted at runtime by `QueryLoader.load()`.  The result must be
wrapped in `<zerograph_result>` tags so the parser extracts it cleanly.

### Standard template

```scala
{
  import io.shiftleft.codepropertygraph.generated.nodes._
  import io.shiftleft.semanticcpg.language._
  import scala.collection.mutable

  val myPattern  = "{{my_pattern}}"    // string variable — keep the quotes
  val maxResults = {{max_results}}     // numeric variable — no quotes

  val output = new StringBuilder()

  output.append("My Analysis\n")
  output.append("=" * 60 + "\n\n")

  val results = cpg.call
    .name(myPattern)
    .take(maxResults)
    .l

  if (results.isEmpty) {
    output.append("Nothing found.\n")
  } else {
    results.zipWithIndex.foreach { case (c, idx) =>
      output.append(s"--- Result ${idx + 1} ---\n")
      output.append(s"Location: ${c.location.filename}:${c.location.lineNumber.getOrElse(-1)}\n")
      output.append(s"Code:     ${c.code}\n\n")
    }
    output.append(s"Total: ${results.size}\n")
  }

  "<zerograph_result>\n" + output.toString() + "</zerograph_result>"
}
```

### Template variable rules

| Variable kind | Scala declaration | Python call |
|---|---|---|
| String | `val x = "{{x}}"` | `QueryLoader.load("q", x="value")` |
| Integer | `val n = {{n}}` | `QueryLoader.load("q", n=50)` |
| Long (node ID) | `val id = {{id}}L` | `QueryLoader.load("q", id=12345)` |

User-supplied values are sanitised against template injection before
substitution — `{{` inside a value is escaped automatically.

### Filename filtering

Use `{{filename}}` in your query to restrict results to a path substring:

```scala
val filename = "{{filename}}"
val filtered = if (filename.nonEmpty) {
  results.filter(_.location.filename.exists(_.contains(filename)))
} else results
```

This matches `/src/parser.c` but not `/src/myparser.c`.

---

## Step 2 — Register the Python Tool

Add your tool inside `register_extension_handlers()` in `zerograph/handlers/extension_handlers.py`.

### Minimal example

```python
@mcp.tool(description="Find my custom pattern.")
def zg_my_detector(
    codebase_hash: Annotated[str, Field(description="Hash from zg_index_repo")],
    my_pattern: Annotated[str, Field(description="Regex for call names")] = ".*",
    max_results: Annotated[int, Field(ge=1, le=500)] = 50,
) -> str:
    info = _get_codebase(services, codebase_hash)
    query = QueryLoader.load(
        "your_query_name",
        my_pattern=my_pattern,
        max_results=max_results,
    )
    return _run_query(
        services,
        codebase_hash,
        info.cpg_path,
        query,
        tool_name="zg_my_detector",
        cache_params={"my_pattern": my_pattern, "max_results": max_results},
    )
```

### Naming convention

- Tool names: `zg_<verb>_<noun>` (e.g. `zg_find_command_injection`)
- Template file: `zerograph/templates/<query_name>.scala` matching the first argument to `QueryLoader.load()`

---

## Step 3 — Restart and Test

```bash
python server.py
```

Call your tool from any MCP client or:

```bash
python agents/cli.py <repo-url>
```

---

## QueryLoader API

Loads `zerograph/templates/<query_name>.scala`, substitutes every `{{key}}`
with sanitised values, and returns the rendered Scala snippet.

```python
query = QueryLoader.load(
    "your_query_name",       # matches zerograph/templates/your_query_name.scala
    my_pattern="system",
    max_results=25,
)
```

Executes the rendered graph query string, extracts the `<zerograph_result>` content,
and returns plain text for the MCP response.

---

## Bundled example: command injection sinks

See `zerograph/templates/command_injection_sinks.scala` and the
`zg_find_command_injection` tool in `extension_handlers.py`.

---

## Development workflow

1. Draft the Scala template in `zerograph/templates/my_detector.scala`.
2. Add the Python wrapper in `extension_handlers.py`.
3. Index a test repo with `zg_index_repo`.
4. Call your tool; iterate on the template.
5. Commit the template and handler together.

Before committing, render a sample query to a scratch file. Wrap output in
`<zerograph_result>` tags and test against a known vulnerable fixture.

---

## Common pitfalls

### 1. Missing result wrapper

**Every** text query must end with:

```scala
"<zerograph_result>\n" + output.toString() + "</zerograph_result>"
```

Without this, `_run_query()` cannot parse the engine output.

### 2. JSON vs text output

Use a `StringBuilder` + `<zerograph_result>` wrapper to produce readable,
grep-friendly reports.  Avoid `.toJsonPretty` unless you also teach the
Python side to parse JSON.

### 3. Template injection

Never interpolate raw user strings into Scala without going through
`QueryLoader.load()` — it escapes `{{` in values.

### 4. CPG not ready

Always call `_get_codebase()` first; it raises if `zg_index_repo` was not run.

---

## Restarting the engine

```bash
docker compose -f deploy/stack.yaml restart zg-runtime
python server.py
```

---

## Quick reference

| Piece | Location |
|-------|----------|
| Scala templates | `zerograph/templates/` |
| MCP tool registration | `zerograph/handlers/extension_handlers.py` |
| Result marker | `<zerograph_result>…</zerograph_result>` |
| Loader | `zerograph.templates.loader.QueryLoader` |

Add new detectors by dropping a `.scala` file into `templates/` and wiring it up as a dedicated tool.

**Use `<zerograph_result>` wrapping, not `.toJsonPretty`.** Prefer
a `StringBuilder` + `<zerograph_result>` wrapper to produce readable,
structured text for LLM agents and CLI users.
