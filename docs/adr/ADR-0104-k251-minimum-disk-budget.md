# ADR-0104 — Presupuesto mínimo de disco para K251

## Estado

**Accepted — aprobada explícitamente por el usuario el 2026-09-29 (America/Bogota).**

## Contexto

K251 es obligatorio en V1 y exige cerrar la admisión de mutaciones ante disco lleno, detener y esperar a los workers, preservar sus artefactos y reabrir solo después de comprobar la recuperación y el espacio. ADR-0015 requiere presupuestar DB/WAL, logs, temporales, CBZ y thumbnails; ADR-0079 fija la ecuación `free_bytes >= recovery_demand_bytes + minimum_reserved_bytes`, la reserva atómica antes del claim, la comprobación read-only cada 30 segundos y el bloqueo fail-closed cuando no se puede demostrar espacio seguro. Esas ADR no asignan cifras a `minimum_reserved_bytes` ni definen cómo convertir los límites por job en una reserva por volumen.

ADR-0088 ya fija los límites por job: hasta 2 GiB para temporales de páginas y hasta 2 GiB para el CBZ en staging/publicación. Por tanto, un job puede requerir hasta 4 GiB de escrituras de artefactos. La aplicación permite hasta dos descargas concurrentes (ADR-0086). El código configura hasta diez archivos de log de 25 MiB cada uno, y el manifest tiene un máximo de 8 MiB con hasta cuatro sidecars de recovery retenidos/transitorios por job según ADR-0079.

## Decisión

La política aprobada fija el presupuesto mínimo y el comportamiento requerido para cerrar K251 en V1. No añade el panel ni las capacidades generales del DiskBudgetManager avanzado asignadas a V1.2.

### Límites propuestos

- Reserva mínima libre: **512 MiB por cada volumen administrado**.
- Reserva máxima inicial de artefactos: **4 GiB por job**, dividida en hasta 2 GiB de temporales de páginas y hasta 2 GiB de CBZ/staging, conforme a ADR-0088.
- Demanda dinámica de recuperación para el volumen que contiene SQLite: `db_bytes + max(db_bytes, wal_bytes)`. `db_bytes` y `wal_bytes` son los tamaños observados de la DB y su WAL al evaluar el presupuesto; la cifra representa una copia de DB para recuperación más margen de checkpoint/escritura hasta el mayor entre el tamaño de DB y el WAL existente. Si no puede medirse alguno, el volumen se considera inseguro.

Las cifras usan unidades binarias: 1 GiB = 1 073 741 824 bytes; 1 MiB = 1 048 576 bytes.

### Regla de admisión por volumen

Se identifica el volumen físico de cada directorio administrado. No se supone que `data_root`, la biblioteca y SQLite estén en el mismo volumen. Para cada volumen `v`, antes de reclamar un job:

```text
required_bytes(v) = outstanding_reservations(v)
                 + new_job_reservation(v)
                 + recovery_demand_bytes(v)
                 + 536_870_912

admit only if free_bytes(v) >= required_bytes(v)
```

`new_job_reservation(v)` asigna a cada volumen solo las partes de la cota del job que se escribirán allí; por ejemplo, si páginas temporales y CBZ están en volúmenes distintos, cada reserva se divide entre ellos. `recovery_demand_bytes(v)` es cero salvo en el volumen de SQLite. Cada volumen aplica la reserva mínima de 512 MiB. Una identidad de volumen, capacidad, espacio libre, ruta o tamaño requerido desconocido, inválido o fuera de rango deniega nuevas admisiones.

La reserva de un job representa sus bytes máximos aún no escritos. Cada escritura confirmada convierte exactamente esos bytes reservados en bytes ocupados bajo la coordinación atómica del presupuesto; una escritura ambigua conserva la reserva hasta reconciliarse. Cancelar, fallar o perder conectividad no libera una reserva hasta que el worker haya parado, confirmado su quiescencia y terminado la clasificación preservadora de sus artefactos. En startup no se despachan jobs hasta reconciliar los jobs existentes y recuperar las reservas restantes a partir del inventario verificable.

### Justificación de la reserva mínima de 512 MiB

La cifra aprobada cubre en conjunto:

- hasta 250 MiB para el máximo configurado de diez logs de 25 MiB;
- hasta 64 MiB para dos jobs concurrentes y cuatro sidecars de manifest de hasta 8 MiB por job;
- 198 MiB de margen operativo.

La copia de recuperación de SQLite se presupuesta aparte mediante `recovery_demand_bytes`. La reserva de 512 MiB es un margen adicional y no autoriza superar límites de páginas, manifest, CBZ o temporales de ADR-0088. Los thumbnails no tienen reserva de disco en esta propuesta porque el caché V1 actual es RAM-only; si pasan a persistirse, se requiere una ADR antes de cambiar este cálculo.

### Reacción a falta de espacio y reapertura

- El gate compartido del volumen se cierra sincrónicamente ante `ENOSPC`, `ERROR_DISK_FULL`, `ERROR_HANDLE_DISK_FULL` o `SQLITE_FULL`. No se crean claims nuevos ni se permite otra mutación después del cierre.
- K096 detiene todos los workers del volumen afectado y espera sus acknowledgements de quiescencia. Se preservan páginas, `.part`, manifests, sidecars, CBZ y cualquier resultado ambiguo; no se reintenta una operación por falta de espacio.
- Mientras el presupuesto sea inseguro solo se realizan comprobaciones read-only cada 30 segundos. La reserva vuelve a calcularse con tamaños y espacio actuales; no se presume que borrar temporales sea seguro.
- El gate solo se reabre después de quiescencia completa, recovery/DB verificados, identidad de volumen estable y una nueva evaluación que cumpla la ecuación en cada volumen afectado. Si recovery o la comprobación SQLite falla, se conserva el gate cerrado y se activa el resultado de Safe Mode que corresponda.

## Verificación requerida para K251

La implementación se prueba después de aprobar esta ADR, con inyección de fallos y sin modificar datos del usuario:

1. El límite exacto de la ecuación admite; un byte menos rechaza antes del CAS de claim y antes de abrir archivos de salida.
2. Dos claims concurrentes no pueden asignar dos veces la misma capacidad; un write convierte una reserva por el mismo número de bytes confirmados y un resultado ambiguo no libera capacidad.
3. Se verifica la división de las reservas si DB, temporales de páginas y CBZ están en volúmenes distintos; volumen o medición desconocidos mantienen cerrado el gate.
4. Un startup con jobs pendientes no despacha antes de recovery, inventario verificable y readmisión de sus reservas restantes.
5. `ENOSPC` y `SQLITE_FULL` en cada frontera de claim, escritura, commit y recovery cierran el gate sin pérdida ni sobrescritura; K096 espera quiescencia y no hay mutaciones posteriores.
6. El monitoreo cada 30 segundos no muta datos; el gate solo se reabre tras DB/recovery íntegros y presupuesto suficiente, incluidos los casos justo en el límite y un byte por debajo.

El gate no pasa si solo se conserva un error en logs, si un job permanece en cuarentena sin salida durable, si un worker no confirma quiescencia, o si el test excluye una reserva activa del total.

## Consecuencias

- 512 MiB y la ecuación de recuperación son deliberadamente conservadores: en volúmenes pequeños pueden impedir una descarga que habría cabido, pero dejan una cifra verificable para reserva y recuperación.
- Dos jobs pueden reservar hasta 8 GiB agregados para artefactos, además de la recuperación y el margen por volumen. El espacio usado previamente ya se refleja en `free_bytes`.
- La aprobación de esta ADR fija la política, pero no aprueba la instalación de una build ni declara K251 PASS. K251 requiere completar y documentar las verificaciones indicadas en esta ADR y en el gate vigente.

## Alternativas consideradas

- Mantener la reserva indefinida sin número: rechazada porque no permite una decisión auditable en el borde de espacio.
- Usar únicamente una cuota por job: rechazada porque no contempla DB/WAL, logs ni la competencia entre jobs.
- Permitir que el SO descubra el disco lleno: rechazada por ADR-0015; el fallo tardío no es una política de admisión ni de recovery.

## Reversión

Cambiar 512 MiB, la fórmula de recuperación, límites por job o la política de cierre/reapertura requiere una ADR sucesora y repetir las pruebas de borde, concurrencia, crash y preservación. No reducir la reserva de forma automática ante un rechazo de admisión.

## Referencias

- `docs/YOMIRA_V1_SPEC.md`, §§61, 90–94.
- `docs/YOMIRA_MASTER_EXECUTION_PLAN_V1_SCOPE.md`, K251 y alcance V1.
- ADR-0015 — Presupuesto de disco.
- ADR-0079 — Metadata y reconciliación del checkpoint de recovery.
- ADR-0086 — Concurrencia de descarga V1.
- ADR-0088 — Tamaño máximo del CBZ V1.
