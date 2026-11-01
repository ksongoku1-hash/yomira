# Herramientas de desarrollo — 1 de noviembre de 2026

La revisión de las herramientas web redujo los resultados de `npm audit` de **16 avisos** (13 moderados y 3 altos) a **0**. La revisión con `--omit=dev` permanece en **0**. Son resultados del catálogo de avisos consultado en esta fecha, no una certificación de ausencia de todos los defectos posibles.

Las dependencias de producción y sus resoluciones permanecen idénticas a las de 0.1.18. No se cambió la versión de la aplicación ni el motor, su configuración, los datos de lectura o el comportamiento del lector.

| Herramienta | Antes | Resolución comprobada |
| --- | --- | --- |
| Vite | 8.3.1 | 8.3.5 |
| Vitest | 5.0.2 | 5.0.5 |
| source-map-js, transitiva | 1.2.1 | 1.2.2 |
| undici, transitiva de las pruebas | 8.11.2 | 8.12.3 |
| minimatch, transitiva del análisis | 10.2.6 | 11.0.1 |

Vite y Vitest son los únicos pins directos actualizados. El lock conserva las versiones e integridades de las dependencias de producción y fija las resoluciones transitivas de las herramientas. Node **24.21.0**, npm **11.19.0**, ESLint, TypeScript y jsdom conservan sus versiones.

## Excepción acotada de minimatch

El aviso de minimatch afecta a las versiones anteriores a 11.0.1, mientras los consumidores actuales siguen solicitando la rama 10. Se utiliza `overrides: { "minimatch@^10": "11.0.1" }`; no fuerza otras ramas futuras. La excepción debe retirarse cuando las herramientas padre admitan una versión corregida. El comentario `shortcut:` de la configuración de ESLint registra este seguimiento.

Se compararon **100 rutas reales y de comprobación** usando la versión anterior y la nueva: la selección de archivos, las reglas y los globales del navegador y de las pruebas coinciden. También pasaron el análisis completo del proyecto y la validación del árbol mediante `npm ls --all`.

## Validación

- Instalación aislada mediante `npm ci --ignore-scripts`: sin avisos de seguridad.
- Auditoría completa y auditoría de producción: **0 avisos**.
- ESLint, tipos y compilación web: aprobados.
- Suite completa de interfaz: **613 pruebas aprobadas en 44 archivos**, sin reintentos ni ampliación de tiempos. Incluye el recorrido de 2000 páginas y la biblioteca de 10000 series.
- Bundle compilado en navegador: recorrido Inicio → Biblioteca → Serie → Lector a **390 × 844** y **1280 × 800**, imágenes decodificadas, navegación entre páginas, progreso y opciones. La fixture sólo registra una fuente sintética; no se repitió la validación de proveedores externos.
- [Validación nativa en Arch](https://github.com/ksongoku1-hash/yomira/actions/runs/41363240926): auditoría sin avisos, lint y compilación aprobados; **57 pruebas nativas aprobadas** y análisis de tipos sin errores.

El flujo de Arch ahora ejecuta `npm audit --audit-level=moderate` y lint antes de construir el paquete. Un aviso moderado o superior detiene la validación. La auditoría incluye las herramientas de desarrollo.

No se repitió la suite completa del servidor ni la prueba del instalador: no cambió su implementación. Los paquetes publicados de 0.1.18 conservan sus archivos y sumas; este cambio actualiza el entorno de desarrollo y las futuras construcciones.

## Fuentes primarias

- [Vite y launch-editor en Windows](https://github.com/advisories/GHSA-587h-7wmj-h5pm).
- [Registro de mocks de Vitest](https://github.com/advisories/GHSA-qxg6-89mg-rqv8).
- [Límites de source-map-js](https://github.com/advisories/GHSA-68fv-2mgg-jv7q).
- [Validación TLS de undici](https://github.com/advisories/GHSA-2m5v-pch6-74hx).
- [Amplificación de patrones de minimatch](https://github.com/advisories/GHSA-g3hv-75wg-m249).
