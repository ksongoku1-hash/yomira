# Yomira para Arch Linux y SteamOS — primera versión de prueba

Este paquete contiene la adaptación **nativa de Python y la interfaz web** para Linux x86_64.
No ejecuta el programa de Windows mediante Wine. Está preparado para probarse en Arch y en el
**modo Escritorio de Steam Deck**. El arranque, los adaptadores de almacenamiento y la importación
ya superaron pruebas automatizadas en un contenedor Arch; falta la instalación física en Steam Deck.
No lo consideres una versión estable ni una sustitución de tu biblioteca actual de Windows.

## Instalación

1. En Steam Deck, cambia al modo Escritorio.
2. Extrae `Yomira-0.1.16-Linux-x86_64-preview.tar.gz` en una carpeta.
3. Abre una terminal dentro de esa carpeta y ejecuta:

   ```bash
   ./Instalar-Yomira.sh
   ```

4. Al terminar, abre **Yomira** desde el menú de aplicaciones. Se abrirá tu navegador.
5. Usa **Cerrar Yomira** en el mismo menú para detener el servidor. Cerrar la pestaña del navegador
   permite que siga funcionando en segundo plano.

La primera instalación requiere Internet para obtener Python 3.14.7 y las dependencias fijadas.
El gestor de runtime uv 0.13.6 está incluido con sus licencias y su SHA-256 verificado frente a la
distribución oficial de Astral. No se utiliza `sudo`, no se modifica la partición del sistema,
no se desactiva el modo de solo lectura de SteamOS y no se añaden servicios del sistema.
Se instala únicamente un servicio **del usuario** y dos accesos de aplicaciones.

El instalador copia la aplicación a `~/.local/share/yomira-app/0.1.16-linux-preview`, por lo que
puedes borrar la carpeta extraída de Descargas después. Si cambiaste `XDG_DATA_HOME`, se respeta
esa ubicación. Para reinstalar, cierra primero Yomira; el instalador se niega a reemplazarla mientras
su servicio esté activo.

## Funciones incluidas en la adaptación

- Biblioteca, interfaz adaptable y lector local de mangas, manhwa y cómics.
- Importación de imágenes, CBZ y EPUB de imágenes.
- Progreso, marcadores, historial, opciones de lectura y caché.
- Conector público de MangaDex para búsquedas, capítulos, lectura online y descargas.
- Recomendaciones públicas mediante las integraciones existentes.
- Acceso móvil con PIN y preferencia de inicio automático. El cortafuegos se configura con las
  herramientas de Linux; la app no modifica sus reglas automáticamente.

Estas funciones describen el alcance del código adaptado; la validación automática cubre una
biblioteca temporal y fuentes simuladas. La disponibilidad de páginas externas depende
de cada proveedor y de sus contenidos.

## Diferencias de esta primera versión

- **FMD no está incluido.** Las 21 fuentes del motor Windows no tienen todavía una distribución
  nativa validada. No se anuncia paridad de fuentes con Windows.
- **MTP y la detección/exportación a lectores USB de Windows no están portadas.** Usa el lector
  local; no esperes que aparezcan dispositivos en la pantalla de exportación.
- **Las credenciales protegidas con DPAPI son exclusivas de Windows.** Este paquete se limita a
  fuentes públicas; no guarda contraseñas en texto plano ni intenta convertir secretos de Windows.
- **El actualizador de instaladores de Windows no se ejecuta en Linux.** Las actualizaciones de
  esta edición serán manuales hasta disponer de un formato de publicación validado.
- No se ha probado el modo Juego de Steam Deck ni sus mandos. La referencia inicial es el modo
  Escritorio con navegador.

## Datos y cierre

Los datos se guardan en `~/.local/share/yomira` y los mangas en `~/Documents/Mangas de Yomira`,
salvo rutas configuradas explícitamente. Usa almacenamiento local; se rechazan sistemas de archivos
de red y sistemas no reconocidos para conservar las protecciones de persistencia.
La aplicación sigue escuchando en `127.0.0.1:8000`; el acceso desde otros dispositivos requiere
activar expresamente el acceso móvil en Ajustes.

Comandos útiles:

```bash
systemctl --user status yomira.service
journalctl --user -u yomira.service -n 80
systemctl --user stop yomira.service
```

Si no arranca, consulta esos registros y el espacio libre antes de repetir la instalación. La
política de espacio de seguridad de Yomira se mantiene; no se desactivó para hacer pasar el port.

## Desarrollo y validación

El código añade identidad por dispositivo/inodo, respeto por mayúsculas en rutas Linux, bloqueos
de archivos entre procesos y publicación con `renameat2(RENAME_NOREPLACE)` y sincronización de
directorios. Las comprobaciones de memoria y disco leen contadores de Linux y fallan de forma
segura cuando no están disponibles.

El repositorio incluye `.github/workflows/linux-preview.yml`, que prepara un contenedor Arch,
construye la interfaz y ejecuta las pruebas nativas antes de generar el paquete. La primera
ejecución detectó una comprobación de rutas que conservaba el separador de Windows; se corrigió
en el publicador compartido y la siguiente ejecución pasó, incluida la importación y lectura de
imágenes, CBZ, ZIP y EPUB. También se verificaron las regresiones de Windows.

La rama de prueba está en https://github.com/ksongoku1-hash/yomira/pull/1 . El último resultado y
el paquete generado se consultan en la pestaña de comprobaciones de ese PR. Una prueba en un
contenedor no certifica el escritorio, el navegador ni el hardware de Steam Deck. Antes de publicar
una versión estable falta una instalación real en SteamOS. No se han validado todos los tipos de
disco; si sus contadores no están disponibles, se mantiene la pausa preventiva de descargas.

Referencias técnicas:

- https://specifications.freedesktop.org/basedir/0.8/
- https://man7.org/linux/man-pages/man2/rename.2.html
- https://docs.astral.sh/uv/guides/install-python/

El directorio `tools` incluye las licencias de uv y los datos de procedencia de su binario oficial.
Consulta también `THIRD_PARTY_NOTICES.md`. No se incluyen mangas, bases de datos de usuario,
cookies, claves, datos personales ni el motor FMD de Windows.
