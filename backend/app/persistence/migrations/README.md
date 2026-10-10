# Database migrations

Alembic is the only schema writer. The application calls
`app.persistence.migrations.runner.MigrationRunner.upgrade_to_head()` while it
holds the database's exclusive maintenance window. Online migrations require
that application-managed connection; the Alembic configuration contains no
user database path.

Before an upgrade, the runner checks SQLite integrity, verifies that the
recorded Alembic revision agrees with the latest `schema_history` state, and
creates and verifies a SQLite Backup API snapshot. A failed migration is
rolled back from that verified snapshot. The runner then checks database
integrity, the single Alembic head, required tables, and the audit record
before it returns success.

Each revision must be linear, transactional, bounded, and safe for SQLite.
Use Alembic batch operations for table alterations. Do not use
`metadata.create_all()`, `alembic stamp`, or direct application DDL. The
`schema_history` table and its append-only triggers are created by the base
revision; K046 will add its application model and integration.
