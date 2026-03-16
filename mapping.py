"""Arena → Odoo field mapping.

Translates Arena item data into Odoo product.template and mrp.bom.line values.
Category and UoM mapping is driven by config (editable in dashboard).
Auto-matching by name is attempted first, then manual overrides, then defaults.
"""

import logging

logger = logging.getLogger(__name__)

# Module-level caches (populated once per sync run via build_auto_maps)
_auto_category_map: dict[str, int] = {}   # Arena category name → Odoo categ_id
_auto_uom_map: dict[str, int] = {}        # Arena UoM name → Odoo uom.uom id
_safe_default_category_id: int | None = None  # First valid category from Odoo
_safe_default_uom_id: int | None = None        # First valid UoM from Odoo


def build_auto_maps(odoo_client) -> None:
    """Fetch Odoo categories and UoMs, build name-based auto-match maps.

    Call once before processing items (e.g. at sync/transfer start).
    Also discovers safe fallback IDs that actually exist in Odoo,
    so we never reference a category/UoM ID that was deleted.
    """
    global _auto_category_map, _auto_uom_map
    global _safe_default_category_id, _safe_default_uom_id

    # Categories: match Arena name against the last segment of Odoo's complete_name
    # e.g. Arena "Battery" matches Odoo "Hardware / Electrical / Battery"
    try:
        cats = odoo_client.get_product_categories()
        _auto_category_map = {}
        _safe_default_category_id = None
        for cat in cats:
            cid = cat["id"]
            complete = cat.get("complete_name", "")
            short_name = cat.get("name", "")
            # Pick "All" as safe fallback, or first category if "All" doesn't exist
            if short_name == "All":
                _safe_default_category_id = cid
            elif _safe_default_category_id is None:
                _safe_default_category_id = cid
            # Map both the short name and any trailing segment
            if short_name:
                _auto_category_map[short_name] = cid
            # Also map by last segment of complete_name (after last " / ")
            if " / " in complete:
                leaf = complete.rsplit(" / ", 1)[-1]
                if leaf and leaf not in _auto_category_map:
                    _auto_category_map[leaf] = cid
        logger.info("Auto-mapped %d Odoo categories by name (safe default id=%s)",
                     len(_auto_category_map), _safe_default_category_id)
    except Exception as e:
        logger.warning("Could not auto-map categories: %s", e)
        _auto_category_map = {}

    # UoMs: match Arena UoM name against Odoo uom name
    try:
        uoms = odoo_client.get_uom_list()
        _auto_uom_map = {}
        _safe_default_uom_id = None
        for u in uoms:
            uid = u["id"]
            uname = u.get("name", "")
            if uname == "Units" or _safe_default_uom_id is None:
                _safe_default_uom_id = uid
            if uname:
                _auto_uom_map[uname] = uid
        logger.info("Auto-mapped %d Odoo UoMs by name (safe default id=%s)",
                     len(_auto_uom_map), _safe_default_uom_id)
    except Exception as e:
        logger.warning("Could not auto-map UoMs: %s", e)
        _auto_uom_map = {}


def resolve_category(category_name: str, mapping_config: dict) -> int:
    """Resolve Arena category name to Odoo categ_id.

    Priority: 1) manual override in config  2) auto-match by name  3) default
    """
    cat_map = mapping_config.get("categories", {})
    cfg_default = mapping_config.get("default_category_id", 1)
    # Use the safe default discovered from Odoo if the configured one might not exist
    default_cat = _safe_default_category_id or cfg_default

    if not category_name:
        return default_cat

    # 1. Manual override
    if category_name in cat_map:
        return cat_map[category_name]

    # 2. Auto-match by name
    if category_name in _auto_category_map:
        return _auto_category_map[category_name]

    logger.warning("Unmapped Arena category '%s' -- using default (id=%d)", category_name, default_cat)
    return default_cat


def map_arena_item_to_odoo_product(item: dict, mapping_config: dict) -> dict:
    """Convert an Arena item to Odoo product.template create values.

    Args:
        item: Arena item dict from API
        mapping_config: config["mapping"] with categories, uom, defaults
    """
    number = item.get("number", "")
    name = item.get("name", number)
    lifecycle = item.get("_lifecycle") or item.get("lifecycle", "")
    if lifecycle and lifecycle != "In Production":
        name = f"{name} ({lifecycle})"
    cat = item.get("category") or ""
    category_name = cat.get("name", "") if isinstance(cat, dict) else str(cat)
    uom_name = item.get("uom", "")

    uom_map = mapping_config.get("uom", {})
    cfg_uom = mapping_config.get("default_uom_id", 1)
    default_uom = _safe_default_uom_id or cfg_uom

    # Resolve category (auto-match → manual override → default)
    categ_id = resolve_category(category_name, mapping_config)

    # Resolve UoM: manual override → auto-match → default
    uom_id = uom_map.get(uom_name) or _auto_uom_map.get(uom_name) or default_uom

    # Build description with all Arena metadata
    desc_lines = []
    if item.get("description"):
        desc_lines.append(item["description"])
    desc_lines.append(f"Arena Part Number: {number}")
    revision = item.get("revisionNumber") or item.get("revision", "")
    if revision:
        desc_lines.append(f"Arena Revision: {revision}")
    if category_name:
        desc_lines.append(f"Arena Category: {category_name}")
    if item.get("assemblyType") or item.get("assembly_type"):
        desc_lines.append(f"Type: {item.get('assemblyType') or item.get('assembly_type')}")

    # Build default_code with revision suffix: E-BAT-00003-V001
    if revision:
        default_code = f"{number}-V{str(revision).zfill(3)}"
    else:
        default_code = number

    return {
        "name": name,
        "default_code": default_code,
        "type": "consu",
        "categ_id": categ_id,
        "uom_id": uom_id,
        "sale_ok": False,
        "purchase_ok": True,
        "list_price": 0.0,
        "standard_price": 0.0,
        "description": "\n".join(desc_lines),
    }


def map_bom_line(component_variant_id: int, quantity: float, uom_name: str, mapping_config: dict) -> dict:
    """Convert an Arena BOM line to Odoo mrp.bom.line values.

    Args:
        component_variant_id: Odoo product.product ID (variant, not template)
        quantity: Quantity from Arena BOM
        uom_name: Arena UoM string
        mapping_config: config["mapping"]
    """
    uom_map = mapping_config.get("uom", {})
    cfg_uom = mapping_config.get("default_uom_id", 1)
    default_uom = _safe_default_uom_id or cfg_uom

    # Same priority chain as product mapping: manual override → auto-match → default
    uom_id = uom_map.get(uom_name) or _auto_uom_map.get(uom_name) or default_uom

    return {
        "product_id": component_variant_id,
        "product_qty": quantity,
        "product_uom_id": uom_id,
    }
