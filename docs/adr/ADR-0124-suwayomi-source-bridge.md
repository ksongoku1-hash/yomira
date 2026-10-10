# ADR-0124: Puente local de fuentes Suwayomi

## Estado

Accepted — solicitado explícitamente por el usuario el 2026-10-05.

## Contexto

El usuario quiere ampliar las fuentes disponibles en Yomira con una experiencia similar a Kaori. Kaori usa Suwayomi como servidor de fuentes; las extensiones de Tachiyomi/Mihon se ejecutan dentro del proceso separado de Suwayomi, no dentro de Yomira.

Yomira debe conservar la autoridad sobre su biblioteca, cola, validación de páginas, creación y publicación de CBZ y exportación. Las páginas que devuelve Suwayomi pueden contener URLs firmadas y estas no deben guardarse, mostrarse ni registrarse.

## Decisión

1. Añadir `yomira.suwayomi` como conector first-party opcional en la búsqueda y flujo de descarga existentes.
2. Conectar solo con `http://127.0.0.1:4567`, puerto predeterminado del servidor local. La allowlist es exacta; no se acepta configurar hosts, puertos ni orígenes remotos desde la fuente. Este es el único origen HTTP loopback permitido por el transporte de descargas en producción; los demás orígenes HTTP siguen limitados a transportes de prueba inyectados.
3. Usar GraphQL local para enumerar fuentes instaladas y buscar títulos, consultar metadatos y capítulos, y refrescar las referencias de páginas. El límite es 100 fuentes instaladas; cada fuente se consulta en una sola página por búsqueda. Se falla explícitamente si excede el límite.
4. Descartar las URLs de página devueltas por `fetchChapterPages` tras validar únicamente su forma y cantidad. Para obtener cada imagen, resolver una referencia opaca propia a la ruta local de proxy de página de Suwayomi. Yomira persiste solo referencias opacas y orden; el transporte valida bytes usando los límites actuales.
5. No registrar cuerpos GraphQL, URLs ni excepciones del servidor. Errores de GraphQL se convierten en errores de conector sanitizados con traza.
6. No instalar, iniciar, actualizar ni incluir el servidor o extensiones de Suwayomi en Yomira en este incremento. Las extensiones instaladas se ejecutan fuera de Yomira, dentro del proceso de Suwayomi; esto no afirma que estén aisladas por un sandbox del sistema operativo.
7. No integrar autenticación de Suwayomi. El servidor debe estar limitado a loopback y accesible sin credenciales. La descarga, validación, biblioteca, CBZ y exportación permanecen en Yomira.

## Consecuencias

- Para obtener resultados reales se requiere instalar e iniciar Suwayomi y configurar al menos una extensión en su interfaz. La versión de desarrollo verificada no tenía un servidor activo en el puerto 4567, por lo que no se puede afirmar una búsqueda o descarga real hasta realizar esa configuración.
- Si Suwayomi no está disponible, Yomira muestra el fallo trazable de esa fuente y conserva operativas las demás fuentes.
- Solo se exponen en la UI nombres de títulos, portadas locales de Suwayomi, etiquetas de fuente y referencias necesarias para el flujo interno. Las URLs de origen no se entregan al frontend.
- Un cambio de puerto, autenticación, instalación del servidor o administración de extensiones requiere una ADR sucesora.

## Verificación

- Probar el cliente GraphQL POST con origen exacto, cuerpo acotado y redirecciones rechazadas.
- Probar búsqueda por fuentes instaladas, metadatos/capítulos, referencias de página opacas y el uso exclusivo del proxy loopback.
- Confirmar que URLs firmadas no aparecen en respuestas, referencias persistidas, representaciones ni logs.
- Ejecutar pruebas enfocadas, Ruff, Pyright y compilación/pruebas del frontend.
- Completar smoke real de Suwayomi cuando el usuario instale e inicie el servidor con una extensión.

## Referencias

- [Kaori: README](https://github.com/riyaniii/Kaori)
- [Suwayomi Server: README](https://github.com/Suwayomi/Suwayomi-Server/blob/master/README.md)
- [Suwayomi Server: configuración de extensiones](https://github.com/Suwayomi/Suwayomi-Server/wiki/Getting-Extensions)
- [Suwayomi VUI: esquema GraphQL](https://github.com/Suwayomi/Suwayomi-VUI/blob/master/schema.graphql)
