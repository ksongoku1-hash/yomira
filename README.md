# Yomira 0.1.8

Yomira es una aplicación local para Windows que organiza, descarga y permite leer mangas desde una interfaz web. Los archivos y la biblioteca se guardan en el equipo del usuario.

## Descargar y probar

**Para usar Yomira en Windows 10/11 de 64 bits:**

1. [Descarga Yomira-Setup-0.1.8.exe](https://github.com/ksongoku1-hash/yomira/releases/download/v0.1.8/Yomira-Setup-0.1.8.exe).
2. Abre el archivo y pulsa **Instalar**. El instalador está en español y no necesita permisos de administrador.
3. Pulsa **Finalizar** con **Abrir Yomira** marcado. La página se abre sola en tu navegador.

Después, abre el acceso directo **Yomira** del escritorio o del menú Inicio. **No necesitas comandos, pip, Python, Node.js ni instalar FMD por separado.** Cada persona instala Yomira en su propio PC y tiene su propia biblioteca.

También hay una [versión portátil](https://github.com/ksongoku1-hash/yomira/releases/download/v0.1.8/Yomira-Windows-x64-0.1.8.zip): extrae todo el ZIP y abre **Yomira.exe**. No ejecutes el programa dentro del ZIP.

El navegador se abre cuando el servidor está listo. Puedes minimizar la ventana de Yomira; vuelve a ella y pulsa **Ctrl+C** para cerrar la aplicación. Cerrar la pestaña del navegador deja las descargas en marcha. Si el navegador no se abre, entra en http://127.0.0.1:8000/library.

La descarga incluye FMD adaptado en segundo plano, catálogos públicos de títulos y licencias. La misma Release ofrece el código fuente correspondiente a FMD y las sumas SHA-256. Consulta [las instrucciones sencillas](installer/LEEME.txt).

Para actualizar, cierra Yomira e instala la nueva versión. Los mangas, datos y progreso se guardan fuera de la carpeta del programa. Si usabas una versión portátil, cierra la anterior antes de abrir la instalada.

Esta versión es una prueba local para Windows x64 y no está firmada digitalmente. No se ha certificado en un segundo ordenador. No incluyas mangas, bases de datos, tokens ni cookies cuando comuniques un error; indica la acción, la fuente, la obra y el capítulo afectados.

## Funciones actuales

- Biblioteca con portadas, metadatos y progreso de lectura.
- Importación de carpetas con imágenes, CBZ/ZIP y EPUB con imágenes.
- Descargas por capítulos, cola por manga y búsqueda de alternativas para capítulos que fallan.
- Fuentes mediante el motor FMD2 adaptado; MangaDex aporta búsquedas y metadatos.
- Exportación de un manga o un intervalo en un solo CBR a Books de un lector conectado mediante MTP, además de destinos USB con letra de unidad.
- Historial de envíos, verificación de archivos y reintentos.

La aplicación sigue en desarrollo. Las fuentes externas pueden cambiar o dejar de responder. La lectura en un B6 Color se comprobó con Vinland Saga; eso no certifica todos los dispositivos o archivos.


## Ordenar capítulos y organizar descargas

En Buscar, abre **Ver capítulos** y pulsa **Ascendente / Descendente** para ver primero los capítulos iniciales o los últimos. Puedes filtrar por nombre, estado e idioma. **Seleccionar pendientes visibles** marca hasta 100 capítulos que faltan y coinciden con los filtros. También puedes seleccionar un intervalo, invertir los visibles o limpiar la selección. Cambiar el orden o los filtros conserva lo que marcaste; la selección muestra si hay capítulos ocultos por los filtros. El orden de visualización no cambia el orden numérico de procesamiento de la cola.

En Biblioteca, abre un manga y usa el mismo botón sobre **Capítulos listos para leer**. El orden se aplica al listado completo, incluso cuando tiene varias páginas.

En Descargas, **Descargas por manga** reúne todos los trabajos pendientes de cada manga, aunque estén en distintas páginas de la cola. Pulsa **Pausar grupo**: termina el capítulo en curso y deja avanzar a otros mangas. Pulsa **Reanudar grupo** para continuar con los mismos trabajos. La pausa queda guardada al cerrar Yomira y también se aplica a nuevos capítulos de ese manga mientras siga pausado. No se borran archivos ni se duplican trabajos.

## Preparar desarrollo en Windows

Necesitas Git, Python y Node.js en las versiones indicadas en [runtime-versions.json](runtime-versions.json). Los archivos backend/requirements.lock y frontend/package-lock.json fijan las dependencias.

Desde la raíz del repositorio:

```powershell
py -3.14 -m venv backend/.venv
backend/.venv/Scripts/python.exe -m pip install -r backend/requirements.lock
npm ci --prefix frontend
npm run build --prefix frontend
powershell -ExecutionPolicy Bypass -File scripts/start-yomira.ps1
```

Abre http://127.0.0.1:8000. El servidor sirve la interfaz compilada. Para trabajar con Vite, usa npm run dev --prefix frontend y un servidor de desarrollo en el puerto configurado por su proxy.

Sin FMD2 puedes utilizar la biblioteca y el lector local. Para habilitar las fuentes de FMD2 necesitas la versión adaptada, sus DLL y módulos Lua; el ejecutable oficial sin los cambios de Yomira no expone su API. Consulta [la integración de FMD2](third_party/fmd2/README.md).

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-yomira.ps1 -FmdExecutable 'D:/Herramientas/FMD-Yomira/fmd.exe'
```

## Carpetas y configuración

La biblioteca utiliza la carpeta Mangas de Yomira dentro de Documentos del usuario, incluida su ubicación real si Windows la redirige a OneDrive. Los datos de la aplicación se guardan por defecto en %LOCALAPPDATA%/Yomira/YomiraData.

Puedes configurar YOMIRA_DATA_ROOT, YOMIRA_LIBRARY_ROOT, YOMIRA_API_PORT y YOMIRA_FMD2_EXECUTABLE mediante variables de entorno. Las rutas deben ser absolutas. No se carga automáticamente un archivo .env.

No copies bases de datos, bibliotecas, credenciales ni configuraciones de otro usuario dentro del repositorio. Para una prueba aislada, asigna carpetas nuevas a YOMIRA_DATA_ROOT y YOMIRA_LIBRARY_ROOT y utiliza un puerto libre.

## Pruebas

```powershell
Push-Location backend
.venv/Scripts/python.exe -m pytest
Pop-Location
npm test --prefix frontend
npm run typecheck --prefix frontend
npm run lint --prefix frontend
```

scripts/quality-gate.ps1 agrupa las comprobaciones del proyecto y permite especificar Python, Node y npm. El repositorio conserva documentos de diseño históricos; los informes y capturas locales de ejecución quedan fuera de esta primera copia. Algunos documentos históricos hacen referencia a esos informes conservados en el equipo de desarrollo. Consulta [el estado de publicación](docs/GITHUB_PREPARATION.md) para conocer las comprobaciones y limitaciones de esta copia.

## Instalador

El instalador publicado incorpora la interfaz, Python y el motor FMD2 adaptado. Para mantener o reconstruir la distribución consulta [la guía de empaquetado](docs/BUILD_RELEASE.md). Los scripts backend/build_windows.ps1 e installer/build_installer.ps1 construyen los componentes; scripts/package-release.ps1 reúne el motor verificado, las licencias, el instalador y el ZIP portátil.

Subir el repositorio a GitHub no aloja el servidor ni hace accesibles los mangas por Internet.

## Licencias

El repositorio y la descarga de prueba son públicos. La licencia abierta del código propio de Yomira sigue pendiente de decisión; publicar el código no cambia las licencias de los componentes externos. FMD2 y sus cambios conservan la licencia GPL v2 de su origen; se incluyen las atribuciones y el parche en third_party/fmd2. Consulta [las notas de licencias](THIRD_PARTY_NOTICES.md).
