# Toolchain de desarrollo fijado

**Fecha de fijación:** 2026-09-25.  
**Plataforma verificada:** Windows x64.  
**Trazabilidad:** K015; ADR-0019; fuentes oficiales de Python y Node.js.

| Elemento | Versión exacta | Distribución | SHA-256 |
|---|---|---|---|
| CPython | 3.14.7 | Instalador Windows x64 | `9d9eb2709ef81bf5cd30db3c2096bdbc4ea10087c22e62f27d356b36f6ae9649` |
| SQLite | 3.50.4 | Incluido en CPython 3.14.7; probado en Windows x64 | Parte del runtime de Python |
| Node.js | 24.21.0 LTS | ZIP Windows x64 | `158f7685b44de51f6c0df1d153526cbcd3e1bc739a8dfc607721cef75de9e541` |
| npm | 11.19.0 | Incluido en distribución Node fijada | Lockfile versión 3 |

Fuentes oficiales:

- Python 3.14.7: https://www.python.org/downloads/release/python-3147/
- Node 24.21.0 LTS y checksums firmados: https://nodejs.org/en/blog/release/v24.21.0
- Versiones LTS de Node.js: https://nodejs.org/en/about/previous-releases
- Formato npm package-lock: https://docs.npmjs.com/cli/v11/configuring-npm/package-lock-json/

Los hashes de los dos paquetes oficiales se calcularon localmente tras descargar y coincidieron con las sumas publicadas en sus fuentes oficiales. La extracción local de Python para esta ejecución usa su distribución embebible en `work/`, con pip añadido solo al entorno temporal; el instalador tradicional no pudo crear su caché de Windows bajo el sandbox. Eso no cambia el runtime fijado ni añade archivos de runtime al producto.

`runtime-versions.json`, `.python-version`, `.node-version` y `.nvmrc` son la referencia del proyecto. Python y Node fijan los runtimes; `backend/requirements.lock` y `frontend/package-lock.json` fijan los árboles de dependencias. No se usa un lockfile generado por una versión distinta de npm.

Antes de refrescar cualquier pin, revisar el estado de soporte, compatibilidad de paquetes, hashes y quality gate. No seguir automáticamente una versión `Current` de Node para builds de producto; usar una línea LTS con soporte vigente.
