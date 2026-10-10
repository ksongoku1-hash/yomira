# ADR-0046 — Contrato base del Source Connector

## Status

Accepted for K059 implementation — 2026-09-26.

## Context

K059 necesita fijar la frontera entre cada conector y el core sin adelantar los esquemas que K061–K064 deben definir para búsqueda, series, capítulos y páginas. ADR-0006 ya prohíbe que el conector escriba en la biblioteca, ejecute SQL arbitrario, construya CBZ o coordine estados de jobs. La especificación enumera las operaciones conceptuales, pero todavía no determina tipos concretos para sus datos.

El test de contrato local de K059 falla al importar `app.connectors.protocol`: no existe el paquete ni una interfaz que permita a Pyright verificar estructuralmente el fixture.

## Decision

- `SourceConnector` será un `typing.Protocol` genérico y sin estado. Los conectores lo satisfacen estructuralmente al proporcionar las operaciones requeridas; no se exige herencia ni se añade registro o descubrimiento en runtime.
- El protocolo define `health_check`, `search`, `get_series`, `get_chapters`, `get_chapter`, `get_pages` y `normalize_metadata`, con las entradas y salidas genéricas necesarias para tiparlos sin fijar los DTOs de tareas posteriores.
- Las seis operaciones que pueden realizar I/O remoto son asíncronas. `normalize_metadata` es síncrona, ya que representa una transformación local del dato recibido.
- Los resultados de búsqueda, capítulos y páginas se expresan como `Sequence`, sin exigir una representación mutable concreta.
- Los DTOs, validaciones de payload, errores `SRC_*`, y la regla para invocar una operación según capacidades pertenecen respectivamente a K061–K065 y K060. K059 no añade un método especial para `UPDATE_CHECK` ni define su comportamiento.
- Este protocolo es una frontera de tipos, no un sandbox de seguridad. Las restricciones de acceso del proceso corresponden a K067.
- La compatibilidad del contrato se verifica con fixtures locales sin Internet y con el chequeo estricto de Pyright.

## Alternatives Considered

- **Clase base abstracta:** requerir herencia impondría acoplamiento a una jerarquía concreta y dificultaría adaptar conectores existentes; no es necesaria para comprobar el contrato estático.
- **Firmas con `Any` o `object`:** evitarían definir tipos ahora, pero ocultarían incompatibilidades en los límites entre connector y core.
- **Definir todos los DTOs en K059:** adelantaría el alcance de K061–K064 y mezclaría contratos independientes en una sola tarea.
- **Validación o despacho runtime en el protocolo:** mezclaría el contrato de implementación con las políticas de capacidades y el registry de K068.

## Consequences

- Pyright puede detectar conectores que omiten métodos o declaran firmas incompatibles, aunque no hereden del protocolo.
- Las implementaciones remotas pueden hacer I/O concurrente mediante operaciones asíncronas; la normalización permanece local y síncrona.
- Los tipos genéricos se concretarán en los contratos posteriores, que podrán validar el contenido sin cambiar la frontera de aislamiento de ADR-0006.
- Como el protocolo no impone herencia ni validación runtime, los gates de compatibilidad deberán ejecutar fixtures de contrato y chequeo de tipos.

## Failure Modes

- Un conector omite una operación o declara una firma incompatible: Pyright o el gate de contrato falla antes de aceptar la implementación.
- Un payload remoto incumple el contrato de datos: los contratos y validaciones de K061–K064 deben rechazarlo y K065 asignará el error trazable; no se fabricarán resultados.
- Se intenta invocar una operación que el connector no ofrece: K060 definirá la comprobación de capacidades y K065 su error estable; este ADR no prescribe una llamada de red de fallback.
- Un protocolo se confunde con aislamiento de seguridad: los permisos efectivos se resolverán en K067; la anotación de tipos no limita por sí sola el acceso del proceso.

## Reversal Strategy

Cambiar el modelo asíncrono, los nombres de las operaciones o la frontera estructural requiere un ADR nuevo y pruebas de compatibilidad. No se deben reinterpretar silenciosamente los conectores existentes ni concederles acceso a DB o a la biblioteca para resolver una incompatibilidad.
