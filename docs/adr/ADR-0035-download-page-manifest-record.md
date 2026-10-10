# ADR-0035 — Registro persistente de páginas de descarga

## Status

Accepted — decisión de implementación de K044, conforme al manifest de capítulo de la sección 21 y a las reglas de filesystem de las secciones 14, 16 y 22.

## Context

La especificación define los campos de cada página (`ordinal`, `remote_reference`, `temporary_path`, `status`, `size`, `sha256`, `width`, `height`, `mime`, `attempts`), pero no precisa base del ordinal, valores de `status`, cuándo se asigna el temporal ni qué campos quedan pendientes antes de recibir bytes. K043 ya da identidad estable a cada DownloadJob.

## Decision

- `DownloadPage` se identifica con clave primaria compuesta `(job_id, ordinal)`. `ordinal` es entero de base uno y único dentro de su job; el primer recurso de página tiene ordinal 1.
- `job_id` referencia `download_jobs.job_id` con `ON DELETE RESTRICT`. El historial de páginas no desaparece al borrar un job.
- `remote_reference` se almacena como texto opaco no vacío, exactamente como se recibe; no se analiza, normaliza ni recorta.
- `status` es texto opaco no vacío y se conserva exactamente. Una página nueva se crea como `PENDING`. La especificación no define el vocabulario ni las transiciones de estado de página; K044 no infiere ni impone estados adicionales. La tarea que formalice el manifest y su recuperación deberá cerrar ese vocabulario mediante ADR y migración si corresponde.
- `temporary_path` es anulable hasta que el motor reserve un temporal. Cuando existe, es una ruta relativa POSIX segura respecto de `YomiraData/temp/`; no almacena una ruta absoluta ni accede al filesystem. La ruta no puede escapar de su raíz y es única mientras esté registrada, para que dos páginas no publiquen ni sobrescriban el mismo temporal.
- `size_bytes`, `sha256`, `width`, `height` y `mime` son anulables mientras la página aún no tenga metadatos observados. Tamaño permite enteros no negativos; hash, 64 hexadecimales minúsculos; dimensiones se guardan juntas como enteros positivos; `mime` presente no puede estar vacío. Cero bytes se conserva como observación y su aceptación como imagen se decide en el validador, no en la fila.
- `attempts` es entero no negativo e inicia en cero. La cota de reintentos y su política corresponden al motor de descarga.
- Las páginas se ordenan por `ordinal`; datos remotos, tamaño y metadatos incompletos no se reparan ni se inventan al leerlos.

## Alternatives Considered

- Clave UUID independiente: añade identidad sin aportar estabilidad más allá de `(job_id, ordinal)` y permite duplicar ordinales.
- Orden cero-base: no coincide con la numeración humana y de archivo `001.jpg` del flujo de descarga.
- Guardar absoluto el temporal: acopla la DB a una cuenta, unidad o raíz concreta y permite eludir la raíz autorizada.
- Hacer `status` un enum improvisado: la especificación no define los valores ni las transiciones de página.
- Asignar una ruta temporal al construir la fila: inventaría el esquema de nombres y confundiría una ruta reservada con un archivo ya creado.

## Positive Consequences

- El ordinal localiza exactamente cada página sin redondeos ni claves artificiales.
- La asociación por FK conserva páginas junto a su job y bloquea borrados implícitos.
- Una ruta temporal compartida no puede causar que una página sobrescriba el archivo de otra.
- Las respuestas parciales de red pueden persistirse antes de que todos sus metadatos existan.

## Negative Consequences

- Un job no se puede borrar físicamente mientras conserve filas de página; una eliminación futura necesita política y operación explícita.
- `status` abierto conserva estados futuros pero requiere validación en la frontera del manifest una vez aprobada la máquina de estados de página.
- Resolver `temporary_path` exige que el servicio futuro la valide bajo `YomiraData/temp/` antes de abrirla; una fila segura léxicamente no prueba que el archivo exista.

## Failure Modes

- El mismo ordinal se asigna dos veces dentro del job.
- Dos páginas apuntan al mismo temporal y una sobrescribe los bytes de la otra.
- Una ruta absoluta o con `..` escapa de `temp/`.
- Metadatos inválidos se convierten silenciosamente en valores aparentes como hash vacío o dimensiones parciales.
- El reader interpreta un estado desconocido como una página completa.

## Reversal Strategy

Cambiar identidad, base del ordinal, unicidad de ruta o campos obliga a nueva ADR y migración con backup, conteos por job, comprobación de FK y `integrity_check`. Cerrar el vocabulario de `status` requiere definir primero su recuperación y cómo se traducen estados existentes; no se remapean automáticamente.
