# Revisión de dependencias backend

**Fecha:** 2026-09-26
**Trazabilidad:** ADR-0002, ADR-0019, ADR-0025, ADR-0041, K034, K036, K052
**Plataforma medida:** Windows x64, CPython 3.14.7

| Paquete | Tipo | Versión fijada | Licencia declarada | Wheel Windows CPython 3.14 | Motivo |
|---|---|---:|---|---:|---|
| SQLAlchemy | Directa | 2.0.54 | MIT | 2,169,220 bytes | Engine, sesiones y mapeo de persistencia requeridos por ADR-0002; rama 2.0 para adopción conservadora. |
| greenlet | Transitiva de SQLAlchemy | 3.5.6 | MIT AND PSF-2.0 | 327,473 bytes | Requisito publicado por SQLAlchemy; el código Yomira usa su engine síncrono y no llama greenlets directamente. |
| Alembic | Directa | 1.20.0 | MIT | 268,719 bytes | Migraciones versionadas obligatorias; el entorno usa la conexión acotada de Yomira y una copia pre-migración verificada. |
| Mako | Transitiva de Alembic | 1.4.3 | MIT | 80,164 bytes | Renderiza las plantillas de revisiones Alembic; no se usa para contenido de usuario. |
| MarkupSafe | Transitiva de Mako | 3.0.3 | BSD-3-Clause | 15,341 bytes | Dependencia de Mako para escape seguro de texto de plantilla. |
| Pillow | Directa | 12.3.0 | MIT-CMU | 7.2 MB | Stack de imágenes aprobado por ADR-0002; K052 genera WebP sin pérdida para miniaturas acotadas. Rueda oficial Windows x64 para CPython 3.14. |

El peso combinado de SQLAlchemy y greenlet es 2,496,693 bytes (2.38 MiB). Alembic y sus dos dependencias nuevas suman 364,224 bytes (0.35 MiB); el conjunto medido suma 2,860,917 bytes (2.73 MiB). Los hashes SHA-256 de los wheels revisados son:

- SQLAlchemy: `f4e8f955d13af83fb4e35c3472e5377ee22d3445eada1e5e48199588edb69835`.
- greenlet: `f8f0bd690e1a41294ac87905e8121c81a3761ec2583c768f13467428606c8c7a`.
- Alembic: `77eb101048d95f982c0353e9233404889dcd7a6fc244c107836c0e2fc9cf7d9d`.
- Mako: `723296007c870bfd6b3f0c3230dba7198096e5269297ebf5e4eff9e7ffa39d4f`.
- MarkupSafe: `bdc919ead48f234740ad807933cdf545180bfbe9342c2bb451556db2ed958581`.

La alternativa de usar solo `sqlite3` estándar reduce tamaño, pero contradice el stack SQLAlchemy aprobado y obligaría a implementar a mano el mapeo y las sesiones. No se añade driver async; la base síncrona evita otra capa de conexiones para la DB local.

`pip-audit 2.10.1` revisó todo `backend/requirements.lock`, incluido Alembic, el 2026-09-25 y no encontró vulnerabilidades conocidas en las versiones fijadas. Este resultado describe la base de avisos consultada durante esa ejecución, no reemplaza la auditoría de releases posteriores.

Actualización K052 (2026-09-26): `pip-audit 2.10.1` volvió a revisar todos los pines exactos de `backend/requirements.lock`, incluido Pillow 12.3.0, con `--no-deps --disable-pip`; no encontró vulnerabilidades conocidas. Este resultado puntual usa el lock completo y la base de avisos disponible durante la revisión.

Fuentes de licencia y mantenimiento:

- [SQLAlchemy en PyPI](https://pypi.org/project/SQLAlchemy/2.0.54/)
- [greenlet en PyPI](https://pypi.org/project/greenlet/3.5.6/)
- [Alembic 1.20.0 en PyPI](https://pypi.org/project/alembic/1.20.0/)
- [Mako 1.4.3 en PyPI](https://pypi.org/project/Mako/1.4.3/)
- [MarkupSafe 3.0.3 en PyPI](https://pypi.org/project/MarkupSafe/3.0.3/)
- [Pillow 12.3.0 en PyPI](https://pypi.org/project/pillow/12.3.0/)
- [Pillow 12.3.0 — release notes oficiales](https://github.com/python-pillow/Pillow/blob/main/docs/releasenotes/12.3.0.rst)
- [SQLAlchemy — dialecto SQLite](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html)
- [Alembic — Migration Environment](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
- [Alembic — SQLite batch migrations](https://alembic.sqlalchemy.org/en/latest/batch.html)
