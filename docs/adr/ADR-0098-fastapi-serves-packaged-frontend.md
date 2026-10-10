# ADR-0098 — FastAPI sirve el frontend empaquetado

## Estado

Accepted — 2026-09-27.

## Contexto

El cliente React usa rutas de navegador y durante el desarrollo se sirve con Vite, que ya reenvía `/api/v1` al backend local. El instalador V1 debe entregar el frontend compilado sin requerir Node.js en la máquina de uso. El backend solo acepta direcciones loopback y debe conservar el contrato JSON de `/api/v1`.

## Decisión

1. En `app_env=dev` y `app_env=test`, Vite sigue sirviendo la UI; FastAPI no registra rutas SPA.
2. En `app_env=prod`, FastAPI resuelve `index.html` y `assets/` desde `app/web` dentro del bundle PyInstaller (`sys._MEIPASS`). En ejecución desde el checkout se permite `frontend/dist` para validar el modo empaquetado. K258 debe incluir el build Vite bajo `app/web`.
3. Si el build, `index.html` o `assets/` no están presentes, la creación del backend empaquetado falla cerrada con un error de inicio trazable.
4. `/assets/*` solo sirve archivos mediante `StaticFiles` desde el directorio empaquetado. `/` y rutas SPA sin extensión sirven `index.html`. Un recurso ausente o con extensión desconocida da 404; `/api` y `/api/*` nunca reciben el fallback SPA.
5. La UI y API comparten origen loopback. Esta decisión no habilita LAN, no cambia el proxy del desarrollo ni añade una dependencia de red externa.

## Consecuencias

- El ejecutable puede alojar una UI local sin un proceso Node/Vite separado.
- Un paquete incompleto no aparenta estar listo ni devuelve HTML para una solicitud API desconocida.
- K258 debe colocar el contenido en la ruta del bundle esperada; K266 aún requiere prueba en Windows limpio.
- El servidor no expone paths de archivos del host en rutas de UI ni permite solicitar archivos arbitrarios fuera de los recursos construidos.

## Verificación

- Resolver el directorio en checkout y en `_MEIPASS`.
- Verificar `GET /`, una ruta SPA, un asset construido y un endpoint API en modo producción.
- Verificar 404 para API desconocida y assets ausentes; verificar que `dev` no agregue fallback.
- Ejecutar build Vite antes del Packaging Gate; no declarar K257 ni el Release Gate aprobados solo por estas pruebas unitarias.
