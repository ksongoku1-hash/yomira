# ADR-0062 — Desconexión determinista a mitad de página

## Status

Accepted for K075 implementation — 2026-09-26.

## Context

La especificación §83 requiere una caída de conexión a mitad de contenido. Para validar integridad, el fixture debe distinguir un HTTP error antes del body de un response que anuncia una imagen completa pero entrega solo un prefijo. El test debe comprobar lo que observa el cliente HTTP, no simularlo lanzando una excepción local.

## Decision

- El escenario `DISCONNECT` mantiene normales los endpoints de búsqueda y metadata, y afecta solo a `/api/v1/pages/mock-chapter-1/page-1`.
- El server envía HTTP 200, `Content-Type: image/png` y el `Content-Length` del PNG completo, luego escribe exactamente la mitad del cuerpo válido y cierra el lado de escritura de la conexión.
- El cliente de prueba debe observar `http.client.IncompleteRead` con el prefijo recibido y el número restante. Un body completo en HTTP 200 no satisface el escenario.
- El handler libera su worker mediante `finally`. El contador debe volver a cero y `/health` debe responder después de la lectura incompleta.
- No se guarda el prefijo parcial en disco; la futura capa de descarga será responsable de escribir a `.part`, borrar o conservar recovery state según su contrato y nunca promover el archivo incompleto.

## Alternatives Considered

- **Lanzar `IncompleteRead` directamente en el cliente:** no prueba transporte ni comportamiento del parser HTTP.
- **Cerrar la conexión antes de headers:** reproduce una desconexión temprana, no la pérdida a mitad de página.
- **Enviar un HTTP status error:** no reproduce body truncado, que requiere su propia ruta de recuperación.
- **Enviar bytes parciales con Content-Length igual a lo enviado:** hace que HTTP considere la respuesta completa y oculta corrupción por truncamiento.

## Consequences

- El test valida headers y bytes recibidos en una transferencia real de loopback.
- La interrupción es determinista y corta; no representa problemas de conectividad de larga duración ni fallos de Windows suspend/resume.
- El Mock Server puede seguir respondiendo mientras el test confirma limpieza y recuperación.

## Failure Modes

- Longitud anunciada igual al prefijo: el cliente no detecta body incompleto y el test falla.
- Bytes recibidos no son exactamente la mitad: escenario perdió determinismo.
- Worker queda retenido o health deja de responder: recuperación del fixture falla.

## Reversal Strategy

Cambiar ruta, posición de corte, status, headers o semántica de cleanup exige actualizar los contract tests y preservar el escenario como caso HTTP truncado distinto de timeout y status error.
