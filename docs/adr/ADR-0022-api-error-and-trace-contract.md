# ADR-0022 — Contrato uniforme de errores y correlación HTTP

## Status

Accepted — decisión de implementación de Fase 3, registrada antes de K027 y K028.

## Context

El contrato aprobado exige errores con código estable, mensaje, trace_id y política de retry. K027 requiere una respuesta uniforme también para rutas y métodos rechazados; K028 necesita definir cómo correlacionar solicitudes entrantes, respuestas, errores y logs. El contrato previo no especificaba un código para rechazos HTTP generales ni la cabecera de correlación.

## Decision

- Añadir API_REQUEST_REJECTED para respuestas HTTP controladas 4xx que no sean errores de validación de schema. Conservar el status HTTP, usar retryable=false y un mensaje genérico sanitizado.
- Las validaciones de request devuelven status 422 y API_VALIDATION_FAILED.
- Un error de validación puede incluir details, con un máximo de 20 elementos {field, issue}. field contiene una ruta de schema sanitizada; issue contiene el tipo estable de error. Ambos tienen longitud acotada. No se incluyen valores, body, input ni el mensaje arbitrario del validador.
- Errores inesperados devuelven status 500, SYS_INTERNAL_ERROR, mensaje genérico y retryable=false. La respuesta nunca incluye stack trace ni texto de excepción.
- La cabecera de entrada y respuesta es X-Trace-ID. Solo se acepta un UUID canónico como correlación entrante. Si falta o no es válido, el backend genera un UUIDv4. El mismo valor se usa en logs, cabecera de respuesta y cuerpo de error.
- El cliente no usa valores de details para tomar decisiones; compara el código estable y muestra el mensaje.

## Alternatives Considered

- Reutilizar API_VALIDATION_FAILED para 404/405: atribuye incorrectamente el fallo a un schema.
- Devolver texto o detalle de excepción del framework: puede filtrar datos del request o implementación interna.
- Aceptar cualquier cadena en X-Trace-ID: permite formatos ambiguos y valores de cabecera no seguros.
- Incluir valores de entrada en detalles de validación: facilita diagnóstico, pero puede exponer secretos o datos privados.

## Positive Consequences

- Los clientes reciben un formato predecible para errores 4xx y 5xx.
- Un mismo UUID correlaciona request, error y evento de log.
- La información de validación ayuda a ubicar un campo sin reflejar el valor enviado.

## Negative Consequences

- Algunos errores deliberadamente no explican detalles internos.
- Un identificador entrante no canónico se reemplaza, por lo que el caller debe usar la cabecera de respuesta.

## Failure Modes

- La ruta de error usa un trace_id distinto al de la cabecera.
- La sanitización refleja un valor del request en details.
- Un handler deja escapar el texto o stack de una excepción al cliente.

## Reversal Strategy

Todo cambio a códigos, status, forma o límites de error requiere nueva versión del contrato, actualización del cliente y pruebas de compatibilidad antes de retirar esta política.
