# ADR-0043 — Persistencia y resolución de autoridad de metadata

## Status

Accepted for K056 implementation — 2026-09-26. Complementa ADR-0013 sin cambiar su precedencia.

## Context

La especificación exige conservar por campo los valores de fuente, locales y overrides para título, portada, autor, descripción, géneros, estado e idioma. El esquema actual guarda valores locales en `series`, pero `series_sources` conserva solo el título remoto y no existe `metadata_overrides`. Elegir una fuente en silencio cuando dos fuentes discrepan podría cambiar lo que ve el usuario sin trazabilidad.

## Decision

- `Series` guarda los valores `LOCAL_CANONICAL`: `display_title` es el título presentado; `canonical_title` conserva su función local de búsqueda/orden. Portada, descripción, estado e idioma conservan sus columnas actuales; autor y géneros se agregan como campos opcionales.
- Cada vínculo `SeriesSource` conserva separadamente sus siete valores de fuente: título, URL de portada, autor, descripción, géneros, estado e idioma. No se copian sobre `Series` durante una actualización de fuente.
- `metadata_overrides` tiene como clave primaria `(series_id, field_name)` y almacena como máximo un override por serie y campo. La existencia de la fila significa “establecido”: `""`, `[]` y JSON `null` son valores explícitos, distintos de la ausencia de fila. El override de portada usa una FK tipada a Asset; una fila con portada nula elimina la portada efectiva sin borrar los valores inferiores.
- La resolución se aplica campo a campo: override presente, luego valor local no nulo, luego metadata de fuente. Se conservan todos los candidatos de fuente y su asociación. Si los valores de fuente coinciden, se puede usar ese valor; si difieren y no existe un valor de mayor autoridad, el campo queda en conflicto sin valor efectivo hasta que una decisión local o override lo resuelva.
- El detalle de serie devuelve, por campo, el valor efectivo, la autoridad, el estado del override y los valores de cada asociación con el nombre de la fuente. La interfaz deja elegir un candidato en conflicto mediante un override local; quitarlo restaura la resolución inferior y conserva todos los candidatos.
- Una portada remota continúa siendo una URL de fuente, nunca un Asset local. La interfaz no la asigna como portada local hasta que exista un Asset `COVER`; un override nulo permite dejar la portada sin asignar de forma explícita.
- `PUT /api/v1/series/{series_id}/metadata-overrides` crea o actualiza un override explícito; `DELETE /api/v1/series/{series_id}/metadata-overrides/{field}` lo retira y deja el estado intacto si ya no existe.
- Los valores se conservan exactamente; los géneros se representan como arreglos JSON de texto. La aplicación valida los tipos al escribir y al leer. SQLite valida JSON, identidad, FK, timestamps y que una portada apunte a un Asset `COVER`.
- La migración `0013_metadata_authority` agrega columnas anulables y la tabla sin reescribir las filas existentes. Los campos previos de `series` siguen siendo locales; `SeriesSource.remote_title` existente sigue siendo metadata de fuente. El runner crea y verifica el backup antes de aplicar el cambio.

## Alternatives Considered

- Copiar metadata remota sobre `series`: destruye la procedencia local y puede pisar elecciones anteriores.
- Guardar overrides en settings JSON o configuración: separa las decisiones del registro, debilita FKs y complica transacciones/restauración.
- Una sola fila remota por serie: pierde valores distintos de otras fuentes asociadas.
- Elegir por orden de llegada o UUID cuando hay conflicto: convierte un orden accidental en autoridad de producto.
- Usar `cover_asset_id` para una URL remota: mezcla un Asset local validado con un recurso remoto no confiable.

## Positive Consequences

- Los tres niveles quedan persistidos y auditables por campo sin reemplazar contenido anterior.
- Un override nulo o vacío se conserva como decisión explícita y se puede retirar para revelar el nivel inferior.
- Una discrepancia entre fuentes no produce una selección arbitraria.
- Los valores de portada local mantienen la integridad de FK y tipo `COVER`.

## Negative Consequences

- Los conflictos entre fuentes requieren una resolución explícita antes de mostrar un único valor efectivo.
- La selección de una portada remota espera a que el flujo de descarga y validación de Assets pueda crear un Asset `COVER` local.
- La tabla de overrides y el modelo `SeriesSource` requieren más validación y migraciones que un campo plano.

## Failure Modes

- Tratar una fila de override con JSON `null` como si no existiera.
- Descartar candidatos de una fuente al actualizar otra.
- Clasificar un valor vacío como ausente o normalizarlo al persistir.
- Aceptar portada de tipo distinto de `COVER`.
- Aplicar la migración sin backup verificado o sin confirmar los valores anteriores.

## Reversal Strategy

No eliminar overrides ni columnas remotas durante un downgrade si contienen valores. Un cambio de precedencia o estrategia para resolver fuentes requiere un nuevo ADR y migración con backup, conteos y pruebas de round-trip/restauración.
