# ADR-0052 — Errores tipados de Source Connector

## Status

Accepted for K065 implementation — 2026-09-26.

## Context

El catálogo aprobado ya contiene `SRC_SCHEMA_CHANGED` y `SRC_CIRCUIT_OPEN`, pero `ErrorCode` no los implementa y K060/K065 necesitan clasificar capacidades no soportadas y fallos del connector. Una excepción cruda puede perder código, etapa/operación, correlación o incluir en su mensaje HTML, URLs con tokens y otras entradas remotas.

## Decision

- `ErrorCode` incorpora cuatro códigos de fuente: `SRC_CAPABILITY_UNSUPPORTED`, `SRC_CIRCUIT_OPEN`, `SRC_CONNECTOR_FAILED` y `SRC_SCHEMA_CHANGED`, documentados en `docs/ERROR_CODES.md`.
- `SourceConnectorError` solo acepta esos valores `SRC_*` y una operación del vocabulario cerrado: health check, search, get series/chapters/chapter/pages o normalización.
- La excepción contiene `code`, `operation`, `retryable` y `trace_id`. Reutiliza el trace del contexto activo; si no existe, crea UUIDv4 para que trabajos fuera de HTTP también puedan correlacionar el fallo.
- Sus mensajes son plantillas fijas. No acepta texto arbitrario del connector ni incluye `str(cause)`, cuerpos remotos, URLs o secretos. La excepción original permanece disponible mediante chaining de Python (`raise ... from cause`) para diagnóstico interno.
- Retryability está fijada a `false` para capacidad no soportada y schema cambiado; `true` para circuito abierto, sujeto a cooldown; para `SRC_CONNECTOR_FAILED` es obligatorio clasificarla explícitamente según una causa transitoria segura. El scheduler debe limitar intentos y respetar el cleanup/reconcile de temporales.
- Una excepción no tipada o una causa desconocida no se convierte automáticamente a `SRC_CONNECTOR_FAILED`: permanece un error inesperado (`SYS_INTERNAL_ERROR`) hasta que el owner la clasifique.
- K065 no añade HTTP status ni integración de endpoint sin uso en rutas actuales. La capa API/job que maneje estos errores debe propagar el mismo código y trace, y no comparar mensajes humanos.

## Alternatives Considered

- **Usar `SYS_INTERNAL_ERROR` para cada fallo de connector:** oculta si falló capability, contrato remoto o circuito e impide recuperación específica.
- **Aceptar mensajes arbitrarios en la excepción:** mejora aparente el detalle, pero puede exponer tokens, respuestas remotas o datos personales.
- **Marcar todos los fallos reintentables:** puede repetir errores de parser, causar rate limit o multiplicar estados inciertos.
- **Asignar retryability automática al fallo genérico:** la excepción no conoce si el efecto remoto fue seguro; el owner debe clasificarlo explícitamente.
- **Crear otro catálogo local de strings:** permitiría divergencias entre API, logs, UI y documentación.

## Consequences

- El catálogo Python y el documento aprobado se verifican en conjunto; los códigos nuevos son únicos y documentados.
- La correlación acompaña la excepción tanto en request local como en trabajo background.
- Los componentes consumidores deben registrar `error_code`, operación, retryability y trace sin imprimir el texto de causa o referencias sensibles.
- La respuesta HTTP concreta queda para la integración de la API de búsqueda/connector; K065 no inventa endpoints ni status codes.

## Failure Modes

- Código ajeno a `SRC_` o código sin documentación: la construcción falla.
- Retryability omitida en `SRC_CONNECTOR_FAILED`: falla cerrada; el error no se declara reintentable por defecto.
- Error envuelto sin `raise ... from cause`: se pierde el diagnóstico interno; tests de contrato requieren conservar el cause cuando se clasifica.
- Se registra el cause como texto: puede filtrar tokens o respuesta remota; solo se exponen código, operación y trace.
- El error llega a un handler y se reemplaza por `SYS_INTERNAL_ERROR`: se pierde clasificación; las tareas que lo propaguen deben preservar campos estructurados.

## Reversal Strategy

Retirar o renombrar códigos requiere migrar el catálogo, consumidores y pruebas juntas. Eliminar causas internas, cambiar política retry o redefinir correlación requiere un ADR nuevo y pruebas que verifiquen sanitización y continuidad de trace.
