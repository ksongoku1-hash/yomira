# Yomira 0.1.16 — lector híbrido local

Yomira es una aplicación local para Windows que organiza, descarga y permite leer mangas desde una interfaz web. Los archivos y la biblioteca se guardan en el equipo del usuario.

La versión 0.1.16 reúne el nuevo inicio y las opciones del lector, la reapertura online con progreso guardado, las recomendaciones públicas y 21 fuentes integradas. Incluye actualización de la aplicación desde GitHub Releases y refresco automático de los catálogos de Inventario Oculto y Ragnarok. Consulta las [notas de esta versión](docs/releases/0.1.16.md) para conocer las mejoras y sus límites.

## Descargar Yomira 0.1.16

**Para usar Yomira en Windows 10/11 de 64 bits:**

1. [Descarga Yomira-Setup-0.1.16.exe](https://github.com/ksongoku1-hash/yomira/releases/download/v0.1.16/Yomira-Setup-0.1.16.exe).
2. Abre el archivo y pulsa **Instalar**. El instalador está en español y no necesita permisos de administrador.
3. Pulsa **Finalizar** con **Abrir Yomira** marcado. La página se abre sola en tu navegador.

Después, abre el acceso directo **Yomira** del escritorio o del menú Inicio. **No necesitas comandos, pip, Python, Node.js ni instalar FMD por separado.** Cada persona instala Yomira en su propio PC y tiene su propia biblioteca.

También hay una [versión portátil](https://github.com/ksongoku1-hash/yomira/releases/download/v0.1.16/Yomira-Windows-x64-0.1.16.zip): extrae todo el ZIP y abre **Yomira.exe**. No ejecutes el programa dentro del ZIP.

El navegador se abre cuando el servidor está listo. Puedes minimizar la ventana de Yomira; vuelve a ella y pulsa **Ctrl+C** para cerrar la aplicación. Cerrar la pestaña del navegador deja las descargas en marcha. Si el navegador no se abre, entra en http://127.0.0.1:8000/library.

La descarga incluye FMD adaptado en segundo plano, catálogos públicos de títulos y licencias. La misma Release ofrece el código fuente correspondiente a FMD y las sumas SHA-256. Consulta [las instrucciones sencillas](installer/LEEME.txt).

Si vienes de la versión 0.1.11 o de otra copia sin actualizador, cierra Yomira e instala esta versión manualmente una vez. Después, la versión instalada buscará y descargará las próximas actualizaciones; pulsa **Reiniciar y actualizar** cuando aparezca el aviso. Los mangas, datos y progreso se guardan fuera de la carpeta del programa. Las copias portátiles se actualizan manualmente.

Esta versión es una prueba local para Windows x64 y no está firmada digitalmente. No se ha certificado en un segundo ordenador. No incluyas mangas, bases de datos, tokens ni cookies cuando comuniques un error; indica la acción, la fuente, la obra y el capítulo afectados.

## Funciones actuales

- Biblioteca con portadas, metadatos y progreso de lectura.
- Importación de carpetas con imágenes, CBZ/ZIP y EPUB con imágenes.
- Descargas por capítulos, cola por manga y búsqueda de alternativas para capítulos que fallan.
- Fuentes mediante el motor FMD2 adaptado; MangaDex aporta búsquedas y metadatos.
- Exportación de un manga o un intervalo en un solo CBR a Books de un lector conectado mediante MTP, además de destinos USB con letra de unidad.
- Historial de envíos, verificación de archivos y reintentos.
- Recomendaciones del ranking público de MangaDex, con búsqueda de títulos alternativos en las fuentes.
- Lectura online con navegación entre capítulos, progreso persistente y descargas opcionales desde el lector.
- Actualizaciones verificadas de la aplicación y refresco en segundo plano de dos catálogos públicos.

La aplicación sigue en desarrollo. Las fuentes externas pueden cambiar o dejar de responder. La lectura en un B6 Color se comprobó con Vinland Saga; eso no certifica todos los dispositivos o archivos.


## Ordenar capítulos y organizar descargas

En Buscar, abre **Ver capítulos** y pulsa el triángulo **▲ / ▼** para ver primero los capítulos iniciales o los últimos. Puedes filtrar por nombre, estado e idioma. **Seleccionar pendientes visibles** marca hasta 100 capítulos que faltan y coinciden con los filtros. También puedes seleccionar un intervalo, invertir los visibles o limpiar la selección. Cambiar el orden o los filtros conserva lo que marcaste; la selección muestra si hay capítulos ocultos por los filtros. El orden de visualización no cambia el orden numérico de procesamiento de la cola.

En Biblioteca, abre un manga y pulsa el triángulo a la derecha, justo encima de la lista de **Capítulos listos para leer**. El orden se aplica al listado completo, incluso cuando tiene varias páginas.

En Descargas, **Descargas por manga** reúne todos los trabajos pendientes de cada manga, aunque estén en distintas páginas de la cola. Pulsa **Pausar grupo**: termina el capítulo en curso y deja avanzar a otros mangas. Pulsa **Reanudar grupo** para continuar con los mismos trabajos. La pausa queda guardada al cerrar Yomira y también se aplica a nuevos capítulos de ese manga mientras siga pausado. No se borran archivos ni se duplican trabajos.

## Desarrollo en Windows

La carpeta raíz del repositorio contiene el código. Abre PowerShell allí. El launcher usa el runtime Python privado del proyecto y, si no se personalizan, las carpetas de datos y mangas dentro de user-data:

    powershell -ExecutionPolicy Bypass -File .\scripts\start-yomira.ps1

Para restaurar dependencias después de cambiar los archivos fijados, Python y
Node están en runtime\python-3.14.7 y runtime\node-24.21.0:

    .\runtime\node-24.21.0\npm.cmd ci --prefix frontend
    Push-Location backend
    ..\runtime\python-3.14.7\python.exe -m pip install -r requirements.lock
    Pop-Location

Las rutas predeterminadas son user-data/libraries/Mangas para la biblioteca y user-data/AppData/Yomira/YomiraData para los datos, la base de datos, los trabajos, los registros y la configuración activa. El launcher configura esas rutas si no existen valores explícitos de YOMIRA_DATA_ROOT y YOMIRA_LIBRARY_ROOT. También puedes asignar raíces absolutas mediante esas variables. YOMIRA_API_PORT y YOMIRA_FMD2_EXECUTABLE conservan su función.

Pruebas:

    Push-Location backend
    ..\runtime\python-3.14.7\python.exe -m pytest
    Pop-Location
    .\runtime\node-24.21.0\npm.cmd test --prefix frontend
    .\runtime\node-24.21.0\npm.cmd run typecheck --prefix frontend
    .\runtime\node-24.21.0\npm.cmd run lint --prefix frontend

scripts/quality-gate.ps1 ejecuta las validaciones completas con esos runtimes
locales. Para probar la interfaz contra el API sin tocar datos del usuario,
usa scripts/smoke-integration.ps1 o la vista previa offline de
backend/tests/offline_preview.py.

El código fuente se publica en GitHub. Después de una compilación correcta, scripts/package-release.ps1 guarda en D:\Yomira-Backup\releases solo el ZIP portátil y el instalador de esa versión, con verificación SHA-256 y retención de cinco versiones. La biblioteca, la base de datos y las descargas no forman parte de esa copia.
## Instalador

El instalador publicado incorpora la interfaz, Python y el motor FMD2 adaptado. Para mantener o reconstruir la distribución consulta [la guía de empaquetado](docs/BUILD_RELEASE.md). Los scripts backend/build_windows.ps1 e installer/build_installer.ps1 construyen los componentes; scripts/package-release.ps1 reúne el motor verificado, las licencias, el instalador y el ZIP portátil.

Subir el repositorio a GitHub no aloja el servidor ni hace accesibles los mangas por Internet.

## Licencias

El repositorio y la descarga de prueba son públicos. La licencia abierta del código propio de Yomira sigue pendiente de decisión; publicar el código no cambia las licencias de los componentes externos. FMD2 y sus cambios conservan la licencia GPL v2 de su origen; se incluyen las atribuciones y el parche en third_party/fmd2. Consulta [las notas de licencias](THIRD_PARTY_NOTICES.md).



## Actualizaciones de la aplicación

La versión instalada comprueba GitHub Releases diez segundos después de iniciar y cada seis horas. Descarga automáticamente el instalador de una versión estable superior y comprueba su tamaño y SHA-256 antes de ofrecer **Reiniciar y actualizar**. Puedes consultar el estado o reintentar desde Ajustes. El aviso no interrumpe el lector. La biblioteca y el progreso permanecen fuera de la instalación.

Las copias portátiles y de desarrollo muestran el enlace a Releases y no reemplazan sus archivos. Quienes usan una versión anterior al actualizador necesitan instalar esta versión manualmente una vez. El actualizador solo acepta `Yomira-Setup-X.Y.Z.exe` de este repositorio, con `digest` SHA-256 publicado por GitHub; no instala borradores ni versiones preliminares.
