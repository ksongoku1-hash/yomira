# ADR-0115 — Compatibilidad con AlphaData y rutas CBZ anteriores

## Status

Accepted — decisión necesaria para preservar la Alpha instalada al cambiar la raíz visible de CBZ, 2026-10-04.

## Context

La instancia Windows activa usa `%LOCALAPPDATA%\Yomira\AlphaData`; la raíz canónica `%LOCALAPPDATA%\Yomira\YomiraData` no existe. La base activa pasó `PRAGMA quick_check` y contiene biblioteca, progreso y ocho assets CBZ. Sus rutas CBZ persistidas son `library/chapters/...`, relativas a la raíz de datos. El código nuevo publica CBZ en `chapters/...`, relativos a `Documentos\Mangas`.

Sin compatibilidad explícita, el ejecutable actualizado abriría una base vacía en `YomiraData` y resolvería las rutas CBZ anteriores bajo la nueva raíz de biblioteca. La biblioteca y el progreso parecerían desaparecer aunque sus datos siguieran en disco.

## Decision

- `YOMIRA_DATA_ROOT`, cuando se define explícitamente, conserva prioridad. Sin override, se usa `YomiraData` si ya contiene `database/yomira.sqlite3`; de lo contrario, si existe `AlphaData/database/yomira.sqlite3`, se adopta `AlphaData` en su ubicación actual. Perfiles nuevos continúan usando `YomiraData`.
- La aplicación no copia, renombra, borra ni reescribe automáticamente el contenido existente de `AlphaData` como parte de este cambio.
- Los assets CBZ con ruta persistida `library/chapters/...` siguen resolviéndose desde `data_root`. Los CBZ nuevos, con ruta `chapters/...`, se resuelven desde `library_root`. Lector y exportación USB aplican la misma regla determinista.
- Portadas y datos operativos permanecen relativos a `data_root`. Las nuevas descargas se publican bajo `library_root`; al aprobarse este ADR, su valor predeterminado era `Documentos/Mangas` según ADR-0114. La ubicación predeterminada vigente se define en ADR-0120.
- La adopción del perfil existente mantiene las comprobaciones de identidad, integridad, volumen y bloqueo de instancia ya definidas. Si la raíz seleccionada no es válida, Yomira falla cerrado sin crear una base alternativa ni ocultar la base existente.

## Consequences

- La Alpha instalada conserva su base de datos, trabajos, biblioteca y progreso de lectura al iniciar la versión actualizada.
- Los CBZ ya existentes permanecen en `AlphaData/library/chapters`; la aplicación puede seguir leyéndolos. Los CBZ nuevos quedan en `Documentos/Mangas/chapters`.
- No se crean copias duplicadas ni se modifica SQLite solo por cambiar la ubicación predeterminada de nuevas descargas.
- Cambiar o consolidar posteriormente la raíz de datos o trasladar CBZ existentes requiere un flujo de migración explícito, verificado e idempotente.

## Verification

- Perfil activo observado en Windows: `AlphaData/database/yomira.sqlite3`, esquema `0021_download_job_archives`, cinco series, ocho CBZ, cuatro posiciones de lectura y comprobación SQLite `ok`.
- La regresión debe probar selección de `AlphaData` cuando `YomiraData` no tiene base, prioridad de una base canónica ya existente, lectura de una ruta CBZ antigua y resolución USB de ambas formas de ruta.

## Reversal Strategy

Revertir exige un ADR nuevo y una migración verificada si ya existen CBZ registrados bajo `Documentos/Mangas`. No eliminar ni renombrar `AlphaData` como parte de la reversión.
