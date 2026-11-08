# tinyhttp-lite

A small, dependency-free HTTP helper library. It exists so THEMIS has a real project to triage.
It parses requests, builds responses, handles byte ranges and routes paths.

```python
from tinyhttp import Response, Router

router = Router()
router.add("GET", "/ping", lambda request: Response(200, body="pong"))
```
