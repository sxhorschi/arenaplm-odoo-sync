"""Unified transfer engine: Arena PLM -> Odoo ERP.

Single source of truth for all product creation, BOM reconciliation,
and state management. Both manual transfer and auto-sync delegate here.

Key invariants:
  - Every transferred product gets its BOM relationships (as parent AND component)
  - Parent assemblies are auto-created when a component references them
  - Lifecycle transitions (In Design -> In Production) update the Odoo product name
  - State shape is consistent everywhere via make_state_entry()
  - Thread-safe: a single lock protects state writes and progress mutations
"""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from arena_client import ArenaClient
from odoo_client import OdooClient
from mapping import map_arena_item_to_odoo_product, map_bom_line, build_auto_maps

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent))
STATE_FILE = _DATA_DIR / "sync_state.json"
_state_lock = threading.Lock()
_engine_lock = threading.Lock()  # prevents concurrent transfer + sync


# ── State persistence ────────────────────────────────────────────────

def load_state() -> dict:
    with _state_lock:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
        return {"items": {}, "runs": []}


def save_state(state: dict) -> None:
    with _state_lock:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)


def is_engine_busy() -> bool:
    """Check if a transfer or sync is currently running."""
    return _engine_lock.locked()


def make_state_entry(
    *,
    number: str,
    name: str,
    revision: str = "",
    category: str = "",
    assembly_type: str = "",
    lifecycle: str = "In Production",
    bom_components: list[dict] | None = None,
    hash: str = "",
    status: str = "SYNCED",
    error: str | None = None,
    odoo_template_id: int | None = None,
    odoo_variant_id: int | None = None,
    odoo_bom_id: int | None = None,
) -> dict:
    """Create a consistently-shaped state entry. Every path that writes
    to state['items'] MUST use this function."""
    comps = bom_components or []
    return {
        "number": number,
        "name": name,
        "revision": revision,
        "category": category,
        "assembly_type": assembly_type,
        "lifecycle": lifecycle,
        "bom_component_count": len(comps),
        "bom_component_numbers": [c["number"] for c in comps],
        "bom_components": comps,
        "hash": hash,
        "status": status,
        "error": error,
        "odoo_template_id": odoo_template_id,
        "odoo_variant_id": odoo_variant_id,
        "odoo_bom_id": odoo_bom_id,
        "synced_at": datetime.now().isoformat(),
    }


# ── BOM helpers ──────────────────────────────────────────────────────

def _extract_bom_components(bom_lines: list[dict]) -> list[dict]:
    """Extract component info from Arena BOM lines into state format."""
    comps = []
    for bl in bom_lines:
        ci = bl.get("item") or {}
        cn = ci.get("number", "")
        if cn:
            comps.append({
                "number": cn,
                "name": ci.get("name", ""),
                "qty": bl.get("quantity", 0),
            })
    return comps


def _build_desired_bom_lines(
    bom_lines: list[dict],
    code_map: dict[str, int],
    odoo: OdooClient,
    mapping_config: dict,
) -> tuple[list[tuple[int, dict]], list[str]]:
    """Resolve Arena BOM lines to Odoo BOM line values.

    Returns (desired_lines, skipped_numbers) where desired_lines is
    [(variant_id, line_vals)] for components found in Odoo.
    """
    desired = []
    skipped = []
    for line in bom_lines:
        comp_info = line.get("item") or {}
        comp_number = comp_info.get("number", "")
        comp_tmpl = code_map.get(comp_number)
        if not comp_tmpl:
            if comp_number:
                skipped.append(comp_number)
            continue
        comp_variant = odoo.get_product_variant_id(comp_tmpl)
        if not comp_variant:
            skipped.append(f"{comp_number} (no variant)")
            continue
        line_vals = map_bom_line(
            comp_variant,
            line.get("quantity", 1),
            comp_info.get("uom", ""),
            mapping_config,
        )
        desired.append((comp_variant, line_vals))
    return desired, skipped


def reconcile_bom(
    assembly_tmpl_id: int,
    bom_lines: list[dict],
    code_map: dict[str, int],
    odoo: OdooClient,
    mapping_config: dict,
) -> tuple[int | None, str, list[str]]:
    """Single unified BOM reconciliation function.

    Returns (bom_id, action, skipped_numbers) where action is one of:
      'created', 'updated', 'unchanged', 'no_lines'
    """
    desired, skipped = _build_desired_bom_lines(bom_lines, code_map, odoo, mapping_config)
    if not desired:
        return None, "no_lines", skipped

    existing_bom_id = odoo.find_bom_by_product(assembly_tmpl_id)

    if not existing_bom_id:
        bom_id = odoo.create_bom(assembly_tmpl_id, [lv for _, lv in desired])
        return bom_id, "created", skipped

    # BOM exists — add missing lines
    existing_lines = odoo.get_bom_lines(existing_bom_id)
    existing_product_ids = {
        (ln["product_id"][0] if isinstance(ln["product_id"], (list, tuple)) else ln["product_id"])
        for ln in existing_lines if ln.get("product_id")
    }
    new_lines = [lv for vid, lv in desired if vid not in existing_product_ids]
    if new_lines:
        odoo.update_bom_add_lines(existing_bom_id, new_lines)
        return existing_bom_id, "updated", skipped

    return existing_bom_id, "unchanged", skipped


# ── Topological sort ─────────────────────────────────────────────────

def resolve_creation_order(items: list[dict], bom_map: dict[str, list[dict]]) -> list[dict]:
    """Sort items so components come before assemblies (Kahn's algorithm)."""
    guid_set = {item["guid"] for item in items}
    item_by_guid = {item["guid"]: item for item in items}

    deps: dict[str, set[str]] = {item["guid"]: set() for item in items}
    dependents: dict[str, list[str]] = {item["guid"]: [] for item in items}

    for parent_guid, lines in bom_map.items():
        if parent_guid not in guid_set:
            continue
        for line in lines:
            comp_guid = (line.get("item") or {}).get("guid")
            if comp_guid and comp_guid in guid_set:
                deps[parent_guid].add(comp_guid)
                dependents[comp_guid].append(parent_guid)

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

    remaining = guid_set - set(ordered)
    if remaining:
        logger.warning("Circular BOM dependencies for %d items — appending anyway", len(remaining))
        ordered.extend(remaining)

    return [item_by_guid[g] for g in ordered if g in item_by_guid]


# ── Product creation / update ────────────────────────────────────────

def ensure_product_in_odoo(
    item: dict,
    code_map: dict[str, int],
    odoo: OdooClient,
    mapping_config: dict,
) -> tuple[int, int | None, str]:
    """Create or update a product in Odoo. Mutates code_map on create.

    Returns (template_id, variant_id, action) where action is 'created',
    'updated', or 'exists'.
    """
    number = item.get("number", "")
    existing_tmpl = code_map.get(number)

    if existing_tmpl:
        # Check for lifecycle transition — update name if needed
        product_vals = map_arena_item_to_odoo_product(item, mapping_config)
        update_vals = {k: v for k, v in product_vals.items() if k not in ("list_price", "standard_price")}
        odoo.update_product(existing_tmpl, update_vals)
        variant_id = odoo.get_product_variant_id(existing_tmpl)
        return existing_tmpl, variant_id, "updated"

    product_vals = map_arena_item_to_odoo_product(item, mapping_config)
    tmpl_id = odoo.create_product(product_vals)
    code_map[number] = tmpl_id
    variant_id = odoo.get_product_variant_id(tmpl_id)
    return tmpl_id, variant_id, "created"


# ── Lifecycle transitions ────────────────────────────────────────────

def detect_lifecycle_transitions(
    items: list[dict], state: dict
) -> list[dict]:
    """Find items whose lifecycle changed since last sync."""
    transitions = []
    for item in items:
        guid = item.get("guid", "")
        current = item.get("_lifecycle", "")
        stored = state.get("items", {}).get(guid, {}).get("lifecycle", "")
        if stored and current and stored != current:
            transitions.append({
                "guid": guid,
                "number": item.get("number", ""),
                "name": item.get("name", ""),
                "old_lifecycle": stored,
                "new_lifecycle": current,
                "odoo_template_id": state["items"][guid].get("odoo_template_id"),
            })
    return transitions


def apply_lifecycle_transitions(
    transitions: list[dict], odoo: OdooClient
) -> int:
    """Update Odoo product names for lifecycle changes.
    Returns count of products updated."""
    count = 0
    for t in transitions:
        tmpl_id = t.get("odoo_template_id")
        if not tmpl_id:
            continue
        try:
            name = t["name"]
            old_phase = t["old_lifecycle"]
            new_phase = t["new_lifecycle"]

            # Remove old suffix if present
            if old_phase and old_phase != "In Production":
                suffix = f" ({old_phase})"
                if name.endswith(suffix):
                    name = name[:-len(suffix)]

            # Add new suffix if not In Production
            if new_phase and new_phase != "In Production":
                name = f"{name} ({new_phase})"

            odoo.update_product(tmpl_id, {"name": name})
            count += 1
            logger.info("Lifecycle transition: %s '%s' -> '%s'", t["number"], old_phase, new_phase)
        except Exception as e:
            logger.error("Failed lifecycle update for %s: %s", t["number"], e)
    return count


# ── Transfer progress ────────────────────────────────────────────────

class TransferProgress:
    """Thread-safe transfer progress tracker."""

    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.total = 0
        self.done = 0
        self.current = ""
        self.phase = ""
        self.results: list[dict] = []

    def start(self, total: int):
        with self._lock:
            self.running = True
            self.total = total
            self.done = 0
            self.current = ""
            self.phase = "products"
            self.results = []

    def finish(self):
        with self._lock:
            self.running = False
            self.current = ""

    def update(self, *, current: str | None = None, phase: str | None = None,
               done_increment: int = 0, result: dict | None = None):
        with self._lock:
            if current is not None:
                self.current = current
            if phase is not None:
                self.phase = phase
            self.done += done_increment
            if result:
                self.results.append(result)

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "total": self.total,
                "done": self.done,
                "current": self.current,
                "phase": self.phase,
                "results": list(self.results),
            }


progress = TransferProgress()


# ── Main transfer function ───────────────────────────────────────────

def transfer_items(
    items_to_transfer: list[dict],
    arena: ArenaClient,
    odoo: OdooClient,
    mapping_config: dict,
    on_activity: callable = None,
) -> dict:
    """Transfer selected items to Odoo with full BOM reconciliation.

    This is the ONLY function that creates products and BOMs in Odoo.
    Both manual transfer and auto-sync call this.

    Args:
        items_to_transfer: Arena items to transfer (from fetch or sync)
        arena: Authenticated Arena client
        odoo: Authenticated Odoo client
        mapping_config: config["mapping"]
        on_activity: Optional callback(level, message) for activity logging

    Returns result dict with counts and per-item details.
    """

    if not _engine_lock.acquire(blocking=False):
        return {
            "started_at": datetime.now().isoformat(),
            "finished_at": datetime.now().isoformat(),
            "errors": [{"number": "GLOBAL", "name": "Engine", "error": "Another transfer/sync is already running"}],
            "products_created": 0, "products_updated": 0,
            "boms_created": 0, "boms_updated": 0, "bom_errors": 0,
            "skipped_unchanged": 0, "missing_components": [],
            "assemblies_auto_created": 0, "lifecycle_transitions": 0,
        }

    def log(level: str, msg: str):
        logger.info(msg) if level != "ERROR" else logger.error(msg)
        if on_activity:
            on_activity(level, msg)

    result = {
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "products_created": 0,
        "products_updated": 0,
        "boms_created": 0,
        "boms_updated": 0,
        "bom_errors": 0,
        "skipped_unchanged": 0,
        "errors": [],
        "missing_components": [],
        "assemblies_auto_created": 0,
        "lifecycle_transitions": 0,
    }

    state = load_state()
    if "items" not in state:
        state["items"] = {}

    try:
        build_auto_maps(odoo)

        # ── Phase 1: Create/update products ──────────────────────────
        progress.start(len(items_to_transfer))
        code_map = odoo.find_all_products_with_codes()
        log("INFO", f"Loaded {len(code_map)} existing Odoo product codes")

        # Sort: components first, then sub-assemblies, then top-level
        type_order = {"NOT_AN_ASSEMBLY": 0, "": 0, "SUB_ASSEMBLY": 1, "TOP_LEVEL_ASSEMBLY": 2}
        sorted_items = sorted(
            items_to_transfer,
            key=lambda i: type_order.get(i.get("assembly_type", ""), 0),
        )

        for item in sorted_items:
            number = item.get("number", "?")
            name = item.get("name", "?")
            guid = item.get("guid", "")
            progress.update(current=f"{number} — {name}")

            entry = {"number": number, "name": name, "status": "ok",
                     "error": None, "odoo_template_id": None, "odoo_bom_id": None}

            try:
                tmpl_id, variant_id, action = ensure_product_in_odoo(
                    item, code_map, odoo, mapping_config
                )
                entry["odoo_template_id"] = tmpl_id
                entry["status"] = "created" if action == "created" else "exists"

                if action == "created":
                    result["products_created"] += 1
                else:
                    result["products_updated"] += 1

                # Build BOM component list from item data
                raw_comps = item.get("bom_components", [])
                bom_comps = [
                    {"number": c.get("number", ""), "name": c.get("name", ""), "qty": c.get("qty", 0)}
                    for c in raw_comps if c.get("number")
                ]

                state["items"][guid] = make_state_entry(
                    number=number,
                    name=name,
                    revision=item.get("revision", ""),
                    category=item.get("category", ""),
                    assembly_type=item.get("assembly_type", ""),
                    lifecycle=item.get("lifecycle", "In Production"),
                    bom_components=bom_comps,
                    status="SYNCED",
                    odoo_template_id=tmpl_id,
                    odoo_variant_id=variant_id,
                )
                save_state(state)

            except Exception as e:
                entry["status"] = "error"
                entry["error"] = str(e)
                result["errors"].append({"number": number, "name": name, "error": str(e)})
                logger.error("Transfer failed for %s: %s", number, e, exc_info=True)

            progress.update(done_increment=1, result=entry)

        # ── Phase 2: BOM reconciliation for ALL assemblies ───────────
        progress.update(phase="boms", current="Fetching Arena assemblies for BOM reconciliation...")
        log("INFO", "Starting BOM reconciliation...")

        all_items = arena.get_items_for_sync()
        assemblies = [
            i for i in all_items
            if i.get("assemblyType") not in (None, "", "NOT_AN_ASSEMBLY")
        ]

        # Refresh code_map to include newly created products
        code_map = odoo.find_all_products_with_codes()

        # Track which numbers we transferred for auto-create decisions
        transferred_numbers = {item.get("number", "") for item in sorted_items}

        boms_created = 0
        boms_updated = 0
        bom_errors = 0
        first_bom_error = None
        assemblies_auto_created = 0

        for asm in assemblies:
            asm_number = asm.get("number", "")
            asm_guid = asm.get("guid", "")

            bom_lines = arena.get_bom_for_item(asm_guid)
            if not bom_lines:
                continue

            # Check if this assembly references components in Odoo
            has_odoo_component = False
            references_transferred = False
            for line in bom_lines:
                comp_number = (line.get("item") or {}).get("number", "")
                if code_map.get(comp_number):
                    has_odoo_component = True
                if comp_number in transferred_numbers:
                    references_transferred = True

            if not has_odoo_component:
                continue

            # Ensure assembly exists in Odoo (auto-create if needed)
            asm_tmpl = code_map.get(asm_number)
            if not asm_tmpl and references_transferred:
                progress.update(current=f"Auto-creating assembly {asm_number}...")
                try:
                    asm_tmpl, asm_variant, _ = ensure_product_in_odoo(
                        {
                            "number": asm_number,
                            "name": asm.get("name", ""),
                            "revisionNumber": asm.get("revisionNumber", ""),
                            "category": asm.get("category"),
                            "assemblyType": asm.get("assemblyType", ""),
                            "description": asm.get("description", ""),
                            "_lifecycle": asm.get("_lifecycle", "In Production"),
                        },
                        code_map, odoo, mapping_config,
                    )
                    assemblies_auto_created += 1
                    log("INFO", f"Auto-created assembly {asm_number} in Odoo")
                except Exception as e:
                    logger.error("Auto-create assembly %s failed: %s", asm_number, e, exc_info=True)
                    continue
            elif not asm_tmpl:
                continue

            # Always store BOM relationship in state (even if Odoo write fails)
            bom_comps = _extract_bom_components(bom_lines)
            existing_state = state["items"].get(asm_guid, {})
            state["items"][asm_guid] = make_state_entry(
                number=asm_number,
                name=asm.get("name", ""),
                revision=existing_state.get("revision", asm.get("revisionNumber", "")),
                category=existing_state.get("category", (asm.get("category") or {}).get("name", "")),
                assembly_type=asm.get("assemblyType", ""),
                lifecycle=asm.get("_lifecycle", existing_state.get("lifecycle", "In Production")),
                bom_components=bom_comps,
                hash=existing_state.get("hash", ""),
                status=existing_state.get("status", "SYNCED"),
                error=existing_state.get("error"),
                odoo_template_id=asm_tmpl or existing_state.get("odoo_template_id"),
                odoo_variant_id=existing_state.get("odoo_variant_id"),
                odoo_bom_id=existing_state.get("odoo_bom_id"),
            )
            save_state(state)

            # Reconcile BOM in Odoo
            progress.update(current=f"BOM: {asm_number}")
            try:
                bom_id, action, skipped = reconcile_bom(
                    asm_tmpl, bom_lines, code_map, odoo, mapping_config
                )
                if action == "created":
                    boms_created += 1
                elif action == "updated":
                    boms_updated += 1

                if bom_id and asm_guid in state["items"]:
                    state["items"][asm_guid]["odoo_bom_id"] = bom_id
                    save_state(state)

                # Update progress results for this assembly
                for r in progress.results:
                    if r["number"] == asm_number:
                        r["odoo_bom_id"] = bom_id
                        break

                if skipped:
                    logger.warning("BOM %s: %d components not in Odoo: %s",
                                   asm_number, len(skipped), ", ".join(skipped))

            except Exception as e:
                logger.error("BOM failed for %s: %s", asm_number, e, exc_info=True)
                bom_errors += 1
                if not first_bom_error:
                    first_bom_error = str(e)

        result["boms_created"] = boms_created
        result["boms_updated"] = boms_updated
        result["bom_errors"] = bom_errors
        result["assemblies_auto_created"] = assemblies_auto_created

        bom_msg = f"BOM reconciliation: {boms_created} created, {boms_updated} updated"
        if assemblies_auto_created:
            bom_msg += f", {assemblies_auto_created} assemblies auto-created"
        if bom_errors:
            bom_msg += f", {bom_errors} FAILED"
        log("OK" if bom_errors == 0 else "ERROR", bom_msg)

        if first_bom_error:
            log("ERROR", f"BOM error (check Odoo permissions): {first_bom_error}")

        # ── Phase 3: Lifecycle transitions ───────────────────────────
        transitions = detect_lifecycle_transitions(all_items, state)
        if transitions:
            progress.update(phase="lifecycle", current="Updating lifecycle transitions...")
            count = apply_lifecycle_transitions(transitions, odoo)
            result["lifecycle_transitions"] = count
            if count:
                log("INFO", f"Updated {count} product names for lifecycle transitions")
                # Update state with new lifecycle values
                for t in transitions:
                    guid = t["guid"]
                    if guid in state["items"]:
                        state["items"][guid]["lifecycle"] = t["new_lifecycle"]
                save_state(state)

    except Exception as e:
        logger.error("Transfer failed globally: %s", e, exc_info=True)
        result["errors"].append({"number": "GLOBAL", "name": "Transfer engine", "error": str(e)})
        log("ERROR", f"Transfer failed: {e}")

    result["finished_at"] = datetime.now().isoformat()
    progress.finish()
    _engine_lock.release()
    return result


# ── Full sync (auto-sync entry point) ────────────────────────────────

def run_full_sync(
    arena: ArenaClient,
    odoo: OdooClient,
    mapping_config: dict,
    on_activity: callable = None,
) -> dict:
    """Fetch all items from Arena and sync to Odoo.

    This is the auto-sync entry point. It fetches items, applies
    hash-based change detection, fetches BOMs, and delegates to
    transfer_items for the actual work.
    """

    def log(level: str, msg: str):
        logger.info(msg) if level != "ERROR" else logger.error(msg)
        if on_activity:
            on_activity(level, msg)

    result = {
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "items_fetched": 0,
        "products_created": 0,
        "products_updated": 0,
        "boms_created": 0,
        "boms_updated": 0,
        "skipped_unchanged": 0,
        "errors": [],
        "missing_components": [],
    }

    try:
        logger.info("=" * 60)
        logger.info("FULL SYNC STARTED at %s", result["started_at"])
        logger.info("=" * 60)

        items = arena.get_items_for_sync() or []
        result["items_fetched"] = len(items)

        if not items:
            log("INFO", "Nothing to sync.")
            result["finished_at"] = datetime.now().isoformat()
            return result

        in_production_guids = {
            item["guid"] for item in items if item.get("_lifecycle") == "In Production"
        }

        # Fetch BOMs and track missing components
        bom_map: dict[str, list[dict]] = {}
        for item in items:
            assembly_type = item.get("assemblyType", "")
            if assembly_type and assembly_type != "NOT_AN_ASSEMBLY":
                guid = item["guid"]
                bom_lines = arena.get_bom_for_item(guid)
                if bom_lines:
                    bom_map[guid] = bom_lines
                for line in bom_lines:
                    comp_guid = (line.get("item") or {}).get("guid")
                    if comp_guid and comp_guid not in in_production_guids:
                        result["missing_components"].append({
                            "parent_number": item.get("number", "?"),
                            "parent_name": item.get("name", "?"),
                            "component_number": (line.get("item") or {}).get("number", "?"),
                            "component_name": (line.get("item") or {}).get("name", "?"),
                            "quantity": line.get("quantity", 0),
                        })

        if result["missing_components"]:
            log("WARN", f"{len(result['missing_components'])} BOM components not in production")

        # Topological sort
        ordered_items = resolve_creation_order(items, bom_map)

        # Hash-based skip: filter out unchanged items
        state = load_state()
        items_to_process = []
        for item in ordered_items:
            guid = item["guid"]
            item_hash = ArenaClient.item_hash(item)
            existing = state.get("items", {}).get(guid, {})
            if existing.get("hash") == item_hash and existing.get("status") == "SYNCED":
                result["skipped_unchanged"] += 1
                continue
            # Attach hash and BOM data for transfer
            item["_hash"] = item_hash
            item["_bom_lines"] = bom_map.get(guid, [])
            items_to_process.append(item)

        if not items_to_process:
            log("INFO", "All items unchanged — nothing to sync")
            result["finished_at"] = datetime.now().isoformat()
            return result

        log("INFO", f"Processing {len(items_to_process)} items ({result['skipped_unchanged']} unchanged)")

        # Prepare items in the format transfer_items expects
        transfer_items_list = []
        for item in items_to_process:
            bom_lines = item.get("_bom_lines", [])
            transfer_items_list.append({
                "guid": item.get("guid", ""),
                "number": item.get("number", ""),
                "name": item.get("name", ""),
                "revision": item.get("revisionNumber", ""),
                "category": (item.get("category") or {}).get("name", ""),
                "assembly_type": item.get("assemblyType", ""),
                "lifecycle": item.get("_lifecycle", "In Production"),
                "bom_count": len(bom_lines),
                "bom_components": [
                    {
                        "number": (l.get("item") or {}).get("number", ""),
                        "name": (l.get("item") or {}).get("name", ""),
                        "qty": l.get("quantity", 0),
                    }
                    for l in bom_lines
                ],
            })

        # Delegate to unified transfer
        transfer_result = transfer_items(
            transfer_items_list, arena, odoo, mapping_config, on_activity
        )

        # Merge results
        result["products_created"] = transfer_result["products_created"]
        result["products_updated"] = transfer_result["products_updated"]
        result["boms_created"] = transfer_result["boms_created"]
        result["boms_updated"] = transfer_result.get("boms_updated", 0)
        result["errors"] = transfer_result["errors"]

        # Update hashes in state for successfully synced items
        state = load_state()
        for item in items_to_process:
            guid = item.get("guid", "")
            if guid in state.get("items", {}) and state["items"][guid].get("status") == "SYNCED":
                state["items"][guid]["hash"] = item.get("_hash", "")
        save_state(state)

        # Save run history
        run_summary = {
            "started_at": result["started_at"],
            "finished_at": datetime.now().isoformat(),
            "fetched": result["items_fetched"],
            "created": result["products_created"],
            "updated": result["products_updated"],
            "boms": result["boms_created"],
            "skipped": result["skipped_unchanged"],
            "errors": len(result["errors"]),
            "missing_components": len(result["missing_components"]),
        }
        state["runs"] = state.get("runs", [])
        state["runs"].insert(0, run_summary)
        state["runs"] = state["runs"][:50]
        save_state(state)

    except Exception as e:
        logger.error("Full sync failed: %s", e, exc_info=True)
        result["errors"].append({"number": "GLOBAL", "name": "Sync engine", "error": str(e)})

    result["finished_at"] = datetime.now().isoformat()
    logger.info("=" * 60)
    logger.info("SYNC COMPLETE: %d fetched, %d created, %d updated, %d BOMs, %d skipped, %d errors",
                result["items_fetched"], result["products_created"], result["products_updated"],
                result["boms_created"], result["skipped_unchanged"], len(result["errors"]))
    logger.info("=" * 60)

    return result
