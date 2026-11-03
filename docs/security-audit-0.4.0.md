# Auditoría de seguridad — OPL Manager 0.4.0

**Fecha:** 2026-11-03 · **Alcance:** `oplcore.py`, `opl_manager.py`, `updater.py`, `tray.py`, scripts de
empaquetado y workflows de GitHub Actions (versión auditada: 0.3.1).

## Marco de referencia

- **ISO/IEC 27100:2020** (*Cybersecurity — Overview and concepts*): define el modelo con el que se hizo el
  análisis: **activos** → **amenazas** → **vulnerabilidades** → **riesgo** → **controles**, organizados en las
  funciones *identificar, proteger, detectar, responder y recuperar*. Es una norma de conceptos y no trae
  controles concretos.
- **ISO/IEC 27002:2022**: catálogo de controles que se usó para cada corrección (los números entre corchetes).
- **ISO/IEC 29147 / 30111**: divulgación y manejo de vulnerabilidades (ver [SECURITY.md](../SECURITY.md)).

## Activos y amenazas (identificar)

| Activo | Amenaza principal |
|---|---|
| Equipo del usuario (ejecución de código) | Actualización manipulada que se instala sola |
| Privilegios de root (instalación `.deb` con pkexec) | Paquete manipulado instalado como root |
| Archivos del usuario y de la biblioteca OPL | Rutas que escapan de la carpeta; enlaces simbólicos |
| Disponibilidad de la app | Archivos de disco o respuestas de red manipulados |
| Cadena de suministro (CI, releases) | Acciones de terceros o tokens comprometidos |

## Hallazgos y correcciones

| # | Gravedad | Hallazgo (0.3.1) | Corrección (0.4.0) | Control |
|---|---|---|---|---|
| 1 | **Crítica** | La actualización automática solo comprobaba el SHA256 publicado **en el mismo release**: quien pudiera subir archivos al release (token o cuenta comprometidos) podía hacer que todos los clientes instalaran código suyo, incluso como root vía `.deb`. | Firma Ed25519 de `SHA256SUMS` con clave guardada como secreto del CI; la clave pública va fija en la app; sin firma válida no se instala nada. | [8.24] criptografía, [8.32] cambios, [8.19] instalación de software |
| 2 | Alta | Sin forma de obligar a actualizar las versiones vulnerables. | Manifiesto firmado `release.json` con `min_safe_version`: actualización **obligatoria**, aunque las automáticas estén apagadas; un diálogo que no se puede cerrar bloquea la app hasta actualizar (sin cortar copias en curso). | [8.8] gestión de vulnerabilidades técnicas |
| 3 | Media | Las URLs y los nombres de los archivos venían de la API sin validar: con `tag_name` se armaba la ruta del `.deb` (path traversal) y `urllib` aceptaba `file://` y redirecciones a `http://`. | Validación estricta del tag (`vX.Y.Z`), de los nombres y de los prefijos de URL; solo HTTPS, también en las redirecciones. | [8.28] codificación segura, [8.20] seguridad de redes |
| 4 | Media | Descargas sin límite de tamaño (API, actualizaciones, arte): podían agotar la memoria. | Límites: 2 MB (API), 256 KB (sumas/firma), 200 MB (paquetes), 16 MB (imágenes). | [8.6] gestión de capacidad |
| 5 | Media | Un título vacío o formado solo por caracteres no válidos (p. ej. `/`) devolvía el texto original sin limpiar: `opl_filename` daba `/.iso` y la copia o el cambio de nombre escribía **fuera de la biblioteca**. | El nombre siempre se limpia, nunca empieza con `.` y se rechaza si contiene separadores; el código de juego se valida (`SLUS_209.46`). | [8.28] |
| 6 | Baja | Una ISO con el índice dañado provocaba `IndexError` al escanear (fallaba el escaneo de la bandeja). | Comprobación de límites y captura del error. | [8.28] |
| 7 | Baja | El arte de la bandeja se copiaba siguiendo enlaces simbólicos: un `ART/*.png` que apuntara a un archivo privado se copiaba al pendrive. | Solo se copian archivos PNG normales (no enlaces) y de tamaño razonable. | [8.12] prevención de fuga de datos |
| 8 | Baja | Portadas sin límite de píxeles (*decompression bomb*). | `MAX_IMAGE_PIXELS` y limpieza de temporales. | [8.28] |
| 9 | Baja | Archivo `.deb` en un directorio temporal con permisos de lectura y escritura antes de `pkexec`. | Archivo `0444` y directorio `0555` mientras se instala; rutas absolutas a `pkexec`/`apt-get`; `--` antes del archivo. | [8.18] programas con privilegios |
| 10 | Baja | Texto remoto (versión, errores) se mostraba como *markup* en el banner. | Escapado con `GLib.markup_escape_text`. | [8.28] |
| 11 | Media | CI: acciones fijadas por etiqueta (mutable), permisos por defecto, credenciales persistidas en el checkout, sin análisis estático. | Acciones fijadas por SHA, `permissions` mínimos, `persist-credentials: false`, bandit en el CI, entorno `release` protegido y atestación de procedencia SLSA. | [8.25] ciclo de desarrollo seguro, [8.30] desarrollo externo, [5.21] cadena de suministro TIC |
| 12 | Info | Configuración y caché creadas con permisos por defecto; caché escrita sin atomicidad y aceptada sin validar. | Carpetas `0700`, escritura atómica, validación de las entradas de la caché. | [8.3] restricción de acceso |

## Detectar, responder y recuperar

- **Detectar:** pruebas automáticas de seguridad (`tests/`: firma, rollback, URLs, rutas, ISO dañada, enlaces) y
  bandit en cada push y en cada release.
- **Responder:** proceso de divulgación en [SECURITY.md](../SECURITY.md); `min_safe_version` fuerza la corrección
  en todas las instalaciones.
- **Recuperar:** el reemplazo del ejecutable es atómico (si algo falla, queda la versión anterior); nunca se
  instala una versión anterior.

## Riesgo residual

- Las instalaciones 0.3.1 o anteriores todavía **no verifican firmas**: se actualizan a 0.4.0 con el mecanismo
  anterior (solo SHA256). Desde 0.4.0 toda la cadena queda protegida.
- El modo `.deb` necesita la contraseña del usuario: el bloqueo es obligatorio, pero la instalación depende de
  que la escriba.
- Quien controle el usuario local ya puede modificar el ejecutable instalado en `~/.local/bin`: eso queda fuera
  del modelo de amenazas.
