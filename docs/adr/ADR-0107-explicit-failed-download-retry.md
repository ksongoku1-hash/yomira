# ADR-0107 — Reintento explícito de descargas fallidas

- Estado: ACCEPTED para el alcance autorizado por el usuario: «solucionalo aunque tengas que cambiar el plan principal».
- Fecha: 2026-10-01 (America/Bogota).
- Autoridad: integridad y trazabilidad de la SPEC; ADR-0082, ADR-0083, ADR-0089, ADR-0104 y ADR-0105.

## Contexto y causa comprobada

El trabajo `53f5f150-a802-404f-bace-652524056e0d` de ZonaTMO falló al recibir HTTP 403 en la página 10 de 22. Sus nueve temporales existentes conservan tamaño y SHA-256 válidos. Una petición posterior a la misma imagen, con HTTPX y los encabezados originales, recibió HTTP 200 `image/webp`. No se identifica la causa del rechazo remoto a partir de estas dos respuestas.

FAILED es un estado terminal. La reselección del capítulo ya permite crear otra identidad de trabajo, pero Descargas carecía de una acción directa para hacerlo. La ausencia de esa acción se reproduce con pruebas que fallan por ruta y botón inexistentes.

## Decisión

1. Se añade `POST /api/v1/downloads/{job_id}/retry` y el botón «Reintentar descarga» para FAILED. Se reutiliza la creación idempotente existente; no se reabre el estado terminal del trabajo original.
2. El intento recibe otro UUID y conserva capítulo, fuente y prioridad. El trabajo previo, sus páginas, intentos, hashes, manifest y archivos permanecen intactos. El nuevo trabajo obtiene referencias actuales y vuelve a descargar todas las páginas; esta acción no promete reanudar los nueve temporales históricos.
3. La transacción de escritura existente serializa la selección/creación. Peticiones simultáneas sobre el mismo fallo reutilizan el trabajo activo o COMPLETED posterior. Se rechazan estados distintos de FAILED, intención de cancelación, fuente deshabilitada, capítulo no disponible y una cancelación posterior. Un fallo posterior requiere otra acción explícita del usuario.
4. La UI deshabilita la acción mientras espera respuesta, muestra errores con código y trace_id e invalida el listado después de una respuesta válida. El job fallido sigue visible. El servidor notifica al dispatcher solo después de confirmar la transacción.
5. Se conservan el transporte normal, los orígenes exactos, la separación de URLs firmadas en memoria, límites de recursos y el rechazo de 403. No se añaden retries automáticos de 403 ni modificaciones de sesión, cookies o encabezados para sortear un rechazo.

## Verificación

RED → causa raíz de la acción ausente → implementación → GREEN → regresión: API concurrente/idempotente, rechazo de estados y cancelación, conservación de evidencia, UI explícita y errores trazables; regresión de transporte/conectores/worker, análisis estático y build. El cierre operativo requiere un reintento del capítulo por Yomira con CBZ íntegro e indexado. La respuesta 200 aislada no basta para declarar éxito del capítulo.

## Ajuste del plan

Se añade este seguimiento de la Alpha al plan maestro, sin renumerar K001–K284 ni alterar las reglas de ejecución, commits, quality gates o criterios finales. Su resultado se registra en `docs/reports/FAILED_DOWNLOAD_RETRY_ALPHA_2026-10-01.md`.
