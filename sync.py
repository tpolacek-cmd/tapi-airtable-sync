"""
TAPI — Airtable → Supabase Sync
Sincroniza Contactos BizDev y CRM (clientes) de Airtable a Supabase.
Corre cada 15 minutos via GitHub Actions.
"""

import base64
import json
import os
import sys
import logging
import requests
from datetime import datetime, timezone

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Credenciales (desde env vars / GitHub Secrets) ────────────────────────────
AIRTABLE_TOKEN       = os.environ["AIRTABLE_TOKEN"]
AIRTABLE_BASE_ID     = os.environ["AIRTABLE_BASE_ID"]
SUPABASE_URL         = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

# ── IDs de tablas Airtable ─────────────────────────────────────────────────────
TABLE_CONTACTOS = "tbl9elB9e34E5AdUD"
TABLE_CRM       = "tblb7D95sZFtSkBCn"

# ── Headers ────────────────────────────────────────────────────────────────────
AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type":  "application/json",
}

def _sb_headers(extra: dict | None = None) -> dict:
    h = {
        "apikey":        SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type":  "application/json",
    }
    if extra:
        h.update(extra)
    return h


# ── Diagnostics ────────────────────────────────────────────────────────────────

def _jwt_role(token: str) -> str:
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
        return json.loads(base64.b64decode(padded)).get("role", "unknown")
    except Exception:
        return "parse-error"


def startup_check():
    role = _jwt_role(SUPABASE_SERVICE_KEY)
    log.info(f"  Supabase URL : {SUPABASE_URL}")
    log.info(f"  JWT role     : {role}")
    if role != "service_role":
        log.error(f"FATAL: JWT role is '{role}', expected 'service_role'. Check GitHub Secret SUPABASE_SERVICE_KEY.")
        sys.exit(1)


# ── Supabase helpers ───────────────────────────────────────────────────────────

def supabase_count(table: str) -> int:
    """Row count via GET with count=exact. Falls back to full-fetch if no Content-Range."""
    url  = f"{SUPABASE_URL}/rest/v1/{table}"
    resp = requests.get(
        url,
        headers=_sb_headers({"Prefer": "count=exact"}),
        params={"select": "id", "limit": "1"},
    )
    cr = resp.headers.get("Content-Range", "")
    log.info(f"  supabase_count({table}): status={resp.status_code} Content-Range={cr!r}")
    if resp.ok and "/" in cr:
        total_str = cr.split("/")[-1]
        if total_str.isdigit():
            return int(total_str)
    # Fallback: fetch all IDs and count in Python
    log.info(f"  supabase_count({table}): falling back to full-ID fetch")
    resp2 = requests.get(url, headers=_sb_headers(), params={"select": "id", "limit": "10000"})
    if resp2.ok:
        return len(resp2.json())
    log.error(f"  supabase_count fallback failed: {resp2.status_code} {resp2.text[:100]}")
    return -1


def supabase_delete_all(table: str) -> None:
    """Delete every row in table, then verify count == 0."""
    before = supabase_count(table)
    log.info(f"  [DELETE] {table}: {before} rows before delete")

    # Embed filter in URL to avoid any requests-param encoding ambiguity.
    # airtable_id IS NOT NULL covers every synced row (schema: airtable_id NOT NULL).
    url  = f"{SUPABASE_URL}/rest/v1/{table}?airtable_id=not.is.null"
    resp = requests.delete(url, headers=_sb_headers({"Prefer": "return=minimal"}))
    log.info(f"  [DELETE] HTTP {resp.status_code} | body={resp.text[:200]!r}")

    if resp.status_code not in (200, 204):
        log.error(f"  [DELETE] FAILED {table}: {resp.status_code} {resp.text[:300]}")
        resp.raise_for_status()

    after = supabase_count(table)
    log.info(f"  [DELETE] {table}: {after} rows after delete")

    if after != 0:
        log.error(f"  [DELETE] FATAL: {after} rows still in {table} after delete. Aborting insert.")
        raise RuntimeError(f"Delete verification failed: {after} rows remain in {table}")

    log.info(f"  [DELETE] {table}: OK — cleared {before} rows")


def supabase_insert(table: str, rows: list[dict]) -> int:
    """Plain INSERT in batches of 500. Returns total rows inserted."""
    if not rows:
        return 0

    BATCH   = 500
    total   = 0
    headers = _sb_headers({"Prefer": "return=minimal"})

    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        resp  = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=headers, json=batch)
        if resp.status_code not in (200, 201, 204):
            log.error(f"  Supabase error en {table}: {resp.status_code} {resp.text[:300]}")
            resp.raise_for_status()
        total += len(batch)
        log.info(f"  Supabase {table}: inserted batch {i//BATCH + 1} ({len(batch)} rows)")

    log.info(f"  Supabase {table}: {total} filas insertadas OK")
    return total


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Airtable helper ────────────────────────────────────────────────────────────

def airtable_get_all(table_id: str, fields: list[str]) -> list[dict]:
    url     = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_id}"
    records = []
    params  = {"fields[]": fields, "pageSize": 100}

    while True:
        resp = requests.get(url, headers=AIRTABLE_HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        params["offset"] = offset

    log.info(f"  Airtable {table_id}: {len(records)} registros leídos")
    return records


# ── Sync clientes ──────────────────────────────────────────────────────────────

def sync_clientes() -> int:
    log.info("── Sync clientes ──")

    fields = [
        "Cliente", "Tipo", "Owner", "Status BP", "Status CI/CO/PR",
        "Tier", "Pais", "Score Empresa Marketing", "Justificación Score Empresa",
        "MAU's", "TPV Mensual", "Valor",
    ]
    records = airtable_get_all(TABLE_CRM, fields)

    rows = []
    for r in records:
        f = r.get("fields", {})
        rows.append({
            "airtable_id":         r["id"],
            "nombre":              f.get("Cliente") or "",
            "tipo":                f.get("Tipo"),
            "owner":               f.get("Owner"),
            "status_bp":           f.get("Status BP"),
            "status_cico":         f.get("Status CI/CO/PR"),
            "tier":                f.get("Tier"),
            "pais":                f.get("Pais"),
            "score_marketing":     f.get("Score Empresa Marketing"),
            "justificacion_score": f.get("Justificación Score Empresa"),
            "maus":                f.get("MAU's"),
            "tpv_mensual":         f.get("TPV Mensual"),
            "valor":               f.get("Valor"),
            "synced_at":           now_iso(),
            "updated_at":          now_iso(),
        })

    supabase_delete_all("bizdev_clientes")
    return supabase_insert("bizdev_clientes", rows)


# ── Sync contactos ─────────────────────────────────────────────────────────────

def build_cliente_map() -> dict[str, str]:
    url  = f"{SUPABASE_URL}/rest/v1/bizdev_clientes"
    resp = requests.get(
        url,
        headers=_sb_headers(),
        params={"select": "id,airtable_id", "limit": 5000},
    )
    resp.raise_for_status()
    return {row["airtable_id"]: row["id"] for row in resp.json()}


def sync_contactos(cliente_map: dict[str, str]) -> int:
    log.info("── Sync contactos ──")

    fields = [
        "fld8nAMWfKfipMgv8",  # Nombre
        "fldaHcADHxSUcw1Rr",  # Apellido
        "fldmXti9SGuGbJzG8",  # Correo
        "fldI77R8ZfWirYcaW",  # Número de teléfono
        "fldL4YpgoTrg05o6S",  # Rol
        "fldZFEk1ZJGrv9fVp",  # Vertical
        "fldJ5nbAQ0jt8YxZp",  # Pais
        "fld6awZ2suNeeFDCg",  # Tipo
        "fld1SvyIEiwlfy7N5",  # Rating Persona
        "fldjcu0hTbdheUxWl",  # Categoría Marketing
        "fldl0r9btzeqLybTE",  # Notas
        "fldYIs7UO4L95I35B",  # Clientes (linked record)
    ]
    records = airtable_get_all(TABLE_CONTACTOS, fields)

    rows = []
    for r in records:
        f = r.get("fields", {})

        clientes_linked     = f.get("fldYIs7UO4L95I35B", [])
        cliente_airtable_id = clientes_linked[0] if clientes_linked else None
        cliente_id          = cliente_map.get(cliente_airtable_id) if cliente_airtable_id else None

        cat_mktg = f.get("fldjcu0hTbdheUxWl")
        if isinstance(cat_mktg, str):
            cat_mktg = [cat_mktg]

        rows.append({
            "airtable_id":         r["id"],
            "nombre":              f.get("fld8nAMWfKfipMgv8") or "",
            "apellido":            f.get("fldaHcADHxSUcw1Rr"),
            "email":               f.get("fldmXti9SGuGbJzG8"),
            "telefono":            f.get("fldI77R8ZfWirYcaW"),
            "rol":                 f.get("fldL4YpgoTrg05o6S"),
            "vertical":            f.get("fldZFEk1ZJGrv9fVp"),
            "pais":                f.get("fldJ5nbAQ0jt8YxZp"),
            "tipo":                f.get("fld6awZ2suNeeFDCg"),
            "rating_persona":      f.get("fld1SvyIEiwlfy7N5"),
            "categoria_marketing": cat_mktg,
            "notas":               f.get("fldl0r9btzeqLybTE"),
            "cliente_id":          cliente_id,
            "synced_at":           now_iso(),
            "updated_at":          now_iso(),
        })

    supabase_delete_all("bizdev_contactos")
    return supabase_insert("bizdev_contactos", rows)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log.info(f"=== TAPI Airtable→Supabase Sync — {now_iso()} ===")
    startup_check()

    try:
        n_clientes  = sync_clientes()
        cliente_map = build_cliente_map()
        n_contactos = sync_contactos(cliente_map)
        log.info(f"=== Sync completo: {n_clientes} clientes, {n_contactos} contactos ===")
    except Exception as e:
        log.error(f"Sync falló: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
