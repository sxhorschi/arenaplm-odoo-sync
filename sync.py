"""Sync engine: Arena PLM → Odoo ERP.

Fetches "In Production" items from Arena, creates products + BOMs in Odoo.

State is tracked per item in sync_state.json with statuses:
  PENDING → PRODUCT_CREATED → SYNCED | ERROR

Key behaviors:
  - Idempotent: uses Arena part number as Odoo default_code to prevent dupes
  - Dependency-aware: topological sort ensures components exist before assemblies
  - Per-record error isolation: one failure doesn't block the rest
  - Change detection: SHA-256 hash skips unchanged items
  - Missing component tracking: logs which BOM components are not yet in production
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from arena_client import ArenaClient
from odoo_client import OdooClient
from mapping import map_arena_item_to_odoo_product, map_bom_line

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / "sync_state.json"


# ── State persistence ────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"items": {}, "runs": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ── Topological sort ─────────────────────────────────────────────────

def resolve_creation_order(items: list[dict], bom_map: dict[str, list[dict]]) -> list[dict]:
    """Sort items so components come before the assemblies that use them.

    Only considers dependencies within the provided item set (all "In Production").
    Uses Kahn's algorithm. Cycles are appended at the end with a warning.
    """
    guid_set = {item["guid"] for item in items}
    item_by_guid = {item["guid"]: item for item in items}

    # Build adjacency: guid → set of guids it depends on (components it needs)
    deps: dict[str, set[str]] = {item["guid"]: set() for item in items}
    # Reverse: guid → set of guids that depend on it
    dependents: dict[str, list[str]] = {item["guid"]: [] for item in items}

    for parent_guid, lines in bom_map.items():
        if parent_guid not in guid_set:
            continue
        for line in lines:
            comp_guid = (line.get("item") or {}).get("guid")
            if comp_guid and comp_guid in guid_set:
                deps[parent_guid].add(comp_guid)
                dependents[comp_guid].append(parent_guid)

    # Kahn's algorithm
    in_degree = {guid: len(dep_set) for guid, dep_set in deps.items()}
    queue = [g for g, d in in_degree.items() if d == 0]
    ordered = []

    while queue:
        guid = queue.pop(0)
        ordered.append(guid)
        for dep_guid in dependents.get(guid, []):
            in_degree[dep_guid] -= 1
            if in_degree[dep_guid] == 0:
                queue.append(dep_guid)

    # Anything left has circular dependencies
    remaining = guid_set - set(ordered)
    if remaining:
        logger.warning("Circular BOM dependencies detected for %d items — appending anyway", len(remaining))
        ordered.extend(remaining)

    return [item_by_guid[g] for g in ordered if g in item_by_guid]


# ── Main sync ────────────────────────────────────────────────────────

def run_sync(arena: ArenaClient, odoo: OdooClient, mapping_config: dict) -> dict:
    """Execute a full sync cycle.

    Returns a detailed result dict used by the dashboard.
    """
    result = {
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "items_fetched": 0,
        "products_created": 0,
        "products_updated": 0,
        "boms_created": 0,
        "skipped_unchanged": 0,
        "errors": [],
        "missing_components": [],  # Components referenced in BOMs but not "In Production"
        "items_processed": [],     # Per-item detail for the dashboard
    }

    state = load_state()
    if "items" not in state:
        state["items"] = {}
    if "runs" not in state:
        state["runs"] = []

    try:
        # ── 1. Fetch all "In Production" items from Arena ────────────
        logger.info("=" * 60)
        logger.info("SYNC STARTED at %s", result["started_at"])
        logger.info("=" * 60)

        items = arena.get_items(lifecycle_phase="In Production")
        result["items_fetched"] = len(items)
        logger.info("Fetched %d items from Arena", len(items))

        if not items:
            logger.info("Nothing to sync.")
            result["finished_at"] = datetime.now().isoformat()
            return result

        # Build a lookup of all in-production GUIDs for dependency checks
        in_production_guids = {item["guid"] for item in items}

        # ── 2. Fetch BOMs for assemblies only ─────────────────────────
        bom_map: dict[str, list[dict]] = {}
        for item in items:
            assembly_type = item.get("assemblyType", "")
            if assembly_type and assembly_type != "NOT_AN_ASSEMBLY":
                guid = item["guid"]
                bom_lines = arena.get_bom_for_item(guid)
                if bom_lines:
                    bom_map[guid] = bom_lines

                # Track missing components (in BOM but not "In Production")
                for line in bom_lines:
                    comp_guid = (line.get("item") or {}).get("guid")
                    comp_number = (line.get("item") or {}).get("number", "?")
                    comp_name = (line.get("item") or {}).get("name", "?")
                    if comp_guid and comp_guid not in in_production_guids:
                        missing = {
                            "parent_number": item.get("number", "?"),
                            "parent_name": item.get("name", "?"),
                            "component_number": comp_number,
                            "component_name": comp_name,
                            "quantity": line.get("quantity", 0),
                        }
                        result["missing_components"].append(missing)
                        logger.warning(
                            "Missing component: %s (%s) needed by %s — not 'In Production'",
                            comp_number, comp_name, item.get("number"),
                        )

        logger.info("Fetched BOMs: %d assemblies have BOMs", len(bom_map))

        if result["missing_components"]:
            logger.warning(
                "%d BOM components are NOT in production — their BOM lines will be skipped in Odoo",
                len(result["missing_components"]),
            )

        # ── 3. Sort: components first, then assemblies ───────────────
        ordered_items = resolve_creation_order(items, bom_map)

        # ── 4. Batch-load existing Odoo products ─────────────────────
        code_map = odoo.find_all_products_with_codes()
        logger.info("Loaded %d Odoo product codes for matching", len(code_map))

        # ── 5. Process each item ─────────────────────────────────────
        for item in ordered_items:
            guid = item["guid"]
            number = item.get("number", "?")
            name = item.get("name", "?")
            revision = item.get("revisionNumber", "")
            category = (item.get("category") or {}).get("name", "")
            assembly_type = item.get("assemblyType", "")
            bom_lines = bom_map.get(guid, [])

            item_detail = {
                "number": number,
                "name": name,
                "revision": revision,
                "category": category,
                "assembly_type": assembly_type,
                "bom_count": len(bom_lines),
                "action": None,       # "created", "updated", "skipped", "error"
                "odoo_template_id": None,
                "odoo_bom_id": None,
                "error": None,
            }

            try:
                item_hash = ArenaClient.item_hash(item)

                # Check if unchanged since last sync
                existing = state["items"].get(guid, {})
                if existing.get("hash") == item_hash and existing.get("status") == "SYNCED":
                    item_detail["action"] = "skipped"
                    item_detail["odoo_template_id"] = existing.get("odoo_template_id")
                    item_detail["odoo_bom_id"] = existing.get("odoo_bom_id")
                    result["skipped_unchanged"] += 1
                    result["items_processed"].append(item_detail)
                    continue

                product_vals = map_arena_item_to_odoo_product(item, mapping_config)

                # ── Create or update product in Odoo ─────────────────
                existing_tmpl_id = code_map.get(number)

                if existing_tmpl_id:
                    update_vals = {k: v for k, v in product_vals.items() if k != "default_code"}
                    odoo.update_product(existing_tmpl_id, update_vals)
                    tmpl_id = existing_tmpl_id
                    item_detail["action"] = "updated"
                    result["products_updated"] += 1
                    logger.info("Updated product %s (template=%d)", number, tmpl_id)
                else:
                    tmpl_id = odoo.create_product(product_vals)
                    code_map[number] = tmpl_id  # Add to map for BOM lookups
                    item_detail["action"] = "created"
                    result["products_created"] += 1
                    logger.info("Created product %s -> template=%d", number, tmpl_id)

                item_detail["odoo_template_id"] = tmpl_id
                variant_id = odoo.get_product_variant_id(tmpl_id)

                # ── Create or update BOM ────────────────────────────
                bom_id = None
                if bom_lines:
                    # Resolve all available components
                    desired_lines = []  # [(variant_id, line_vals)]
                    for bom_line in bom_lines:
                        comp_info = bom_line.get("item", {})
                        comp_number = comp_info.get("number", "")
                        quantity = bom_line.get("quantity", 1)

                        comp_tmpl_id = code_map.get(comp_number)
                        if not comp_tmpl_id:
                            logger.warning("BOM: component %s not in Odoo -- skipping line", comp_number)
                            continue

                        comp_variant_id = odoo.get_product_variant_id(comp_tmpl_id)
                        if not comp_variant_id:
                            logger.warning("BOM: no variant for %s -- skipping line", comp_number)
                            continue

                        desired_lines.append((comp_variant_id,
                            map_bom_line(comp_variant_id, quantity, comp_info.get("uom", ""), mapping_config)
                        ))

                    existing_bom_id = odoo.find_bom_by_product(tmpl_id)
                    if not existing_bom_id and desired_lines:
                        bom_id = odoo.create_bom(tmpl_id, [lv for _, lv in desired_lines])
                        item_detail["odoo_bom_id"] = bom_id
                        result["boms_created"] += 1
                        logger.info("Created BOM id=%d for %s (%d/%d lines)",
                                    bom_id, number, len(desired_lines), len(bom_lines))
                    elif existing_bom_id and desired_lines:
                        bom_id = existing_bom_id
                        item_detail["odoo_bom_id"] = bom_id
                        # Check for missing lines and add them
                        existing_lines = odoo.get_bom_lines(existing_bom_id)
                        existing_product_ids = {
                            (ln["product_id"][0] if isinstance(ln["product_id"], (list, tuple)) else ln["product_id"])
                            for ln in existing_lines if ln.get("product_id")
                        }
                        new_lines = [lv for vid, lv in desired_lines if vid not in existing_product_ids]
                        if new_lines:
                            odoo.update_bom_add_lines(existing_bom_id, new_lines)
                            logger.info("Updated BOM id=%d for %s: added %d lines",
                                        existing_bom_id, number, len(new_lines))

                # ── Update state ─────────────────────────────────────
                bom_components = []
                bom_comp_numbers = []
                for bl in bom_lines:
                    ci = bl.get("item") or {}
                    cn = ci.get("number", "")
                    if cn:
                        bom_comp_numbers.append(cn)
                        bom_components.append({
                            "number": cn,
                            "name": ci.get("name", ""),
                            "qty": bl.get("quantity", 0),
                        })

                state["items"][guid] = {
                    "number": number,
                    "name": name,
                    "revision": revision,
                    "category": category,
                    "assembly_type": assembly_type,
                    "bom_component_count": len(bom_lines),
                    "bom_component_numbers": bom_comp_numbers,
                    "bom_components": bom_components,
                    "hash": item_hash,
                    "status": "SYNCED",
                    "error": None,
                    "odoo_template_id": tmpl_id,
                    "odoo_variant_id": variant_id,
                    "odoo_bom_id": bom_id,
                    "synced_at": datetime.now().isoformat(),
                }

            except Exception as e:
                logger.error("FAILED: %s (%s): %s", number, name, e, exc_info=True)
                item_detail["action"] = "error"
                item_detail["error"] = str(e)
                result["errors"].append({"number": number, "name": name, "error": str(e)})

                err_bom_comps = []
                err_bom_nums = []
                for bl in bom_lines:
                    ci = bl.get("item") or {}
                    cn = ci.get("number", "")
                    if cn:
                        err_bom_nums.append(cn)
                        err_bom_comps.append({"number": cn, "name": ci.get("name", ""), "qty": bl.get("quantity", 0)})

                state["items"][guid] = {
                    "number": number,
                    "name": name,
                    "revision": revision,
                    "category": category,
                    "assembly_type": assembly_type,
                    "bom_component_count": len(bom_lines),
                    "bom_component_numbers": err_bom_nums,
                    "bom_components": err_bom_comps,
                    "hash": "",
                    "status": "ERROR",
                    "error": str(e),
                    "odoo_template_id": None,
                    "odoo_variant_id": None,
                    "odoo_bom_id": None,
                    "synced_at": datetime.now().isoformat(),
                }

            result["items_processed"].append(item_detail)
            save_state(state)  # Save after each item for crash resilience

    except Exception as e:
        logger.error("Sync failed globally: %s", e, exc_info=True)
        result["errors"].append({"number": "GLOBAL", "name": "Sync engine", "error": str(e)})

    result["finished_at"] = datetime.now().isoformat()

    # Save run history (keep last 50 runs)
    run_summary = {
        "started_at": result["started_at"],
        "finished_at": result["finished_at"],
        "fetched": result["items_fetched"],
        "created": result["products_created"],
        "updated": result["products_updated"],
        "boms": result["boms_created"],
        "skipped": result["skipped_unchanged"],
        "errors": len(result["errors"]),
        "missing_components": len(result["missing_components"]),
    }
    state["runs"].insert(0, run_summary)
    state["runs"] = state["runs"][:50]
    save_state(state)

    logger.info("=" * 60)
    logger.info(
        "SYNC COMPLETE: %d fetched, %d created, %d updated, %d BOMs, %d skipped, %d errors, %d missing components",
        result["items_fetched"], result["products_created"], result["products_updated"],
        result["boms_created"], result["skipped_unchanged"],
        len(result["errors"]), len(result["missing_components"]),
    )
    logger.info("=" * 60)

    return result
