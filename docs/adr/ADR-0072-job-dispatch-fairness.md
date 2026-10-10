# ADR-0072 — Fairness de despacho de jobs por prioridad, source y host

## Status

Accepted — decisión de Arquitectura Principal el 2026-09-26 para completar K087. No se atribuye esta decisión al usuario.

## Context

K086 ordena todos los jobs `HIGH` antes de `NORMAL` y `LOW`; una llegada continua de `HIGH` puede postergar indefinidamente a las otras prioridades. Además, `list_queued(limit=8)` puede llenar su ventana con una sola fuente que tenga una cola grande. El Job Manager debe respetar límites globales, por fuente y por host sin cargar el backlog completo.

Las referencias remotas de conectores son opacas (ADR-0032 y ADR-0051). La base de datos no contiene un origen HTTP resuelto del que pueda derivarse con seguridad el host.

## Decision

- Cada ciclo de despacho usa una rueda repetitiva de ocho slots: cuatro `HIGH`, tres `NORMAL` y uno `LOW`. Si la prioridad del slot actual no tiene jobs, el cursor avanza al siguiente slot disponible; una clase vacía no bloquea a las demás.
- El cursor de prioridad avanza por cada candidato elegido, también cuando el caller solo puede admitir un job. No se reinicia entre lecturas del mismo runtime.
- Dentro de cada prioridad, el cursor de sources selecciona el siguiente `source_id` después del último elegido; al llegar al final, vuelve al primero disponible. Una source no recibe otro turno hasta recorrer las otras sources elegibles. Dentro de una source se conserva FIFO por `created_at_us` y `job_id`.
- La consulta de un candidato devuelve como máximo una fila y usa `LIMIT 1`; completar una ventana de hasta ocho ejecuta hasta ocho selecciones. No se calculan rangos de todo el backlog ni se materializa este en RAM.
- El despachador conserva como máximo ocho candidatos y el cursor actual en memoria. Los cursores se reinician al arrancar el proceso; los jobs siguen siendo durables en SQLite. Si una operación no se pudo admitir por source o host saturado, su job_id se puede excluir en el resto de la ventana y se reintenta en un ciclo posterior.
- `ConcurrencyLimiter` concede permisos atómicos sujeto a límites positivos de concurrencia global, por source y por host. Si un límite está lleno, devuelve `None` sin modificar contadores; el job permanece `QUEUED` y podrá reintentarse en un ciclo posterior. Liberar un permiso inexistente o ya liberado produce un error trazable.
- El permiso de host requiere un `host_key` explícito del resolvedor de solicitudes. La clave representa el origen efectivo normalizado (esquema, hostname IDNA en minúsculas y puerto efectivo); este módulo la trata como dato opaco y no analiza `remote_url`, `remote_reference` ni identificadores del connector. K097/K105 deben proporcionar la clave y volver a adquirir permiso si una redirección cambia el origen.
- El conjunto de permisos activos está acotado por el límite global, que no puede exceder el máximo de ventana 8. La reserva libera el permiso también ante una excepción local; un cierre abrupto termina los permisos volátiles y la reconciliación de jobs queda bajo K091/K094.

## Alternatives Considered

- Mantener prioridad estricta `HIGH > NORMAL > LOW`: deja que un flujo continuo HIGH cause starvation permanente.
- Calcular `ROW_NUMBER` sobre todas las filas pendientes: puede forzar a SQLite a ordenar o materializar un conjunto proporcional al backlog.
- Materializar el backlog y repartirlo en memoria: el consumo de RAM crecería con el número total de jobs y contradice K085.
- Inferir el host desde `remote_reference` o `remote_url`: esos valores son opacos y podrían ser IDs, rutas relativas o datos controlados por el connector.
- Mantener semáforos independientes sin actualización atómica: dos admisiones concurrentes podrían superar un límite.

## Positive Consequences

- La rueda 4:3:1 limita la espera de cada prioridad cuando todas tienen backlog, incluso con una admisión por llamada.
- Las fuentes con colas grandes no ocupan varios turnos antes de que las fuentes activas obtengan el suyo.
- Los límites global, source y host se comprueban y actualizan bajo una única sección crítica, con memoria acotada.
- La política no altera el estado durable, la identidad ni los timestamps de los jobs.

## Negative Consequences

- HIGH ya no tiene precedencia absoluta; comparte la capacidad con NORMAL y LOW para cumplir anti-starvation.
- La proporción 4:3:1 requiere que el mismo cursor viva durante el runtime; una reinicialización del proceso vuelve a iniciar la rueda en `HIGH`.
- Mientras no exista el resolvedor HTTP de K097, K087 puede validar el límite por host solo cuando un caller aporte explícitamente la clave de origen.

## Failure Modes

- Límite inválido, UUID no válido o host key vacía: rechazar antes de cambiar contadores.
- Capacidad global, source o host saturada: no conceder permiso ni cambiar el estado del job.
- Liberación duplicada o ajena: informar el error y conservar el estado de permisos.
- Falta de host key resuelta: el caller no debe iniciar la operación de red; no se usa una referencia opaca como fallback.

## Reversal Strategy

Cambiar cuotas, orden de fuentes, semántica de host key o reglas de concurrencia requiere una ADR nueva y pruebas de anti-starvation, saturación y recuperación. Los jobs persistidos se conservan; una reversión solo cambia futuras selecciones y admisiones.
