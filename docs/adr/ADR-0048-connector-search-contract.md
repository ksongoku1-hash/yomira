# ADR-0048 — Contrato de búsqueda de Source Connector

## Status

Accepted for K061 implementation — 2026-09-26.

## Context

K061 debe tipar el intercambio de búsqueda entre el core y un connector. La especificación enumera `search()` y requiere fixtures offline, pero no fija filtros, paginación, límites de longitud ni normalización de query. `SeriesSource` ya acepta `remote_id` o `remote_url` como identidad remota, conserva sus valores sin normalizarlos y admite un `remote_title` opcional, según ADR-0030.

## Decision

- `ConnectorSearchQuery` contiene únicamente `query: str`. El texto se conserva exactamente y se permite vacío; este contrato no recorta, cambia mayúsculas, añade filtros ni establece límites ausentes de la especificación.
- `ConnectorSearchResult` contiene `remote_id`, `remote_url` y `remote_title`, cada uno anulable. Debe incluir por lo menos `remote_id` o `remote_url`; cualquier referencia presente debe ser un string no vacío. La validación no parsea ni normaliza IDs o URLs.
- `remote_title` es opcional y, si se proporciona, se conserva exactamente incluso si está vacío. No es una clave, identidad ni regla para fusionar resultados.
- Ambos DTOs son inmutables. Un resultado de búsqueda contiene solo identidad remota y título; la metadata normalizada de serie corresponde a K062.
- `SourceConnector.search` recibe `ConnectorSearchQuery` y devuelve `Sequence[ConnectorSearchResult]`. El connector conserva el orden que devuelve la fuente; K061 no ordena, deduplica ni pagina resultados.
- El caller debe comprobar la capacidad `SEARCH` definida en K060. K065 asignará errores `SRC_*`; la forma de API/UI y sus límites se definen en sus tareas posteriores.
- Fixtures y pruebas de contrato usan datos locales; K061 no exige Internet.

## Alternatives Considered

- **Añadir filtros, idioma, orden o paginación:** serían útiles para algunas fuentes, pero no tienen semántica normativa en la especificación y una regla común podría descartar resultados válidos.
- **Normalizar el query antes de enviarlo:** puede alterar búsquedas sensibles a espacios, puntuación o casing específicos de una fuente.
- **Exigir tanto `remote_id` como `remote_url`:** descarta conectores que ofrecen solo una referencia, contradiciendo el modelo aprobado en ADR-0030.
- **Usar `remote_title` como identidad:** títulos se repiten y cambian; contradice ADR-0028 y ADR-0030 y podría fusionar obras distintas.
- **Incluir toda la metadata de serie en el resultado de búsqueda:** duplica K062 y expande payloads de búsqueda sin una exigencia de la especificación.

## Consequences

- El core puede conservar referencias de fuentes con una sola forma de identidad sin cambiar sus bytes/texto.
- La consulta queda disponible para conectores heterogéneos sin que el SDK altere silenciosamente el texto.
- El título no basta para crear una asociación ni deduplicar resultados. La persistencia y matching se mantienen fuera de K061.
- La UI/API futura debe definir sus propios límites de entrada sin cambiar el texto del contrato de connector por inferencia.

## Failure Modes

- Resultado sin ID ni URL, o con referencia presente pero vacía: se rechaza antes de pasar el resultado al core.
- Resultado duplicado o aliases distintos para la misma obra: no se fusionan por título; la política de asociación requiere una decisión explícita posterior.
- Query vacío o con espacios: se transmite intacto; la política de interacción de usuario corresponde al caller.
- El connector no declara `SEARCH`: el caller debe rechazar la operación con el error `SRC_*` definido en K065, no invocar un fallback.

## Reversal Strategy

Cambiar nulabilidad, identidad, conservación de texto u orden de resultados requiere un ADR nuevo y fixtures de compatibilidad. Los datos recibidos previamente no se reescriben ni deduplican automáticamente.
