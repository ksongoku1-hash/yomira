# ADR-0100 — Consultas recientes de Home

## Estado

Aceptado — concreta las dos secciones de Home exigidas por `docs/UI_LAYOUTS.md`. No modifica ni renumera tareas del plan de ejecución.

## Contexto

Home debe mostrar biblioteca reciente y lectura reciente cuando existan datos locales. El contrato de `GET /api/v1/library` está fijado por ADR-0039 en orden alfabético para que la paginación sea estable. No debe reinterpretarse ese orden como recencia.

`Series.created_at` identifica cuándo se incorporó el registro local. `Series.updated_at` cambia con ediciones del registro y no representa actividad de lectura. `ReaderProgress.updated_at` identifica cuándo se guardó la posición de una pareja serie/capítulo; un GET de progreso de una posición ya existente no registra una nueva apertura. No hay historial de sesiones de lectura.

## Decisión

- Se conservan sin cambios el orden, filtros y paginación de `GET /api/v1/library` establecidos por ADR-0039.
- Home consulta `GET /api/v1/library/recent?limit=N`. La respuesta contiene hasta `N` series y `meta.limit`, sin `COUNT` ni `offset`. `N` tiene valor por defecto 4 y rango permitido de 1 a 20. El orden es `created_at_us DESC, series_id ASC`; “reciente” significa incorporada recientemente a esta biblioteca local.
- Home consulta `GET /api/v1/reader/progress/recent?limit=N`. La respuesta contiene hasta `N` posiciones guardadas, con identidad y título de serie, identidad y etiqueta de capítulo, `page_index`, porcentaje y `updated_at`; `meta.limit` informa el límite aplicado. No incluye `COUNT` ni `offset`. `N` tiene valor por defecto 4 y rango permitido de 1 a 20.
- El orden de progreso es `updated_at_us DESC, series_id ASC, chapter_id ASC`. La granularidad es una fila por capítulo, por lo que dos filas recientes pueden pertenecer a la misma serie. “Lectura reciente” se rotula en la interfaz como “Progreso guardado recientemente”; la fecha es la del último guardado de posición, no la hora de apertura ni una sesión.
- Las consultas usan una sola lectura SQL cada una, joins para los títulos requeridos y `LIMIT` validado. No se filtran candidatos por disponibilidad de archivo ni se recorre una lista adicional: si un capítulo dejó de estar disponible, la navegación normal del lector muestra su error trazable y el progreso persistido se conserva.
- Una migración agrega índices compuestos que respaldan los órdenes de recencia: series por `(created_at_us DESC, series_id ASC)` y progreso por `(updated_at_us DESC, series_id ASC, chapter_id ASC)`. No cambia ni elimina filas de usuario.

## Alternativas consideradas

- Cambiar el orden de `GET /api/v1/library`: rompe el contrato y la estabilidad de paginación de ADR-0039.
- Usar `Series.updated_at`: confunde cambios de metadatos con incorporación o lectura.
- Etiquetar `ReaderProgress.updated_at` como “última apertura”: afirma un evento que el modelo no registra.
- Agregar eventos por sesión de lectura: aumenta el esquema y la escritura persistida sin ser necesario para cumplir el layout actual.
- Calcular un total u ofrecer offset en vistas de Home: hace trabajo adicional que la UI no necesita y permite escaneos/páginas de tamaño innecesario.
- Filtrar por archivos todavía legibles y seguir buscando hasta completar el cupo: el coste de exploración dependería de cuántas referencias obsoletas existan; el endpoint mantiene un tope de candidatos estricto.

## Consecuencias

- La interfaz puede mostrar hasta cuatro incorporaciones y hasta cuatro posiciones guardadas con orden determinista, incluso con títulos o marcas de tiempo iguales.
- La lista de progreso no constituye un historial y puede repetir una serie cuando se guardaron posiciones de varios capítulos.
- No se promete que una fecha de progreso corresponda a cada apertura; la interfaz debe usar lenguaje de guardado.
- Se añade una migración reversible de índices y dos consultas acotadas. El límite máximo de 20 acota serialización y transferencia; el índice temporal evita ordenar la tabla completa para obtener los candidatos recientes.

## Fallos que deben probarse

- Límite ausente usa 4; límites 1 y 20 se aceptan; 0 y 21 se rechazan con validación API estándar.
- Tiempos iguales ordenan por los UUID según las reglas definidas.
- La lista reciente de series usa `created_at_us`, aunque `updated_at_us` cambie.
- La lista reciente de progreso refleja el último UPSERT por capítulo y puede incluir dos capítulos de una misma serie.
- Ninguna vista reciente altera `GET /api/v1/library` ni calcula un total.
- La migración conserva todas las filas y la reversión elimina solo los índices nuevos.
- Un error de consulta conserva la envoltura de error, código y traza estándar; no borra ni reescribe contenido.

## Reversión

Se pueden retirar los dos endpoints y sus consultas sin modificar contenido persistido. La migración inversa elimina únicamente los dos índices nuevos; no borra registros de Series ni ReaderProgress. El listado alfabético de ADR-0039 permanece intacto.
