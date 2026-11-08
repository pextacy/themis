"""Golden rules that can be checked by reading the source."""

import ast
from pathlib import Path

SRC = Path("src/themis")
PROVIDER_SDKS = {"openai", "anthropic", "together", "groq", "boto3", "mistralai", "cohere"}


def imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_no_provider_sdk_anywhere() -> None:
    for path in SRC.rglob("*.py"):
        assert not imports(path) & PROVIDER_SDKS, path


def test_only_the_client_builds_http_requests() -> None:
    allowed = {
        SRC / "otari" / "client.py",
        SRC / "doctor" / "runner.py",
        SRC / "intake" / "github.py",
        SRC / "evidence" / "dependencies.py",
        SRC / "evidence" / "registries.py",
        SRC / "intake" / "forgejo.py",
    }
    for path in SRC.rglob("*.py"):
        if path not in allowed:
            assert "httpx" not in imports(path), path
    assert "httpx.AsyncClient" not in (SRC / "doctor" / "runner.py").read_text()


def test_no_module_is_too_long() -> None:
    for path in SRC.rglob("*.py"):
        assert len(path.read_text().splitlines()) <= 400, path


def test_nothing_can_close_or_merge() -> None:
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        assert '"state": "closed"' not in text and "/merge" not in text, path


def test_github_intake_never_talks_to_otari() -> None:
    text = (SRC / "intake" / "github.py").read_text()
    assert "otari.ai" not in text and "OtariClient" not in text


def test_submission_text_never_reaches_a_system_prompt() -> None:
    for name in ("agents/clerk.py", "shield/guardrails.py"):
        tree = ast.parse((SRC / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                pairs = dict(zip(node.keys, node.values, strict=True))
                role = next((v for k, v in pairs.items() if isinstance(k, ast.Constant) and k.value == "role"), None)
                if isinstance(role, ast.Constant) and role.value == "system":
                    content = next(v for k, v in pairs.items() if isinstance(k, ast.Constant) and k.value == "content")
                    assert isinstance(content, ast.Name) and content.id in ("system", "SYSTEM"), name
