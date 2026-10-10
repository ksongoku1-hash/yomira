import ast
from pathlib import Path

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from app.persistence.base import MODEL_NAMING_CONVENTION, PersistenceBase

BACKEND_ROOT = Path(__file__).parents[1]


def test_persistence_base_uses_shared_metadata_and_stable_constraint_names() -> None:
    assert issubclass(PersistenceBase, DeclarativeBase)
    assert isinstance(PersistenceBase.metadata, MetaData)
    assert PersistenceBase.metadata.naming_convention == MODEL_NAMING_CONVENTION
    assert MODEL_NAMING_CONVENTION == {
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }


def test_domain_modules_do_not_import_persistence_or_external_adapters() -> None:
    domain_root = BACKEND_ROOT / "app" / "domain"
    domain_modules = sorted(domain_root.rglob("*.py"))
    assert domain_modules

    for module_path in domain_modules:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules = [node.module]

            for imported_module in imported_modules:
                assert not imported_module.startswith("sqlalchemy")
                assert not imported_module.startswith("app.persistence")
                assert imported_module not in {
                    "fastapi",
                    "httpx",
                    "sqlite3",
                }
