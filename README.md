# AusNet myHomeEnergy – Home Assistant Integration

Automatically sync your **AusNet smart meter** data into Home Assistant's Energy dashboard. The integration signs into [myhomeenergy.com.au](https://myhomeenergy.com.au) with your credentials and downloads your NEM12 interval data on a schedule — no manual CSV exports required.

A manual `import_csv` service is also available for one-off backfills from a locally-saved NEM12 file.

## Supported data

| Channel | Description |
|---------|-------------|
| E1 | Grid import (electricity consumed from the grid) |
| E2 | Grid export (solar feed-in) |

Interval lengths of **15, 30, and 60 minutes** are all supported.

## Installation

### HACS (recommended)

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add this repository URL and select category **Integration**.
3. Search for **AusNet myHomeEnergy** and install it.
4. Restart Home Assistant.

### Manual

1. Copy the `custom_components/ausnet_myhomeenergy` folder into your HA `config/custom_components/` directory.
2. Restart Home Assistant.

## Automatic setup (recommended)

1. Go to **Settings → Devices & Services → Add Integration** and search for **AusNet myHomeEnergy**.
2. Enter your **myHomeEnergy email address** and **password**.
3. Optionally enter your **NMI** (National Meter Identifier, printed on your electricity bill). Leave blank to detect it automatically from your meter data.
4. Click **Submit**.

Home Assistant will immediately fetch up to 90 days of history and then refresh every 6 hours. Both E1 (import) and E2 (export/solar) channels are retrieved automatically.

### Adding to the Energy dashboard

After setup, go to **Settings → Energy** and add the following statistics:

| Channel | Statistic ID |
|---------|-------------|
| E1 (grid import) | `ausnet_myhomeenergy:ausnet_<NMI>_energy_import` |
| E2 (solar export) | `ausnet_myhomeenergy:ausnet_<NMI>_energy_export` |

Replace `<NMI>` with your actual NMI (visible in **Settings → System → Logs** after the first successful sync).

## Manual import (optional)

The `ausnet_myhomeenergy.import_csv` service lets you import a NEM12 file you've downloaded yourself. This is useful for bulk backfills or if automatic retrieval is unavailable.

### Getting your NEM12 file

1. Log in to [myhomeenergy.com.au](https://myhomeenergy.com.au).
2. Navigate to **My Data → Download** and select **NEM12 / Detailed Format**.
3. Choose your desired date range and download the CSV.
4. Copy the file to your HA host, e.g. `/config/www/ausnet_nem12.csv`.

### Calling the service

```yaml
service: ausnet_myhomeenergy.import_csv
data:
  file_path: /config/www/ausnet_nem12.csv
  timezone: Australia/Melbourne   # see below for other states
  channel: E1                     # E1 = import, E2 = export (solar feed-in)
  # nmi_override: "6123456789"   # optional – only needed if the NMI in the CSV is wrong
```

Run the service a second time with `channel: E2` to import your solar export data.

### Service parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `file_path` | ✅ | — | Absolute path to the NEM12 CSV on the HA host |
| `timezone` | ❌ | `Australia/Melbourne` | IANA timezone for the meter's local time |
| `channel` | ❌ | `E1` | `E1` (import) or `E2` (export/solar) |
| `nmi_override` | ❌ | *(from CSV)* | Override the NMI detected in the CSV |

### Timezone by state

| State / Territory | Timezone string |
|-------------------|-----------------|
| VIC, NSW, TAS, ACT | `Australia/Melbourne` or `Australia/Sydney` |
| QLD | `Australia/Brisbane` |
| SA | `Australia/Adelaide` |
| WA | `Australia/Perth` |
| NT | `Australia/Darwin` |

## Troubleshooting

- Check **Settings → System → Logs** and filter for `ausnet_myhomeenergy` to see sync progress and any errors.
- Statistics may take a few minutes to appear in the Energy dashboard after the first sync.
- If automatic login fails, confirm your credentials work at [myhomeenergy.com.au](https://myhomeenergy.com.au). If the portal has added CAPTCHA protection the integration will log a clear error — please open an issue.
- Re-importing the same date range (manual or automatic) overwrites existing statistics with the new values.

## Contributing

Pull requests are welcome. Please open an issue first to discuss significant changes.

## Licence

MIT
