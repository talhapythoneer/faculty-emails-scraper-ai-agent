# Faculty Emails Extractor

Fills the client's workbook (`CTEP Freelance Project Institutions File.xlsx`) in three modules:

| Module | Command | Output | What it does |
|---|---|---|---|
| 1 | `python run.py m1` | `output/01_websites.xlsx` | Google search → official website + confidence |
| 2 | `python run.py m2` | `output/02_programs.xlsx` | Google `site:` search first → undergraduate programs page (Column D) + academics page + confidence. Crawls the site only when Google is not sure |
| 3 | `python run.py m3` | `output/03_final.xlsx` | Qualifying majors (Column E), faculty pages (links, then Google per department), leadership emails (Column F), R1/Non-R1 |

## Setup

1. Python 3.10+ and Google Chrome installed.
2. `pip install -U -r requirements.txt`
3. Copy `.env.example` to `.env` and add:
   - `SERPER_API_KEYS` (https://serper.dev, 2,500 free searches per key, then about $1 per 1,000): one key or several separated by commas, used one by one (the next key takes over when one runs out). Google searches for all
     modules. Without it, searches go through the Chrome window (slow, with CAPTCHAs).
   - `GROQ_API_KEY`: Module 3's AI steps (Groq `gpt-oss-120b`, about $2–4 for all 304 institutions). Without it,
     Module 3 runs on rules only. To use Claude instead, set `llm.provider: anthropic` in `config.yaml` and add
     `ANTHROPIC_API_KEY`.
   - `SCORECARD_API_KEY` (optional, free at https://api.data.gov/signup/): cross-checks websites in Module 1.

## Workflow

```
python run.py m1 --state KY --limit 5     # try a few first
python run.py m1                          # all institutions (about 1.5 h, Chrome window stays open)
```
Open `output/01_websites.xlsx` → **Review** sheet. For wrong rows, type the correct homepage in
`url_override` (Websites or Review sheet) and save. Then:

```
python run.py m2                          # Google first; crawls only the rows Google could not settle
python run.py m2 --google-only            # or: Google only, review, then run "python run.py m2" to crawl the rest
```
Check `output/02_programs.xlsx` the same way (`programs_url_override`).

Review rules (both workbooks): you can type the fix in the `*_override` column **or** over the URL itself
(`official_url` / `programs_url`). If the row has no usable website, type a short note instead of a URL (e.g.
`permanently closed`, `no undergraduate programs`): the row is skipped and the note goes to the client's NOTES column. The `steps` column shows what ran for each
row (google / google2 / academics / crawl), and `academics_url` holds the academics page when one was needed. Then:

```
python run.py m3
python run.py cost                        # AI tokens + Serper searches, estimated $ so far
```
`output/03_final.xlsx` = the client's sheet with D/E/F/I/NOTES filled, plus **Programs** (which majors were
included/excluded and why), **Contacts** (every email found, with name/title/role/source page; `selected = Y`
went into Column F), and **Review** (rows to check by hand).

## Useful flags

| Flag | Meaning |
|---|---|
| `--state KY` / `--state Kentucky` | Only that state (repeatable) |
| `--unitid 156189` | Only that institution (repeatable) |
| `--limit 10` | At most 10 institutions this run |
| `--force` | Re-process rows already done (cached pages are reused) |
| `--no-cache` | Download pages / search Google again |
| `--no-browser` | Never open Chrome (Modules 2-3) |
| `--workers 6` | Parallel institutions (Modules 2-3) |
| `--export-only` | Just rebuild the output spreadsheet |
| `--google-only` | Module 2: Google results only, no crawling |
| `--retry-blocked` | Module 3: re-run only institutions whose website showed a CAPTCHA that could not be passed |
| `-v` | Debug messages in the console (always written to `output/logs/`) |

## Notes

- Every run is resumable: finished rows are skipped. Rows whose input changed (e.g. you set an override) are
  re-processed automatically.
- Keep the output workbooks **closed in Excel** while a module runs; otherwise the result is saved under a new name.
- Google results are cached, so re-running a module does not spend Serper credits again.
- Bot checks on websites (Cloudflare "Verify you are human", reCAPTCHA, hCaptcha) are handled in the undetected
  Chrome window. The script waits, reloads, and clicks the checkbox. For Cloudflare it clicks with the real mouse,
  so the cursor moves on its own for about a second. If the check is not passed, Chrome is closed, the script waits
  2–4 minutes and tries the same page again. If the site still blocks it, the site is skipped for the rest of the
  run, and a note is added. Picture puzzles cannot be solved by clicking. Settings: `browser.challenge_*` in
  `config.yaml`. Set `challenge_mouse_click: false` to stop the mouse moving.
- Chrome searches only (no Serper key): if Google shows a CAPTCHA, the script beeps and waits (up to 15 min) for you to
  solve it in the Chrome window.
- If Chrome fails to start with a driver version error, set `browser.chrome_version_main` in `config.yaml`
  (your Chrome major version, e.g. `129`).
- Which majors count, keyword weights, role rules, and limits are all in `config.yaml`.
- Emails are never guessed: only addresses found on the institution's own pages are kept.
- Delete `output/cache.sqlite` to start completely fresh.
