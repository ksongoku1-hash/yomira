# ADR-0076 — Contrato del manifest versionado de DownloadJob

## Status

Accepted — decisión de Arquitectura Principal el 2026-09-26 para K092, derivada de la sección 21 y los límites de recursos de la especificación. No se atribuye esta decisión al usuario. Los topes iniciales se validarán en K239.

## Context

La sección 21 muestra un manifest por job con `schema_version`, `job_id`, `source_id`, `chapter_id`, `expected_pages` y `pages`; su ejemplo es conceptual, no un manifest poblado con páginas omitidas. Cada página tiene un registro propio y `status` describe su avance, mientras que el estado del job se conserva en su máquina independiente según ADR-0068. ADR-0035 definió los campos de página y dejó el vocabulario de `status` abierto hasta formalizar el manifest y su recuperación. La especificación no fija el rango máximo de los enteros persistidos; K092 cierra una cota inicial compatible con SQLite para que K093 pueda persistirlos sin desbordamiento. K092 necesita un formato interoperable y validable antes de que K093 lo escriba atómicamente y K094 reconcilie sus archivos tras una interrupción.

## Decision

- La versión 1 es un objeto JSON UTF-8 sin BOM. Las únicas claves de nivel superior son `schema_version`, `job_id`, `source_id`, `chapter_id`, `expected_pages` y `pages`. Un lector v1 rechaza campos desconocidos, claves duplicadas, JSON no UTF-8, valores no finitos y versiones distintas de 1; nunca degrada ni reescribe datos de una versión futura.
- Los tres IDs se codifican como UUIDv4 en texto canónico minúsculo con guiones. `expected_pages` es un entero exacto entre cero y 10 000 y debe ser igual al número de entradas de `pages`. Al crear el manifest, se registra una entrada `PENDING` por cada página esperada, con ordinales contiguos desde 1; una vez iniciado el job, el estado de cada página evoluciona conforme a la política posterior del motor. Cero es representable para conservar la respuesta del connector, pero no demuestra que el capítulo pueda completarse. El `pages: []` del ejemplo conceptual no autoriza omitir páginas cuando `expected_pages` es mayor que cero.
- `pages` conserva el orden original y usa ordinales contiguos desde 1. Referencias remotas duplicadas se conservan. Cada objeto de página contiene exactamente `ordinal`, `remote_reference`, `temporary_path`, `status`, `size`, `sha256`, `width`, `height`, `mime` y `attempts`. `size` en JSON corresponde a `size_bytes` en dominio/SQLite. La validación de tipos, hashes, dimensiones y rutas sigue ADR-0035; no se normalizan textos opacos.
- Para compatibilidad con `INTEGER` signed de SQLite, `size`, `width`, `height` y `attempts` deben ser enteros exactos que no excedan `2^63-1` (`9223372036854775807`), además de respetar sus mínimos: `size` y `attempts` no negativos; dimensiones positivas. La especificación no fija este máximo: es una decisión inicial de esta ADR para K093 y se revisará con mediciones y benchmarks en K239.
- El vocabulario v1 de `status` por página es cerrado: `PENDING` (registrada, sin transferencia activa), `DOWNLOADING` (transferencia en curso), `DOWNLOADED` (stream cerrado y bytes/hash declarados, pendiente de validación básica), `VALIDATED` (validación básica declarada y archivo promovido dentro del staging; no significa CBZ publicado), `FAILED` (sin más intentos para esa página) e `INTERRUPTED` (requiere reconciliación antes de reutilizar archivos). Un estado desconocido se rechaza y nunca se traduce silenciosamente. Estos estados no son estados de Job: no hay correspondencia uno a uno ni se deriva el estado del job desde sus páginas. K094 reconcilia por separado el estado del job, las páginas, el manifest y los archivos; K097 implementa las transiciones y la política de reintentos del motor.
- El temporal `.part` permanece bajo `YomiraData/temp/` durante la descarga. Tras superar la validación básica, el motor lo renombra dentro del mismo staging a la ruta de página sin el sufijo `.part`; `temporary_path` conserva una ruta relativa segura bajo `YomiraData/temp/` y apunta al nombre promovido. Por eso, `DOWNLOADED` requiere `temporary_path` no nulo terminado en `.part`, `size` presente y `sha256` presente; `VALIDATED` requiere `temporary_path` no nulo que no termine en `.part`, `size`, `sha256`, `width`, `height` y `mime` presentes. `size` mantiene la regla de entero no negativo; `sha256` debe tener forma de 64 caracteres hexadecimales minúsculos; `width` y `height` deben ser enteros positivos presentes en pareja; `mime` no puede estar vacío. Para `PENDING`, `DOWNLOADING`, `FAILED` e `INTERRUPTED`, K092 no impone requisitos adicionales de campo según el estado. Las reglas generales de tipo, forma y seguridad de ruta de ADR-0035 sí se aplican a todos los valores presentes.
- La semántica de identidad y los límites Windows de `temporary_path` se precisan en ADR-0077. K092 conserva y serializa exactamente cada ruta Unicode válida, rechaza rutas duplicadas por clave `casefold()` dentro del mismo manifest y no consulta ni modifica el filesystem. K093 debe imponer unicidad global persistente de esa misma clave y crear destinos sin sobrescribir archivos existentes; antes de indexar datos previos, debe detectar colisiones y fallar sin borrar ni renombrar rutas o archivos. Esta decisión se limita a `temporary_path`: no restringe ni normaliza `remote_reference`, nombres de biblioteca/exportación u otros metadatos Unicode.
- K092 valida estructura, tipos, vocabulario y evidencia declarada por estado, pero no consulta el filesystem, no comprueba que un archivo exista, no calcula ni compara el hash real, no decodifica imágenes y no ejecuta recovery. La forma declarada de `size`, `sha256`, dimensiones, MIME y ruta no prueba que coincida con los bytes. La validación del contenido y la reconciliación de DB/manifest/filesystem pertenecen a las etapas posteriores, incluida K094.
- El manifest admite como máximo 10 000 páginas, 8 MiB serializados, profundidad JSON 32 y 430 000 tokens JSON. El preflight cuenta tokens léxicos JSON (delimitadores, separadores, claves y valores; ignora whitespace) y profundidad de anidamiento antes de materializar el objeto completo. También rechaza lexemas numéricos JSON de más de 20 caracteres —contando signo y cualquier parte fraccionaria o exponente— antes de materializar. Se rechaza el valor completo que exceda cualquiera de los topes; no se trunca ni se parte. Son valores iniciales de esta ADR, versionados y sujetos a medición en K239 con perfiles mínimo, referencia y stress.
- La serialización pura produce JSON determinista UTF-8, claves de objetos ordenadas, separadores compactos y todas las claves opcionales presentes con `null` cuando no tengan observación. Antes de materializar la salida, el serializer calcula de forma acotada el tamaño exacto en bytes del JSON UTF-8 que produciría; si supera 8 MiB, rechaza el manifest sin generar una salida sobredimensionada. Si cabe en el límite, emite la salida compacta determinista. El parser comprueba el límite de bytes antes de decodificar y aplica los límites de profundidad y tokens durante el preflight. K092 no realiza I/O de archivos; K093 define escritura atómica y K094 la recuperación filesystem ↔ DB.
- SQLite conserva sus textos `status` existentes. No se agrega migración ni se remapean filas: un valor que no pertenezca al vocabulario v1 bloquea su conversión a manifest sin modificar la fila original. K095 aportará el diagnóstico visible de esa contradicción.

## Alternatives Considered

- Aceptar campos y versiones desconocidos como si fueran v1: podría interpretar mal o perder datos añadidos por una versión futura.
- Reordenar o deduplicar páginas: altera la secuencia exacta devuelta por el connector y contradice ADR-0051.
- Mantener `status` como texto arbitrario también en el manifest: dificulta recuperación determinista y deja sin resolver la decisión pendiente de ADR-0035.
- Cargar manifests de tamaño ilimitado o recortar páginas excedentes: crea consumo no acotado o pérdida silenciosa de contenido.
- Escribir desde el serializador: mezcla el contrato de datos con atomicidad; la persistencia corresponde a K093.

## Positive Consequences

- El formato v1 es estable, verificable y determinista, y mantiene el orden y las referencias remotas sin transformación.
- Un lector nuevo falla cerrado ante datos futuros o contradictorios sin modificar el archivo original.
- Los límites fijan un máximo de memoria de entrada y un máximo de registros de página por job.
- El contrato deja separadas serialización, persistencia atómica y recuperación.

## Negative Consequences

- Un manifest mayor de 8 MiB, con más de 10 000 páginas, más de 32 niveles o más de 430 000 tokens se rechaza aunque sea legítimo; K239 puede proponer nuevos límites mediante ADR, nunca mediante truncamiento.
- Estados de página emitidos fuera del vocabulario v1 no pueden exportarse hasta definir una conversión explícita con preservación de datos.
- Los límites de página y el tamaño de referencia requieren pruebas de frontera y benchmarks en K239.

## Failure Modes

- JSON truncado, codificación inválida, clave duplicada, esquema desconocido, lexema numérico mayor de 20 caracteres o exceso de límites de preflight: rechazo sin modificar la fuente.
- Número de páginas distinto de `expected_pages`, ordinal repetido o hueco: rechazo del objeto completo.
- Estado de página desconocido, evidencia declarada ausente para `DOWNLOADED`/`VALIDATED`, hash mal formado o path temporal inseguro: rechazo; nunca se asume que la página está completa. La existencia y el contenido real del archivo se verifican después, no en K092.
- El límite de páginas o bytes se excede: rechazo antes de persistir o publicar.

## Reversal Strategy

La versión 1 es inmutable. Cambiar claves, tipos, vocabulario, límites, semántica de estado o relación entre estados requiere nueva ADR y una versión explícita con migración verificable. No degradar manifests futuros ni reparar rutas, hashes, estados u ordinales de forma heurística.
