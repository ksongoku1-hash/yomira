# Informe — Fase 6: Source Connector SDK

**Estado:** PASS
**Fecha:** 2026-09-26
**Tareas:** K059–K070
**Alcance:** contratos tipados de conectores, capacidades, Vault local, host context, registry, MockConnector offline y gate contractual.

## Resultado de tareas

| Tarea | Estado | Resultado |
|---|---|---|
| K059 | PASS | `SourceConnector` define operaciones remotas asíncronas y normalización local síncrona; ADR-0046. |
| K060 | PASS | Metadata inmutable con ID, versión y capacidades cerradas; ADR-0047. |
| K061 | PASS | Contratos de Search preservan query y referencias sin normalización; ADR-0048. |
| K062 | PASS | Metadata de serie separa payload crudo de campos normalizados; ADR-0049. |
| K063 | PASS | Contrato de capítulo preserva IDs, etiquetas y orden exactos sin parser heurístico; ADR-0050. |
| K064 | PASS | Referencias de página opacas; secuencias conservan orden y duplicados; ADR-0051. |
| K065 | PASS | Errores `SRC_*` con operación, traza, mensaje sanitizado y retryabilidad clasificada; ADR-0052. |
| K066 | PASS | `SecretVault` local respaldado por DPAPI, referencias opacas, escritura atómica, cuotas, expiración y revocación; ADR-0053. |
| K067 | PASS | `ConnectorHostContext` expone solo `SecretVault`; guard AST detecta imports de persistencia y escrituras directas; ADR-0054. |
| K068 | PASS | Registry inmutable de allowlist, resolución lazy, una instancia por ID y verificación de metadata/capacidad; ADR-0055. |
| K069 | PASS | MockConnector determinista, offline y en memoria, con fixture inmutable; ADR-0056. |
| K070 | PASS | Contract tests verifican protocolo, preservación de valores, errores, registry y sandbox; ADR-0057. |

## Evidencia del MockConnector y del SDK Gate

- El conector de fixture publica solo `SEARCH`, `METADATA`, `CHAPTER_LIST` y `PAGE_LIST`; no declara `UPDATE_CHECK`.
- La búsqueda coincide con el texto exacto `fixture`. Un resultado sintético apunta a `mock-series-1`; la serie tiene `mock-chapter-1` y tres referencias de página en el orden `page-2`, `page-1`, `page-1`.
- Los tests verifican tuplas inmutables, etiquetas y referencias exactas, metadata normalizada solo desde campos reconocidos, y errores `SRC_CONNECTOR_FAILED` no reintentables con UUIDv4 y sin referencias en el mensaje.
- El registry no instancia el fixture al leer metadata ni al rechazar `UPDATE_CHECK`; resuelve y reutiliza una instancia al solicitar una capacidad soportada.
- La suite sandbox AST cubre el paquete entero de connectors y falla ante acceso directo a persistencia o escritura al filesystem.
- `MockConnector` no se registra automáticamente y no representa un connector remoto. No abre sockets ni toca archivos.

## Verificación final

El quality gate completo se ejecutó con los runtimes aprobados y terminó con `QUALITY GATE: PASS`.

| Verificación | Resultado |
|---|---|
| Python / SQLite / Node / npm | Python 3.14.7 / SQLite 3.50.4 / Node 24.21.0 / npm 11.19.0. |
| Dependencias | `npm ls --depth=0` y `pip check`: PASS, sin dependencias rotas. |
| TypeScript / ESLint | PASS. |
| Vitest | 28 pruebas aprobadas. |
| Build frontend | Vite PASS; 79 módulos transformados. |
| Pytest | 274 pruebas aprobadas. |
| Ruff | PASS. |
| Pyright | 0 errores, 0 warnings y 0 informations. |
| Smoke integración | PASS; arranque, root, `/library`, API de biblioteca y endpoints de sistema por loopback, cierre controlado. |
| `git diff --check` | PASS antes del commit de fase. |

Pytest mostró un `PytestCacheWarning` no bloqueante al actualizar `.pytest_cache` por una carpeta preexistente con el mismo nombre. No produjo fallos de pruebas y el proceso completó los demás checks.

## Fallos reproducidos y resolución

1. La prueba inicial de K069 falló en colección con `ModuleNotFoundError: app.connectors.mock`, confirmando que el MockConnector no existía. Se implementó el fixture offline y la suite K069 pasó.
2. Pyright detectó géneros parcialmente desconocidos al normalizar un payload `Mapping[str, object]`. Se validó la forma `tuple` y que cada elemento fuese `str` antes de conservar el valor; Pyright pasó sin perder contenido ni realizar conversiones.
3. La primera ejecución enfocada de K070 intentó usar el directorio temporal de usuario, cuyo acceso está denegado al proceso. La ruta temporal aislada no existía aún, así que pytest no la aceptó. Se creó esa carpeta dedicada bajo `work/yomira-toolchain` y se repitieron las pruebas; 27 pasaron.
4. La suite contractual hizo visible una pérdida de tipo de los elementos al inspeccionar retornos anotados como `Sequence`. Como el fixture siempre entrega secuencias inmutables, se anotaron como tuplas concretas, compatibles con el protocolo `Sequence`; Pyright pasó con tipos precisos.
5. Ruff reportó imports fuera de orden en la suite nueva. Se ordenaron y la verificación enfocada volvió a pasar.

## Límites vigentes

- Este SDK ofrece contracts y una implementación sintética; los servidores falsos de fallos de red pertenecen a Fase 7 (K071–K080).
- No hay conectores reales de manga incluidos en esta fase.
- ADR-0054 establece una frontera de capabilities para connectors first-party confiables dentro del proceso; no es aislamiento de plugins hostiles por el sistema operativo.
- `UPDATE_CHECK` permanece no despachable hasta que se apruebe el contrato de K187.

## Decisión de fase

K059–K070 cumplen sus tareas. El quality gate completo pasa. Fase 6 queda `PASS`; puede iniciar Fase 7 con el Mock Manga Server offline/local y conservar los contratos establecidos por el SDK.
