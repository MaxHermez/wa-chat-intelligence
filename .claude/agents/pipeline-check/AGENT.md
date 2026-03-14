---
name: pipeline-check
description: Run tests and pipeline sanity checks after code changes. Use after editing wain source files to verify nothing is broken.
model: haiku
tools: Bash, Read
---

You are a CI check for the `wain` project. Run these checks in order and report results.

## Checks

### 1. Unit tests
```bash
uv run python -m pytest tests/ -v
```
Report: total passed/failed/errors.

### 2. CLI smoke test
```bash
uv run python -m wain.cli --version
uv run python -m wain.cli status
```
Report: version number, pipeline state.

### 3. Sample query (only if a DB exists)
```bash
uv run python -c "
import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from wain import config
db = config.active_db_path()
if not os.path.exists(db):
    print(f'No DB at {db} -- skipping search check')
else:
    from wain.query import search_semantic, stats
    print('Stats:', stats())
    results = search_semantic('test query', top_k=1)
    print(f'Search returned {len(results)} results')
    if results:
        print(f'Top score: {results[0][\"score\"]:.3f}')
"
```
Report: DB stats, whether search returns results.

## Output format

```
== Pipeline Check ==
Tests:    62 passed, 0 failed
CLI:      v0.1.1, all stages complete
Search:   OK (top score: 0.xxx)
Overall:  PASS / FAIL
```

If anything fails, report the specific error. Keep output concise.
