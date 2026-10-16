# ADR-0133 — Proteger adquisiciones activas frente a observadores y peticiones nuevas

## Estado

Accepted para H2b.1, 2026-10-11. Completa este requisito de coordinación, no la sesión durable ni la fase H2.

## Problema y alcance

`Fmd2Connector._unsettled_acquisitions` incluía adquisiciones activas con un consumidor esperando. Tanto `connection_available` como una segunda llamada a `acquire_pages` recorrían ese conjunto y cancelaban cualquier tarea todavía no terminal. Un lector progresivo perdería su capítulo al consultar la conexión o admitir otra descarga.

Además, el conjunto se actualizaba después de recibir la confirmación del POST. Si el motor aceptaba el capítulo pero la confirmación se perdía, la cancelación y la recuperación no reconocían esa tarea.

Los consumidores reales son `SourcePageService`, la preparación/reconciliación de planes del worker y su comprobación de conexión. Se mantienen sus contratos, IDs, validación, transporte, staging y publicación de CBZ. No se requiere cambiar endpoints, fuentes, progreso ni frontend para corregir el problema en el puente compartido.

## Decisión

- Serializar `acquire_pages` mediante un lock por instancia del conector, conservando la adquisición nativa de un capítulo a la vez. Una petición en espera no crea una tarea nativa ni cancela a su propietario. Cancelar esa espera tampoco envía DELETE.
- Reservar `_unsettled_acquisitions` exclusivamente para intentos abandonados cuyo estado terminal no se ha confirmado. Las consultas de disponibilidad y conexión no intervienen sobre un propietario activo.
- En errores o cancelaciones después de intentar el POST, registrar el UUID antes de recuperar, incluso si no llegó la confirmación. Usar el mecanismo existente de consulta/cancelación protegido con `asyncio.shield`.
- Serializar también la recuperación de abandonos, para que consultas concurrentes no dupliquen cancelaciones ni borren evidencias antes de terminar la comprobación.
- No admitir otro capítulo mientras quede un abandono sin confirmar. Conservar la petición de cancelación y volver a comprobar en los puntos existentes. Un estado terminal permite continuar; una orden DELETE por sí sola no lo permite.
- Iniciar/verificar el proceso antes de la región que puede dejar un POST ambiguo. Un fallo de arranque sin envío no genera un abandono ficticio. Reutilizar la misma comprobación de arranque desde el cliente HTTP.

## Límites

Esta coordinación es de memoria y protege una sola instancia del puente. No representa una sesión de lector, un propietario durable, una cuota global de caché ni una transferencia de propiedad entre lectura y descarga. Un capítulo ya adquirido conserva el flujo existente de staging/finalización; el lock no autoriza borrar sus imágenes ni garantiza por sí mismo un límite global de archivos ya completados.

Si una solicitud es ambigua y el motor no permite confirmar su estado, la admisión permanece cerrada. Este incremento no inventa una confirmación de terminación ni implementa recuperación durable tras reinicio. Esa recuperación y los intentos identificados por sesión forman parte del resto de H2b.

## Prueba y reversión

Las pruebas de regresión reproducen primero cuatro fallos en el código anterior. Comprueban consulta de conexión durante adquisición, espera y cancelación de un segundo consumidor, cancelación del propietario, pérdida/cancelación de confirmación del POST, arranque fallido, cancelación no confirmada y consultas concurrentes de recuperación.

La aceptación reutiliza el ensayo Windows de H2a y añade una comprobación de conexión, una segunda adquisición en espera y la cancelación de esa espera mientras la primera página está validada y la segunda sigue bloqueada. El motor no crea la tarea en espera, la primera disponibilidad permanece igual y la adquisición original termina normalmente.

La copia `Yomira-H2a-antes-de-H2b.zip` conserva el código previo. Revertir este incremento no requiere migraciones ni modificar biblioteca, historial, secretos o capítulos personales.
