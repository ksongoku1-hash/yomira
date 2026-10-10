# ADR-0071 — Orden inicial de prioridades para jobs

## Status

Accepted — decisión de Arquitectura Principal el 2026-09-26, conforme a las prioridades tipadas de ADR-0034 y la sección 20 de la especificación. No se atribuye esta decisión al usuario.

## Context

K086 debe hacer que `HIGH`, `NORMAL` y `LOW` afecten el despacho. La cola durable de K085 ya lee lotes pequeños desde SQLite y conserva orden FIFO determinista.

## Decision

- El orden de selección es `HIGH`, luego `NORMAL`, luego `LOW`.
- Dentro de cada prioridad, se conserva FIFO por `created_at_us` y `job_id`.
- Prioridad solo cambia el orden de selección; no altera status, identidad, timestamps ni prioridad persistida.
- El orden final de K086 permanece sujeto a las reglas anti-starvation y fairness global/source/host de K087. No se aprueba la liberación de Fase 8 hasta que esas reglas estén verificadas.

## Alternatives Considered

- Ignorar prioridad y usar FIFO global: no materializa la diferencia entre `HIGH`, `NORMAL` y `LOW`.
- Ordenar por timestamp sin `job_id`: empates de microsegundos pueden producir órdenes no deterministas.
- Permitir que `HIGH` ignore indefinidamente `NORMAL`/`LOW`: contradice la regla de que un job HIGH no cause starvation permanente y requiere una política de fairness de K087.

## Positive Consequences

- El próximo lote da precedencia visible a jobs `HIGH` sin perder orden estable dentro de una prioridad.
- El `LIMIT` de K085 continúa acotando cuántos registros llegan a memoria.

## Negative Consequences

- Este orden por sí solo no impide starvation bajo una llegada continua de jobs de prioridad superior; K087 debe implementar fairness antes de completar la fase.

## Failure Modes

- Prioridad no reconocida: la constraint SQLite/domain la rechaza; no se convierte silenciosamente a otra clase.
- Empate temporal: `job_id` desempata sin depender del orden incidental del motor.

## Reversal Strategy

Modificar ranking o desempate requiere una ADR y pruebas con cargas mixtas; conservar los valores de prioridad existentes y no reescribirlos al cambiar la política.
