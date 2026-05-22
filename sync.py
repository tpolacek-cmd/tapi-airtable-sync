"""
TAPI — Airtable → Supabase Sync
Sincroniza Contactos BizDev y CRM (clientes) de Airtable a Supabase.
Corre cada 15 minutos via GitHub Actions.
"""

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
AIRTABLE_TOKEN      = os.environ["AIRTABLE_TOKEN"]       # Personal Access Token de Airtable
AIRTABLE_BASE_ID    = os.environ["AIRTABLE_BASE_ID"]     # appHeFqYDGYDUJVbt
SUPABASE_URL        = os.environ["SUPABASE_URL"]         # https://rqowrsfbkcpuzfbpsljw.supabase.co
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"] # JWT service_role (legacy)

# ── IDs de tablas Airtable ─────────────────────────────────────────────────────
TABLE_CONTACTOS = "tbl9elB9e34E5AdUD"   # Contactos BizDev
TABLE_CRM       = "tblb7D95sZFtSkBCn"   # CRM (clientes)

# ── Headers ────────────────────────────────────────────────────────────────────
AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type":  "application/json",
}
SUPABASE_HEADERS = {
    "apikey":        SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates",  # upsert
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def airtable_get_all(table_id: str, fields: list[str]) -> list[dict]:
    """Lee todos los registros de una tabla de Airtable (maneja paginación)."""
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


def supabase_upsert(table: str, rows: list[dict]) -> int:
    """Hace upsert en Supabase. Devuelve cantidad de filas procesadas."""
    if not rows:
        return 0

    # Supabase acepta hasta 1000 filas por request
    BATCH = 500
    total = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        url  = f"{SUPABASE_URL}/rest/v1/{table}"
        resp = requests.post(url, headers=SUPABASE_HEADERS, json=batch)
        if resp.status_code not in (200, 201):
            log.error(f"  Supabase error en {table}: {resp.status_code} {resp.text[:300]}")
            resp.raise_for_status()
        total += len(batch)

    log.info(f"  Supabase {table}: {total} filas upserted")
    return total


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Sync clientes (CRM → bizdev_clientes) ─────────────────────────────────────

def sync_clientes():
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

    return supabase_upsert("bizdev_clientes", rows)


# ── Sync contactos (Contactos BizDev → bizdev_contactos) ──────────────────────

def build_cliente_map() -> dict[str, str]:
    """
    Devuelve un dict {airtable_record_id → supabase_uuid} para bizdev_clientes.
    Necesario para setear cliente_id en cada contacto.
    """
    url  = f"{SUPABASE_URL}/rest/v1/bizdev_clientes"
    resp = requests.get(
        url,
        headers={**SUPABASE_HEADERS, "Prefer": ""},
        params={"select": "id,airtable_id", "limit": 5000},
    )
    resp.raise_for_status()
    return {row["airtable_id"]: row["id"] for row in resp.json()}


def sync_contactos(cliente_map: dict[str, str]):
    log.info("── Sync contactos ──")

    fields = [
        "Nombre", "Apellido", "Correo", "Número de teléfono", "Rol",
        "Vertical", "Pais", "Tipo", "Rating Persona",
        "Categoría Marketing", "Notas",
        "Clientes",   # linked record → airtable_id del cliente
    ]
    records = airtable_get_all(TABLE_CONTACTOS, fields)

    rows = []
    for r in records:
        f = r.get("fields", {})

        # Resolver cliente_id: tomar el primer linked record y mapear a UUID de Supabase
        clientes_linked = f.get("Clientes", [])
        cliente_airtable_id = clientes_linked[0] if clientes_linked else None
        cliente_id = cliente_map.get(cliente_airtable_id) if cliente_airtable_id else None

        # categoria_marketing es multiselect en Airtable → array en Supabase
        cat_mktg = f.get("Categoría Marketing")
        if isinstance(cat_mktg, str):
            cat_mktg = [cat_mktg]

        rows.append({
            "airtable_id":        r["id"],
            "nombre":             f.get("Nombre") or "",
            "apellido":           f.get("Apellido"),
            "email":              f.get("Correo"),
            "telefono":           f.get("Número de teléfono"),
            "rol":                f.get("Rol"),
            "vertical":           f.get("Vertical"),
            "pais":               f.get("Pais"),
            "tipo":               f.get("Tipo"),
            "rating_persona":     f.get("Rating Persona"),   # A / B / C
            "categoria_marketing": cat_mktg,
            "notas":              f.get("Notas"),
            "cliente_id":         cliente_id,
            "synced_at":          now_iso(),
            "updated_at":         now_iso(),
        })

    return supabase_upsert("bizdev_contactos", rows)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log.info(f"=== TAPI Airtable→Supabase Sync — {now_iso()} ===")

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
