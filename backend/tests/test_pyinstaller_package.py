import os
import runpy
from pathlib import Path

import pytest

from package_config import PackageInputError, discover_package_inputs

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_package_input_plan_contains_frontend_under_app_web() -> None:
    plan = discover_package_inputs(REPOSITORY_ROOT)

    frontend = next(item for item in plan if item.destination == "app/web")
    assert frontend.source == REPOSITORY_ROOT / "frontend" / "dist"
    assert frontend.source.is_dir()
    assert (frontend.source / "index.html").is_file()
    assert (frontend.source / "assets").is_dir()


def test_package_input_plan_contains_alembic_runtime_files() -> None:
    plan = discover_package_inputs(REPOSITORY_ROOT)

    config = next(item for item in plan if item.destination == ".")
    migrations = next(
        item for item in plan if item.destination == "app/persistence/migrations"
    )
    assert config.source == REPOSITORY_ROOT / "backend" / "alembic.ini"
    assert migrations.source == (
        REPOSITORY_ROOT / "backend" / "app" / "persistence" / "migrations"
    )
    assert (migrations.source / "env.py").is_file()
    assert list((migrations.source / "versions").glob("*.py"))


def test_package_input_plan_contains_only_explicit_application_resources() -> None:
    plan = discover_package_inputs(REPOSITORY_ROOT)

    destinations = {item.destination for item in plan}
    assert destinations == {".", "app/persistence/migrations", "app/web"}
    assert all(item.source.is_absolute() for item in plan)


def test_package_input_discovery_fails_closed_without_vite_build(
    tmp_path: Path,
) -> None:
    backend = tmp_path / "backend"
    migrations = backend / "app" / "persistence" / "migrations"
    (migrations / "versions").mkdir(parents=True)
    (migrations / "env.py").write_text("", encoding="utf-8")
    (migrations / "versions" / "0001_initial.py").write_text("", encoding="utf-8")
    (backend / "alembic.ini").write_text("", encoding="utf-8")

    with pytest.raises(PackageInputError, match="frontend build"):
        discover_package_inputs(tmp_path)


def test_package_input_discovery_fails_closed_for_incomplete_migrations(
    tmp_path: Path,
) -> None:
    frontend = tmp_path / "frontend" / "dist"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html>", encoding="utf-8")
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "alembic.ini").write_text("", encoding="utf-8")
    migrations = backend / "app" / "persistence" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "env.py").write_text("", encoding="utf-8")

    with pytest.raises(PackageInputError, match="migration revisions"):
        discover_package_inputs(tmp_path)


def test_package_spec_declares_onedir_executable_and_production_hook() -> None:
    spec = (REPOSITORY_ROOT / "backend" / "yomira.spec").read_text(
        encoding="utf-8"
    )

    assert 'backend_root / "app" / "main.py"' in spec
    assert "name=\"Yomira\"" in spec
    assert "exclude_binaries=True" in spec
    assert "COLLECT(" in spec
    assert "runtime_hooks" in spec
    assert "set_production_mode.py" in spec
    assert "datas=file_datas" in spec
    assert "a.datas += Tree(" in spec
    assert "\n        datas += Tree(" not in spec


@pytest.mark.parametrize("existing_value", [None, ""])
def test_packaged_runtime_hook_uses_production_for_missing_or_blank_environment(
    monkeypatch: pytest.MonkeyPatch,
    existing_value: str | None,
) -> None:
    if existing_value is None:
        monkeypatch.delenv("YOMIRA_APP_ENV", raising=False)
    else:
        monkeypatch.setenv("YOMIRA_APP_ENV", existing_value)

    runpy.run_path(
        str(REPOSITORY_ROOT / "backend" / "packaging" / "set_production_mode.py")
    )

    assert os.environ["YOMIRA_APP_ENV"] == "prod"


def test_packaged_runtime_hook_keeps_explicit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOMIRA_APP_ENV", "dev")

    runpy.run_path(
        str(REPOSITORY_ROOT / "backend" / "packaging" / "set_production_mode.py")
    )

    assert os.environ["YOMIRA_APP_ENV"] == "dev"


def test_package_toolchain_pins_match_the_resolved_lock() -> None:
    direct_requirements = (
        REPOSITORY_ROOT / "backend" / "requirements-package.in"
    ).read_text(encoding="utf-8")
    locked_requirements = (
        REPOSITORY_ROOT / "backend" / "requirements-package.lock"
    ).read_text(encoding="utf-8")

    assert "-r requirements.lock" in direct_requirements
    assert "-r requirements.lock" in locked_requirements
    direct_pin = next(
        line for line in direct_requirements.splitlines() if line.startswith("PyInstaller==")
    )
    assert direct_pin.lower() in locked_requirements.lower().splitlines()
