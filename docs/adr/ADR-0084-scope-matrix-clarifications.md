# ADR-0084 — Aclaraciones de alcance para habilitar pruebas end-to-end de V1

## Estado

Accepted — 2026-09-27. Refinamiento de alcance autorizado por el usuario al pedir seguir la directriz de simplificación sin abandonar el desarrollo actual. Es sucesora de alcance de ADR-0081 y no modifica la SPEC aprobada ni el plan canónico K001–K284.

## Contexto

La primera revisión del plan sucesor preservó los IDs, pero dejó contradicciones internas: K058 aún exigía el benchmark de 10 000 series como gate V1, K221 aparecía en dos versiones, y la clasificación de varias capacidades excedía el mínimo del flujo principal. También hacía falta alinear el default de procesamiento de imágenes con ADR-0009 y definir la prueba del camino Bigme/USB sin debilitar la preservación.

La aplicación ya permite buscar con el fixture, seleccionar capítulos, crear DownloadJobs y conservarlos al reiniciar el backend. Aún no transmite páginas, valida bytes ni genera CBZ. El objetivo inmediato es completar de forma incremental el flujo principal y habilitar pruebas observables; no declarar V1 listo antes de sus gates.

## Decisión

1. `docs/YOMIRA_MASTER_EXECUTION_PLAN_V1_SCOPE.md` se actualiza a versión 1.1. Antes de editarlo, se conserva una copia de V1.0. El plan canónico y la SPEC aprobada permanecen intactos. La matriz sigue enumerando K001–K284 exactamente una vez.
2. K058, que exige benchmark de 100/1 000/10 000 series sintéticas, pasa a V1.3+. V1 conserva una prueba funcional pequeña de biblioteca e integridad, sin declararla benchmark ni gate de K058. La evidencia ya obtenida para K058 se conserva como histórica y no se invalida.
3. V1 implementa una cola persistente, FIFO y acotada; idempotencia de jobs, cancelación segura, retries limitados y reanudación únicamente de páginas completas verificadas. K086 prioridades, K087 fairness por fuente/host y K089 pausa manual segura pasan a V1.1. K090 backpressure dinámico y K104 Circuit Breaker permanecen V1.1. Sus límites estáticos, preflight de disco y errores básicos siguen en V1.
4. K123 detección de páginas duplicadas por hash, K124 anomalías relativas de resolución, K126 clasificación de tipos de página y K147 metadata extendida por página pasan a V1.1. En V1 se conservan todas las referencias/páginas en el orden de origen, incluso si los hashes coinciden; no se omiten ni colapsan bytes por deduplicación. La validación de MIME, firma, decode, truncamiento y límites de imagen sigue en V1.
5. K221 Real Connector Gate pertenece a V1 y cubre el Mock más Connector A compatible. V1.1 implementa Connector B en K215–K220 y repite la cobertura K221 ampliada, sin duplicar la fila K221 en la matriz ni cambiar su identidad.
6. ADR-0009 mantiene la disponibilidad de OFF/SAFE/E_READER, pero para V1 el modo predeterminado y efectivo es OFF: Yomira valida y empaqueta los bytes originales sin transformación automática. SAFE solo se habilita tras los gates de K131–K142 en V1.1. E_READER describe la experiencia del lector y no implica transformar ni sobrescribir imágenes.
7. Exportación segura a USB/Bigme sigue en V1. Se prueba con staging temporal, hash, rename, desconexión/reintento y verificación de que CBZ y biblioteca de origen permanecen intactos. USB no se declara probado únicamente por pasar LAN.
8. K267 se limita a una actualización básica soportada que demuestre que el data root y la biblioteca sobreviven a una migración compatible. El rollback y upgrade avanzados de K268 pasan a V1.3+; V1.2 retiene los mecanismos de backup, restore y verificación que los harán posibles.
9. V1 ya admite la prueba funcional de búsqueda → selección → DownloadJob persistente, que está disponible hoy. La siguiente entrega implementa page fetch, streaming acotado a `.part`, verificación, CBZ y biblioteca en ese orden. La interfaz debe distinguir `QUEUED` de descargado; ninguna capacidad se marca PASS hasta completar su gate.
10. El plan añade hitos incrementales separados del Release Gate: Alpha local (Mock→búsqueda→capítulos→job durable), pipeline de contenido (Mock HTTP→CBZ→biblioteca) y release V1. Pasar un hito temprano permite probar esa porción sin declarar completados los gates restantes.
11. WP5 excluye explícitamente K123/K124/K126/K147, que pasan a V1.1; K129 valida en V1 la validación básica segura, y su matriz diagnóstica ampliada llega en V1.1. El gate Alpha se enlaza a `docs/reports/V1_ALPHA_SMOKE_2026-09-27.md`; no sustituye el reporte de pipeline ni el reporte de Release V1.
12. Los work packages V1 incluyen tareas V1 y las fronteras arquitectónicas necesarias; no pueden incluir tareas implementables asignadas solo a releases posteriores. Así, WP2 omite K047 (V1.2), WP4 omite K095 (V1.1) y WP5 conserva K130 como frontera de modos/procesador; todo el código SAFE de K131–K142 sigue diferido.

## Consecuencias

- Las capacidades avanzadas se posponen sin borrar requisitos ni testearlas como si hubieran pasado.
- La lectura/validación básica de imágenes conserva contenido; la deduplicación nunca puede eliminar páginas en V1.
- Bigme/USB conserva su cobertura independiente de LAN.
- Los benchmarks ya ejecutados se preservan, pero no elevan el mínimo de entrada de V1.
- El primer objetivo funcional sigue bloqueado hasta que el pipeline de bytes, CBZ, lector y cierre/reapertura hayan pasado las verificaciones de la sección 10 del plan sucesor.

## Verificación

- Validar automáticamente que la matriz activa contiene cada `K001`…`K284` exactamente una vez, sin huecos ni duplicados.
- Confirmar que las asignaciones de K058, K086, K087, K089, K090, K104, K123, K124, K126, K147, K221, K267 y K268 coinciden entre work packages, matriz, secciones de versiones y Release Gate.
- Confirmar que WP5 no vuelve a incluir tareas V1.1 y que V1.2 sólo prepara el rollback avanzado de K268, sin atribuirle su implementación.
- Comparar WP0–WP8 con la columna Release de K001–K284: sólo se incluyen tareas V1 y fundamentos arquitectónicos; cada K V1 ejecutable pertenece a un paquete funcional.
- Registrar por separado el resultado de cada hito; el PASS de Alpha local no se propaga al pipeline de contenido ni al Release Gate.
- Ejecutar las verificaciones existentes del frontend/API y continuar con tests primero para el siguiente slice de descarga.
