# Componentes externos y distribución

La primera copia de Yomira se prepara para uso privado. No se ha elegido todavía una licencia pública para su código propio. Los archivos externos no adquieren otra licencia por estar en este repositorio.

## FMD2

Origen: https://github.com/dazedcat19/FMD2

La versión de origen y los cambios de Yomira se conservan en `third_party/fmd2`. Su licencia original GPL v2 y sus avisos de terceros se incluyen sin modificaciones. El motor funciona como un proceso local separado. Los ejecutables, las DLL, los datos del usuario y el directorio de herramientas no se incluyen en el código de Yomira.

Antes de distribuir un motor compilado, acompáñalo con su licencia, los avisos pertinentes y el código fuente correspondiente a esa compilación. El parche conservado permite recuperar los cambios sobre la revisión indicada; las bibliotecas adicionales de compilación y sus licencias también necesitan revisión para un paquete redistribuible.

## Dependencias Python y JavaScript

Las versiones se fijan en `backend/requirements.lock`, `backend/requirements-package.lock` y `frontend/package-lock.json`. Los metadatos de licencias declaradas están inventariados en [third_party/dependency-licenses.json](third_party/dependency-licenses.json): 39 dependencias Python y 227 entradas JavaScript, incluidas dependencias de desarrollo y variantes opcionales de plataforma. Faltan los metadatos de seis herramientas de empaquetado Python en el entorno revisado; están identificadas en el inventario. Esos metadatos no sustituyen las licencias ni constituyen una revisión completa de redistribución. Un paquete público debe incorporar los avisos exigidos por sus componentes.

## Imágenes de marca

Los recursos de `frontend/public/assets/brand` forman parte del diseño local de Yomira. Su origen y los derechos para una distribución pública aún deben documentarse; su presencia en esta copia privada no certifica derechos de terceros ni convierte las imágenes en dominio público.

## Mangas y datos personales

No se incluyen mangas descargados, bibliotecas personales, portadas extraídas de la biblioteca, bases de datos, credenciales ni registros del usuario. Las pruebas utilizan archivos sintéticos cuando necesitan imágenes o archivos comprimidos.
