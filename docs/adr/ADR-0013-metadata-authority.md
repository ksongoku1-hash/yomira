# ADR-0013 — Autoridad de metadatos

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

Los conectores pueden devolver títulos, descripciones, idioma, autores o portadas distintos a los elegidos localmente.

## Decision

Aplicar precedencia por campo `USER_OVERRIDE` > `LOCAL_CANONICAL` > `SOURCE_METADATA`. Conservar y distinguir los valores remoto, local y override; nunca sobrescribir silenciosamente una elección del usuario.

## Alternatives Considered

- Última escritura gana: permite que una fuente borre silenciosamente una corrección local.
- Bloquear toda metadata remota: impide importar información inicial útil.
- Fusionar automáticamente por título: puede unir obras distintas.

## Positive Consequences

- La autoridad de cada valor es visible y reversible.
- Una fuente puede aportar datos sin dominar el estado local.

## Negative Consequences

- El modelo almacena más de un valor por campo sensible.
- La UI debe indicar el origen y permitir resolver conflictos.

## Failure Modes

- Confundir campo no establecido con override vacío, o reemplazar un override por sincronización.

## Reversal Strategy

Migrar precedencias por campo conservando snapshots y procedencia. Cualquier orden nuevo requiere ADR y una vista previa antes de aplicar cambios en lote.
