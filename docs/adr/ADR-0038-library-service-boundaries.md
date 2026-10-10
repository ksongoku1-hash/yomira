# ADR-0038 — Límites del LibraryService

## Status

Accepted — decisión de implementación de K049, 2026-09-26.

## Context

K049 introduce el servicio local de biblioteca antes de exponerlo mediante la API. `Series` ya define UUIDv4, valores de texto exactos y timestamps UTC; ADR-0028 establece que el título canónico no es único y que dos obras con el mismo título no deben fusionarse. Falta fijar quién abre transacciones y cómo se comporta un alta cuyo ID ya existe.

## Decision

- `LibraryService` recibe la base de datos por inyección y mantiene la frontera de casos de uso; el repositorio convierte entre dominio y filas SQLAlchemy.
- `add_series` es inserción, no upsert. Persiste una entidad `Series` válida dentro de una transacción breve. Si el UUID ya existe, la restricción primaria rechaza el alta y conserva la fila previa.
- Títulos iguales con UUID distintos representan entidades distintas; no se busca ni fusiona automáticamente por título.
- `get_series` recibe un UUID y devuelve la entidad inmutable recuperada o `None` cuando no existe. Los textos se devuelven exactamente como fueron persistidos.
- El listado paginado se define por ADR-0039; K049 no decide filtros ni orden alternativos.
- K049 no modifica metadata existente. Cualquier actualización y resolución `USER_OVERRIDE > LOCAL_CANONICAL > SOURCE_METADATA` queda para K056.
- El servicio no resuelve rutas ni abre archivos; los assets y raíces quedan para las tareas correspondientes.

## Alternatives Considered

- Upsert por ID: podría sobrescribir metadata existente en un alta repetida.
- Fusionar por título: contradice ADR-0028 y puede unir obras distintas.
- Permitir que el servicio use ORM fuera de un repositorio: acopla el caso de uso al esquema físico.
- Resolver paths en el servicio: incumple ADR-0008 y mezcla persistencia con acceso filesystem.

## Positive Consequences

- Las altas y consultas tienen semántica explícita y transacciones pequeñas.
- La identidad y el texto conservados no dependen de heurísticas.
- La API puede reutilizar un servicio que no expone sesiones SQLAlchemy.

## Negative Consequences

- Una repetición del alta con el mismo ID devuelve un error de integridad; la API deberá clasificarlo explícitamente.
- Los filtros y actualizaciones siguen fuera del alcance; se fijan en K055 y K056 respectivamente.

## Failure Modes

- El repositorio modifica texto o timestamps al mapear.
- Un error de commit deja una entidad parcial o reemplaza la entidad existente.
- El servicio comparte una sesión entre operaciones.

## Reversal Strategy

Cambiar altas insert-only, identidad o el límite transaccional requiere ADR nuevo, migración/compatibilidad cuando corresponda y pruebas que demuestren conservación de los datos existentes.
