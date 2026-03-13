"""Arena -> Odoo Sync — Web Dashboard Backend.

Thin Flask route handlers. All transfer/sync logic lives in transfer.py.
"""

import json
import logging
import threading
from datetime import datetime

from flask import Flask, jsonify, render_template, request

from arena_client import ArenaClient
from odoo_client import OdooClient
from config import load_config, save_config, is_arena_configured, is_odoo_configured
from transfer import (
    transfer_items, run_full_sync, load_state, save_state,
    progress as transfer_progress, is_engine_busy,
)
from sync import start_scheduler, stop_scheduler, is_scheduler_active

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

# ── In-memory activity log ───────────────────────────────────────────

activity_log: list[dict] = []
_log_lock = threading.Lock()
MAX_LOG = 500


def log_activity(level: str, message: str, details: str = "") -> None:
    with _log_lock:
        activity_log.insert(0, {
            "ts": datetime.now().isoformat(),
            "level": level,
            "message": message,
            "details": details,
        })
        if len(activity_log) > MAX_LOG:
            activity_log.pop()


# ── Runtime state ────────────────────────────────────────────────────

sync_runtime = {
    "running": False,
    "last_run": None,
    "last_result": None,
}


# ── Client builders ──────────────────────────────────────────────────

def build_arena(config: dict) -> ArenaClient:
    a = config["arena"]
    return ArenaClient(a["api_url"], a["email"], a["password"], a["workspace_id"])


def build_odoo(config: dict) -> OdooClient:
    o = config["odoo"]
    return OdooClient(o["url"], o["db"], o["user"], o["password"])


def _authenticated_arena():
    config = load_config()
    arena = build_arena(config)
    arena.authenticate()
    return arena


def _authenticated_odoo():
    config = load_config()
    odoo = build_odoo(config)
    odoo.authenticate()
    return odoo


def _mapping_config():
    return load_config().get("mapping", {})


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

    return jsonify({
        "runtime": {
            "running": sync_runtime["running"],
            "last_run": sync_runtime["last_run"],
            "scheduler_active": is_scheduler_active(),
        },
        "config_ok": {
            "arena": is_arena_configured(config),
            "odoo": is_odoo_configured(config),
        },
        "odoo_url": config.get("odoo", {}).get("url", ""),
        "stats": {
            "total": total,
            "synced": synced,
            "errored": errored,
            "with_bom": with_bom,
            "assemblies": assemblies,
            "components": total - assemblies,
        },
        "last_run": sync_runtime.get("last_result"),
        "run_history": runs[:20],
    })


@app.route("/api/items")
def api_items():
    state = load_state()
    all_items = state.get("items", {})

    synced_numbers = {d.get("number", "") for d in all_items.values()}
    number_to_odoo = {
        d.get("number", ""): {
            "odoo_template_id": d.get("odoo_template_id"),
            "odoo_bom_id": d.get("odoo_bom_id"),
        }
        for d in all_items.values()
    }

    # Reverse map: component number -> parent assemblies
    comp_to_parents: dict[str, list[dict]] = {}
    for guid, d in all_items.items():
        parent_number = d.get("number", "")
        parent_name = d.get("name", "")
        parent_in_odoo = bool(d.get("odoo_template_id"))
        for cn in d.get("bom_component_numbers", []):
            if cn not in comp_to_parents:
                comp_to_parents[cn] = []
            comp_to_parents[cn].append({
                "number": parent_number,
                "name": parent_name,
                "in_odoo": parent_in_odoo,
            })

    items = []
    for guid, d in all_items.items():
        raw_comps = d.get("bom_components", [])
        if not raw_comps and d.get("bom_component_numbers"):
            raw_comps = [{"number": cn, "name": "", "qty": ""} for cn in d["bom_component_numbers"]]

        enriched_comps = []
        missing_components = []
        for c in raw_comps:
            cn = c.get("number", "")
            in_synced = cn in synced_numbers
            comp_odoo = number_to_odoo.get(cn, {})
            enriched_comps.append({
                "number": cn,
                "name": c.get("name", ""),
                "qty": c.get("qty", ""),
                "in_synced": in_synced,
                "in_odoo": bool(comp_odoo.get("odoo_template_id")),
            })
            if not in_synced:
                missing_components.append({"number": cn, "name": c.get("name", ""), "qty": c.get("qty", "")})

        items.append({
            "guid": guid,
            "number": d.get("number", ""),
            "name": d.get("name", ""),
            "revision": d.get("revision", ""),
            "category": d.get("category", ""),
            "assembly_type": d.get("assembly_type", ""),
            "lifecycle": d.get("lifecycle", "In Production"),
            "bom_component_count": d.get("bom_component_count", 0),
            "bom_components": enriched_comps,
            "missing_components": missing_components,
            "used_in": comp_to_parents.get(d.get("number", ""), []),
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
    with _log_lock:
        return jsonify(activity_log[:200])


# ── Routes: Fetch Arena ──────────────────────────────────────────────

@app.route("/api/fetch-arena", methods=["POST"])
def api_fetch_arena():
    """Fetch items from Arena and cross-check against Odoo."""
    try:
        config = load_config()
        if not is_arena_configured(config):
            return jsonify({"error": "Arena not configured"}), 400

        arena = build_arena(config)
        arena.authenticate()

        odoo = None
        if is_odoo_configured(config):
            try:
                odoo = build_odoo(config)
                odoo.authenticate()
            except Exception as e:
                log_activity("WARN", f"Odoo not reachable for cross-check: {e}")

        items = arena.get_items_for_sync()
        in_prod_guids = {i["guid"] for i in items if i.get("_lifecycle") == "In Production"}

        # Batch-fetch Odoo products
        odoo_products = {}
        if odoo:
            try:
                odoo_products = odoo.find_all_products_with_codes()
                log_activity("INFO", f"Odoo cross-check: found {len(odoo_products)} products with codes")
            except Exception as e:
                log_activity("WARN", f"Could not fetch Odoo products: {e}")

        # Cross-check with saved state: items we previously synced should be in Odoo
        state = load_state()
        state_items = state.get("items", {})
        synced_numbers = {s.get("number") for s in state_items.values() if s.get("status") == "SYNCED"}

        # Build reverse lookup: component guid -> parent assemblies
        comp_to_assemblies: dict[str, list[dict]] = {}
        result = []

        for item in items:
            guid = item.get("guid", "")
            number = item.get("number", "").strip()
            odoo_template_id = odoo_products.get(number)

            # Also check saved state — if we synced it before, use stored template ID
            if not odoo_template_id and guid in state_items:
                stored = state_items[guid]
                if stored.get("odoo_template_id") and stored.get("status") == "SYNCED":
                    odoo_template_id = stored["odoo_template_id"]
                    logger.warning("Product %s not found by code in Odoo but exists in state (tmpl=%s)", number, odoo_template_id)

            odoo_status = "exists" if odoo_template_id else "new" if odoo else "unchecked"

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
                if cg:
                    if cg not in comp_to_assemblies:
                        comp_to_assemblies[cg] = []
                    comp_to_assemblies[cg].append({
                        "number": number,
                        "name": item.get("name", ""),
                        "in_odoo": bool(odoo_template_id),
                    })

            result.append({
                "guid": guid,
                "number": number,
                "name": item.get("name", ""),
                "revision": item.get("revisionNumber", ""),
                "category": (item.get("category") or {}).get("name", ""),
                "assembly_type": assembly_type,
                "lifecycle": item.get("_lifecycle", "In Production"),
                "bom_count": len(bom_lines),
                "bom_components": [
                    {
                        "number": (l.get("item") or {}).get("number", ""),
                        "name": (l.get("item") or {}).get("name", ""),
                        "qty": l.get("quantity", 0),
                        "in_production": (l.get("item") or {}).get("guid") in in_prod_guids,
                        "in_odoo": bool(
                            odoo_products.get((l.get("item") or {}).get("number", "").strip())
                            or (l.get("item") or {}).get("number", "").strip() in synced_numbers
                        ),
                    }
                    for l in bom_lines
                ],
                "missing_components": missing,
                "odoo_status": odoo_status,
                "odoo_template_id": odoo_template_id,
            })

        # Enrich with used_in_assemblies
        for item_data, item_raw in zip(result, items):
            item_data["used_in_assemblies"] = comp_to_assemblies.get(item_raw.get("guid", ""), [])

        new_count = sum(1 for r in result if r["odoo_status"] == "new")
        exists_count = sum(1 for r in result if r["odoo_status"] == "exists")
        log_activity("INFO", f"Fetched {len(result)} items from Arena: {new_count} new, {exists_count} in Odoo")
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Routes: Transfer ─────────────────────────────────────────────────

@app.route("/api/transfer", methods=["POST"])
def api_transfer():
    """Transfer selected items to Odoo."""
    if transfer_progress.running or is_engine_busy():
        return jsonify({"error": "Transfer already in progress"}), 409

    data = request.json or {}
    items_to_transfer = data.get("items", [])
    if not items_to_transfer:
        return jsonify({"error": "No items selected"}), 400

    # Pre-flight: verify credentials BEFORE starting background thread
    config = load_config()
    if not is_arena_configured(config):
        return jsonify({"error": "Arena is not configured. Go to Settings and enter your Arena credentials."}), 400
    if not is_odoo_configured(config):
        return jsonify({"error": "Odoo is not configured. Go to Settings and enter your Odoo credentials."}), 400

    try:
        arena = build_arena(config)
        arena.authenticate()
    except Exception as e:
        return jsonify({"error": f"Arena login failed: {e}"}), 400

    try:
        odoo = build_odoo(config)
        odoo.authenticate()
    except Exception as e:
        return jsonify({"error": f"Odoo login failed: {e}"}), 400

    def do_transfer():
        try:
            result = transfer_items(
                items_to_transfer, arena, odoo,
                config.get("mapping", {}), log_activity,
            )

            created = sum(1 for r in transfer_progress.results if r["status"] == "created")
            skipped = sum(1 for r in transfer_progress.results if r["status"] == "exists")
            errors = sum(1 for r in transfer_progress.results if r["status"] == "error")
            log_activity(
                "OK" if errors == 0 else "ERROR",
                f"Transfer done: {created} created, {skipped} existing, "
                f"{result['boms_created']} BOMs created, {errors} errors",
            )
        except Exception as e:
            log_activity("ERROR", f"Transfer failed: {e}")
            transfer_progress.update(result={"number": "GLOBAL", "name": "", "status": "error", "error": str(e), "odoo_template_id": None, "odoo_bom_id": None})
            transfer_progress.finish()

    threading.Thread(target=do_transfer, daemon=True).start()
    return jsonify({"ok": True, "message": f"Transferring {len(items_to_transfer)} items..."})


@app.route("/api/transfer/progress")
def api_transfer_progress():
    return jsonify(transfer_progress.to_dict())


# ── Routes: Sync ─────────────────────────────────────────────────────

@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Trigger a full sync (fetch all from Arena + transfer to Odoo)."""
    if sync_runtime["running"] or is_engine_busy():
        return jsonify({"error": "Sync already in progress"}), 409

    def do_sync():
        sync_runtime["running"] = True
        log_activity("INFO", "Full sync started")
        try:
            config = load_config()
            arena = build_arena(config)
            odoo = build_odoo(config)
            arena.authenticate()
            odoo.authenticate()

            result = run_full_sync(arena, odoo, config.get("mapping", {}), log_activity)
            sync_runtime["last_result"] = result
            sync_runtime["last_run"] = datetime.now().isoformat()

            errors = result.get("errors", [])
            msg = (
                f"Sync complete: {result['products_created']} created, "
                f"{result['products_updated']} updated, "
                f"{result['boms_created']} BOMs, "
                f"{result['skipped_unchanged']} unchanged"
            )
            if errors:
                msg += f", {len(errors)} ERRORS"
            log_activity("OK" if not errors else "ERROR", msg)
        except Exception as e:
            log_activity("ERROR", f"Sync failed: {e}")
            sync_runtime["last_result"] = {"error": str(e)}
        finally:
            sync_runtime["running"] = False

    threading.Thread(target=do_sync, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync started"})


# ── Routes: State management ─────────────────────────────────────────

@app.route("/api/reset-item", methods=["POST"])
def api_reset_item():
    guid = request.json.get("guid")
    if not guid:
        return jsonify({"error": "Missing guid"}), 400
    state = load_state()
    if guid in state.get("items", {}):
        del state["items"][guid]
        save_state(state)
        return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404


@app.route("/api/reset-errors", methods=["POST"])
def api_reset_errors():
    state = load_state()
    count = 0
    for guid in list(state.get("items", {}).keys()):
        if state["items"][guid].get("status") == "ERROR":
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
        items = client.get_items(lifecycle_phase="In Production")
        return jsonify({"ok": True, "message": f"Connected. {len(items)} items in production."})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/test/odoo", methods=["POST"])
def api_test_odoo():
    try:
        config = load_config()
        client = build_odoo(config)
        uid = client.authenticate()
        version = client.get_server_version()
        return jsonify({"ok": True, "message": f"Connected to Odoo {version} (uid={uid})"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


# ── Routes: Configuration ────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_get_config():
    config = load_config()
    safe = json.loads(json.dumps(config))
    for section in ("arena", "odoo"):
        if safe[section].get("password"):
            safe[section]["password_set"] = True
            safe[section]["password"] = ""
        else:
            safe[section]["password_set"] = False
    return jsonify(safe)


@app.route("/api/config", methods=["PUT"])
def api_save_config():
    incoming = request.json
    config = load_config()

    if "arena" in incoming:
        for key in ("api_url", "email", "workspace_id"):
            if key in incoming["arena"]:
                config["arena"][key] = incoming["arena"][key]
        if incoming["arena"].get("password"):
            config["arena"]["password"] = incoming["arena"]["password"]

    if "odoo" in incoming:
        for key in ("url", "db", "user"):
            if key in incoming["odoo"]:
                config["odoo"][key] = incoming["odoo"][key]
        if incoming["odoo"].get("password"):
            config["odoo"]["password"] = incoming["odoo"]["password"]

    if "sync" in incoming:
        config["sync"].update(incoming["sync"])

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


@app.route("/api/category-preview")
def api_category_preview():
    try:
        config = load_config()
        arena = build_arena(config)
        arena.authenticate()
        odoo = build_odoo(config)
        odoo.authenticate()

        items = arena.get_items_for_sync()
        arena_categories = sorted({
            (item.get("category") or {}).get("name", "")
            for item in items if (item.get("category") or {}).get("name")
        })

        odoo_cats = odoo.get_product_categories()
        by_name = {}
        for cat in odoo_cats:
            by_name[cat.get("name", "")] = {"id": cat["id"], "complete_name": cat.get("complete_name", "")}
            complete = cat.get("complete_name", "")
            if " / " in complete:
                leaf = complete.rsplit(" / ", 1)[-1]
                if leaf not in by_name:
                    by_name[leaf] = {"id": cat["id"], "complete_name": complete}

        manual_map = config.get("mapping", {}).get("categories", {})
        default_id = config.get("mapping", {}).get("default_category_id", 1)

        result = []
        for arena_cat in arena_categories:
            info = {"arena_category": arena_cat, "method": None, "odoo_id": None, "odoo_name": None}
            if arena_cat in manual_map:
                info["method"] = "manual"
                info["odoo_id"] = manual_map[arena_cat]
                m = next((c for c in odoo_cats if c["id"] == manual_map[arena_cat]), None)
                info["odoo_name"] = m.get("complete_name", "") if m else "?"
            elif arena_cat in by_name:
                info["method"] = "auto"
                info["odoo_id"] = by_name[arena_cat]["id"]
                info["odoo_name"] = by_name[arena_cat]["complete_name"]
            else:
                info["method"] = "default"
                info["odoo_id"] = default_id
                m = next((c for c in odoo_cats if c["id"] == default_id), None)
                info["odoo_name"] = m.get("complete_name", "") if m else "?"
            result.append(info)

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Routes: Scheduler ────────────────────────────────────────────────

@app.route("/api/scheduler", methods=["POST"])
def api_scheduler():
    action = request.json.get("action")

    if action == "start":
        if is_scheduler_active():
            return jsonify({"message": "Already running"})
        config = load_config()
        interval = config.get("sync", {}).get("interval_minutes", 15)
        start_scheduler(
            interval, _authenticated_arena, _authenticated_odoo,
            _mapping_config, log_activity,
        )
        log_activity("INFO", f"Auto-sync started: every {interval} min")
        return jsonify({"ok": True, "interval": interval})

    elif action == "stop":
        stop_scheduler()
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
