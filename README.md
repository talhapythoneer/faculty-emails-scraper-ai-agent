# Faculty Emails Extractor

Fills the institutions spreadsheet for the CRCEP / STRONGER mailing list:

- **Column D**: the undergraduate programs page
- **Column E**: qualifying bachelor's majors
- **Column F**: faculty and leadership emails (department chairs, program directors, deans, coordinators)
- **Column I**: Carnegie R1 / Non-R1

It uses **official institution websites only**, and emails are never guessed.

**The delivered results are in `results/Faculty Emails - Final Results.xlsx`.** You only need the rest of this guide
if you want to run the tool again, for example on new institutions.

The work runs in three modules. You check each module's results before running the next one.

| Module | What it finds | Output |
|---|---|---|
| **1** | Each institution's official website (Google search) | `output/01_websites.xlsx` |
| **2** | Each institution's undergraduate programs page | `output/02_programs.xlsx` |
| **3** | Qualifying majors, faculty pages and emails; builds the final spreadsheet | `output/03_final.xlsx` |

---

## 1. Folder structure

```
faculty-emails-extractor/
├── README.md              this guide
├── setup.bat              one-time setup (Windows): installs everything
├── run_module1.bat        runs Module 1 (Windows)
├── run_module2.bat        runs Module 2 (Windows)
├── run_module3.bat        runs Module 3 (Windows)
├── mac_linux/             the same four scripts for macOS / Linux (setup.sh, run_module1.sh ...)
├── .env.example           template for your API keys (setup copies it to .env)
├── config.yaml            settings: target majors, keywords, limits, AI model (optional to edit)
├── requirements.txt       Python packages (installed by setup)
├── run.py                 command-line entry point (the scripts call it)
├── results/
│   └── Faculty Emails - Final Results.xlsx    ← the delivered result (runs never change this file)
├── input/
│   └── CTEP Freelance Project Institutions File.xlsx    the institutions to process
├── src/                   program code
│   ├── m1_websites.py     Module 1
│   ├── m2_programs.py     Module 2
│   ├── m3_faculty.py      Module 3
│   └── common/            shared code (browser, Google search, page fetching, AI, Excel...)
└── output/                where the modules write
    ├── 01_websites.xlsx   Module 1 result, with the manual corrections already made
    ├── 02_programs.xlsx   Module 2 result, with the manual corrections and notes already made
    ├── 03_final.xlsx      Module 3 result (created by a run)
    ├── cache.sqlite       database: every result, page and search (created by a run; lets runs resume)
    └── logs/              one log file per run
```

---

## 2. Setup (once)

**You need:**

- **Windows 10/11** (or macOS / Linux)
- **Google Chrome**
- **Python 3.10 or newer** from python.org. On Windows, tick **"Add python.exe to PATH"** in the installer.
- **API keys:**
  - **Serper** for Google searches. Sign up at serper.dev: 2,500 free searches, then about $1 per 1,000.
  - **Groq** for the AI steps in Module 3. Get it at console.groq.com: about $2–4 per 300 institutions.

**Steps:**

1. Double-click **`setup.bat`**. It installs everything into a private `.venv` folder and opens the `.env` file.
2. Paste your keys into `.env`:
   ```
   SERPER_API_KEYS=your-serper-key
   GROQ_API_KEY=your-groq-key
   ```
   Save and close the file.

On macOS / Linux, run `bash mac_linux/setup.sh` in a terminal, then edit `.env` with any text editor.

---

## 3. Running

Run the modules in order. Keep the output spreadsheets **closed in Excel** while a module runs.

| Step | Do this | Time for ~300 institutions |
|---|---|---|
| 1 | Double-click **`run_module1.bat`**. Check the **Review** sheet of `01_websites.xlsx` | ~10–30 min |
| 2 | Double-click **`run_module2.bat`**. Check the **Review** sheet of `02_programs.xlsx` | ~1–1.5 h |
| 3 | Double-click **`run_module3.bat`**. The result is `03_final.xlsx` | ~3–8 h |

On macOS / Linux, use `bash mac_linux/run_module1.sh` and so on.

- **Every run can be stopped and resumed.** Finished institutions are skipped next time, and downloaded pages are
  reused. `03_final.xlsx` is updated after each institution.
- **Keep the computer awake and unlocked during Module 3.** A Chrome window opens for some websites. When a site
  shows a "Verify you are human" check, the mouse clicks it by itself for about a second.

**Options.** Type these after the script name in a terminal (Command Prompt / PowerShell in this folder):

| Option | Meaning |
|---|---|
| `--state KY` | Only this state (name or 2-letter code; can be repeated) |
| `--unitid 156189` | Only this institution (can be repeated) |
| `--limit 10` | At most 10 institutions this run (good for a first test) |
| `--force` | Process again institutions that are already done |
| `--export-only` | Only rebuild the output spreadsheet (seconds) |
| `--retry-blocked` | Module 3: only institutions whose website showed a CAPTCHA last time |

Example: `run_module3.bat --state KY --limit 10`

**New institutions:** replace the file in `input/` (same columns), then run the three modules again.

---

## 4. Checking and correcting results

Each module's spreadsheet has a **Review** sheet listing the rows worth a look. Fixes are typed into the
spreadsheet; the next module, or the next run, uses them.

| File | To fix | Type in column |
|---|---|---|
| `01_websites.xlsx` | Wrong website | `url_override` (or type over `official_url`) |
| `02_programs.xlsx` | Wrong programs page | `programs_url_override` (or type over `programs_url`) |
| `02_programs.xlsx` | A note for the final spreadsheet | `client_note` |
| either | Institution should be skipped | A note instead of a URL in the override column, e.g. `permanently closed` |

A `client_note` replaces the automatic note in Column G (NOTES) of the final spreadsheet. If it says
"no qualifying…" or "no bachelor's…", Columns E and F are left empty. After editing notes only, run
`run_module3.bat --export-only` to update `03_final.xlsx` in seconds.

---

## 5. Output files

### `output/03_final.xlsx` (and the delivered `results/Faculty Emails - Final Results.xlsx`): the final result

| Sheet | Contents |
|---|---|
| **Sheet1** | The original institutions sheet with **D** programs page, **E** qualifying bachelor's majors (`; `-separated), **F** emails (`; `-separated), **G** NOTES (why something is missing, e.g. "faculty emails not published on the website") and **I** Carnegie R1 / Non-R1. All other columns are unchanged. |
| **Contacts** | One row per person found: `department`, `name`, `title`, `role` (chair, program director, dean, coordinator, professor, office), `email`, `selected` (TRUE = in Column F), `programs`, `source_url` (the page the email was found on). Grouped by department, so a department can be filtered and removed. |
| **Programs** | Every program name found, with `decision` (included / excluded) and the `reason`. Shows why a major is or is not in Column E. |
| **Faculty pages** | For each major, the page its contacts came from and how many emails it had. |
| **Review** | Institutions with no majors, no emails, or a website that blocked automated access. |

**How contacts are chosen for Column F:**

- Per major: all chairs, program directors, deans and coordinators. If a major has none, up to 3 professors.
- Undergraduate research, advising and career-office emails are added when few others are found.
- Every email appears on the institution's own website.
- Generic mailboxes (admissions@, info@, webmaster@...) and other domains are left out.

### `output/01_websites.xlsx`: Module 1

| Column | Meaning |
|---|---|
| `official_url`, `domain` | The website found |
| `confidence_score`, `confidence_level` | High / Medium / Low; Medium and Low rows are also on the **Review** sheet |
| `method` | How it was found (Google knowledge panel, search result...) |
| `top3_candidates` | Other websites considered |
| `url_override` | Your correction (optional) |

### `output/02_programs.xlsx`: Module 2

| Column | Meaning |
|---|---|
| `programs_url` | The undergraduate programs page (Column D) |
| `confidence_score`, `confidence_level` | High / Medium / Low; Medium and Low rows are also on the **Review** sheet |
| `degree_marker_count`, `target_link_count` | Evidence that the page lists degrees: degree mentions, and links to target majors |
| `academics_url` | The academics page, used when no programs page is found |
| `steps` | What was tried: google / academics / crawl |
| `programs_url_override`, `client_note` | Your corrections and notes (optional) |

---

## 6. Settings and costs

- **`config.yaml`** holds:
  - target majors (`majors.target_keywords`) and excluded program types (`majors.exclude_patterns`)
  - roles that are never selected (`m3.exclude_title_patterns`)
  - the AI model (`llm`) and browser options (`browser`)
- **Costs:** `run.py cost` shows AI tokens and Serper searches used so far. To run it on Windows:
  `.venv\Scripts\python.exe run.py cost`.
- **Without keys:**
  - With no Groq key, Module 3 uses rules only.
  - With no Serper key, searches go through Chrome, which is slower.

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| "is open in Excel - live updates paused" | Close the spreadsheet; it is updated again after the next institution. Nothing is lost. |
| Chrome does not start / driver version error | Update Google Chrome. If it still fails, set `browser.chrome_version_main` in `config.yaml` to your Chrome major version, e.g. `141`. |
| A run was stopped | Run the same script again; it continues where it stopped. |
| Some institutions show "website blocks automated access (CAPTCHA)" | Later, with the PC unlocked, run `run_module3.bat --retry-blocked`. |
| Start completely fresh | Delete the `output` folder (the delivered file in `results/` stays). |
