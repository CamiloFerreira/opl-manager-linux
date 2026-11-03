# Política de seguridad

## Versiones con soporte

Solo la última versión publicada recibe correcciones. Cuando una versión corrige vulnerabilidades, las anteriores
quedan marcadas como **inseguras** (`min_safe_version` en el manifiesto firmado `release.json`) y la app las
actualiza de forma **obligatoria**.

| Versión  | Soporte |
|----------|---------|
| ≥ 0.4.0  | ✅ |
| < 0.4.0  | ❌ actualizar (actualizaciones sin firma digital) |

## Reportar una vulnerabilidad

No abras un issue público. Usa **[Report a vulnerability](https://github.com/CamiloFerreira/opl-manager-linux/security/advisories/new)**
(aviso privado de GitHub) con: versión, distro, pasos para reproducir e impacto.

Proceso (basado en ISO/IEC 29147 — divulgación — e ISO/IEC 30111 — manejo de vulnerabilidades):

1. **Acuse de recibo** en un máximo de 7 días.
2. **Verificación y clasificación** de la gravedad (CVSS v3.1).
3. **Corrección**: versión nueva con `min_safe_version` actualizado, de modo que todas las instalaciones
   vulnerables se actualicen de forma obligatoria.
4. **Divulgación coordinada**: aviso de seguridad (GitHub Security Advisory) publicado junto con la corrección,
   dando crédito a quien lo reportó si lo desea.

## Cómo se protegen las actualizaciones

| Control | Detalle |
|---|---|
| Autenticidad | `SHA256SUMS` se firma con Ed25519 en el CI. La clave pública está fija en `updater.py`; sin una firma válida no se instala nada. |
| Integridad | Cada archivo descargado se compara con su SHA256 del `SHA256SUMS` firmado. |
| Transporte | Solo HTTPS (también en las redirecciones) y solo URLs de los releases de este repositorio. |
| Anti-rollback | Nunca se instala una versión igual o anterior a la instalada. |
| Procedencia | Cada release lleva una atestación SLSA: `gh attestation verify opl-manager --repo CamiloFerreira/opl-manager-linux`. |
| Disponibilidad | Límites de tamaño en todas las descargas. |
| Cadena de suministro | GitHub Actions fijadas por SHA, permisos mínimos y análisis estático (bandit) en cada cambio. |

Verificación manual de un release:
```bash
sha256sum -c SHA256SUMS --ignore-missing
python3 updater.py verify SHA256SUMS SHA256SUMS.sig
```

## Gestión de la clave de firma

- La clave privada solo existe como secreto `RELEASE_SIGNING_KEY` del entorno `release` de GitHub Actions y en
  una copia de respaldo fuera de línea del mantenedor. Nunca se guarda en el repositorio.
- Si la clave se viera comprometida: se genera una nueva, se publica una versión firmada con la clave anterior
  que trae la nueva clave pública y `min_safe_version` igual a esa versión, y luego se revoca la anterior.
