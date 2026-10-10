# ADR-0044 - Resolución segura de rutas relativas de biblioteca

## Status

Accepted — implementación de K057 en cumplimiento de ADR-0008 y ADR-0033.

## Context

ADR-0008 requiere guardar rutas relativas a una raíz de biblioteca autorizada y resolverlas en el backend antes del acceso al filesystem. ADR-0033 fija validación léxica de `Asset.relative_path`, pero reserva para la capa de filesystem la comprobación de contención real. En Windows, un segmento puede ser un symlink o junction cuyo destino escapa de la raíz aunque el texto de la ruta sea seguro.

## Decision

- `LibraryPathMapper` recibe una `library_root` absoluta. La raíz debe existir y ser un directorio cada vez que se resuelve una ruta. Una ruta relativa de configuración no se interpreta con respecto al directorio de trabajo actual.
- La raíz configurada se canonicaliza con resolución estricta. Si la propia raíz configurada es un symlink/junction, su destino canónico pasa a ser la raíz autorizada.
- La entrada debe cumplir las invariantes de `Asset.relative_path`: ruta POSIX relativa, sin segmentos vacíos, `.` o `..`, separadores inversos, ADS, caracteres inválidos ni nombres de dispositivo reservados.
- La ruta destino puede no existir todavía para que el mapeador sirva también a una operación futura de creación. Se canonicalizan los componentes existentes; el resultado solo se devuelve si permanece dentro de la raíz canónica. Symlinks/junctions dentro de la biblioteca que apunten dentro de ella se permiten; los que resuelvan fuera se rechazan.
- Resolver una ruta no crea directorios, abre archivos ni modifica contenido. Los fallos se exponen como `LIB_PATH_UNAVAILABLE`, con una razón tipada y sin incluir la ruta recibida ni la raíz configurada en el mensaje.
- `EXPORT_COPY` debe recibir la raíz del destino de exportación mediante su flujo. Este mapeador no asume que una exportación está dentro de `library_root`.
- Esta validación garantiza contención en el instante de resolución. Un servicio futuro de lectura/escritura debe mantener esa garantía durante la operación, protegerse contra cambios concurrentes de symlinks/junctions y aplicar los retries definidos para bloqueos del SO; conservar una ruta resuelta no es un permiso permanente para operar sobre ella.

## Alternatives Considered

- Componer `library_root / relative_path` sin resolver componentes: no detecta escapes por symlink/junction.
- Exigir que el archivo final ya exista: impide mapear destinos para publicación futura.
- Crear automáticamente la raíz o subdirectorios durante el mapeo: mezcla resolución con mutación y puede escribir en una unidad desconectada o mal configurada.
- Permitir raíces relativas al directorio de trabajo: hace que el significado de la biblioteca dependa de cómo se inició el proceso.

## Positive Consequences

- La ruta canónica queda contenida bajo una raíz estable y portable.
- Las rutas no existentes pueden validarse sin crear archivos.
- Las referencias inválidas fallan cerradas y no se filtran en errores visibles.

## Negative Consequences

- Cada resolución consulta el filesystem para validar la raíz y canonicalizar componentes.
- Cambios concurrentes en la estructura del filesystem requieren protección adicional en la operación que abra o escriba el archivo.
- Una raíz desconectada o ausente no puede resolverse hasta que vuelva a estar disponible.

## Failure Modes

- Raíz ausente, no absoluta, no disponible o no-directorio.
- Segmento inválido, traversal o nombre no portable a Windows.
- Symlink/junction que redirige un componente fuera de la raíz.
- Error del sistema al canonicalizar por permisos, volumen desconectado o ciclo de enlaces.

## Reversal Strategy

Cambiar la semántica de la raíz, permitir rutas externas o relajar la contención requiere un nuevo ADR. Las operaciones existentes deben seguir rechazando rutas ambiguas hasta migrar referencias con backup y verificación.
