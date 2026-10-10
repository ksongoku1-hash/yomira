# ADR-0109 — Catálogo externo para metadatos ausentes

- Estado: ACCEPTED bajo la petición del usuario de completar portadas y metadatos desde un sitio externo cuando falten en la fuente.
- Fecha: 2026-10-01 (America/Bogota).
- Autoridad: SPEC, resolución de metadatos vigente y ADR de portadas verificadas; prioridad de integridad y procedencia.

## Decisión

Se registra `yomira.metadata.mangadex` como catálogo de búsqueda y metadatos. No ofrece capítulos, páginas ni descarga de capítulos. Usa el transporte existente, sin dependencias nuevas, cookies ni credenciales: TLS, orígenes exactos `https://api.mangadex.org` y `https://uploads.mangadex.org`, respuestas/portadas de hasta 4 MiB, los timeouts y límites de concurrencia vigentes y hasta cinco redirects solo a orígenes registrados. El segundo origen sirve exclusivamente portadas. Las peticiones del catálogo se identifican con un User-Agent de Yomira; no se simula un navegador. Las portadas solo admiten URLs estáticas sin query ni fragment.

La prioridad por campo es corrección del usuario (incluido null o vacío), valor canónico local, metadatos de fuentes de capítulos y, finalmente, catálogo externo. Un conflicto entre fuentes primarias no se resuelve eligiendo datos externos. Se conservan todos los candidatos y su procedencia. La asociación externa se almacena como SeriesSource, con el identificador y la URL pública de la ficha; no se copia la ficha sobre campos canónicos. No requiere migración. La restricción existente de identidad remota sigue vigente: una ficha ya vinculada a otra serie local causa conflicto, sin fusionar series ni modificar asociaciones.

El respaldo automático se ejecuta al asegurar/actualizar metadatos y portadas, solo si hay campos realmente ausentes y no hay ya una asociación con el catálogo. La búsqueda es acotada a 100 resultados. Solo se asocia automáticamente una única coincidencia exacta del título o alias normalizado (Unicode NFKC, casefold y espacios), con año idéntico si el título local termina explícitamente en `(YYYY)`, y si la respuesta representa el conjunto completo. No se eliminan palabras de edición ni subtítulos. Cero, varias coincidencias o respuesta incompleta dejan los datos intactos. La interfaz permite buscar y elegir explícitamente entre diez candidatos. Una nueva elección no reemplaza una ficha ya vinculada.

Los fallos del respaldo automático se registran con código y trace_id, sin URLs/cuerpos, y no impiden conservar ni descargar capítulos. La búsqueda/selección explícita devuelve errores trazables. Desactivar la fuente del catálogo impide nuevos accesos de red. Los lectores y thumbnails no consultan el catálogo. Las portadas importadas usan el pipeline vigente de verificación, SHA-256, presupuesto de disco, publicación sin reemplazo y lectura local. Una corrección concurrente prevalece al revalidar antes del índice.

## Verificación

Pruebas que fallen antes de la implementación para User-Agent acotado y sin fugas, coincidencias/ediciones/año/ambigüedad, prioridad de campos y null explícito, persistencia idempotente/conflicto, consulta y selección API/UI, validación de portada y orígenes. Regresión de conectores y downloads, Ruff/Pyright, TypeScript/ESLint, build nueva y prueba API no visual de la biblioteca existente. Se conserva el CBZ y no se vuelve a descargar el capítulo. El smoke visual sigue pendiente por decisión previa del usuario.

## Ajuste de cota durante integración

La búsqueda real de Naruto devolvió 61 registros y 275 283 bytes. La ficha canónica contiene 137 relaciones; el límite inicial de 64 rechazaba el resultado. El parser ahora admite hasta 256 relaciones por ficha, conserva UUIDs válidos y el límite de respuesta HTTP de 4 MiB. Las pruebas cubren 137 y 256 relaciones y rechazan 257. Esta cota no amplía orígenes ni capacidades.
