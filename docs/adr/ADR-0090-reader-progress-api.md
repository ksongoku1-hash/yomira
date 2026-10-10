# ADR-0090 — API de progreso de lectura

## Estado

Aceptado — concreta K179 y las secciones 7.8, 41 y 52 de la SPEC. No cambia el esquema persistido definido por ADR-0036.

## Contexto

La SPEC define `ReaderProgress` y exige que el lector guarde/restaure progreso, pero no define los endpoints ni el comportamiento cuando cambia el número de páginas del CBZ. El dominio existente ya conserva `page_index` cero-based y porcentaje exacto en centésimas; falta el servicio que los persista y el contrato entre lector y backend.

## Decisión

- El backend expone `GET /api/v1/reader/{chapter_id}/progress` y `PUT /api/v1/reader/{chapter_id}/progress`.
- GET devuelve `{ "data": null, "meta": {} }` cuando aún no existe posición guardada. Si existe, devuelve `series_id`, `chapter_id`, `page_index`, `percentage` y `updated_at` mediante el envelope estándar. `percentage` se serializa como texto decimal con dos decimales para no perder precisión JSON; `updated_at` es ISO-8601 UTC.
- PUT acepta únicamente `{ "page_index": <entero cero-based> }`. El backend deriva serie, cantidad de páginas y porcentaje del capítulo local publicado y verificado; el cliente no puede asociar progreso a otra serie ni enviar un porcentaje contradictorio.
- PUT acepta solo índices dentro de `[0, page_count)`. La página única obtiene 100 %. Para capítulos con más páginas, la primera página equivale a 0 %, la última a 100 % y los valores intermedios se redondean al basis point más cercano usando aritmética entera; los empates se redondean hacia arriba. No se usa `float`.
- Al abrir un capítulo, el lector consulta primero metadata y luego progreso. Si un CBZ reemplazado tiene menos páginas que el índice guardado, la UI muestra la última página disponible sin sobrescribir el índice guardado hasta que el usuario cambie de posición.
- Si aún no existe una posición y la página inicial visible se decodifica correctamente, el lector persiste esa posición inicial una vez. Así también queda registrada la lectura de capítulos de una sola página (100 %) sin guardar páginas que no llegaron a mostrarse.
- Los cambios de posición se guardan en orden por capítulo desde el cliente, incluso si el lector se desmonta y vuelve a abrir el mismo capítulo: una escritura activa como máximo y, mientras espera, solo se conserva la posición más reciente pendiente. Al remontar, el lector retoma la posición más reciente de esa cola y serializa nuevas elecciones detrás de la escritura activa. Los componentes desmontados dejan de recibir actualizaciones de UI. Un fallo se muestra con traza/código y habilita reintentar; no se indica “guardado” antes del éxito HTTP.
- Solo se guarda progreso para un capítulo local cuyo CBZ haya sido verificado y esté disponible al ReaderAssetService. La escritura sustituye la posición de esa misma pareja `(series_id, chapter_id)` según ADR-0036.

## Alternativas consideradas

- Aceptar `series_id` o `percentage` desde el cliente: permite identidades contradictorias y porcentajes que no corresponden a la posición real.
- Cambiar `page_index` al último disponible al leer: sobrescribe silenciosamente evidencia válida si el CBZ fue reemplazado o si una respuesta está temporalmente incompleta.
- Hacer debounce sin serializar PUT: dos solicitudes concurrentes podrían completarse fuera de orden y restaurar una posición antigua.
- Rechazar capítulos que no tienen una fila previa de progreso: impide el primer guardado.

## Consecuencias

- El backend añade un repositorio/servicio de progreso y dos operaciones API pequeñas; no requiere migración adicional.
- El lector puede recuperar la posición tras cerrar y volver a abrir la aplicación.
- Un fallo de red/local API conserva la fila previa y se presenta como fallo de guardado; no se informa falsamente de persistencia.

## Fallos que deben probarse

- Capítulo desconocido o no disponible: 404 sin insertar progreso.
- Índice negativo, no entero o fuera de `page_count`: 422 y fila previa intacta.
- Error SQLite: error trazable y fila previa intacta.
- Capítulo con una página y capítulo con muchas páginas: porcentaje exacto según la regla anterior.
- Respuestas de PUT retrasadas: la última posición escogida por el usuario queda guardada.
- PUT retrasado durante desmontaje/remontaje del lector: no se solapan escrituras del mismo capítulo y la nueva posición elegida queda como la última guardada.
- Índice guardado que excede el nuevo número de páginas: la interfaz presenta la última disponible y no lo sobrescribe automáticamente.
- Primera página que falla al descargarse/decodificarse: no crea progreso inicial.

## Reversión

Retirar los endpoints no requiere migración ni elimina filas de progreso. Si cambia el contrato o el cálculo del porcentaje, una ADR sucesora debe preservar el esquema y las posiciones ya almacenadas conforme a ADR-0036.
