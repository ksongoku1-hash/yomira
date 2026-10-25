# Yomira 0.1.15 — entrega local comprobada

> Informe histórico de un paquete preliminar local. El motor, las verificaciones y los archivos de la publicación actual se describen en [Yomira 0.1.16](releases/0.1.16.md). Los paquetes de esta nota no son los archivos distribuidos por la Release 0.1.16.

Reúne el trabajo reciente del lector y las interfaces: controles y animaciones, búsqueda global de capítulos, biblioteca con filtros, inicio por manga, filtros de descargas y selección de capítulos entre páginas. Incorpora la reparación de reapertura online y mantiene el identificador del capítulo cuando cambian los enlaces antiguos de NovelCool.

## Lectura y conservación del progreso

Se comprobó con los ejecutables empaquetados una biblioteca nueva, la importación de dos CBZ de muestra, el guardado de la página 3 y las preferencias. Al sustituir el contenido del paquete 0.1.14 por el 0.1.15 y abrir de nuevo, las tablas de biblioteca, capítulos, progreso, preferencias y descargas siguieron idénticas; los CBZ conservaron sus bytes.

En una copia privada del perfil se abrió The Boxer 16 en NovelCool con imágenes reales, se guardó la página 3, se cerró y reabrió la sesión, se pasó al siguiente capítulo disponible (Ch. 20 Qasim Al Jahad) y se regresó al identificador original del 16. Tras reiniciar todo el ejecutable, la posición seguía en la página 3. Se comprobó también la interfaz del lector restaurado. Otra fuente: {"source": "ManhwaWeb", "status": "unavailable", "cause": "HTTPStatusError", "diagnostic": {"source": "ManhwaWeb", "packaged_reader_status": 503, "motor_plan_status": 502, "upstream_http_status": 404, "upstream_content_type": "application/json", "catalog_http_status": 200, "catalog_chapters": 0}}.

La correspondencia del catálogo acepta el enlace exacto o un único título idéntico dentro de la obra. El registro del servidor conserva la identidad antigua de NovelCool solo cuando coinciden la obra, el título y el idioma sin ambigüedad. No altera la referencia original ni migra el progreso. Esta lógica compartida también se comprueba frente a las descargas.

## Motor y construcción

El motor es el ejecutable verificado, SHA-256 98ec7539f9f82e4edc1a149605039c591c071a524ecc7573788ad8a2279956fa. El módulo NovelCool está en third_party/fmd2/lua/templates/NovelCool.lua, con SHA-256 fijado en upstream.json. Los scripts de restauración y empaquetado validan y copian esa misma fuente, sin depender del perfil personal. Se comprobó el rechazo de un módulo alterado antes de descargar fuentes o construir.

La recuperación del motor se probó con una fuente ficticia de 10.000 páginas, leyendo la primera y la última, sin descargar el capítulo completo ni guardar imágenes online en disco. Se interrumpió únicamente el proceso de prueba y el motor volvió a abrir el lector con el progreso conservado.

Validación: 155 casos únicos del servidor y 195 de la interfaz aprobados, TypeScript, ESLint, Ruff y Pyright del cambio correctos. Instalador compilado con Inno Setup 7.1.0. Se verificaron los 1260 archivos del ZIP y la versión dentro del ejecutable. La biblioteca personal, las 34 posiciones guardadas, las preferencias y los trabajos de descarga siguen idénticos a la copia anterior a la prueba. SQLite: ok. La aplicación activa en E: sirve 0.1.15 y el mismo frontend que el paquete.

## Archivos y alcance

- installer/Yomira-Setup-0.1.15.exe: instalador para Windows x64.
- Yomira-Windows-x64-0.1.15.zip: extraer todo antes de abrir Yomira.exe.
- Yomira-fuentes-0.1.15.zip: fuentes de la aplicación y cambios del motor; sin perfil personal, biblioteca, registros ni runtimes de desarrollo.
- SHA256SUMS.txt, artifacts.json y verificacion.json: integridad y resultados.
- NOTAS_GITHUB.md: descripción preparada para una futura Release estable.

La actualización se comprobó sustituyendo el contenido del paquete en una instalación de prueba; no se ejecutó Inno Setup contra la instalación personal ni se certificó un Windows recién instalado. El cierre manual por Ctrl+C completa el cierre de recursos, aunque Uvicorn/PyInstaller puede terminar mostrando KeyboardInterrupt en la consola de prueba. El actualizador se comprueba con instalador final y metadatos sintéticos del repositorio correcto; no se publicó esta versión en GitHub ni se activó su distribución automática. La disponibilidad de páginas externas depende de cada fuente.
