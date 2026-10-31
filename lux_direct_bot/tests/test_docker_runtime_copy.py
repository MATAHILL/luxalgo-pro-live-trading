"""Ensure Docker includes all local runtime imports before Render deploy."""
import ast
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_docker_copy_contains_every_local_app_module():
    docker=(ROOT/"Dockerfile").read_text(encoding="utf-8")
    copied=set()
    for line in docker.splitlines():
        if line.startswith("COPY ") and line.endswith(" ./"):
            copied.update(piece for piece in line[5:-3].split() if piece.endswith(".py"))
    assert "app.py" in copied
    checked=set()
    stack=["app.py"]
    while stack:
        name=stack.pop()
        if name in checked:
            continue
        checked.add(name)
        file=ROOT/name
        assert file.is_file(),name
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node,ast.ImportFrom) and node.module:
                local=ROOT/(node.module.split(".")[0]+".py")
                if local.is_file() and local.name not in checked:
                    stack.append(local.name)
    assert checked<=copied, f"Docker missing imported modules: {sorted(checked-copied)}"
    assert {"binance_preflight.py","signal_levels.py"}<=copied
