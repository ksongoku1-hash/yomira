# ADR-0103 — Criterio de aceptación de K116 para V1

## Estado

Aceptado por el usuario — 2026-09-29 (UTC 2026-09-30).

## Contexto

El alcance aprobado clasifica K116 como obligatorio para V1 y K253 (soak test) como V1.3+. K116 se describe como un gate del motor de descargas con estrés e integridad, pero no fija tamaño de carga, repeticiones ni un SLA. La secuencia extendida de fuga/soak no debe reasignarse a K116 si el scope la difiere.

El arnés `backend/tests/run_k116_loopback_smoke.py` registra correctamente `fixture_pipeline_result=PASS`, pero deja `k116_gate_result=NO PASS` en toda corrida individual por requerir estrés sostenido y comportamiento de fuga. Así, su salida no evalúa el gate V1 con los límites existentes.

La corrida de referencia anterior a esta aprobación, `C:\t\k116-2x100-update-20260929a\k116-loopback-20260929T203225_566525Z`, terminó 200/200 transferencias, 2 jobs `COMPLETED`, dos CBZ íntegros y verificación separada tras reabrir la base. Midió pico de working set del proceso de 130 285 568 bytes, sin errores del muestreador, y 200 temporales que sumaron 13 107 200 bytes. El perfil observó concurrencia global 2 y máximo 1 por host.

## Decisión aceptada

Por aprobación expresa del usuario el 2026-09-29, K116 V1 se acepta mediante un smoke offline representativo de **2 jobs × 100 páginas de 65 536 bytes** en dos orígenes fixture. Es una carga concurrente finita; no se presenta como soak ni como prueba de ausencia de fugas a largo plazo.

El resultado será `PASS` únicamente si se cumplen todos los puntos:

1. Se descargan y validan las 200 ocurrencias; ambos jobs acaban `COMPLETED`, se conserva la referencia duplicada y no hay fallos HTTP/transferencia.
2. La fixture observa exactamente 2 solicitudes globales concurrentes y no más de 1 por host, conforme a ADR-0086.
3. Cada CBZ contiene 100 páginas y `ComicInfo.xml`, pasa CRC y verificación de SHA-256 contra las páginas/asset registrados; SQLite devuelve `quick_check=ok` y cero violaciones de claves foráneas.
4. Un proceso separado reabre la DB, consulta el índice y lee la última página de cada capítulo. Tamaño y SHA-256 coinciden con la evidencia durable anterior al cierre.
5. La instrumentación no informa errores; el pico de working set del proceso es menor que **300 000 000 bytes**, usando como techo de aceptación el objetivo inicial de descarga normal `<300 MB` de SPEC §92. El total de temporales de cada job no supera **2 147 483 648 bytes**, límite aprobado en ADR-0088.
6. El artefacto conserva comando, versiones de Windows/Python, resultado de la reapertura y hashes SHA-256 de los archivos de aplicación y del arnés usados en esa ejecución.

CPU, handles y duración se registran para diagnóstico, sin convertirlos en límites de aprobación: el plan y la SPEC no fijan umbrales para K116. Los 240 segundos del arnés no son un SLA; un timeout solo falla si el job queda incompleto, no por exceder un tiempo de producto no aprobado.

K116 usa fixtures y no sustituye K221 (fuente real), K259 (EXE empaquetada), K185/K186 (lector y hardware) ni K253 (soak V1.3+). Esta decisión no cambia el código de producción.

## Aplicación y resultado — 2026-09-29 (UTC 2026-09-30)

La corrida posterior a la aprobación está preservada en `C:\t\YomiraK116ADR0103-20260930\k116-loopback-20260930T025221_386528Z`. Se ejecutó con `backend/tests/run_k116_approved_gate.py`, CPython 3.14.7 y Windows NT 10.0.26200.2. El runner 2×100 terminó con código 0 en **287,56 s**; ese tiempo es diagnóstico y no un límite de aceptación.

El verificador produjo `k116_gate_result=PASS`: pasaron los seis criterios ADR-0103, junto con la presencia y lectura de artefactos. Se completaron **200/200** transferencias de 65 536 bytes; ambos trabajos terminaron `COMPLETED`; el fixture observó concurrencia global 2 y una solicitud por host. Los dos CBZ contienen 100 páginas y `ComicInfo.xml`, miden **6 562 812 bytes**, pasan CRC y tienen SHA-256 `ac6bccb50556fce31ac9c2e6e6dd19d46e0778a00a742c633323e4cf2f4884ca`. SQLite devolvió `quick_check=ok` y cero violaciones FK. Un proceso separado reabrió el índice y leyó la página 100 de cada capítulo, con tamaño y SHA-256 coincidentes con la evidencia pre-cierre. No hubo errores del muestreador; el pico de working set fue **129 642 496 bytes**. Los temporales sumaron **6 553 600 bytes por trabajo** y **13 107 200 bytes** en total.

`provenance_before.json` y `provenance_after.json` cubren **177 archivos** de `backend/app`, arneses, verificadores, lockfiles, SPEC, scope y ADR. El árbol SHA-256 coincide antes y después: `8043293771F2DDAA985B80CEFA67A1E4ED76B13691A0216A1B136112BEDCAC84`; el detalle de cada hash, el comando y la reapertura están preservados junto a `k116_gate_evaluation.json`. El informe completo es `docs/reports/K116_DOWNLOAD_ENGINE_AUDIT_2026-09-28.md`.

## Consecuencias

- K116 pasa en V1 con la evidencia acotada definida aquí; el soak K253 sigue en V1.3+.
- El límite de 300 MB pasa de objetivo inicial de SPEC §92 a techo de aceptación de este gate; modificarlo requiere una revisión versionada.
- Un perfil de fixture no demuestra que una fuente real, el ejecutable empaquetado o un dispositivo físico funcione; sus gates permanecen separados.

## Referencias

- `docs/YOMIRA_V1_SPEC.md`, §§90–94 y Release Gate.
- `docs/YOMIRA_MASTER_EXECUTION_PLAN.md`, K116.
- `docs/YOMIRA_MASTER_EXECUTION_PLAN_V1_SCOPE.md`, K116, K185, K186, K253 y Release Gate V1.
- ADR-0086 — Límites de recursos para descarga de páginas V1.
- ADR-0088 — Tamaño máximo del CBZ V1.
- `docs/reports/K116_DOWNLOAD_ENGINE_AUDIT_2026-09-28.md`.
