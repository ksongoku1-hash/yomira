# ADR-0031 — Identidad y representación de Chapter

## Status

Accepted — decisión de implementación de K040.

## Context

La especificación define los campos de `Chapter`, muestra `12.10` como `(major=12, minor=10, sort_key=[12, 10, 0])`, y exige que un extra textual pueda carecer de major/minor. ADR-0026 fija UUIDv4 para identidades, enteros anulables para major/minor y una secuencia canónica de enteros para la clave de orden. No se define la gramática de variantes, tipos, volúmenes o idiomas, ni la política para etiquetas repetidas.

## Decision

- `chapter_id` es la identidad UUIDv4. `series_id` es una FK `ON DELETE RESTRICT`.
- `chapter_raw_label` y `display_label` son textos obligatorios y no vacíos. Se conservan exactamente, sin recorte, parsing destructivo ni normalización. `display_label` debe proveerse explícitamente; K040 no lo deriva del raw label.
- `chapter_major`, `chapter_minor` y `volume_number` son enteros exactos o `NULL`. El dominio rechaza `float` y `bool`. No se impone rango, positividad ni regla de pareja entre major/minor porque la especificación no los define.
- `chapter_variant`, `chapter_type` y `language` son textos opcionales opacos. Se conserva el valor exacto; `EXTRA` es un valor válido de ejemplo, no una lista exhaustiva de tipos permitidos.
- `chapter_sort_key` es una tupla obligatoria de enteros, incluso cuando está vacía. Se persiste como JSON compacto de array (`[]` para una secuencia vacía) y se valida como array al leerlo. Nunca se serializa mediante números de punto flotante. Una tupla vacía representa que el parser no derivó una posición numérica; K040 no define un orden automático para esos capítulos.
- No se impone unicidad por raw label, display label, major/minor, variante, volumen o idioma. Etiquetas iguales no son identidad y no causan fusión automática.
- La tabla solo indexa `series_id`. No se indexa la representación JSON como clave de orden SQL porque su orden lexicográfico no equivale al orden numérico de las tuplas.
- K040 no añade timestamps de creación/modificación ausentes de la lista normativa.

## Alternatives Considered

- `float` o decimal binario para chapter number: redondea o pierde distinciones como `12.10`.
- Usar el raw label como PK o hacerla única: confunde repeticiones legítimas y etiquetas distintas para el mismo ordinal.
- Cerrar `chapter_type` a `EXTRA` u otros valores inventados: rechaza tipos que futuros conectores puedan aportar.
- Forzar major y minor presentes/ausentes como pareja, o imponer que sean positivos: reglas no establecidas por la especificación.
- Guardar `chapter_sort_key` como texto libre o lexicográfico: permite claves mal formadas o un orden SQL numérico incorrecto.
- Derivar `display_label` del raw label: introduce una transformación implícita no definida por la especificación.

## Positive Consequences

- El raw label y sus partes numéricas permanecen disponibles sin redondeo ni pérdida de formato.
- Las extensiones futuras de variantes, tipos e idiomas no requieren migrar un enum cerrado.
- Duplicados visibles pueden conservarse; su identidad permanece independiente del texto.

## Negative Consequences

- El consumidor que ordene capítulos debe comparar las tuplas decodificadas numéricamente y definir aparte cómo ubicar claves vacías.
- La nulabilidad abierta de major/minor permite registros parcialmente parseados.
- Los valores de capítulo inválidos en texto JSON levantan un error de mapeo y requieren diagnóstico; el mapeo no los repara silenciosamente.

## Failure Modes

- Un parser convierte `12.10` a `12.1` o a `float` antes de guardarlo.
- Se interpreta la representación JSON como orden lexicográfico.
- Una migración introduce unicidad por etiqueta o normaliza valores existentes.

## Reversal Strategy

Cambiar identidad, tipos de número, representación de sort key, nulabilidad, unicidad o reglas de texto requiere nueva ADR, migración versionada y pruebas de round-trip, integridad referencial y regresión con etiquetas decimales.
