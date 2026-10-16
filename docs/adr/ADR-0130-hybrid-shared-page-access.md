# ADR-0130 — Acceso común a páginas para el lector híbrido

## Estado

Accepted para el incremento H1a — 2026-10-11, dentro de la autonomía técnica otorgada por el usuario para iniciar el lector híbrido preservando la SPEC y el pipeline. Los contratos progresivos, caché persistente y cambios de progreso online requieren decisiones sucesoras; esta ADR no los declara implementados ni aceptados en detalle.

## Contexto

`DownloadWorker` resuelve versión/capacidad de fuente, adquiere y valida referencias y construye la allowlist de transporte. Repite adquisición en el refresco de página. El futuro consumidor online necesita las mismas reglas sin depender de transiciones de un job ni crear otro cliente de fuente.

FMD2 `get_pages` crea un UUID anónimo; `acquire_pages` admite el UUID estable del trabajo. El puente y el core solo exponen páginas de un capítulo terminado. Por tanto, reutilizar este método no demuestra lectura progresiva.

## Decisión

- Extraer el acceso a `app/services/source_pages.py`, en el core. No introducir capabilities de persistencia dentro de `app/connectors`.
- `SourcePageService.resolve` comprueba versión y PAGE_LIST antes de usar la instancia única del registry existente. Mantener las familias/operaciones de error actuales.
- `acquire_pages` exige un UUIDv4 estable suministrado por el consumidor. Mantiene orden y duplicados y aplica los límites y tipos ya exigidos por el worker, tanto en adquisición inicial como en refresco. No crea UUID, temporales, jobs, clientes HTTP ni sesiones SQLite.
- `download_origins` usa los orígenes de descarga del registry, excluyendo los de portada, y conserva el rechazo de conjunto vacío. El transporte sigue validando cada petición y redirect.
- El worker conserva metadata/lineage de capítulo, cancelación interruptible, checkpoints, manifiestos, presupuesto, descarga/validación de bytes, refresco con igualdad del plan, CBZ, ComicInfo, publicación e indexación. FMD2 sigue liberando staging solo al completar esas etapas.
- La nueva clase tiene como único estado una referencia al registry existente. No mantiene una segunda caché ni duplica instancias de conector. La composición online posterior podrá usar ese mismo registry y servicio.

## Alcance y compatibilidad

H1a no cambia API, frontend de producto, esquema SQLite, formatos de manifest/CBZ, roots, fuentes autorizadas, límites de red o instalador. Los callers externos del worker conservan su constructor. Los dos puntos de adquisición del worker pasan por el mismo servicio. Un plan inválido también se rechaza durante refresco; nunca se adopta un plan que no cumpla el contrato ni uno que difiera del original.

La corrección de dos selectores de `SettingsPage.test.tsx` es mantenimiento de pruebas encontrado en el baseline: esperar el texto de guardado correcto entre varios estados de pantalla. No modifica la UI.

También se actualiza el inventario esperado de `test_factory.py` para las dos rutas de grupos de descarga ya existentes. El diagnóstico y el plan H0–H6 quedan en [INITIAL_AUDIT.md](../hybrid/INITIAL_AUDIT.md).

## Validación

- Probar versión, capability antes de factory, exclusión de hosts de portada, UUID estable, orden y duplicados, lista vacía/mal tipada/excesiva, errores sanitizados y propagación de cancelación.
- Ejecutar regresiones worker/transporte/CBZ/lector/progreso/manifiestos/recuperación/presupuesto, incluidos refrescos que cambian el plan y cancelación.
- Ruff y Pyright sobre los archivos afectados, y comparar las deudas previas sin ocultarlas.
- Registrar pruebas de referencia, regresión y arranque Windows en el informe de entrega. Ningún avance habilita por sí solo lectura online o completa H1.

Resultado H1a: 387 pruebas de regresión aprobadas, 3 omitidas y ninguna fallida; Ruff completo PASS; Pyright de archivos afectados sin errores; smoke Windows aislado PASS. El gate Pyright global de referencia conserva 267 errores previos. No se certifica una release ni un instalador nuevo.

## Reversión

Restaurar el worker y retirar el servicio/test/ADR de la candidata. No requiere migración, eliminar archivos, cambiar roots ni tocar datos personales. El commit y los ZIP/bundle previos permanecen disponibles.
