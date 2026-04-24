# ha-ausnet — CLAUDE.md

## Overview

Home Assistant custom integration for **AusNet myHomeEnergy** (`myhomeenergy.com.au`). Fetches NEM12 electricity interval data (import E1 / export E2) and writes it to HA's long-term statistics for the Energy dashboard.

## Repository structure

```
custom_components/ausnet_myhomeenergy/
  __init__.py          Integration setup, service registration
  ausnet_client.py     HTTP client: login, cookie auth, NEM12/JSON download
  config_flow.py       Config-flow UI (email + password + optional session cookie)
  coordinator.py       DataUpdateCoordinator: periodic refresh, stat writing
  import_csv.py        NEM12 CSV parser + manual import_csv service
  const.py             Constants: domain, config keys, stat IDs, intervals
  manifest.json        HACS/HA integration metadata
  strings.json         UI string definitions (schema for en.json)
  translations/en.json English UI strings
```

## Key design decisions

- **Cookie jar**: `aiohttp.CookieJar(unsafe=True)` is required because the `.ASPXAUTH` cookie is issued for `.ausnetservices.com.au` while login is on `myhomeenergy.com.au`.
- **NEM12 download**: Uses `_START_DOWNLOAD_URL` (`/api/Sitecore/Dashboard/StartDownload`). The portal omits the NEM12 record-100 header and starts the file at record-200, so the validator accepts either `"100,"` or `"200,"` as the opening token.
- **reCAPTCHA fallback**: When the portal blocks automated login, the user can paste their `.ASPXAUTH` cookie (from browser DevTools) into the optional "Session cookie" config field. `authenticate_with_cookie()` injects it and verifies with a login-page redirect check.
- **Statistics**: External long-term statistics are written as cumulative kWh sums. Stat IDs follow the pattern `ausnet_myhomeenergy:ausnet_{nmi}_energy_import/export`.

## Authentication flow

1. `config_flow.py` → `AusNetClient.authenticate()` (email + password) **or** `authenticate_with_cookie()` (pasted `.ASPXAUTH` value).
2. On success the config entry stores `email`, `password`, `nmi`, and optionally `session_cookie`.
3. `coordinator.py` reuses whichever auth method was stored and re-authenticates when the session expires.

## Config-entry keys (`const.py`)

| Key | Description |
|-----|-------------|
| `email` | Portal login email |
| `password` | Portal login password |
| `nmi` | NMI override (auto-detected from NEM12 if blank) |
| `session_cookie` | `.ASPXAUTH` cookie value; used when reCAPTCHA blocks email/password login |

## Common tasks

### Run linting / type-checking
There is no dedicated test suite. Use standard HA dev tools:
```bash
pip install homeassistant
python -m mypy custom_components/ausnet_myhomeenergy --ignore-missing-imports
```

### Change the update interval
Edit `UPDATE_INTERVAL_HOURS` in `const.py`.

### Bump integration version
Edit `"version"` in `manifest.json` and `VERSION` in `config_flow.py` (add a migration handler if the config-entry schema changes in a breaking way).
