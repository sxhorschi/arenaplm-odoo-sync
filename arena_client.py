"""Arena PLM REST API client.

Handles session-based authentication (email + password + workspace ID),
automatic token refresh on 401, and rate limiting.
"""

import time
import logging
import hashlib
import json
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)


class ArenaClient:
    """Client for the Arena PLM REST API."""

    def __init__(self, api_url: str, email: str, password: str, workspace_id: str):
        self.api_url = api_url.rstrip("/")
        self.email = email
        self.password = password
        self.workspace_id = workspace_id

        self._session_id: str | None = None
        self._session_expires: datetime | None = None
        self._last_request_time: float = 0
        self._min_request_interval = 0.2  # 200ms between requests

    # ── Authentication ───────────────────────────────────────────────

    def authenticate(self) -> None:
        """Login to Arena and store session token."""
        resp = requests.post(
            f"{self.api_url}/login",
            json={
                "email": self.email,
                "password": self.password,
                "workspaceId": self.workspace_id,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        self._session_id = data.get("arenaSessionId")
        if not self._session_id:
            raise ValueError(f"Arena login failed: no session ID returned. Response: {data}")

        # Arena tokens expire after 24h inactivity; refresh after 23h
        self._session_expires = datetime.now() + timedelta(hours=23)
        logger.info("Arena: authenticated successfully")

    def _ensure_session(self) -> None:
        if self._session_id and self._session_expires and datetime.now() < self._session_expires:
            return
        self.authenticate()

    # ── HTTP ─────────────────────────────────────────────────────────

    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    def _request(self, method: str, path: str, **kwargs) -> dict:
        self._ensure_session()
        self._rate_limit()

        url = f"{self.api_url}{path}"
        headers = {"arena_session_id": self._session_id, "Content-Type": "application/json"}

        resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)

        if resp.status_code == 401:
            logger.warning("Arena: 401, re-authenticating...")
            self.authenticate()
            headers["arena_session_id"] = self._session_id
            resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)

        resp.raise_for_status()
        return resp.json()

    # ── Items ────────────────────────────────────────────────────────

    def get_items(self, lifecycle_phase: str | None = None) -> list[dict]:
        """Fetch all items, optionally filtered by lifecycle phase. Auto-paginates.

        Filtering is done client-side because Arena's query param filtering
        is not consistently supported across all instances.
        """
        all_items = []
        offset = 0
        limit = 400

        while True:
            params = {"offset": offset, "limit": limit}
            data = self._request("GET", "/items", params=params)
            results = data.get("results", [])
            all_items.extend(results)

            count = data.get("count", len(results))
            offset += limit
            if offset >= count:
                break

        # Client-side phase filter
        if lifecycle_phase:
            all_items = [
                item for item in all_items
                if (item.get("lifecyclePhase") or {}).get("name") == lifecycle_phase
            ]

        logger.info("Arena: fetched %d items (phase=%s)", len(all_items), lifecycle_phase or "all")
        return all_items

    def get_items_for_sync(self) -> list[dict]:
        """Fetch all items relevant for sync.

        Inclusion rules (hierarchy-aware):
          Level 0 — TOP_LEVEL_ASSEMBLY: included if In Production OR In Design
          Level 1 — SUB_ASSEMBLY (In Design): included only if
                    (a) at least one BOM component is In Production, AND
                    (b) it is referenced in the BOM of an included top-level assembly
          Level 2 — Components (In Production): always included

        Each item gets an '_lifecycle' field so callers know the phase.
        """
        all_raw = self.get_items()  # fetch everything (unfiltered)

        # ── Pass 1: classify items ──
        in_prod_guids: set[str] = set()
        top_levels: list[dict] = []        # TOP_LEVEL_ASSEMBLY in prod or design
        design_subs: list[dict] = []       # SUB_ASSEMBLY in design (candidates)

        result = []
        for item in all_raw:
            phase = (item.get("lifecyclePhase") or {}).get("name", "")
            asm_type = item.get("assemblyType", "")
            item["_lifecycle"] = phase

            if phase == "In Production":
                result.append(item)
                in_prod_guids.add(item.get("guid", ""))
            elif phase == "In Design" and asm_type == "TOP_LEVEL_ASSEMBLY":
                result.append(item)
                top_levels.append(item)
            elif phase == "In Design" and asm_type == "SUB_ASSEMBLY":
                design_subs.append(item)

        # Also collect In Production top-levels for BOM lookups
        for item in result:
            if item.get("assemblyType") == "TOP_LEVEL_ASSEMBLY" and item["_lifecycle"] == "In Production":
                top_levels.append(item)

        if not design_subs:
            self._log_sync_stats(result, 0)
            return result

        # ── Pass 2: find which sub-assembly GUIDs are referenced by
        #    included top-level assemblies ──
        sub_guids_in_top_boms: set[str] = set()
        for tl in top_levels:
            bom_lines = self.get_bom_for_item(tl["guid"])
            for line in bom_lines:
                comp_guid = (line.get("item") or {}).get("guid", "")
                if comp_guid:
                    sub_guids_in_top_boms.add(comp_guid)

        # ── Pass 3: include design sub-assemblies that pass both checks ──
        design_sub_included = 0
        for item in design_subs:
            guid = item.get("guid", "")

            # Check (b): referenced by an included top-level assembly?
            if guid not in sub_guids_in_top_boms:
                continue

            # Check (a): has at least one In Production component?
            bom_lines = self.get_bom_for_item(guid)
            has_prod_component = any(
                (line.get("item") or {}).get("guid") in in_prod_guids
                for line in bom_lines
            )
            if has_prod_component:
                result.append(item)
                design_sub_included += 1

        self._log_sync_stats(result, design_sub_included)
        return result

    def _log_sync_stats(self, result: list[dict], design_sub_count: int) -> None:
        in_prod = sum(1 for i in result if i["_lifecycle"] == "In Production")
        in_design = len(result) - in_prod
        logger.info("Arena: %d items for sync (%d In Production, %d In Design: %d top-level + %d sub-assemblies)",
                     len(result), in_prod, in_design,
                     in_design - design_sub_count, design_sub_count)

    def get_item(self, guid: str) -> dict:
        return self._request("GET", f"/items/{guid}")

    # ── BOMs ─────────────────────────────────────────────────────────

    def get_bom_for_item(self, item_guid: str) -> list[dict]:
        """Get BOM lines for an item. Returns [] if no BOM (404)."""
        try:
            data = self._request("GET", f"/items/{item_guid}/bom")
            return data.get("results", [])
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return []
            raise

    # ── Utility ──────────────────────────────────────────────────────

    @staticmethod
    def item_hash(item: dict) -> str:
        serialized = json.dumps(item, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode()).hexdigest()
