"""Arena → Odoo Sync — Web Dashboard Backend.

All configuration happens through the dashboard UI.
No .env file needed for API credentials.
"""

import json
import logging
import threading
import time
from datetime import datetime

import schedule
from flask import Flask, jsonify, render_template, request

from arena_client import ArenaClient
from odoo_client import OdooClient
from config import load_config, save_config, is_arena_configured, is_odoo_configured
from sync import run_sync, load_state, save_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("sync.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── In-memory state ──────────────────────────────────────────────────

activity_log: list[dict] = []
MAX_LOG = 500

sync_runtime = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "scheduler_active": False,
}


def log_activity(level: str, message: str, details: str = "") -> None:
    activity_log.insert(0, {
        "ts": datetime.now().isoformat(),
        "level": level,
        "message": message,
        "details": details,
    })
    if len(activity_log) > MAX_LOG:
        activity_log.pop()


# ── Client builders ──────────────────────────────────────────────────

def build_arena(config: dict) -> ArenaClient:
    a = config["arena"]
    return ArenaClient(a["api_url"], a["email"], a["password"], a["workspace_id"])


def build_odoo(config: dict) -> OdooClient:
    o = config["odoo"]
    return OdooClient(o["url"], o["db"], o["user"], o["password"])


# ── Sync job ─────────────────────────────────────────────────────────

def _run_sync_job():
    if sync_runtime["running"]:
        log_activity("WARN", "Sync already running — skipping")
        return

    sync_runtime["running"] = True
    log_activity("INFO", "Sync started")

    try:
        config = load_config()
        if not is_arena_configured(config):
            raise ValueError("Arena API not configured — go to Settings")
        if not is_odoo_configured(config):
            raise ValueError("Odoo API not configured — go to Settings")

        arena = build_arena(config)
        odoo = build_odoo(config)
        arena.authenticate()
        odoo.authenticate()

        result = run_sync(arena, odoo, config.get("mapping", {}))
        sync_runtime["last_result"] = result
        sync_runtime["last_run"] = datetime.now().isoformat()

        errors = result.get("errors", [])
        missing = result.get("missing_components", [])

        msg = (
            f"Sync complete: {result['products_created']} created, "
            f"{result['products_updated']} updated, "
            f"{result['boms_created']} BOMs, "
            f"{result['skipped_unchanged']} unchanged"
        )
        if errors:
            msg += f", {len(errors)} ERRORS"
        if missing:
            msg += f", {len(missing)} missing components"

        log_activity("OK" if not errors else "ERROR", msg)
        return result

    except Exception as e:
        log_activity("ERROR", f"Sync failed: {e}")
        sync_runtime["last_result"] = {"error": str(e)}
        return None
    finally:
        sync_runtime["running"] = False


def _scheduler_loop():
    while sync_runtime["scheduler_active"]:
        schedule.run_pending()
        time.sleep(1)


# ── Routes: Pages ────────────────────────────────────────────────────

@app.route("/")
def page_dashboard():
    return render_template("dashboard.html")


# ── Routes: Status & Data ────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    config = load_config()
    state = load_state()
    items = state.get("items", {})
    runs = state.get("runs", [])

    total = len(items)
    synced = sum(1 for v in items.values() if v.get("status") == "SYNCED")
    errored = sum(1 for v in items.values() if v.get("status") == "ERROR")
    with_bom = sum(1 for v in items.values() if v.get("odoo_bom_id"))
    assemblies = sum(1 for v in items.values()
                     if v.get("assembly_type") and v["assembly_type"] != "NOT_AN_ASSEMBLY")
    components = total - assemblies

    return jsonify({
        "runtime": sync_runtime,
        "config_ok": {
            "arena": is_arena_configured(config),
            "odoo": is_odoo_configured(config),
        },
        "stats": {
            "total": total,
            "synced": synced,
            "errored": errored,
            "with_bom": with_bom,
            "assemblies": assemblies,
            "components": components,
        },
        "last_run": sync_runtime.get("last_result"),
        "run_history": runs[:20],
    })


@app.route("/api/items")
def api_items():
    state = load_state()
    items = []
    for guid, d in state.get("items", {}).items():
        items.append({
            "guid": guid,
            "number": d.get("number", ""),
            "name": d.get("name", ""),
            "revision": d.get("revision", ""),
            "category": d.get("category", ""),
            "assembly_type": d.get("assembly_type", ""),
            "bom_component_count": d.get("bom_component_count", 0),
            "bom_component_numbers": d.get("bom_component_numbers", []),
            "status": d.get("status", "PENDING"),
            "error": d.get("error"),
            "odoo_template_id": d.get("odoo_template_id"),
            "odoo_bom_id": d.get("odoo_bom_id"),
            "synced_at": d.get("synced_at", ""),
        })
    items.sort(key=lambda x: x["number"])
    return jsonify(items)


@app.route("/api/activity")
def api_activity():
    return jsonify(activity_log[:200])


# ── Routes: Actions ──────────────────────────────────────────────────

@app.route("/api/sync", methods=["POST"])
def api_sync():
    if sync_runtime["running"]:
        return jsonify({"error": "Sync already in progress"}), 409
    threading.Thread(target=_run_sync_job, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync started"})


@app.route("/api/fetch-arena", methods=["POST"])
def api_fetch_arena():
    """Fetch all 'In Production' items from Arena and cross-check against Odoo.

    Returns each item with an 'odoo_status' field:
      - 'new': not in Odoo yet
      - 'exists': already in Odoo (with template ID)
    """
    try:
        config = load_config()
        if not is_arena_configured(config):
            return jsonify({"error": "Arena not configured"}), 400

        arena = build_arena(config)
        arena.authenticate()

        # Check Odoo if configured
        odoo = None
        if is_odoo_configured(config):
            try:
                odoo = build_odoo(config)
                odoo.authenticate()
            except Exception as e:
                log_activity("WARN", f"Odoo not reachable for cross-check: {e}")

        items = arena.get_items(lifecycle_phase="In Production")
        in_prod_guids = {i["guid"] for i in items}

        # Batch-fetch all products with default_code from Odoo
        odoo_products = {}  # {default_code: template_id}
        if odoo:
            try:
                odoo_products = odoo.find_all_products_with_codes()
                log_activity("INFO", f"Found {len(odoo_products)} products with internal references in Odoo")
            except Exception as e:
                log_activity("WARN", f"Could not batch-fetch Odoo products: {e}")

        result = []
        for item in items:
            guid = item.get("guid", "")
            number = item.get("number", "")

            # Check Odoo: match by raw part number OR ARENA-prefixed code
            odoo_template_id = odoo_products.get(number) or odoo_products.get(f"ARENA-{number}")
            if odoo:
                odoo_status = "exists" if odoo_template_id else "new"
            else:
                odoo_status = "unchecked"

            # Only fetch BOM for actual assemblies (skip NOT_AN_ASSEMBLY)
            assembly_type = item.get("assemblyType", "")
            is_assembly = assembly_type and assembly_type != "NOT_AN_ASSEMBLY"
            bom_lines = arena.get_bom_for_item(guid) if is_assembly else []
            missing = []
            for l in bom_lines:
                cg = (l.get("item") or {}).get("guid")
                if cg and cg not in in_prod_guids:
                    missing.append({
                        "number": (l.get("item") or {}).get("number", "?"),
                        "name": (l.get("item") or {}).get("name", "?"),
                        "qty": l.get("quantity", 0),
                    })

            result.append({
                "guid": guid,
                "number": number,
                "name": item.get("name", ""),
                "revision": item.get("revisionNumber", ""),
                "category": (item.get("category") or {}).get("name", ""),
                "assembly_type": item.get("assemblyType", ""),
                "bom_count": len(bom_lines),
                "bom_components": [
                    {
                        "number": l.get("item", {}).get("number", ""),
                        "name": l.get("item", {}).get("name", ""),
                        "qty": l.get("quantity", 0),
                        "in_production": (l.get("item") or {}).get("guid") in in_prod_guids,
                    }
                    for l in bom_lines
                ],
                "missing_components": missing,
                "odoo_status": odoo_status,
                "odoo_template_id": odoo_template_id,
            })

        new_count = sum(1 for r in result if r["odoo_status"] == "new")
        exists_count = sum(1 for r in result if r["odoo_status"] == "exists")
        log_activity("INFO", f"Fetched {len(result)} items from Arena: {new_count} new, {exists_count} already in Odoo")
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# In-memory transfer progress
transfer_progress = {"running": False, "total": 0, "done": 0, "current": "", "results": [], "phase": ""}


@app.route("/api/transfer", methods=["POST"])
def api_transfer():
    """Transfer selected Arena items to Odoo. Expects { guids: [...], items: [...] }."""
    if transfer_progress["running"]:
        return jsonify({"error": "Transfer already in progress"}), 409

    data = request.json or {}
    items_to_transfer = data.get("items", [])
    if not items_to_transfer:
        return jsonify({"error": "No items selected"}), 400

    def do_transfer():
        transfer_progress["running"] = True
        transfer_progress["done"] = 0
        transfer_progress["current"] = ""
        transfer_progress["results"] = []
        transfer_progress["phase"] = "products"

        try:
            config = load_config()
            odoo = build_odoo(config)
            odoo.authenticate()
            mapping_cfg = config.get("mapping", {})
            state = load_state()
            if "items" not in state:
                state["items"] = {}

            from mapping import map_arena_item_to_odoo_product, map_bom_line

            # Sort: components (NOT_AN_ASSEMBLY) first, then sub-assemblies, then top-level
            # This ensures components exist in Odoo before assemblies try to reference them in BOMs
            type_order = {"NOT_AN_ASSEMBLY": 0, "": 0, "SUB_ASSEMBLY": 1, "TOP_LEVEL_ASSEMBLY": 2}
            sorted_items = sorted(items_to_transfer,
                                  key=lambda i: type_order.get(i.get("assembly_type", ""), 0))

            transfer_progress["total"] = len(sorted_items)

            # Build batch lookup map: Arena part number -> Odoo template ID
            # This is the single source of truth for matching
            code_map = odoo.find_all_products_with_codes()
            log_activity("INFO", f"Transfer: loaded {len(code_map)} Odoo product codes for matching")

            # Phase 1: Create all products first
            for item in sorted_items:
                number = item.get("number", "?")
                name = item.get("name", "?")
                guid = item.get("guid", "")
                transfer_progress["current"] = f"{number} — {name}"
                entry = {"number": number, "name": name, "status": "ok", "error": None,
                         "odoo_template_id": None, "odoo_bom_id": None}

                try:
                    # Use batch map for lookup
                    existing_tmpl = code_map.get(number)

                    if existing_tmpl:
                        entry["status"] = "skipped"
                        entry["odoo_template_id"] = existing_tmpl
                    else:
                        product_vals = map_arena_item_to_odoo_product(item, mapping_cfg)
                        tmpl_id = odoo.create_product(product_vals)
                        variant_id = odoo.get_product_variant_id(tmpl_id)
                        entry["odoo_template_id"] = tmpl_id
                        entry["status"] = "created"

                        # Add to lookup map so BOM phase can find this product
                        code_map[number] = tmpl_id

                        # Extract component numbers from bom_components for "Used In" lookup
                        bom_comp_numbers = [c.get("number", "") for c in item.get("bom_components", []) if c.get("number")]

                        state["items"][guid] = {
                            "number": number,
                            "name": name,
                            "revision": item.get("revision", ""),
                            "category": item.get("category", ""),
                            "assembly_type": item.get("assembly_type", ""),
                            "bom_component_count": item.get("bom_count", 0),
                            "bom_component_numbers": bom_comp_numbers,
                            "hash": "",
                            "status": "SYNCED",
                            "error": None,
                            "odoo_template_id": tmpl_id,
                            "odoo_variant_id": variant_id,
                            "odoo_bom_id": None,
                            "synced_at": datetime.now().isoformat(),
                        }
                        save_state(state)

                except Exception as e:
                    entry["status"] = "error"
                    entry["error"] = str(e)
                    logger.error("Transfer failed for %s: %s", number, e, exc_info=True)

                transfer_progress["results"].append(entry)
                transfer_progress["done"] += 1

            # Phase 2: Reconcile ALL assembly BOMs
            transfer_progress["phase"] = "boms"
            transfer_progress["current"] = "Fetching Arena assemblies for BOM reconciliation..."
            try:
                arena = build_arena(config)
                arena.authenticate()
                all_items = arena.get_items(lifecycle_phase="In Production")
                assemblies = [i for i in all_items
                              if i.get("assemblyType") not in (None, "", "NOT_AN_ASSEMBLY")]

                # Refresh code_map to include newly created products
                code_map = odoo.find_all_products_with_codes()

                boms_created = 0
                for asm in assemblies:
                    asm_number = asm.get("number", "")
                    asm_tmpl = code_map.get(asm_number)
                    if not asm_tmpl:
                        continue  # Assembly not in Odoo yet

                    # Skip if BOM already exists
                    if odoo.find_bom_by_product(asm_tmpl):
                        continue

                    # Fetch BOM lines from Arena
                    bom_lines = arena.get_bom_for_item(asm["guid"])
                    if not bom_lines:
                        continue

                    odoo_bom_lines = []
                    skipped_comps = []
                    for line in bom_lines:
                        comp_item = line.get("item") or {}
                        comp_number = comp_item.get("number", "")
                        comp_tmpl = code_map.get(comp_number)
                        if not comp_tmpl:
                            skipped_comps.append(comp_number)
                            continue
                        comp_variant = odoo.get_product_variant_id(comp_tmpl)
                        if not comp_variant:
                            skipped_comps.append(f"{comp_number} (no variant)")
                            continue
                        odoo_bom_lines.append(map_bom_line(
                            comp_variant, line.get("quantity", 1), "", mapping_cfg
                        ))

                    if skipped_comps:
                        logger.warning("BOM %s: %d components not in Odoo: %s",
                                       asm_number, len(skipped_comps), ", ".join(skipped_comps))

                    if odoo_bom_lines:
                        transfer_progress["current"] = f"BOM: {asm_number} ({len(odoo_bom_lines)} lines)"
                        try:
                            bom_id = odoo.create_bom(asm_tmpl, odoo_bom_lines)
                            boms_created += 1

                            # Update result entry if this assembly was in the transfer
                            for r in transfer_progress["results"]:
                                if r["number"] == asm_number:
                                    r["odoo_bom_id"] = bom_id
                                    break

                            # Update state
                            for guid, sdata in state.get("items", {}).items():
                                if sdata.get("number") == asm_number:
                                    sdata["odoo_bom_id"] = bom_id
                                    save_state(state)
                                    break

                        except Exception as e:
                            logger.error("BOM creation failed for %s: %s", asm_number, e, exc_info=True)
                            for r in transfer_progress["results"]:
                                if r["number"] == asm_number:
                                    r["error"] = (r.get("error") or "") + f" BOM error: {e}"
                                    break

                log_activity("INFO", f"BOM reconciliation: created {boms_created} new BOMs")

            except Exception as e:
                logger.error("BOM reconciliation failed: %s", e, exc_info=True)
                log_activity("WARN", f"BOM reconciliation error: {e}")

            created = sum(1 for r in transfer_progress["results"] if r["status"] == "created")
            skipped = sum(1 for r in transfer_progress["results"] if r["status"] == "skipped")
            errors = sum(1 for r in transfer_progress["results"] if r["status"] == "error")
            boms = sum(1 for r in transfer_progress["results"] if r.get("odoo_bom_id"))
            log_activity("OK" if errors == 0 else "ERROR",
                         f"Transfer done: {created} created, {skipped} skipped, {boms} BOMs, {errors} errors")

        except Exception as e:
            log_activity("ERROR", f"Transfer failed: {e}")
        finally:
            transfer_progress["running"] = False
            transfer_progress["current"] = ""

    threading.Thread(target=do_transfer, daemon=True).start()
    return jsonify({"ok": True, "message": f"Transferring {len(items_to_transfer)} items..."})


@app.route("/api/transfer/progress")
def api_transfer_progress():
    return jsonify(transfer_progress)


@app.route("/api/reset-item", methods=["POST"])
def api_reset_item():
    guid = request.json.get("guid")
    if not guid:
        return jsonify({"error": "Missing guid"}), 400
    state = load_state()
    if guid in state.get("items", {}):
        del state["items"][guid]
        save_state(state)
        log_activity("INFO", f"Reset item for re-sync")
        return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404


@app.route("/api/reset-errors", methods=["POST"])
def api_reset_errors():
    """Reset all items in ERROR status so they retry on next sync."""
    state = load_state()
    count = 0
    for guid, data in list(state.get("items", {}).items()):
        if data.get("status") == "ERROR":
            del state["items"][guid]
            count += 1
    save_state(state)
    log_activity("INFO", f"Reset {count} errored items for retry")
    return jsonify({"ok": True, "reset": count})


# ── Routes: Connection Tests ─────────────────────────────────────────

@app.route("/api/test/arena", methods=["POST"])
def api_test_arena():
    try:
        config = load_config()
        client = build_arena(config)
        client.authenticate()
        # Try a minimal items fetch to verify full access
        items = client.get_items(lifecycle_phase="In Production")
        return jsonify({
            "ok": True,
            "message": f"Connected. Found {len(items)} items in production.",
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/test/odoo", methods=["POST"])
def api_test_odoo():
    try:
        config = load_config()
        client = build_odoo(config)
        uid = client.authenticate()
        version = client.get_server_version()
        return jsonify({
            "ok": True,
            "message": f"Connected to Odoo {version} (uid={uid})",
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


# ── Routes: Odoo Product Diagnostic ───────────────────────────────────

@app.route("/api/debug/odoo-products")
def api_debug_odoo_products():
    """Dump all Odoo products with their key fields for debugging matching logic."""
    try:
        config = load_config()
        client = build_odoo(config)
        client.authenticate()

        # Fetch ALL product.product variants with identifying fields
        var_ids = client.execute("product.product", "search", [[]], {"limit": 500})
        variants = []
        if var_ids:
            variants = client.execute("product.product", "read",
                [var_ids, ["id", "name", "default_code", "barcode", "product_tmpl_id"]])

        # Fetch ALL product.template with identifying fields
        tmpl_ids = client.execute("product.template", "search", [[]], {"limit": 500})
        templates = []
        if tmpl_ids:
            templates = client.execute("product.template", "read",
                [tmpl_ids, ["id", "name", "default_code"]])

        return jsonify({
            "templates_count": len(templates),
            "variants_count": len(variants),
            "templates": templates[:50],
            "variants": variants[:50],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/debug/odoo-search/<part_number>")
def api_debug_odoo_search(part_number):
    """Search Odoo for a specific part number across all relevant fields."""
    try:
        config = load_config()
        client = build_odoo(config)
        client.authenticate()

        results = {}

        # Search product.template by default_code exact
        ids = client.execute("product.template", "search", [[["default_code", "=", part_number]]])
        results["tmpl_by_exact_code"] = ids

        # Search product.template by default_code ilike
        ids = client.execute("product.template", "search", [[["default_code", "ilike", part_number]]])
        results["tmpl_by_ilike_code"] = ids

        # Search product.template by name ilike
        ids = client.execute("product.template", "search", [[["name", "ilike", part_number]]])
        results["tmpl_by_name"] = ids
        if ids:
            results["tmpl_by_name_details"] = client.execute("product.template", "read",
                [ids, ["id", "name", "default_code"]])

        # Search product.product by default_code exact
        ids = client.execute("product.product", "search", [[["default_code", "=", part_number]]])
        results["variant_by_exact_code"] = ids

        # Search product.product by default_code ilike
        ids = client.execute("product.product", "search", [[["default_code", "ilike", part_number]]])
        results["variant_by_ilike_code"] = ids

        # Search product.product by name ilike
        ids = client.execute("product.product", "search", [[["name", "ilike", part_number]]])
        results["variant_by_name"] = ids
        if ids:
            results["variant_by_name_details"] = client.execute("product.product", "read",
                [ids, ["id", "name", "default_code", "product_tmpl_id"]])

        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Routes: Test Product ──────────────────────────────────────────────

@app.route("/api/test/create-product", methods=["POST"])
def api_test_create_product():
    """Create a test product in Odoo to verify write access. Deletes it after."""
    try:
        config = load_config()
        client = build_odoo(config)
        client.authenticate()

        data = request.json or {}
        name = data.get("name", "SYNC-TEST-PRODUCT")
        code = data.get("code", "ARENA-TEST-001")
        keep = data.get("keep", False)  # If true, don't delete after creation

        # Create product
        vals = {
            "name": name,
            "default_code": code,
            "type": "consu",
            "sale_ok": False,
            "purchase_ok": True,
            "description": "Test product created by Arena-Odoo Sync tool.\nSafe to delete.",
        }

        # Check if it already exists
        existing = client.find_product_by_code(code)
        if existing:
            if not keep:
                # Clean it up
                client.execute("product.template", "unlink", [[existing]])
                log_activity("INFO", f"Test product '{code}' already existed — deleted it")

                # Re-create fresh
                pid = client.create_product(vals)
                variant = client.get_product_variant_id(pid)
                client.execute("product.template", "unlink", [[pid]])
                log_activity("OK", f"Test product created (id={pid}) and cleaned up")
                return jsonify({
                    "ok": True,
                    "message": f"Write access verified. Created product id={pid}, variant id={variant}, then deleted it.",
                    "product_id": pid,
                    "cleaned_up": True,
                })
            else:
                return jsonify({
                    "ok": True,
                    "message": f"Product '{code}' already exists (template id={existing}). No action taken.",
                    "product_id": existing,
                    "cleaned_up": False,
                })

        pid = client.create_product(vals)
        variant = client.get_product_variant_id(pid)

        if keep:
            log_activity("OK", f"Test product '{name}' created in Odoo (template={pid}, variant={variant})")
            return jsonify({
                "ok": True,
                "message": f"Product '{name}' created in Odoo. Template ID={pid}, Variant ID={variant}.",
                "product_id": pid,
                "variant_id": variant,
                "cleaned_up": False,
            })
        else:
            client.execute("product.template", "unlink", [[pid]])
            log_activity("OK", f"Test product created (id={pid}) and cleaned up")
            return jsonify({
                "ok": True,
                "message": f"Write access verified. Created product id={pid}, then deleted it.",
                "product_id": pid,
                "cleaned_up": True,
            })

    except Exception as e:
        log_activity("ERROR", f"Test product creation failed: {e}")
        return jsonify({"ok": False, "message": str(e)}), 400


# ── Routes: Configuration ────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_get_config():
    config = load_config()
    # Mask passwords for display
    safe = json.loads(json.dumps(config))
    if safe["arena"].get("password"):
        safe["arena"]["password_set"] = True
        safe["arena"]["password"] = ""
    else:
        safe["arena"]["password_set"] = False
    if safe["odoo"].get("password"):
        safe["odoo"]["password_set"] = True
        safe["odoo"]["password"] = ""
    else:
        safe["odoo"]["password_set"] = False
    return jsonify(safe)


@app.route("/api/config", methods=["PUT"])
def api_save_config():
    incoming = request.json
    config = load_config()

    # Arena settings
    if "arena" in incoming:
        for key in ("api_url", "email", "workspace_id"):
            if key in incoming["arena"]:
                config["arena"][key] = incoming["arena"][key]
        # Only update password if a non-empty value is sent
        if incoming["arena"].get("password"):
            config["arena"]["password"] = incoming["arena"]["password"]

    # Odoo settings
    if "odoo" in incoming:
        for key in ("url", "db", "user"):
            if key in incoming["odoo"]:
                config["odoo"][key] = incoming["odoo"][key]
        if incoming["odoo"].get("password"):
            config["odoo"]["password"] = incoming["odoo"]["password"]

    # Sync settings
    if "sync" in incoming:
        config["sync"].update(incoming["sync"])

    # Mapping
    if "mapping" in incoming:
        config["mapping"].update(incoming["mapping"])

    save_config(config)
    log_activity("INFO", "Configuration updated")
    return jsonify({"ok": True})


@app.route("/api/odoo/categories")
def api_odoo_categories():
    try:
        config = load_config()
        client = build_odoo(config)
        client.authenticate()
        return jsonify(client.get_product_categories())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/odoo/uoms")
def api_odoo_uoms():
    try:
        config = load_config()
        client = build_odoo(config)
        client.authenticate()
        return jsonify(client.get_uom_list())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Routes: Scheduler ────────────────────────────────────────────────

@app.route("/api/scheduler", methods=["POST"])
def api_scheduler():
    action = request.json.get("action")

    if action == "start":
        if sync_runtime["scheduler_active"]:
            return jsonify({"message": "Already running"})

        config = load_config()
        interval = config.get("sync", {}).get("interval_minutes", 15)
        sync_runtime["scheduler_active"] = True

        schedule.clear()
        schedule.every(interval).minutes.do(_run_sync_job)
        threading.Thread(target=_scheduler_loop, daemon=True).start()

        log_activity("INFO", f"Auto-sync started: every {interval} min")
        return jsonify({"ok": True, "interval": interval})

    elif action == "stop":
        sync_runtime["scheduler_active"] = False
        schedule.clear()
        log_activity("INFO", "Auto-sync stopped")
        return jsonify({"ok": True})

    return jsonify({"error": "Invalid action"}), 400


# ── Main ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", "5000"))
    print(f"\n  Arena -> Odoo Sync Dashboard")
    print(f"  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=True)
