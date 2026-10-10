# Yomira

Yomira es una aplicación local para Windows que organiza, descarga y permite leer mangas desde una interfaz web. Los archivos y la biblioteca se guardan en el equipo del usuario.

## Descargar y probar

La versión de prueba para Windows x64 se publica en [Releases](https://github.com/ksongoku1-hash/yomira/releases). Descarga **Yomira-Windows-x64-2026.10.09.zip**, extrae todo su contenido a una carpeta donde puedas escribir y abre **Abrir Yomira.cmd**. No necesitas instalar Python, Node.js ni FMD por separado.

El navegador se abre cuando el servidor está listo. Puedes minimizar la ventana de Yomira; vuelve a ella y pulsa **Ctrl+C** para cerrar la aplicación. Cerrar la pestaña del navegador deja las descargas en marcha. Si el navegador no se abre, entra en http://127.0.0.1:8000/library.

La descarga incluye una biblioteca vacía, FMD adaptado en segundo plano, catálogos públicos de títulos, licencias y un manifiesto de archivos. La misma Release ofrece el código fuente correspondiente a FMD y las sumas SHA-256. Consulta [las instrucciones completas](installer/LEEME.txt).

Esta versión es una prueba local para Windows x64 y no está firmada digitalmente. No se ha certificado en un segundo ordenador. No incluyas mangas, bases de datos, tokens ni cookies cuando comuniques un error; indica la acción, la fuente, la obra y el capítulo afectados.

## Funciones actuales

- Biblioteca con portadas, metadatos y progreso de lectura.
- Importación de carpetas con imágenes, CBZ/ZIP y EPUB con imágenes.
- Descargas por capítulos, cola por manga y búsqueda de alternativas para capítulos que fallan.
- Fuentes mediante el motor FMD2 adaptado; MangaDex aporta búsquedas y metadatos.
- Exportación de un manga o un intervalo en un solo CBR a Books de un lector conectado mediante MTP, además de destinos USB con letra de unidad.
- Historial de envíos, verificación de archivos y reintentos.

La aplicación sigue en desarrollo. Las fuentes externas pueden cambiar o dejar de responder. La lectura en un B6 Color se comprobó con Vinland Saga; eso no certifica todos los dispositivos o archivos.

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

Existen scripts para construir el paquete con PyInstaller y el instalador con Inno Setup, en backend/build_windows.ps1 e installer/build_installer.ps1. Necesitan las dependencias de empaquetado y las herramientas fijadas por el proyecto. El paquete actual de PyInstaller no incorpora automáticamente el motor FMD2 adaptado: debe suministrarse y configurarse por separado.

Subir el repositorio a GitHub no aloja el servidor ni hace accesibles los mangas por Internet.

## Licencias

El repositorio y la descarga de prueba son públicos. La licencia abierta del código propio de Yomira sigue pendiente de decisión; publicar el código no cambia las licencias de los componentes externos. FMD2 y sus cambios conservan la licencia GPL v2 de su origen; se incluyen las atribuciones y el parche en third_party/fmd2. Consulta [las notas de licencias](THIRD_PARTY_NOTICES.md).
