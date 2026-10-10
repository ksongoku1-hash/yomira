# ADR-0060 — Escenarios lentos y timeout finito del servidor falso

## Status

Accepted for K073 implementation — 2026-09-26.

## Context

La especificación §83 exige escenarios `slow` y `timeout`. Un test de resiliencia debe distinguir latencia lenta pero válida de una espera que supera el timeout del cliente, y debe demostrar que el servidor queda disponible después. La capacidad del fixture no puede crecer con el número de conexiones.

## Decision

- Seleccionar `SLOW` o `TIMEOUT` al construir una instancia; no cambiar modo por request mientras existan requests en vuelo.
- `SLOW` retrasa respuestas de fuentes 200 ms y luego devuelve el fixture normal completo. `/health` permanece inmediato para observar el proceso.
- `TIMEOUT` espera 800 ms antes de enviar la primera cabecera. Los tests usan un timeout de cliente menor (150 ms); el servidor no interpreta ni fija el timeout del cliente.
- El worker pool permanece limitado a cuatro. El fixture reporta workers concurrentes activos y el máximo observado para que tests de recursos puedan verificar el límite sin instrumentar código de producto.
- Tras un timeout del cliente, el servidor termina el handler finito, libera su slot y `/health` vuelve a responder. El cierre del context manager espera los workers del servidor antes de devolver control.
- Las duraciones son constantes de test, no configuración de usuario ni afirmaciones sobre tiempos de una red real.

## Alternatives Considered

- **Sleep infinito para timeout:** ocupa un worker para siempre y rompe el cleanup, ocultando fugas de recursos.
- **Generar timeout del lado cliente desde el fixture:** no ejercita el timeout HTTP real observado por un connector.
- **Delay aleatorio o escalado con el host:** hace que la reproducción dependa del hardware y vuelve inestable el gate.
- **Aplicar slow al health check:** impide diferenciar liveness del retraso de una operación remota y dificulta confirmar recuperación.

## Consequences

- El caso slow permite verificar una respuesta íntegra tras demora; el caso timeout valida el comportamiento del cliente con una espera acotada.
- El timeout real de un connector permanece en el cliente, y debe probarse con su configuración propia en una fase de integración.
- La recuperación se observa en loopback; esto no representa un corte físico de red.

## Failure Modes

- Modo slow responde antes del mínimo: el test falla.
- Modo timeout entrega headers dentro del timeout cliente: el test falla.
- Handler no libera el worker luego del timeout: el contador no llega a cero o `/health` deja de responder.
- Concurrencia supera cuatro requests: el test de presupuesto falla.

## Reversal Strategy

Cambiar delays, capacidad de workers, alcance de health o la semántica de respuesta requiere actualizar este ADR, los límites de duración de tests y la evidencia del Mock Gate.
