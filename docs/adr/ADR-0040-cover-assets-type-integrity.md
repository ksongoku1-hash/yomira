# ADR-0040 — Integridad del tipo de Asset usado como portada

## Status

Accepted — decisión de implementación de K051, 2026-09-26.

## Context

K051 define las portadas como Assets. K042 ya guarda `Series.cover_asset_id` como FK `RESTRICT` a `assets.asset_id`, pero una FK solo verifica que el ID exista: permite enlazar `THUMBNAIL`, `CBZ` u otro tipo, y permite cambiar el tipo de un Asset ya usado como portada. Esto hace que una referencia válida por FK no represente necesariamente una portada.

## Decision

- Una serie puede tener `cover_asset_id = NULL` o una referencia a un Asset cuyo `asset_type` sea `COVER`.
- SQLite lo exige con triggers en INSERT/UPDATE de `series.cover_asset_id` y UPDATE de `assets.asset_type`; la regla no depende solo de que todos los callers usen `LibraryService`.
- La migración 0012 inspecciona las asociaciones existentes antes de crear triggers. Si encuentra una referencia no `COVER`, falla; `MigrationRunner` conserva y restaura el backup, sin borrar ni relabelar filas.
- K051 no abre, valida, crea, mueve ni elimina archivos. `verified_at` continúa siendo metadata del Asset; validación de bytes y generación de miniaturas corresponden a servicios posteriores.
- El FK `ON DELETE RESTRICT` existente sigue impidiendo eliminar un Asset referenciado.

## Alternatives Considered

- Confiar solo en la FK: comprueba existencia, no el tipo semántico del Asset.
- Validar solo en la capa de aplicación: callers SQL autorizados podrían escribir una referencia incorrecta.
- Convertir automáticamente Assets incorrectos a COVER o borrar la referencia: cambia metadata o pierde la asociación sin evidencia.
- Verificar archivo y hash al guardar el vínculo: mezcla filesystem e invariantes relacionales, además de duplicar el validador de imágenes.

## Positive Consequences

- Toda referencia de portada válida en SQLite representa un `COVER`, incluso cuando una escritura no usa el repositorio.
- Los datos heredados inválidos bloquean upgrade normal y se conservan mediante backup.
- K051 permanece independiente de filesystem y thumbnailing.

## Negative Consequences

- Las escrituras que violan el tipo fallan con una restricción SQLite y requieren que la capa de aplicación las traduzca a error de dominio/API.
- El estado `verified_at` no garantiza que el archivo siga presente o inalterado; K052 y los servicios filesystem deben comprobarlo antes de servirlo.

## Failure Modes

- Una migración elimina los triggers o cambia sus condiciones sin prueba de FK y tipo.
- El preflight restaura una DB antigua pero permite el arranque normal pese a la asociación incorrecta.
- El UI sirve la ruta de un Asset sin comprobar existencia, raíz permitida o contenido.

## Reversal Strategy

Quitar el enforcement exige ADR y migración reversible. Antes, revisar y reportar cada vínculo, preservar backup, acordar la semántica alternativa y probar upgrade/rollback e integridad completa.
