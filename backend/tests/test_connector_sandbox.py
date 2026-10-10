import ast
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path

import pytest

from app.connectors.sandbox import ConnectorHostContext
from app.secrets.vault import WindowsDpapiSecretVault

_FORBIDDEN_IMPORTS = (
    "app.persistence",
    "app.services",
    "sqlite3",
    "sqlalchemy",
    "pathlib",
    "os",
    "shutil",
    "tempfile",
)
_FORBIDDEN_NETWORK_IMPORTS = (
    "httpx",
    "httpcore",
    "requests",
    "aiohttp",
    "urllib3",
    "urllib.request",
    "socket",
    "http.client",
    "websockets",
)
_FORBIDDEN_FILE_METHODS = {
    "mkdir",
    "rename",
    "rmdir",
    "touch",
    "unlink",
    "write_bytes",
    "write_text",
}


def test_connector_host_context_exposes_only_declared_capabilities(
    tmp_path: Path,
) -> None:
    context = ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(tmp_path))

    assert is_dataclass(context)
    assert [field.name for field in fields(context)] == ["secret_vault", "http_client"]
    assert context.secret_vault is not None
    assert context.http_client is None
    for forbidden_attribute in (
        "database",
        "session",
        "repository",
        "data_root",
        "library_root",
        "job_manager",
        "filesystem",
    ):
        assert not hasattr(context, forbidden_attribute)

    database_attribute = "database"
    with pytest.raises((AttributeError, FrozenInstanceError)):
        setattr(context, database_attribute, object())
    secret_vault_attribute = "secret_vault"
    with pytest.raises(FrozenInstanceError):
        setattr(context, secret_vault_attribute, WindowsDpapiSecretVault(tmp_path))


def test_connector_package_has_no_persistence_or_direct_filesystem_capabilities() -> None:
    project_root = Path(__file__).resolve().parents[2]
    package_directory = project_root / "backend" / "app" / "connectors"
    violations: list[str] = []

    for source_path in package_directory.rglob("*.py"):
        syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(syntax_tree):
            if isinstance(node, ast.Import):
                imported_modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules = [node.module]
            else:
                imported_modules = []

            for imported_module in imported_modules:
                if any(
                    imported_module == forbidden
                    or imported_module.startswith(f"{forbidden}.")
                    for forbidden in _FORBIDDEN_IMPORTS
                ):
                    violations.append(f"{source_path.name}: import {imported_module}")

            if isinstance(node, ast.Call):
                function_name = ""
                if isinstance(node.func, ast.Name) and node.func.id in {"open", "remove"}:
                    function_name = node.func.id
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in _FORBIDDEN_FILE_METHODS
                ):
                    function_name = node.func.attr
                if function_name:
                    violations.append(f"{source_path.name}: call {function_name}()")

    assert violations == []


def test_connector_adapters_cannot_bypass_the_scoped_http_client() -> None:
    project_root = Path(__file__).resolve().parents[2]
    package_directory = project_root / "backend" / "app" / "connectors"
    violations: list[str] = []

    for source_path in package_directory.rglob("*.py"):
        if source_path == package_directory / "http_client.py":
            continue
        syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(syntax_tree):
            if isinstance(node, ast.Import):
                imported_modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules = [node.module]
            else:
                imported_modules = []

            for imported_module in imported_modules:
                if any(
                    imported_module == forbidden
                    or imported_module.startswith(f"{forbidden}.")
                    for forbidden in _FORBIDDEN_NETWORK_IMPORTS
                ):
                    violations.append(f"{source_path.name}: import {imported_module}")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "urllib"
                and any(alias.name == "request" for alias in node.names)
            ):
                violations.append(f"{source_path.name}: import urllib.request")

    assert violations == []
