"""Odoo ERP XML-RPC client.

Handles authentication and provides typed helpers for product and BOM operations.
"""

import logging
import xmlrpc.client

logger = logging.getLogger(__name__)


class OdooClient:
    """Client for Odoo's XML-RPC External API."""

    def __init__(self, url: str, db: str, user: str, password: str):
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.password = password
        self._uid: int | None = None
        self._models: xmlrpc.client.ServerProxy | None = None

    def authenticate(self) -> int:
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
        self._uid = common.authenticate(self.db, self.user, self.password, {})
        if not self._uid:
            raise ValueError("Odoo authentication failed: invalid credentials")
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)
        logger.info("Odoo: authenticated uid=%d", self._uid)
        return self._uid

    def _ensure_auth(self) -> None:
        if not self._uid or not self._models:
            self.authenticate()

    def execute(self, model: str, method: str, args: list, kwargs: dict | None = None):
        self._ensure_auth()
        return self._models.execute_kw(self.db, self._uid, self.password, model, method, args, kwargs or {})

    # ── Products ─────────────────────────────────────────────────────

    def find_product_by_code(self, default_code: str) -> int | None:
        """Find product.template by default_code.

        Searches both product.template and product.product (variant),
        since Odoo 19 stores default_code on the variant level.
        Tries exact match first, then prefix match (code + '-V%') to handle
        version suffixes like -V001.
        """
        for search_domain in [
            [["default_code", "=", default_code]],
            [["default_code", "=like", default_code + "-V%"]],
        ]:
            for model, id_field in [("product.template", None), ("product.product", "product_tmpl_id")]:
                ids = self.execute(model, "search", [search_domain], {"limit": 1})
                if ids:
                    if id_field is None:
                        return ids[0]
                    data = self.execute(model, "read", [ids[:1], [id_field]])
                    if data and data[0].get(id_field):
                        tmpl = data[0][id_field]
                        return tmpl[0] if isinstance(tmpl, (list, tuple)) else tmpl
        return None

    def create_product(self, vals: dict) -> int:
        pid = self.execute("product.template", "create", [vals])
        logger.info("Odoo: created product.template id=%d (%s)", pid, vals.get("name", ""))
        return pid

    def update_product(self, template_id: int, vals: dict) -> None:
        self.execute("product.template", "write", [[template_id], vals])
        logger.info("Odoo: updated product.template id=%d", template_id)

    def get_product_variant_id(self, template_id: int) -> int | None:
        ids = self.execute("product.product", "search", [[["product_tmpl_id", "=", template_id]]])
        return ids[0] if ids else None

    def find_all_products_with_codes(self) -> dict[str, int]:
        """Fetch all products that have a default_code set.

        Returns dict of {default_code: template_id}.
        Also adds entries with version suffixes stripped (e.g. E-BAT-00003-V001 → E-BAT-00003)
        so Arena part numbers can match Odoo codes that have -V### suffixes.

        Searches both product.template and product.product for Odoo 19 compat.
        """
        import re
        result = {}

        def add_code(code: str, tmpl_id: int):
            if not code:
                return
            result[code] = tmpl_id
            # Also register without version suffix: X-YYY-NNNNN-V001 → X-YYY-NNNNN
            stripped = re.sub(r'-V\d+$', '', code)
            if stripped != code and stripped not in result:
                result[stripped] = tmpl_id

        # Search templates with a default_code
        tmpl_ids = self.execute("product.template", "search", [[["default_code", "!=", False]]])
        if tmpl_ids:
            records = self.execute("product.template", "read", [tmpl_ids, ["id", "default_code"]])
            for r in records:
                add_code(r.get("default_code", ""), r["id"])

        # Search variants (Odoo 19 stores default_code on product.product)
        var_ids = self.execute("product.product", "search", [[["default_code", "!=", False]]])
        if var_ids:
            records = self.execute("product.product", "read", [var_ids, ["default_code", "product_tmpl_id"]])
            for r in records:
                code = r.get("default_code", "")
                if code and code not in result:
                    tmpl = r.get("product_tmpl_id")
                    tmpl_id = tmpl[0] if isinstance(tmpl, (list, tuple)) else tmpl
                    add_code(code, tmpl_id)

        return result

    # ── BOMs ─────────────────────────────────────────────────────────

    def find_bom_by_product(self, template_id: int) -> int | None:
        ids = self.execute("mrp.bom", "search", [[["product_tmpl_id", "=", template_id]]])
        return ids[0] if ids else None

    def create_bom(self, template_id: int, bom_lines: list[dict]) -> int:
        vals = {
            "product_tmpl_id": template_id,
            "type": "normal",
            "bom_line_ids": [(0, 0, line) for line in bom_lines],
        }
        bom_id = self.execute("mrp.bom", "create", [vals])
        logger.info("Odoo: created mrp.bom id=%d for template=%d (%d lines)", bom_id, template_id, len(bom_lines))
        return bom_id

    # ── Reference data ───────────────────────────────────────────────

    def get_product_categories(self) -> list[dict]:
        ids = self.execute("product.category", "search", [[]])
        return self.execute("product.category", "read", [ids, ["id", "name", "complete_name"]])

    def get_uom_list(self) -> list[dict]:
        ids = self.execute("uom.uom", "search", [[]])
        return self.execute("uom.uom", "read", [ids, ["id", "name"]])

    def get_server_version(self) -> str:
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
        info = common.version()
        return info.get("server_version", "unknown")
