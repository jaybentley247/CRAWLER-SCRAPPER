# LeadCrawler

A command-line tool that crawls business websites, extracts public contact and marketing details, cleans them, and exports leads to CSV and Excel. Every value keeps the URL it came from, so you can check it before using it.

## Setup

Requires Python 3.11 or newer. Keep the virtual environment outside OneDrive, because syncing thousands of small files is slow and can lock them.

```powershell
python -m venv $env:USERPROFILE\.venvs\leadcrawler
& $env:USERPROFILE\.venvs\leadcrawler\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium   # optional: only needed for JavaScript-only sites
```

Then open `config.yaml` and set `identity.contact_email` to a real inbox you read. The crawler won't start until you do, because this address goes out with every request so site owners can reach you.

## Usage

```powershell
python main.py crawl --url webgenietech.tech     # one site
python main.py crawl --seeds seeds.txt         # many sites
python main.py resume                           # continue the last interrupted run
python main.py export                           # re-export without crawling again
python main.py runs                             # list past runs
```

`crawl` also takes `--max-depth`, `--max-pages`, `--concurrency` and `--no-browser`. Add `--help` to any command for details.

### Seed files

- **`.txt`**: one website per line. Lines starting with `#` are ignored.
- **`.csv`**: needs a `url`, `website`, `site`, `domain` or `link` column, and can include `name`, `category` and `country` (a code like `US`, `CA`, `DE` or a name like `Germany`). Values you supply here beat anything found on the site.

Seeds must be a business's own website, or a link to a PDF or spreadsheet (see below). Seeds on social networks and other `crawl.skip_domains` sites are rejected. **Directory listing pages aren't supported**: the crawler treats each seed's domain as one business, so a directory would come out as a single wrong lead. Export the directory's business websites to a seed file instead.

### PDF and spreadsheet seeds

A seed ending in `.pdf`, `.xls`, `.xlsx`, `.xlsm` or `.xlsb`, such as a published list of contractors, is downloaded and read for emails. Every sheet of a spreadsheet is read. It can sit in the same seed file as websites:

```text
acme-plumbing.com
https://www.vvc.edu/sites/default/files/2021-10/WP%20LIST.pdf
https://booleanstrings.com/wp-content/uploads/2014/01/fortune1000-2012.xls
```

A list like that names many businesses, so each email becomes its own lead: one per company domain (`estimating@acme-paving.com` → `acme-paving.com`), or one per address for freemail such as `@gmail.com`. Things to know:

- **Only emails are read.** Phone numbers, names and addresses in the file are ignored, because they can't be reliably matched to the right email.
- **The file is read, nothing more.** Links in it aren't followed, macros in an `.xlsm` are never run, and links to PDFs and spreadsheets found on websites are still skipped.
- **The file type comes from its contents**, so a `.xls` link that actually serves an `.xlsx` file still works.
- **The usual rules apply**: robots.txt, rate limits, the `fetch.max_bytes` size limit (5 MB by default), and blocks. A site that refuses automated downloads (403 Forbidden) is skipped, not bypassed.
- **Scanned PDFs and pictures have no text to read**, so they give no emails. Password-protected files are skipped.
- **Emails that are cut off or misprinted**, such as one missing its `.com`, are dropped, and so are address formats like `firstname.lastname@acme.com`, which some lists give instead of real addresses.

## What you get

Exports go to `exports/`, named by run:

| File | Contents |
| --- | --- |
| `leads_run1.xlsx` | A `Leads` sheet and an `Evidence` sheet |
| `leads_run1.csv` | The leads |
| `leads_run1_evidence.csv` | Every value found for those columns, with its page, method and confidence |

Use the `.xlsx` if you work in Excel. Excel reads `+1...` in a CSV as a number and displays it in scientific notation.

Leads have the columns listed in `storage.export_columns`, currently `email` and `other_emails`. Sites with none of the contact details being exported are left out; add `--include-empty` to `export` to list them.

Everything else the crawler finds stays in the database. To include more, add columns to `storage.export_columns` in `config.yaml` and run `export` again, with no new crawl. The extra columns available include:

- **`email_type`**: `role` (info@acme.com), `named` (ada@acme.com), `freemail` (acme@gmail.com) or `external` (another company's domain, often the web designer). The best type is picked as the main `email`.
- **`country`**: from the main phone number, otherwise the domain ending. Blank if neither gives one.
- **`category`**: declared by the site if it can, otherwise matched from `target.category_keywords`, otherwise `Other`.
- **`quality_score`**: 0–100, weighted towards contact details.
- **`crawl_status`**: `done`, `blocked`, `unreachable`, `skipped`, `moved` or `error`.
- **`merged_domains`**: other domains merged into this lead, because they share both a contact detail and a similar name.
- **`possible_duplicate_of`**: domains sharing a contact detail under a different name. These aren't merged, because they could be unrelated clients of one agency, so check them yourself.

## How it behaves

### Responsible crawling

These are built in, and no setting turns them off:

- **robots.txt is always obeyed**, including on redirects, and each subdomain's own rules apply. If a server can't say what's allowed (5xx or 429 on robots.txt), the site isn't crawled.
- **One request at a time per site**, with a random 1–3 second pause after each response. The minimum allowed `min_delay` is 1 second, and a robots.txt `Crawl-delay` can only slow things down. Sites asking for more than `max_crawl_delay` are skipped rather than ignored.
- **Backoff and giving up**: 429 and 5xx responses are retried with growing waits, honouring `Retry-After`. After 3 blocks or failures in a row, the crawler stops visiting that site for the rest of the run.
- **An honest User-Agent** with your contact email. Names imitating a browser or search engine are rejected.
- **Bot walls are respected**: Playwright only renders pages that need JavaScript, and is never used to get past a block.
- **Your network is off limits**: links and redirects can't reach localhost or private addresses.

`concurrency` is the number of *sites* crawled at once, so raising it doesn't put more load on any one site.

### Crawl order and limits

Seeds come first, then contact, about, team and location pages, with a boost for footer links. If a site hits its page limit, the pages skipped are the least useful ones. The crawler stays on the site's domain and skips files, login, cart and checkout pages.

`http`/`https`, `www`/non-`www`, trailing slashes and tracking parameters all count as one URL, so a site's page budget isn't spent on copies.

### Any company, any country

Categories only label a company. They never exclude one, so any industry can be crawled, and you can edit `target.category_keywords` freely.

Local phone numbers can only be read once the country is known, so each page's country is worked out in this order: the seed's `country`, the domain ending (`.de`, `.ca`), JSON-LD, the page's language tag, US and Canadian postal codes, then the country named most on the page. If nothing gives a clue, it uses `target.default_region`, then tries `target.fallback_regions` with lower confidence. For `.com` sites with no clues, add a `country` column to your seed file to be sure.

Addresses are recognised in US, Canadian, UK and common European formats. Pages such as the German *Impressum* are visited early, because many European countries require them to list company details.

### Which value wins

Each sighting of a value is saved as a fact with a confidence score, and the highest score wins. JSON-LD scores highest because sites write it on purpose. A value seen on more pages gets a small boost.

Since facts are kept, `export` can rebuild leads after you change the category settings, with no new crawl.

### Interrupting and resuming

A page's results and new links are saved together in one transaction, so stopping at any moment, even in a crash, loses at most the page being fetched. `resume` uses the settings the run started with, not the current `config.yaml`. The database path is the exception: it always comes from the config.

## How long a run takes

With the default settings, each site gets about one page every 2 seconds and 5 sites run at once:

| Sites | ~10 pages per site | Worst case: 30 pages per site |
| --- | --- | --- |
| 500 | about 35 minutes | about 1 hour 40 minutes |
| 5,000 | about 5.5 hours | about 17 hours |

Slow servers and Chromium rendering add time. Increasing `concurrency` shortens this without adding load to any one site.

## Using the data responsibly

A public address isn't automatically permission to send marketing, and the rules depend on where the recipient is, which the `country` column helps you sort by:

- **USA**: CAN-SPAM allows cold email but requires a working opt-out, a real postal address and honest subject lines.
- **Canada**: CASL generally requires consent first, including for business email. Its implied-consent exceptions are narrow, and penalties are high.
- **Europe**: the GDPR covers named emails (`email_type: named`), and national rules vary. Germany, for example, generally requires prior consent even for business-to-business email.
- **Other countries**: most have their own privacy and marketing laws, so check them too.

Before outreach, get advice for the countries you're targeting, honour opt-outs, and check each site's terms. This tool's robots.txt compliance doesn't replace any of that.

## Project layout

```
main.py        CLI entry point
settings.py    Validates config.yaml
models.py      Shared data shapes
config.yaml    All settings
crawler/       Queue, depth and same-domain rules, robots.txt, crawl loop
fetcher/       httpx client, retries, rate limiting, Playwright fallback
extractors/    Email, phone, socials, address, metadata, JSON-LD, PDF and spreadsheet seeds
pipeline/      Cleaning, validation, deduplication, enrichment
storage/       SQLite database, CSV and Excel export
tests/         Test suite
```

The database defaults to `~/.leadcrawler/leads.db`, outside the project. It's rewritten after every page, which clashes with OneDrive sync.

## Tests

```powershell
python -m pytest                  # everything
python -m pytest -m "not browser" # skip the tests needing Chromium
```

Most tests run against a fake web that counts every request. `tests/test_real_http.py` uses real sockets, real delays and real Chromium against a server on your machine. Nothing contacts the internet.
