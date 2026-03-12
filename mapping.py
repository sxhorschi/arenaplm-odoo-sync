"""Arena → Odoo field mapping.

Translates Arena item data into Odoo product.template and mrp.bom.line values.
Category and UoM mapping is driven by config (editable in dashboard).
"""

import logging

logger = logging.getLogger(__name__)


def map_arena_item_to_odoo_product(item: dict, mapping_config: dict) -> dict:
    """Convert an Arena item to Odoo product.template create values.

    Args:
        item: Arena item dict from API
        mapping_config: config["mapping"] with categories, uom, defaults
    """
    number = item.get("number", "")
    name = item.get("name", number)
    cat = item.get("category") or ""
    category_name = cat.get("name", "") if isinstance(cat, dict) else str(cat)
    uom_name = item.get("uom", "")

    cat_map = mapping_config.get("categories", {})
    uom_map = mapping_config.get("uom", {})
    default_cat = mapping_config.get("default_category_id", 1)
    default_uom = mapping_config.get("default_uom_id", 1)

    # Resolve category
    categ_id = cat_map.get(category_name, default_cat)
    if category_name and category_name not in cat_map:
        logger.warning("Unmapped Arena category '%s' -- using default (id=%d)", category_name, default_cat)

    # Resolve UoM
    uom_id = uom_map.get(uom_name, default_uom)

    # Build description with all Arena metadata
    desc_lines = []
    if item.get("description"):
        desc_lines.append(item["description"])
    desc_lines.append(f"Arena Part Number: {number}")
    if item.get("revisionNumber"):
        desc_lines.append(f"Arena Revision: {item['revisionNumber']}")
    if category_name:
        desc_lines.append(f"Arena Category: {category_name}")
    if item.get("assemblyType"):
        desc_lines.append(f"Type: {item['assemblyType']}")

    return {
        "name": name,
        "default_code": number,                   # Use Arena part number directly as internal reference
        "type": "consu",                         # Goods (Odoo 19: consu=Goods, service=Service)
        "categ_id": categ_id,
        "uom_id": uom_id,
        "sale_ok": False,
        "purchase_ok": True,
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
    default_uom = mapping_config.get("default_uom_id", 1)

    return {
        "product_id": component_variant_id,
        "product_qty": quantity,
        "product_uom_id": uom_map.get(uom_name, default_uom),
    }
