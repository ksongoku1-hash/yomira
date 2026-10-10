# ADR-0097 — Historial durable y retención acotada de exportaciones USB

## Estado

Accepted — 2026-09-27. La política de retención fue aprobada explícitamente por el usuario el 2026-09-27.

## Contexto

K204 pide historial. La SPEC §7.9 define `ExportJob` como una exportación a dispositivo, y §67 exige registrar la exportación después de verificar y publicar el CBZ. Los logs estructurados no son fuente de verdad. El núcleo K196–K203 devuelve resultados, pero todavía no persiste jobs; tras un corte de proceso no hay un estado durable que distinga una exportación completa de una interrumpida.

La política de logs no se puede reutilizar para historial: ambas tienen distintos propósitos. SQLite necesita una cota explícita y una exportación no debe comenzar si su registro durable no puede guardarse.

## Decisiones

1. SQLite es la fuente de verdad de `ExportJob`. Crear y confirmar el registro antes de cualquier escritura en el dispositivo. Los logs conservan diagnóstico y correlación, pero no sustituyen el job.
2. El contrato persistido incluye: `export_id`, clave idempotente de solicitud, `asset_id`, `target_id`, etiqueta y filesystem acotados del destino, nombre de destino, tamaño y SHA-256 esperados, estado, timestamps UTC en microsegundos, `trace_id` y, al fallar, `error_code`, etapa, reintentabilidad y tipo de causa. No almacenar ruta absoluta de montaje, bytes del CBZ, páginas ni tokens.
3. La máquina de estados usa `QUEUED → EXPORTING → EXPORTED | SKIPPED | FAILED | INTERRUPTED`. `EXPORTED` se confirma solo después del rename sin reemplazo y verificación. `SKIPPED` requiere que origen y destino coincidan con el hash y tamaño esperados.
4. Un `Idempotency-Key` estable distingue un reintento HTTP de una exportación nueva. Repetir la misma clave con el mismo payload devuelve el mismo job; reutilizarla con otro payload se rechaza. La inserción del job y la reserva idempotente ocurren en la misma transacción SQLite.
5. Después de un cierre inesperado, jobs que aún estén `QUEUED` o `EXPORTING` pasan a `INTERRUPTED` en el siguiente arranque. No se reanuda una escritura automáticamente ni se borra `.part`; el usuario puede iniciar una nueva exportación, que vuelve a verificar origen y destino.
6. Retención aprobada: conservar como máximo **1000 jobs terminales o 90 días desde que cada job se hizo terminal, lo que ocurra primero**. Los jobs no terminales nunca se eliminan. Antes de aceptar uno nuevo, una única transacción elimina terminales vencidos y, si se requiere espacio, los terminales más antiguos hasta dejar como máximo 999; esto reserva el cupo que ocupará el job nuevo al terminar. Si la transacción de limpieza/inserción falla, no se escribe en el USB.
7. La API lista como máximo 100 filas por respuesta y usa paginación estable. El límite de página controla memoria/respuesta; la retención controla el crecimiento persistido. No hay endpoint de borrado manual en esta decisión.

## Alternativas consideradas

- Conservar el historial indefinidamente: rechazado por crecimiento no acotado de SQLite.
- Usar 1000 sin límite de edad: rechazado; registros terminales obsoletos podrían ocupar la cuota indefinidamente.
- Usar únicamente 90 días: rechazado; un volumen de exportaciones alto dentro de ese periodo aún dejaría crecimiento sin límite práctico.
- Usar logs como historial: rechazado porque rotan y no participan en la transacción de datos.
- Reanudar automáticamente tras reiniciar o reconectar el USB: rechazado para evitar escrituras inesperadas al destino; se conservan parciales como evidencia recuperable.
- Eliminar un historial cuando no se pueda reservar espacio y aun así copiar: rechazado porque una operación de exportación sin registro durable pierde trazabilidad.

## Consecuencias

- Cada operación de exportación iniciada queda consultable tras reiniciar.
- Una respuesta perdida se puede repetir con idempotencia sin crear otro job ni duplicar efectos.
- Los límites de retención y paginación acotan SQLite y la memoria de lectura. La limpieza solo afecta metadatos terminales, nunca la biblioteca, el CBZ de origen ni temporales `.part`.
- En caso de fallo de SQLite, se rechaza la nueva exportación antes de tocar el dispositivo.
- La decisión no aprueba la API/UI, integración de arranque, exportación física USB/exFAT ni el Export Gate; estos requieren implementación y evidencia propia.

## Verificación requerida

- Confirmar que no empieza una escritura si falla la inserción del job o la limpieza de retención.
- Confirmar idempotencia de la misma clave y conflicto para payload distinto.
- Confirmar transiciones terminales solo después de verificación/publicación; desconexión y cierre inesperado producen `FAILED`/`INTERRUPTED` sin borrar `.part`.
- Confirmar la retención de 1000/90 días en límites exactos y que no elimina jobs activos.
- Confirmar paginación de máximo 100, logs correlacionados sin rutas absolutas, y migración/backup/restore de la nueva tabla.
