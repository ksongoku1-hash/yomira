import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.factory as factory
import app.frontend_distribution as frontend_distribution
from app.settings import Settings


def _write_frontend(directory: Path) -> None:
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text(
        "<!doctype html><html><body>yomira test shell</body></html>",
        encoding="utf-8",
    )
    (directory / "assets" / "app.js").write_text("globalThis.yomira = true;", encoding="utf-8")


def test_resolves_packaged_web_resources_under_pyinstaller_meipass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_root = tmp_path / "_MEI12345"
    expected = bundle_root / "app" / "web"
    _write_frontend(expected)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_root), raising=False)

    assert frontend_distribution.resolve_frontend_directory() == expected


def test_resolves_vite_dist_in_source_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    module_path = repository / "backend" / "app" / "frontend_distribution.py"
    expected = repository / "frontend" / "dist"
    _write_frontend(expected)
    monkeypatch.setattr(frontend_distribution, "__file__", str(module_path))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert frontend_distribution.resolve_frontend_directory() == expected


def test_production_serves_spa_and_built_assets_without_capturing_api_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend_root = tmp_path / "web"
    _write_frontend(frontend_root)
    monkeypatch.setattr(factory, "resolve_frontend_directory", lambda: frontend_root)
    app = factory.create_app(
        Settings(app_env="prod", data_root=tmp_path / "data"),
    )
    client: Any = TestClient(app, base_url="http://127.0.0.1")

    root = client.get("/")
    nested_route = client.get("/reader/7b792af2-679f-4a57-98c6-cbdd402506c7")
    script = client.get("/assets/app.js")
    liveness = client.get("/api/v1/system/liveness")
    missing_api = client.get("/api/v1/route-that-does-not-exist")
    missing_asset = client.get("/assets/not-built.js")

    assert root.status_code == 200
    assert "yomira test shell" in root.text
    assert nested_route.status_code == 200
    assert "yomira test shell" in nested_route.text
    assert script.status_code == 200
    assert script.text == "globalThis.yomira = true;"
    assert liveness.status_code == 200
    assert liveness.json() == {"status": "alive"}
    assert missing_api.status_code == 404
    assert "yomira test shell" not in missing_api.text
    assert missing_asset.status_code == 404


def test_production_fails_closed_when_built_frontend_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(factory, "resolve_frontend_directory", lambda: None)

    with pytest.raises(frontend_distribution.FrontendDistributionError):
        factory.create_app(Settings(app_env="prod", data_root=tmp_path / "data"))


def test_development_keeps_frontend_on_vite_and_does_not_add_spa_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend_root = tmp_path / "web"
    _write_frontend(frontend_root)
    monkeypatch.setattr(factory, "resolve_frontend_directory", lambda: frontend_root)
    app = factory.create_app(Settings(app_env="dev", data_root=tmp_path / "data"))

    client: Any = TestClient(app, base_url="http://127.0.0.1")
    response = client.get("/reader/chapter-id")

    assert response.status_code == 404
    assert "yomira test shell" not in response.text
