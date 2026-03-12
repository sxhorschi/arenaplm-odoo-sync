# Arena PLM -> Odoo ERP Sync

Standalone tool to transfer products and BOMs from Arena PLM to Odoo ERP.

- Fetches all "In Production" items from Arena (read-only, no writes to Arena)
- Cross-checks against Odoo to show which products already exist
- Lets you select and transfer new items to Odoo as products with BOMs
- Web dashboard for configuration, preview, and transfer

## Quick Start

```bash
pip install -r requirements.txt
python main.py
```

Open http://localhost:5000 and configure your connections in **Settings**.

## Configuration Reference

All settings are entered through the dashboard UI (Settings page). No `.env` file needed. Configuration is stored in `config.json` (auto-created, gitignored).

### Arena PLM Connection

| Field | Where to find it |
|---|---|
| **API Base URL** | For EU instances: `https://api.europe.arenaplm.com/v1`. For US: `https://api.arenasolutions.com/v1`. **Important:** This is the API endpoint, not the web UI URL (`app.europe.arenaplm.com`). |
| **Email** | Your Arena login email. |
| **Password** | Your Arena login password (same as web login). Arena does not use separate API keys. |
| **Workspace ID** | Found in Arena web UI: go to **Settings > Workspace** (top-right gear icon). The 9-digit number shown there (e.g. `898421176`). Also visible in webhooks or admin settings. |

### Odoo ERP Connection

| Field | Where to find it |
|---|---|
| **Odoo URL** | Your Odoo instance URL, e.g. `https://mycompany.odoo.com` or `https://myhost:8069`. Must include the port if non-standard. |
| **Database** | The Odoo database name. Find it via: login page dropdown, or the badge in the top-left corner of the Odoo UI (e.g. `PRE_22549372081512`). For self-hosted instances, you can also query `https://your-odoo/web/database/list`. |
| **Username** | Your Odoo login — typically your email address (e.g. `user@company.com`). |
| **Password / API Key** | Either your Odoo login password, or an **API key** (recommended). To create an API key: Odoo UI > click your avatar (top-right) > **My Profile** > **Account Security** tab > **New API Key**. The generated key replaces the password field. |

### Sync Settings

| Field | Description | Default |
|---|---|---|
| **Auto-Sync Interval** | Minutes between automatic syncs (when auto-sync is enabled). | `15` |
| **Default Category ID** | Odoo `product.category` ID used when an Arena category has no mapping. Find valid IDs via the dashboard's debug endpoint `/api/odoo/categories`, or in Odoo: **Inventory > Configuration > Product Categories**. | Must match your Odoo instance (e.g. `17` for "Hardware"). |
| **Default UoM ID** | Odoo `uom.uom` ID used as fallback unit of measure. Usually `1` = "Units". Check via `/api/odoo/uoms` or in Odoo: **Inventory > Configuration > Units of Measure**. | `1` |

### How Products Are Matched

The tool matches Arena items to existing Odoo products by **Internal Reference** (`default_code`):

- Arena part number `E-BAT-00003` matches Odoo code `E-BAT-00003-V001` (version suffix `-V###` is stripped for matching)
- If a product already exists in Odoo, it is marked as "exists" and skipped during transfer
- New products are created with `default_code` set to the raw Arena part number

### How BOMs Work

- BOMs are only fetched for actual assemblies (`SUB_ASSEMBLY` and `TOP_LEVEL_ASSEMBLY`), not components
- During transfer, BOM lines reference Odoo products by their part number
- Components must already exist in Odoo before an assembly's BOM can be created
- Components referenced in Arena BOMs but not yet "In Production" are listed on the **Missing Components** page

## Project Structure

```
main.py              Entry point — starts the dashboard
app.py               Flask backend with all API routes
arena_client.py      Arena PLM REST API client (read-only)
odoo_client.py       Odoo XML-RPC API client
mapping.py           Arena -> Odoo field translation
sync.py              Full sync orchestrator (auto-sync mode)
config.py            Config file management
templates/
  dashboard.html     Single-page web dashboard
```

## API Endpoints (for debugging)

| Endpoint | Method | Description |
|---|---|---|
| `/api/status` | GET | Dashboard stats and sync state |
| `/api/fetch-arena` | POST | Fetch Arena items + cross-check Odoo |
| `/api/transfer` | POST | Transfer selected items to Odoo |
| `/api/transfer/progress` | GET | Poll transfer progress |
| `/api/test/arena` | POST | Test Arena connection |
| `/api/test/odoo` | POST | Test Odoo connection |
| `/api/config` | GET/PUT | Read/update configuration |
| `/api/debug/odoo-products` | GET | Dump Odoo products (for debugging matching) |
| `/api/debug/odoo-search/<part>` | GET | Search Odoo for a specific part number |

## Notes

- **Arena is read-only.** The tool only uses `POST /login` for authentication and `GET` for all data access. It never modifies Arena data.
- **Odoo 19 compatibility.** Product type uses `consu` (not `product` or `goods`). The `uom_po_id` field was removed in Odoo 19.
- **Rate limiting.** Arena API calls are throttled to 200ms between requests.
