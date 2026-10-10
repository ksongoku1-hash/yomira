# ADR-0047 — Metadata y capacidades declaradas por Source Connector

## Status

Accepted for K060 implementation — 2026-09-26.

## Context

K060 necesita formalizar el ID, la versión y las capacidades de cada implementación de connector. `Source.connector_id` y `Source.connector_version` ya son texto opaco, requerido y no vacío según ADR-0029. La especificación enumera cinco capacidades (`SEARCH`, `METADATA`, `CHAPTER_LIST`, `PAGE_LIST`, `UPDATE_CHECK`), pero no define la gramática de identidad ni la operación que materializa `UPDATE_CHECK`.

## Decision

- `ConnectorMetadata` será un valor inmutable con los campos `connector_id`, `connector_version` y `capabilities`.
- `SourceConnector` publica esa metadata mediante una propiedad de solo lectura `metadata`; cada implementación declara su valor sin añadir configuración mutable al contrato.
- ID y versión deben ser strings no vacíos; se conservan exactamente. No se recortan, normalizan, parsean como SemVer ni se hacen únicos.
- `ConnectorCapability` es un vocabulario cerrado con exactamente los cinco valores de la especificación. Valores desconocidos se rechazan.
- `capabilities` debe ser un `frozenset` de `ConnectorCapability`. Un conjunto vacío es válido: un connector puede declarar que no ofrece operaciones opcionales más allá de su health check.
- La metadata describe la implementación del connector; una instancia `Source` continúa siendo la configuración local individual y conserva sus propios `connector_id` y `connector_version`. K060 no añade columnas ni cambia la identidad persistida.
- `UPDATE_CHECK` se puede declarar, pero esta tarea no añade ni infiere una operación de protocolo para ejecutarlo. La especificación no indica su firma o resultado; esa operación debe quedar resuelta en un ADR antes de K187, que implementa `UpdateService`.
- Las capacidades son afirmaciones declarativas, no permisos de sistema. Su despacho y la prevención de accesos indebidos pertenecen a tareas posteriores del SDK y su sandbox.

## Alternatives Considered

- **SemVer y normalización de ID:** parecen facilitar comparación, pero contradicen el contrato opaco ya aceptado en ADR-0029 y rechazarían versiones futuras o de build.
- **Capacidades como strings libres:** permitirían typos y comportamientos no definidos por la especificación.
- **Lista mutable de capacidades:** permitiría que una instancia cambiara después de validar o registrar su metadata.
- **Exigir al menos una capacidad:** la especificación no establece esa condición y un connector puede limitarse a health check o estar deshabilitado.
- **Inferir `UPDATE_CHECK` como método ahora:** requeriría improvisar su firma, entradas y salidas, que la especificación no define.

## Consequences

- La identidad del connector y la identidad de cada `Source` permanecen coherentes con el esquema existente.
- El contrato puede ser comparado y almacenado sin permitir mutación accidental de capacidades.
- `UPDATE_CHECK` queda visible en el vocabulario sin habilitar invocaciones ambiguas; el trabajo de actualización no debe iniciarse sin definir su contrato.

## Failure Modes

- ID o versión vacíos: la construcción falla antes del registro.
- Capacidad desconocida o valor que no sea `ConnectorCapability`: la construcción falla; no se ignoran declaraciones malformadas.
- Se asume que capacidad declarada autoriza DB, filesystem o red ilimitada: esta metadata no concede permisos; K067 define el límite de acceso.
- Se intenta usar `UPDATE_CHECK` antes de definir su contrato: falta una decisión arquitectónica; no se inventa un método ni se llama a otra operación como fallback.

## Reversal Strategy

Cambiar el vocabulario, mutabilidad o semántica de identidad requiere un ADR nuevo. Si una futura necesidad exige normalizar o migrar identidad, conservar y respaldar los valores originales y probar la correspondencia de cada `Source`; no sobrescribir IDs o versiones en silencio.
