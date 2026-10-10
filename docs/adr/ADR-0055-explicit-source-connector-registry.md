# ADR-0055 — Registry explícito y acotado de Source Connectors

## Status

Accepted for K068 implementation — 2026-09-26.

## Context

K068 solicita un registry controlado. K060 define una identidad de connector opaca y un conjunto cerrado de capacidades; K067 añade `ConnectorHostContext` sin permisos de DB o filesystem. Aún no existe carga de plugins, registry ni connector de producto. `UPDATE_CHECK` está declarado en el vocabulario, pero ADR-0047 no definió una operación de protocolo para ejecutarlo.

## Decision

- `ConnectorRegistration` vincula `ConnectorMetadata` inmutable con una factory tipada que recibe `ConnectorHostContext`. El registry recibe una secuencia explícita de registros first-party; no busca módulos, carpetas, entry points, DLLs ni plugins durante el arranque.
- La construcción rechaza IDs duplicados exactamente, sin normalizarlos ni reemplazar registros previos. Un registry vacío es válido mientras no haya connectors registrados.
- El registry congela su mapa al terminar la construcción. No hay API de registro tardío, reemplazo o unregister durante operaciones.
- Las instancias se crean de forma lazy solo al resolver el ID, y se cachea como máximo una instancia por registro durante la vida del registry. Así el coste máximo de instancias queda acotado por la cantidad de registrations estáticas. Las operaciones de fuente no deben guardar estado por Source en esa instancia.
- Al crear una instancia se compara la metadata publicada por el objeto con la metadata del registro campo por campo. Una discrepancia falla cerrada y la instancia no entra en cache. Una factory no se ejecuta para ID desconocido ni antes de validar una capacidad solicitada.
- `supports(connector_id, capability)` informa solo si la metadata declara una capacidad. `resolve(..., required_capability=...)` comprueba esa declaración antes de crear el objeto; una capacidad declarada ausente produce `SRC_CAPABILITY_UNSUPPORTED` con la operación correspondiente, sin fallback ni I/O. La comprobación de capacidad no reemplaza el contract test del método.
- `UPDATE_CHECK` puede aparecer en metadata, pero no se puede usar como `required_capability` ni despachar hasta que otro ADR defina el contrato de K187. El registry no inventa una operación, firma ni fallback.
- Fallos estructurales del registry tienen razón estable y `trace_id` activo o UUIDv4, mensajes fijos y no incluyen connector IDs ni mensajes de factory. Una excepción de factory se envuelve como `FACTORY_FAILED` con solo el tipo de causa; no hereda un retry de red.

## Alternatives Considered

- **Descubrimiento automático por import scan/entry points:** ejecuta código durante descubrimiento, hace el conjunto efectivo dependiente del filesystem/instalación y permite colisiones difíciles de explicar.
- **Sobrescribir ID duplicado:** puede cambiar silenciosamente el connector elegido por Sources existentes.
- **Instanciar todos los connectors al arranque:** activa código y recursos aun para fuentes que no se usan y puede bloquear startup por una integración inactiva.
- **Crear una instancia nueva por operación:** vuelve impredecible la cantidad de clientes/sesiones y rompe connectors que mantienen estado acotado de transporte.
- **Llamar a un método alternativo cuando falta capacidad:** oculta incumplimiento de contrato y puede hacer I/O con semántica distinta.
- **Tratar `UPDATE_CHECK` como `get_chapters`:** los cambios de capítulos no equivalen a verificación de updates y no existe contrato aprobado para esa operación.

## Consequences

- El registry trabaja solo con connectors first-party incluidos explícitamente por código y recibe las capacidades acordadas por K067.
- La metadata queda consultable sin ejecutar factories. Resolver es perezoso y repetido devuelve la misma instancia mientras el registry viva.
- El límite heterogéneo del registry borra los parámetros de tipo genérico de cada connector; Pyright revisa cada factory al registrarse y el Connector SDK Gate verificará comportamiento con contract tests.
- Una instancia no puede ser estado por Source. Credenciales siempre se obtienen del Vault mediante referencia explícita; la integración de configuración por Source requiere contrato propio antes de connectors reales.
- Una excepción de factory ocurre solo cuando se resuelve ese ID. El caller conserva el trace/contexto y no convierte automáticamente cualquier excepción a retryable.

## Failure Modes

- ID duplicado: construcción falla, sin elegir ganador.
- ID desconocido: lookup falla sin importar módulos ni alterar el registry.
- Capability faltante: falla antes de invocar factory y antes de I/O.
- `UPDATE_CHECK` solicitado: falla con razón estable hasta aprobar su contrato; no llama otra operación.
- Metadata de instancia distinta a la registrada: la resolución falla y no cachea el objeto.
- Factory falla: `FACTORY_FAILED`, sin texto de excepción y con trace; no se cachea un valor incompleto ni se clasifica como fallo remoto reintentable.

## Reversal Strategy

Permitir registro dinámico, reemplazo en caliente, creación por operación o `UPDATE_CHECK` requiere ADR y pruebas de compatibilidad, concurrencia, recursos y Sources persistidas. Nunca reemplazar una entrada activa por ID sin una política explícita de versión y migración.
