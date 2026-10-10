# ADR-0056 — MockConnector determinista y offline

## Status

Accepted for K069 implementation — 2026-09-26.

## Context

K069 solicita un MockConnector para verificar el Source Connector SDK antes de implementar fuentes reales. K059–K065 definen referencias opacas, secuencias con orden y duplicados preservados, metadata normalizada y errores de fuente sanitizados. El fixture no debe depender del servidor de Fase 7 ni introducir I/O externo en las pruebas del SDK.

## Decision

- `MockConnector` es un fixture first-party, sin estado mutable, red, filesystem, reloj ni aleatoriedad. Sus métodos son deterministas y las capacidades declaran únicamente SEARCH, METADATA, CHAPTER_LIST y PAGE_LIST.
- El dataset se limita a la serie `mock-series-1` y el capítulo `mock-chapter-1`. La búsqueda devuelve la serie únicamente para el texto exacto `fixture`; otras consultas producen una secuencia vacía sin alterar ni normalizar el query.
- La metadata cruda y los registros de capítulo/página son inmutables. La lista fija de páginas conserva el orden `page-2`, `page-1`, `page-1` para que el SDK pueda probar el orden y la repetición sin deduplicación.
- Referencias desconocidas fallan con `SRC_CONNECTOR_FAILED`, operación precisa, clasificación no reintentable y mensaje fijo; no se incluyen referencias en el error.
- La normalización copia solo campos reconocidos de tipos admitidos. No infiere valores, convierte etiquetas ni altera metadata local.
- El fixture no se registra automáticamente en producción. Su disponibilidad en el paquete no modifica el allowlist del registry de K068.

## Alternatives Considered

- **Mock HTTP server como dependencia de K069:** mezcla el SDK fixture con las responsabilidades de Fase 7 y añade sockets, puertos y ciclo de vida a pruebas que solo requieren validar contratos.
- **Generar datos aleatorios o dependientes del tiempo:** hace que los resultados y fallos no sean reproducibles.
- **Aceptar cualquier referencia y construir datos sobre la marcha:** oculta errores de test y puede permitir que fixtures inválidos parezcan válidos.
- **Guardar fixtures externos en archivos:** añade dependencia del filesystem al SDK y a su suite de contratos.

## Consequences

- Las pruebas de SDK pueden ejecutarse sin red y con un coste acotado a un pequeño dataset en memoria.
- La búsqueda del fixture tiene semántica deliberadamente exacta y estrecha; no representa la relevancia ni el protocolo de una fuente real.
- Los errores de referencia son fallos permanentes del fixture y no deben interpretarse como una política general para errores HTTP de conectores reales.
- K070 puede ejecutar contratos comunes contra una instancia real del MockConnector sin registrar fuentes, abrir puertos ni consultar servicios externos.

## Failure Modes

- Consulta distinta de `fixture`: devuelve cero resultados determinísticamente.
- Serie o capítulo no incluido en el fixture: devuelve error tipado no reintentable sin incluir el valor recibido.
- Payload mal tipado en normalización: omite el campo incompatible y conserva los campos válidos; nunca transforma datos con heurísticas.

## Reversal Strategy

Cambiar dataset, semántica de búsqueda, capacidades o clasificación de errores requiere actualizar este ADR y los contract tests. Añadir transporte HTTP pertenece al Mock Manga Server de Fase 7 y no debe introducirse dentro de este fixture.
