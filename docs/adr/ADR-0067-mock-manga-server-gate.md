# ADR-0067 — Mock Manga Server Gate

## Status

Accepted for K080 implementation — 2026-09-26.

## Context

K071–K079 agregan escenarios de transporte y datos inválidos. Un test aislado puede dejar de ejecutarse o una variante puede volverse aleatoria sin que el nombre `Mock Gate` lo revele. La especificación exige un servidor reproducible y §87–§89 requieren integration/fault tests offline.

## Decision

- `backend/tests/test_mock_manga_gate.py` es la matriz ejecutable K080 y forma parte automáticamente de Pytest y del quality gate completo.
- Un test compara el conjunto cerrado de `MockScenario` con los 14 escenarios exigidos por §83 y K071–K079. Añadir/eliminar un modo exige actualizar conscientemente el gate y ADR.
- Para cada escenario, el gate abre dos instancias nuevas, emite la request representativa y compara status/resultado, `Content-Type`, longitud declarada, `Retry-After`, body y detalle de truncamiento cuando corresponda.
- El snapshot ignora port efímero, cabecera Date, tiempos medidos y mensajes de excepción del cliente. Para slow/timeout verifica igualdad de respuesta o clasificación timeout, no igualdad de scheduling.
- Las instancias se usan en context manager, el puerto solo está en loopback, y el worker limit se conserva en cuatro en toda la matriz.
- Las pruebas específicas siguen validando semántica (decodificación, recuperación, page count, duplicados); igualdad entre instancias no las sustituye.
- Pytest, Ruff, Pyright y smoke/gates globales deben pasar; no se permite `skip` para escenarios requeridos.

## Alternatives Considered

- **Comparar snapshots completas de HTTP con puerto/Date:** produce falsos cambios entre ejecuciones aunque el fixture sea igual.
- **Comparar solo body:** pasa por alto cambios de status, MIME, `Content-Length`, `Retry-After` o truncamiento.
- **Probar solo que cada endpoint responde una vez:** no demuestra reproducibilidad.
- **Mantener una lista manual separada de Enum sin assert de igualdad:** permite que los tests queden obsoletos cuando un escenario cambia.
- **Registrar snapshots externos o golden files:** añade archivos de mantenimiento para bodies que deben seguir pequeños, sintéticos y exactos.

## Consequences

- El gate detecta escenarios omitidos y no deterministas de forma local, sin Internet ni archivos golden.
- TIMEOUT demora deliberadamente el cleanup hasta que termine el worker finito; el tiempo está acotado por K073.
- Los escenarios prueban fixtures de transporte, no comportamiento final de un Download Engine todavía inexistente.

## Failure Modes

- Enum no coincide con el conjunto requerido: gate falla.
- Dos instancias devuelven status, headers relevantes, body o truncamiento distinto: gate falla.
- Una excepción del cliente no corresponde al tipo de escenario o un worker sigue vivo: falla el escenario específico.
- Se omite una modalidad con skip: revisión/gate debe rechazarse.

## Reversal Strategy

Cambiar el conjunto de casos o qué campos son deterministas requiere actualizar la especificación de pruebas, ADR, matriz K080 e informe de Fase 7. El test debe seguir cubriendo timeout y disconnect aunque su snapshot no sea un HTTP body completo.
