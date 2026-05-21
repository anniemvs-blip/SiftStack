# SiftStack

Full-stack real estate investing operations platform built for [DataSift.ai](https://datasift.ai). Pulls data from any source — web scrapes, scanned PDFs, courthouse terminal photos, Dropbox uploads — standardizes everything through a 10-step enrichment pipeline, and pushes it directly into DataSift ready for niche sequential marketing.

**Requires a DataSift.ai account.** Works with any market, any county, any state.

## What It Does

**One pipeline, many inputs.** No matter how you get the data, it comes out the same way:

```
  Web Scrape (CAPTCHA sites)  ──┐
  Scanned PDF Import (OCR)    ──┤
  Courthouse Terminal Photos  ──┼──→  Enrichment Pipeline  ──→  DataSift Upload  ──→  Niche Sequential
  Dropbox Auto-Polling        ──┤     (10 steps)                (automated)          Marketing
  CSV Re-Import               ──┘
```

### Data Intake (5 methods)

| Method | How It Works | Use Case |
|--------|-------------|----------|
| **Web Scrape** | Playwright browser automation with CAPTCHA solving | Public notice sites, county clerk portals |
| **PDF Import** | pypdfium2 rendering + Tesseract OCR | Scanned tax sale lists, legal documents |
| **Photo Import** | OpenCV preprocessing + OCR + LLM parsing | Phone photos of courthouse terminal screens |
| **Dropbox Watch** | Auto-polls a Dropbox folder every 15 minutes | Runner uploads photos from the field, system processes automatically |
| **CSV Re-Import** | Read existing data, re-enrich with latest APIs | Refresh stale records, merge datasets |

All five methods produce the same `NoticeData` records and flow through the same enrichment pipeline.

### Enrichment Pipeline (10 steps)

Every record gets the same treatment, regardless of source:

1. **Deduplicate** — same property in multiple notices? Keep the most recent
2. **Vacant Land Filter** — remove parcels with no house number
3. **Entity Filter** — flag LLC/Corp owners, research the person behind them
4. **Probate Property Lookup** — 3-tier search: Tax API → Executor family → People search
5. **Tax Delinquency** — parcel lookup, delinquent years + amount
6. **Address Standardization** — Smarty USPS validation, ZIP+4, geocoding, vacancy detection
7. **Commercial Filter** — remove commercial properties (RDI check)
8. **Zillow Enrichment** — Zestimate, MLS status, equity estimate, property details
9. **Obituary Search** — deceased owner detection, heir identification, decision-maker ranking
10. **Data Validation** — catch garbage OCR, verify required fields, compute mailable flag

### DataSift.ai Automation

SiftStack is purpose-built for the DataSift CRM. After enrichment, records are automatically:
- Formatted into 41-column DataSift CSV with tags, lists, and custom fields
- Uploaded to DataSift via Playwright browser automation (5-step wizard)
- Enriched with SiftMap property data (beds, baths, Zestimate)
- Skip traced for phone numbers and emails (DataSift unlimited plan)
- Routed into DataSift's niche sequential marketing campaigns (21 filter presets, 26 TCA sequences)

### Deal Analysis Tools

CLI tools for evaluating deals on the fly:

- **Comp Analysis** — Two-Bucket ARV with 7-tab Excel workbook
- **Rehab Estimator** — 4-tier room-by-room cost estimation
- **Deal Analyzer** — MAO calculation, financing scenarios (HML/conventional), ROI projections
- **Market Analysis** — Zip code scoring with 6 weighted factors
- **Buyer Prospecting** — Cash buyer identification from county records
- **Deep Prospecting** — 4-level research depth with PDF reports

## Quick Start

```bash
# Clone and install
git clone https://github.com/YOUR_USERNAME/SiftStack.git
cd SiftStack
pip install -r requirements.txt
playwright install chromium

# Configure credentials
cp .env.example .env
# Edit .env with your API keys (see Configuration below)

# Run your first scrape
python src/main.py daily --counties Franklin

# Or import a scanned PDF
python src/main.py pdf-import --pdf-path ./tax_sale.pdf --pdf-county Franklin

# Or process courthouse photos
python src/main.py photo-import --folder ./photos --photo-county Franklin --photo-type probate

# Full automated pipeline (scrape + enrich + upload to DataSift + notify Slack)
python src/main.py daily --upload-datasift --notify-slack
```

## Adapting to Your Market

SiftStack is built for Franklin County, OH but the architecture is market-agnostic. Use any county name — the pipeline accepts it and degrades gracefully if a county-specific API (like tax lookup) isn't available:

1. **Saved Searches** — Edit `SAVED_SEARCHES` in `src/config.py` to match your county's notice site
2. **Tax API** — The tax enricher (`src/tax_enricher.py`) queries your county's property tax API. Franklin County Auditor is built in; add yours alongside it
3. **Notice Parser** — The regex patterns in `src/notice_parser.py` handle 7 notice types: foreclosure, tax sale, tax delinquent, probate, eviction, code violation, divorce
4. **Photo Import** — Works with any courthouse terminal in any county — the OCR + LLM pipeline is county-independent
5. **Dropbox Watch** — Create folders for your county (`/YourCounty/foreclosure/`, etc.) and the watcher picks them up automatically
6. **DataSift Presets** — The 21 filter presets and 26 sequence templates are reusable across markets

The enrichment pipeline (Smarty, Zillow, obituary search, skip trace) works nationwide — no market-specific configuration needed.

## Buy Box Configuration

By default, SiftStack filters out property types that don't fit a typical residential wholesaling buy box. **Your strategy may be different.** Use these flags to match your buy box:

### Property Type Filters

| Flag | Default | What It Does |
|------|---------|-------------|
| `--include-vacant` | OFF (removed) | Keep vacant land parcels. Turn ON for land deals, subdivisions, infill lots. |
| `--include-commercial` | OFF (removed) | Keep commercial properties. Turn ON for commercial investing, mixed-use, multifamily. |
| `--include-entities` | OFF (removed) | Keep LLC/Corp/Trust-owned records. Turn ON if you market to entity owners directly. |

### Examples

```bash
# Default — residential only, no vacant land, no commercial, no entities
python src/main.py daily --upload-datasift

# Land investor — keep vacant parcels
python src/main.py daily --include-vacant --upload-datasift

# Commercial investor — keep everything
python src/main.py daily --include-vacant --include-commercial --include-entities --upload-datasift

# Entity researcher — keep entities AND research the person behind them
python src/main.py daily --include-entities --research-entities --upload-datasift
```

### Apify Actor (Cloud)

The same toggles are available in the Apify Console under your Actor's input configuration:
- **Include Vacant Land** — checkbox
- **Include Commercial Properties** — checkbox
- **Include Entity-Owned Properties** — checkbox

These apply to every scheduled run.

## Configuration

### Required (for Franklin County, OH scraping)
| Variable | Service | Cost |
|----------|---------|------|
| `FRANKLIN_OH_SHERIFF_USERNAME` / `_PASSWORD` | [Franklin Co. Sheriff Sale Auction](https://franklin.sheriffsaleauction.ohio.gov) | Free account |
| `DATASIFT_EMAIL` / `DATASIFT_PASSWORD` | [DataSift.ai](https://datasift.ai) CRM | Your existing plan |

Notes: Franklin OH's other primary sources (Probate Court NetData, Treasurer Tax Lien CSV) are public and require no credentials. The TN-only variables (`TNPN_EMAIL`, `CAPTCHA_API_KEY`) remain in `.env.example` for legacy Tennessee deployments but are NOT required for Franklin OH operation.

### Enrichment APIs (optional, pipeline degrades gracefully)
| Variable | Service | Cost | What It Adds |
|----------|---------|------|-------------|
| `SMARTY_AUTH_ID` / `TOKEN` | [Smarty](https://smarty.com) | 250 free/month | USPS validation, geocoding, vacancy |
| `OPENWEBNINJA_API_KEY` | [OpenWeb Ninja](https://openwebninja.com) | 100 free/month | Zestimate, MLS, equity |
| `ANTHROPIC_API_KEY` | [Anthropic](https://console.anthropic.com) | ~$0.001/record | LLM parsing, obituary search |
| `TRACERFY_API_KEY` | [Tracerfy](https://tracerfy.com) | $0.02/record | Phones, emails, mailing addresses |
| `TRESTLE_API_KEY` | [Trestle](https://trestleiq.com) | $0.015/phone | Phone scoring (5-tier dial priority) |

### DataSift + Notifications (required for full pipeline)
| Variable | Service | What It Does |
|----------|---------|-------------|
| `DATASIFT_EMAIL` / `PASSWORD` | [DataSift.ai](https://datasift.ai) | Auto-upload + SiftMap enrich + skip trace |
| `SLACK_WEBHOOK_URL` | Slack / Discord | Daily summaries + error alerts |

### All Intake Methods (optional)
| Variable | Service | What It Does |
|----------|---------|-------------|
| `DROPBOX_APP_KEY` / `SECRET` / `TOKEN` | [Dropbox](https://www.dropbox.com/developers) | Auto-poll for courthouse photos |
| `ANCESTRY_EMAIL` / `PASSWORD` | [Ancestry.com](https://ancestry.com) | SSDI + obituary collection |

Every API is optional. Missing a key? That enrichment step is skipped and the pipeline continues.

## CLI Commands

```bash
# ── Data Acquisition ────────────────────────────────────────────
python src/main.py daily                    # Scrape new notices since last run
python src/main.py historical               # Scrape last 12 months
python src/main.py pdf-import --pdf-path FILE --pdf-county Franklin
python src/main.py photo-import --folder DIR --photo-county Franklin --photo-type probate
python src/main.py dropbox-watch            # Auto-poll Dropbox for new photos
python src/main.py csv-import --csv-path FILE

# ── Deal Analysis ───────────────────────────────────────────────
python src/main.py comp --address "123 Main St"
python src/main.py rehab --address "123 Main St" --tier 2
python src/main.py analyze-deal --address "123 Main St" --purchase-price 150000
python src/main.py market-analysis --counties Franklin
python src/main.py buyer-prospect --counties Franklin
python src/main.py deep-prospect --csv-path output/records.csv --depth 3

# ── CRM Operations ─────────────────────────────────────────────
python src/main.py manage-presets --discover
python src/main.py manage-sold --months-back 12
python src/main.py phone-validate --list-name "Foreclosure"
python src/main.py lead-manage --lead-action qualify

# ── Workflow Tools ──────────────────────────────────────────────
python src/main.py setup-sequences --dry-run
python src/main.py niche-sequential --channel sms --day 1
python src/main.py playbook --blueprint wholesale --market columbus
```

### Common Flags
```bash
--upload-datasift          # Upload results to DataSift.ai
--notify-slack             # Send run summary to Slack/Discord
--skip-smarty              # Skip address standardization
--skip-zillow              # Skip Zillow enrichment
--skip-obituary            # Skip deceased owner detection
--split                    # Separate CSV per county + notice type
--verbose / -v             # Debug logging
```

## Cloud Deployment (Apify)

SiftStack runs as an [Apify Actor](https://apify.com) for scheduled daily automation:

```bash
# Install Apify CLI
npm install -g apify-cli

# Deploy
apify login
apify push

# Configure schedule + secrets in Apify Console
# The Actor runs the full pipeline: scrape → enrich → skip trace → DataSift upload → Slack notify
```

## Architecture

```
src/
├── main.py                  # CLI entry + Apify Actor entry
├── scraper.py               # Playwright web automation + CAPTCHA
├── captcha_solver.py        # 2Captcha reCAPTCHA v2 integration
├── notice_parser.py         # Regex extraction (7 notice types)
├── foreclosure_filter.py    # Trustee sale language matching
├── pdf_importer.py          # Scanned PDF → OCR → parse
├── photo_importer.py        # Courthouse photos → OpenCV → OCR → LLM
├── dropbox_watcher.py       # Cursor-based Dropbox polling
├── image_utils.py           # Shared OCR utilities (Tesseract)
├── llm_parser.py            # Claude Haiku structured extraction
├── llm_client.py            # Multi-backend LLM (Anthropic/Ollama/OpenRouter)
├── enrichment_pipeline.py   # 10-step canonical pipeline
├── address_standardizer.py  # Smarty USPS batch standardization
├── property_enricher.py     # Zillow via OpenWeb Ninja API
├── tax_enricher.py          # County tax API (parcel, delinquency)
├── property_lookup.py       # Async 3-tier probate property search
├── obituary_enricher.py     # Deceased detection + heir identification
├── ancestry_enricher.py     # Ancestry.com SSDI automation
├── entity_researcher.py     # LLC/Corp person extraction
├── tracerfy_skip_tracer.py  # Batch skip trace (phones + emails)
├── phone_validator.py       # Trestle 5-tier scoring
├── data_formatter.py        # CSV dedup + export
├── datasift_formatter.py    # 41-column DataSift CSV builder
├── datasift_uploader.py     # Full DataSift automation (Playwright)
├── comp_analyzer.py         # Two-Bucket ARV analysis
├── deal_analyzer.py         # MAO/ROI/financing scenarios
├── rehab_estimator.py       # 4-tier room-by-room costs
├── market_analyzer.py       # Zip code scoring (6 weights)
├── buyer_prospector.py      # Cash buyer identification
├── deep_prospector.py       # 4-level research + PDF reports
├── lead_manager.py          # 4 Pillars qualification + STABM
├── sequence_templates.py    # 26 TCA sequence definitions
├── niche_sequential.py      # 21 filter preset orchestration
├── playbook_generator.py    # SOP/script/checklist generator
├── report_generator.py      # PDF deep prospecting reports
├── excel_exporter.py        # Multi-sheet Excel workbooks
├── drive_uploader.py        # Google Drive upload
├── slack_notifier.py        # Slack/Discord notifications
└── config.py                # Environment config + constants
```

## Notice Types Supported

| Type | Source | What to Look For |
|------|--------|-----------------|
| Foreclosure | Web scrape, PDF | Trustee sale, deed of trust default |
| Tax Sale | Web scrape, PDF | Delinquent property tax auction |
| Tax Delinquent | Web scrape, PDF | Tax lien, unpaid property taxes |
| Probate | Web scrape, Photos | Estate administration, executor appointment |
| Eviction | Photos | Landlord-tenant, detainer warrant |
| Code Violation | Photos | Building code, compliance deadline |
| Divorce | Photos | Property division, marital assets |

## API Cost Estimates

Running daily in one county (Franklin, OH — volume varies by source):

| Service | Monthly Cost | What It Does |
|---------|-------------|-------------|
| 2Captcha | ~$3 | CAPTCHA solving (~30 notices × 30 days) |
| Smarty | Free | 250 lookups/month covers daily runs |
| OpenWeb Ninja | Free | 100 lookups/month covers most records |
| Anthropic (Haiku) | ~$2 | LLM parsing + obituary search |
| Tracerfy | ~$20 | Skip trace @ $0.02/record |
| Trestle | ~$15 | Phone scoring @ $0.015/phone |
| **Total** | **~$40/month** | Full pipeline, one county |

Requires a [DataSift.ai](https://datasift.ai) subscription ($97/month for unlimited skip trace plan).

## REI Skill Library

13 Claude Co-Work skill files for the DataSift community, distributed at [learn.datasift.ai/claude-skills-rei](https://learn.datasift.ai/claude-skills-rei). Each skill teaches Claude a specific REI workflow:

| Skill | What It Does |
|-------|-------------|
| Market Research | Zip code scoring, Market Finder reports |
| County Data | Notice extraction for all 7 types |
| Buyer Prospector | Cash buyer identification from public records |
| Real Estate Comping | Two-Bucket ARV with adjustment tables |
| Rehab Estimator | 4-tier room-by-room cost estimation |
| Deal Analyzer | Combined comp + rehab + MAO + financing |
| Deep Prospecting | 4-level heir research framework |
| Probate Property Finder | 3-tier property lookup for decedents |
| Phone Validator | Trestle scoring with dial priority tiers |
| Sequential Presets | 21 niche marketing filter presets |
| Sift Sequences | 26 TCA automation templates |
| Sift Operations | CRM operations encyclopedia |
| Playbook Creator | SOP generator from transcripts |

## License

MIT License. See [LICENSE](LICENSE) for details.

## Contributing

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/your-market`)
3. Commit your changes
4. Push to the branch (`git push origin feature/your-market`)
5. Open a Pull Request

Built by [DataSift.ai](https://datasift.ai) for the REI community.
