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

        Inclusion rules:
          1. All 'In Production' items — always included
          2. 'In Design' TOP_LEVEL_ASSEMBLY — always included
          3. 'In Design' SUB_ASSEMBLY — included only if at least one of
             its BOM components is 'In Production'

        Each item gets an '_lifecycle' field so callers know the phase.
        """
        all_raw = self.get_items()  # fetch everything (unfiltered)

        # Pass 1: collect In Production GUIDs and sort candidates
        in_prod_guids: set[str] = set()
        design_subs: list[dict] = []

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
            elif phase == "In Design" and asm_type == "SUB_ASSEMBLY":
                design_subs.append(item)

        # Pass 2: check In Design sub-assemblies — include if any
        # BOM component is In Production
        design_sub_included = 0
        for item in design_subs:
            bom_lines = self.get_bom_for_item(item["guid"])
            has_prod_component = any(
                (line.get("item") or {}).get("guid") in in_prod_guids
                for line in bom_lines
            )
            if has_prod_component:
                result.append(item)
                design_sub_included += 1

        in_prod = sum(1 for i in result if i["_lifecycle"] == "In Production")
        in_design = len(result) - in_prod
        logger.info("Arena: %d items for sync (%d In Production, %d In Design: %d top-level + %d sub-assemblies)",
                     len(result), in_prod, in_design,
                     in_design - design_sub_included, design_sub_included)
        return result

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
