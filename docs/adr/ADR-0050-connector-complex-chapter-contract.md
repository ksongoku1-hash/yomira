# ADR-0050 — Contrato de capítulo complejo de Source Connector

## Status

Accepted for K063 implementation — 2026-09-26.

## Context

K063 debe transportar capítulos de fuentes sin perder etiquetas ni confundir representaciones numéricas. La especificación distingue `Chapter` de `ChapterSource`, muestra `12.10` como major 12, minor 10 y sort key `[12, 10, 0]`, y permite extras como `Extra verano` sin componentes numéricos. ADR-0031 establece que las etiquetas son exactas, los componentes son enteros y no hay un parser común aprobado. ADR-0032 permite una referencia remota por ID o URL.

## Decision

- `ConnectorChapter` combina la identidad/referencia remota de `ChapterSource` (`remote_id`, `remote_url`, `remote_label`) con los campos del capítulo lógico: etiqueta raw/display, major, minor, variant, type, sort key, volume e idioma.
- Debe existir `remote_id` o `remote_url`; cada referencia presente es texto no vacío y se conserva exactamente. `remote_label` es opcional y conserva incluso el texto vacío.
- `chapter_raw_label` y `display_label` son strings obligatorios no vacíos y no se recortan. El conector entrega `display_label`; el core no lo deriva automáticamente.
- Major, minor y volume son enteros exactos o `None`; `bool` y `float` no son enteros válidos. No se imponen rangos, positividad ni relación entre major/minor.
- `chapter_sort_key` es una tupla inmutable de enteros y puede estar vacía. No se convierte a `float`, texto JSON ni un orden lexicográfico en el contrato.
- Variant, type e idioma son strings opcionales opacos; el vocabulario permanece abierto. `EXTRA` es un valor permitido, no un enum cerrado.
- Se conserva la separación: `get_chapters` devuelve `Sequence[ConnectorChapter]`; `get_chapter` devuelve un `ConnectorChapter`. No se deduplican capítulos por etiqueta ni se infiere identidad desde el título.
- K063 define datos validados, no un algoritmo universal que parsee una etiqueta. Cada connector puede poblar componentes solo cuando los pueda determinar sin pérdida; los extras textuales usan `None` y sort key vacía si no se derivan componentes.
- Fixtures son locales; K063 no accede a la red ni persiste capítulos.

## Alternatives Considered

- **Convertir capítulo a `float`:** colapsa `12.10` en `12.1` y puede confundir capítulos distintos.
- **Usar solo la etiqueta raw:** no expresa orden numérico de capítulos decimales ni los componentes estructurados requeridos por el dominio.
- **Crear un parser común de labels:** las fuentes usan gramáticas variables y la especificación no define una heurística común segura.
- **Hacer obligatorios ID y URL remotos:** rechaza fuentes que solo proporcionan una forma de referencia, contrario a ADR-0032.
- **Hacer único o fusionar por raw/display label:** etiquetas pueden repetirse o variar entre fuentes; el ID de capítulo permanece independiente del texto.
- **Cerrar chapter type o idioma a enums:** impediría conservar valores futuros de fuentes.

## Consequences

- Valores como `12.10`, variantes y extras se pueden representar sin pérdida ni redondeo.
- Un parser específico puede informar tanto la observación textual como los componentes que pudo justificar; una clave vacía marca que no hay posición numérica derivada.
- El core debe mapear por separado los campos lógicos y la referencia de fuente al persistir; este contrato no fusiona ni crea entidades.

## Failure Modes

- Un parser reduce `12.10` a `12.1`, ordena la key como texto o pone `float` en un componente: falla validación o fixture antes de persistir.
- Falta ID y URL, o una referencia presente es vacía: el capítulo se rechaza; no se usa el título como fallback de identidad.
- Se fabrica `display_label`, variante, tipo, idioma o posición: no hay fallback; debe preservarse ausencia cuando la fuente no declara el dato.
- Se interpreta key vacía como índice universal: ADR-0031 deja esa política de orden fuera del contrato de capítulo.

## Reversal Strategy

Cambiar la representación numérica, identidad, nulabilidad o semántica de etiquetas requiere ADR nuevo y pruebas de round-trip con decimales, variantes, extras y referencias solo ID/solo URL. No migrar etiquetas ya persistidas mediante parsing heurístico.
