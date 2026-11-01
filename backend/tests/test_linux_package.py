import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

from alembic.config import Config
from alembic.script import ScriptDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_linux_package_bootstraps_its_own_migrations(tmp_path: Path) -> None:
    root = tmp_path / "source"
    shutil.copytree(
        REPOSITORY_ROOT / "backend/app",
        root / "backend/app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in (
        "backend/alembic.ini", "backend/requirements.lock", "README-LINUX.md",
        "THIRD_PARTY_NOTICES.md", "Instalar-Yomira.sh", "Abrir-Yomira.sh",
        "Cerrar-Yomira.sh", "scripts/run-yomira-linux.py", "scripts/open-yomira-linux.py",
        "scripts/install-linux-desktop.py",
    ):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY_ROOT / name, destination)
    (root / "tools").mkdir()
    runtime = b"\x7fELF fixture; this test runs only Python migrations"
    (root / "tools/uv").write_bytes(runtime)
    (root / "tools/uv-source.json").write_text(
        json.dumps({"binary_sha256": hashlib.sha256(runtime).hexdigest()}), encoding="utf-8"
    )
    frontend = root / "frontend/dist"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html>", encoding="utf-8")
    namespace = runpy.run_path(str(REPOSITORY_ROOT / "scripts/package-linux.py"))
    package = cast(Callable[[], Path], namespace["package"])
    package.__globals__["ROOT"] = root
    output = package()
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(output) as archive:
        archive.extractall(extracted, filter="data")
    backend = next(extracted.iterdir()) / "backend"
    config = Config(str(backend / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    assert Path(scripts.dir).resolve().is_relative_to(backend.resolve())
    head = scripts.get_current_head()
    assert head is not None
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", """
import json
import sys
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
sys.path.insert(0, str(Path.cwd()))
import app
assert Path(app.__file__).resolve().is_relative_to(Path.cwd().resolve())
config = Config(str(Path.cwd() / 'alembic.ini'))
with create_engine('sqlite:///:memory:').connect() as connection:
    config.attributes['connection'] = connection
    command.upgrade(config, 'head')
    print(json.dumps({
        'revision': connection.exec_driver_sql(
            'SELECT version_num FROM alembic_version').scalar_one(),
        'integrity': connection.exec_driver_sql('PRAGMA integrity_check').scalar_one(),
        'foreign_keys': connection.exec_driver_sql('PRAGMA foreign_key_check').all(),
    }))
"""],
        cwd=backend,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"revision": head, "integrity": "ok", "foreign_keys": []}
