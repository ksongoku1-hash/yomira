# ADR-0082 — Creación idempotente de jobs desde capítulos seleccionados

## Estado

Accepted — 2026-09-27, decisión de arquitectura adoptada para completar K165/K169 bajo las prioridades de integridad, recuperación y trazabilidad.

## Contexto

La búsqueda y la lista de capítulos del connector son consultas remotas y devuelven referencias, pero la interfaz aún no puede convertir una selección en un `DownloadJob` durable. El modelo del job apunta a `ChapterSource`, de modo que antes de crear el job deben persistirse las relaciones exactas de fuente, serie y capítulo. K110 exige una protección contra jobs duplicados. ADR-0068 también establece que volver a intentar un job terminal crea un `job_id` nuevo y conserva el registro anterior.

## Decisión

- La UI envía un lote limitado de IDs de capítulo seleccionados junto al ID remoto de la serie y el connector allowlisted. El API vuelve a consultar los metadatos y la lista de capítulos al connector; no confía en títulos ni etiquetas presentados por la UI.
- El API valida que todas las referencias seleccionadas correspondan exactamente a capítulos devueltos por el connector antes de abrir una transacción de escritura. Si una referencia no está disponible, se rechaza el lote completo y no se crean fuentes, series, capítulos ni jobs.
- La transacción persiste la relación de fuente, serie y capítulo, y luego inserta los jobs que falten. Un error de persistencia revierte el lote completo. El job queda `QUEUED`; esta ruta no inicia trabajo en memoria ni afirma que haya descargado bytes.
- Una solicitud repetida para el mismo `ChapterSource` devuelve el job existente más reciente si está `COMPLETED` o no es terminal. No modifica su ID, prioridad ni timestamps. Un job `COMPLETED` también representa que el capítulo ya está disponible; una selección repetida no lo vuelve a descargar.
- Si el job más reciente está `FAILED` o `CANCELLED`, una nueva solicitud crea un `job_id` nuevo y conserva el job anterior, conforme a ADR-0068.
- No se colapsan capítulos equivalentes de fuentes diferentes.
- La comprobación de duplicado y la inserción se hacen dentro de `SQLiteDatabase.write_session()`. La exclusión de escritor serializa solicitudes del proceso y el mutex singleton evita dos runtimes de producción sobre la misma base.
- La lista API devuelve una página limitada y orden estable; nunca materializa el historial completo.

## Consecuencias

- K165 puede probar la ruta completa de selección a cola durable y K169 puede verificar Search→Job y su repetición después de volver a leer SQLite.
- El connector actual de fixture enumera referencias, pero no ofrece bytes de página. El worker de descarga, la operación de bytes, la validación, el CBZ y la publicación siguen siendo etapas separadas necesarias para V1; una fila `QUEUED` no se presenta como descarga completada.
- Si el almacenamiento falla, la respuesta no informa éxito y el lote no deja relaciones parciales.
- La lista de jobs y la creación se pueden comprobar sin conectividad de red usando `yomira.mock` en `dev`/`test`.

## Alternativas consideradas

- Crear una fila por cada GET de capítulos: descartado porque convierte una lectura en escritura y puede llenar la biblioteca por navegar.
- Deduplicar por ID de capítulo lógico entre fuentes: descartado porque K167 requiere conservar las fuentes sin fusión automática.
- Impedir duplicados con una restricción global en `download_jobs`: descartado porque los registros terminales deben conservarse y un retry válido de `FAILED`/`CANCELLED` necesita una fila nueva.
- Despachar antes del commit: descartado porque el worker podría observar un job o una identidad relacional que aún no es durable.
