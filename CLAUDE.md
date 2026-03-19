# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Arena PLM to Odoo ERP sync tool. Transfers "In Production" items from Arena as products with BOMs into Odoo. Flask web dashboard for configuration, preview, and transfer. Read-only Arena integration (only GET + login).

## Commands

```bash
pip install -r requirements.txt    # Install deps (requests, schedule, flask)
python main.py                     # Start dashboard at http://localhost:5000
python main.py --port 8080         # Custom port
```

No test suite, no linter configured. Manual testing via the dashboard UI or curl against `/api/test/arena` and `/api/test/odoo`.

## Architecture

```
main.py           Entry point, parses --port, runs Flask in debug mode
app.py            Flask backend (~850 lines): all /api/* routes, activity logging
transfer.py       Unified sync engine (~813 lines): THE critical file
sync.py           Auto-sync scheduler wrapper, delegates to transfer.py
arena_client.py   Arena REST API client (session auth, 200ms rate limit)
odoo_client.py    Odoo XML-RPC client (product/BOM CRUD)
mapping.py        Arena→Odoo field translation (3-tier: manual → auto-match → default)
config.py         JSON config management (config.json, gitignored)
templates/
  dashboard.html  Single-file UI (~1580 lines, all CSS+JS inline)
```

### Data Flow

Both manual transfer and auto-sync converge on `transfer.transfer_items()`:
1. **Phase 1**: Create/update products (topological sort via Kahn's algorithm — components before assemblies)
2. **Phase 2**: BOM reconciliation (create/update BOMs, skip missing components)
3. **Phase 3**: Lifecycle transitions (update product names when phase changes)

Auto-sync adds hash-based change detection (`ArenaClient.item_hash()`) to skip unchanged items.

### Key Design Decisions

- **Unified engine**: `transfer.transfer_items()` is the single source of truth for all product/BOM operations. Both manual and auto-sync use it.
- **Thread safety**: `_state_lock` protects `sync_state.json` I/O; `_engine_lock` prevents concurrent transfer + auto-sync.
- **Factory pattern**: `app.py` builds Arena/Odoo clients on-demand (`build_arena()`, `build_odoo()`) for fresh auth per request.
- **Activity callbacks**: Transfer functions accept `on_activity(level, message)` for real-time UI logging.
- **Odoo 19 compat**: Product type uses `consu` (not `product`). `uom_po_id` field removed in Odoo 19.

### State Persistence

- `config.json` — credentials and mappings (gitignored)
- `sync_state.json` — tracks synced items with Odoo IDs, hashes, run history (gitignored)
- `sync.log` — application log (gitignored)

### Product Matching

Arena part number `E-BAT-00003` matches Odoo `default_code` `E-BAT-00003-V001` (version suffix `-V###` stripped). See `odoo_client.find_product_by_code()`.

### Arena Item Filtering (arena_client.get_items_for_sync)

Items are included based on lifecycle + assembly type:
- Components "In Production" — always included
- Top-level assemblies — included if In Production OR In Design
- Sub-assemblies "In Design" — included only if they have In Production components AND are referenced by an included top-level assembly
