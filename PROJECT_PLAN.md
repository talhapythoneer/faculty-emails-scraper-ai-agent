# Faculty Emails Extractor: Project Plan (3 Modules)

## 1. Context

The client (J. Taylor, via Fiverr) is building a summer-program mailing list (CRCEP / STRONGER). For each institution in
`CTEP Freelance Project Institutions File.xlsx`, we fill these columns using **official institution websites only**:

| Column | Field | Rule |
|---|---|---|
| D | Website Links (for Undergraduate Programs) | The undergraduate programs / majors page. The homepage is OK as a fallback |
| E | Bacc degree | Only bachelor's **majors** in the target fields, all listed in one cell. No minors, certificates, associate or graduate degrees |
| F | Emails | Chairs, program directors, deans or coordinators. If none, 2–3 professors. Undergrad research, advising and career offices also count. Separate with `; `. **Never guess an email. Leave the cell blank, never write "N/A"** |
| I | Carnegie Info (R1 / Non-R1) | Worked out from "Research Activity Designation". No scraping |
| extra | Contact name, title, and source URLs | For the client's spot-checks (asked for in the Fiverr chat) |

**Target majors:** biochemistry, biology, biomedical sciences, chemistry, communication, computer science, data science,
engineering (biomedical, chemical, mechanical), exercise science, health policy, health science, kinesiology, neuroscience,
nursing, marine biology/science, molecular biology, physics, psychology, public health, statistics, and related natural,
health, social or applied sciences.

**Input facts:** 304 institutions, all in states from K to M (MA 59, MO 48, MI 39, MN 38, KY 28, MD 26, LA 24, ME 15, MS 14, MT 12,
plus 1 in the Marshall Islands). No URLs are provided. The file has a `unitid` (IPEDS ID) for every row.
**Deadline: Oct 7.** Sample due to the client around Sep 29.

**Design principle:** the code does everything it can without AI (searching, link scoring, pagination, email regex).
AI is used only for judgement calls inside Module 3. Every module writes a **confidence** score so weak rows can be
reviewed by hand before the next module runs.

**Search principle (updated Sep 26):** Google already ranks "the page on this site about X", so pages are looked up
with Google first (`site:` searches through the **Serper.dev API**) and the website is crawled only when Google is
not convincing. Every Google result is still opened and checked before it can score High.

---

## 2. Architecture overview

```
Input xlsx
   │
   ▼
Module 1: Official website     (Google via Chrome, done)      → output/01_websites.xlsx   [review low confidence]
   │
   ▼
Module 2: Undergrad programs   (Google first → academics page → output/02_programs.xlsx   [review low confidence]
                                fallback → crawl only if unsure)
   │
   ▼
Module 3: Degrees + faculty    (programs → faculty pages       → output/03_final.xlsx
                                (Google per department) →
                                names/emails, AI only where needed)                         [review flagged rows]
```

- Each module runs on its own (`python run.py m1 --state KY --limit 10`), can resume, and caches every page it fetches.
- Every module's output has a `*_override` column. If you fill it during review, the next module uses your value instead.
- Shared SQLite database: page cache (Google results included, so re-runs cost no API credits), per-row status, and logs.
- **Google access** (`config.yaml → search`): the Serper.dev API when `SERPER_API_KEYS` is set. Several keys are used one by one, switching when one runs out of credits (exact URLs, no CAPTCHAs,
  runs in parallel), otherwise the undetected Chrome session. If the API fails during a run (for example, out of credits),
  searches fall back to Chrome.

---

## 3. Module 1: Official website finder (Google via undetected Chrome)

**Goal:** the official homepage/domain for every institution, with a confidence score.

**Status (Sep 26):** done for all 304 through Chrome: 265 High, 33 Medium, 6 Low. Re-runs use the Serper API when
the key is set, and the cached Chrome results are reused.

### Steps
1. **Browser:** `undetected-chromedriver` in visible (not headless) mode with a persistent Chrome profile (cookies kept, so fewer CAPTCHAs).
   One browser session for the whole run.
2. **Query:** `"{Institution name}" {City} {State} official website`, typed into google.com (not a direct `/search?q=` URL)
   with small random typing delays.
3. **Scrape the results page:**
   - The **Knowledge Panel "Website" button**, if present. This is the strongest signal.
   - The top 5 organic results: URL, displayed domain, title, snippet.
4. **Score each candidate domain.** For example:

   | Signal | Points |
   |---|---|
   | Matches the Knowledge Panel website | +40 |
   | Domain ends in `.edu` | +25 |
   | Domain matches the IPEDS/College Scorecard website for this `unitid` (free API cross-check) | +30 |
   | Domain tokens match the institution name or acronym (fuzzy, e.g. `umich` ↔ University of Michigan) | +15 |
   | Result title fuzzy-matches the institution name (rapidfuzz ≥ 85) | +10 |
   | Ranked #1 organic | +5 |
   | Homepage `<title>` matches the name after fetching it (verification fetch) | +10 |
   | Known third-party domain (wikipedia, usnews, niche, collegeboard, petersons, facebook, linkedin, etc.) | −100 |
   | Multi-campus mismatch (e.g. "University of Minnesota **Duluth**" but the page is for Twin Cities; city not found on the page) | −30 |

5. **Confidence level:** `High` ≥ 80, `Medium` 50–79, `Low` < 50. Low and Medium rows go into a **Review** sheet.
6. **Anti-blocking:** random 8–20 s gap between searches, and a longer break every ~25 searches. If a CAPTCHA appears, the script
   **pauses, beeps, and waits** for a manual solve, then continues. 304 searches take about 1.5 hours.

### Output: `output/01_websites.xlsx`
`unitid, institution, city, state, official_url, domain, confidence_score, confidence_level, method (knowledge_panel / organic / scorecard),
top3_candidates, url_override, notes`

### Risks and fallbacks
- Google may still rate-limit Chrome. From Module 2 on, searches go through the Serper.dev API instead.
- Scraping Google directly is against Google's terms of service. The API avoids this, and it is what the client should
  use if they re-run the tool themselves.

---

## 4. Module 2: Undergraduate programs page finder (Google first)

**Goal:** the page that lists undergraduate majors / programs of study, with a confidence score.

### Why Google first
Crawling guesses the programs page from menu link text, which breaks on JavaScript mega-menus and unusual naming.
Google's ranking already answers "which page on this site lists the majors". In a first test on 4 Kentucky
institutions (Alice Lloyd, Asbury, Bellarmine, Berea), Google's top result was the right page every time, all High,
with no crawling.

### Steps (no AI)
`{site}` is the Module 1 host: the whole domain (`x.edu`, which also covers `catalog.x.edu`), or the campus host for
branch campuses (`d.umn.edu`). A branch campus therefore never gets its main campus's pages.

1. **Google, programs page:** `site:{site} undergraduate majors programs`
   - Score the top 6 results on the site: title/URL keywords (the same list the crawl uses) plus a rank bonus (15 / 10 / 6 / 3).
     News, blog, graduate and online penalties still apply.
   - **Open and check** the top 3: count degree markers ("B.S.", "B.A.", "Bachelor", "Major") and target-major links.
     If the plain HTML of Google's top 2 lists no degrees, they are opened in Chrome (JavaScript program finders).
   - Only if the result is not High, run a second query: `site:{site} "majors" OR "programs of study" OR "areas of study" bachelor`.
     A page returned by both queries gets the agreement bonus.
2. **Academics page (fallback):** only if still not High, search `site:{site} academics`. The best result goes in the
   `academics_url` column. The page itself is checked (small colleges often list majors there), and its links are
   scored as a second hop.
3. **Crawl (the original method):** only if still not High. Homepage navigation, catalog sub-site (Acalog / CourseLeaf),
   `sitemap.xml`, second hop, then the same check. Google's candidates stay in the pool.
   `python run.py m2 --google-only` skips this step so the Google results can be reviewed first. The next normal run
   crawls only the rows that are not High.
4. **Choose:** the highest checked score. If the winner is a catalog page and the website's own majors page is within
   10 points, the website page wins, because Column D is read by the client.
5. **Column D fallback:** if no page lists any degrees, use the academics page, then the homepage (always Low).
   Online-only sub-sites (`onlinedegrees.x.edu`, −40) and single-department pages (`/school-of-…/`, −15) lose points,
   because they rarely list all of the institution's majors.
6. **Detect the page type:** `static list`, `catalog`, `JS program finder`, or `A–Z tabs`, for Module 3's pagination.

### Confidence
Keyword score + Google rank + degree-marker count + target-major links + agreement between queries/methods.
A page cannot reach High (≥ 80) on Google rank and title alone. It must list degrees when opened.

### Output: `output/02_programs.xlsx`
`unitid, website_url, domain, programs_url, confidence_score, confidence_level, method (google / google2 / academics /
homepage / catalog / sitemap / second_hop), page_type, degree_marker_count, target_link_count, rendered, academics_url,
steps (which steps ran), top3_candidates, programs_url_override, notes`
→ This fills **Column D**.

---

## 5. Module 3: Degrees, faculty pages, and contacts

**Goal:** qualifying bachelor's majors (Column E), plus leadership/faculty names and emails (Column F and a Contacts sheet).

### 3a. Extract the program list and handle pagination
- Fetch the programs page and follow its pagination:
  - `rel="next"`, numbered page links, `?page=N` / `&page=N` patterns
  - "Load more" / infinite scroll → click or scroll in Chrome until no new items appear
  - A–Z tabs and filter facets (e.g. degree level = Bachelor's)
- Extract every program entry as `{name, url}`.
- **Filter without AI first:** fuzzy-match program names against the target-majors list plus synonyms
  (e.g. "Exercise Physiology", "Biomedical Engineering", "Pre-Med Biology"). Drop names containing minor, certificate,
  associate, concentration, M.S., M.A., Ph.D., graduate, or online-only.
- **AI (Groq `openai/gpt-oss-120b`, paid Developer account; Claude Haiku 4.5 as an alternative) only for:** ambiguous names ("Integrative Studies – Health track"), and pages where programs
  are not clean links (unstructured text). The input is trimmed to the list of names and links, never raw HTML.
- → **Column E** (qualifying majors, `; `-separated) and a program→URL map.

### 3b. Find the faculty pages
- For each qualifying program (grouped by department so shared departments are processed once):
  1. fetch the program page and score its links: "faculty", "faculty & staff", "people", "directory", "our team",
     "contact", "department". Maximum depth 2 (program → department → faculty)
  2. **Google per department:** `site:{site} "{department}" faculty` through the Serper API (up to 30 per
     institution, or 2 when searching through Chrome). A result is used only if its title, snippet or URL names that
     department, so another department's faculty list is never attached to this major
  3. `sitemap.xml` URLs that contain the department slug
  4. **AI fallback** only when nothing above worked. It chooses from a short list of link texts and URLs.

### 3c. Extract names, titles and emails
- **Emails without AI:** `mailto:` links, a regex over the page text, and de-obfuscation (`[at]`/`[dot]`, Cloudflare `data-cfemail`,
  emails assembled in JavaScript, which need the Chrome-rendered page).
- **Names and titles:** parse repeating card/list structures around each email with CSS heuristics. When the structure is unclear,
  send **trimmed snippets** (about 300 characters around each email) to the AI, which returns `{name, title, email, role}`.
- **Directory pagination:** same methods as 3a.
- **Profile pages:** if the listing has no emails, follow individual profile links (maximum 10 per department, starting with
  chair/director titles).

### 3b+. Department precision rules (added Sep 27 after the first sample)
- A candidate faculty page must name the department (URL, link text or title) or sit in the same site section as the
  program page. So the Political Science faculty link on a Psychology page is not used.
- News, blog, dated posts (`/2026/05/...`), events, jobs/HR, and athletics/alumni sub-sites are never faculty pages.
- If a faculty page gives no useful (non-generic) email, the next source is tried (up to 5 per major).
- **Profile pages (added Sep 30):** when a listing shows names but no emails, the individual profile pages are
  opened (chairs and directors first). Profile links are recognised by a name-shaped URL (`/stephen-e-asmus`), a page
  under the listing, or a profile path. The name and title come from the card text. A listing drawn by JavaScript is
  opened in Chrome first.
- **Campus directories (added Sep 30):** `directory.{domain}`, or a "Faculty & Staff Directory" link on the homepage,
  is used for every major. Department pages come first, then the full list, and only people whose title names the
  major are kept.
- A page with more than 60 emails counts as a campus-wide directory. Only people whose title or listing names the
  department are kept from it.
- Teacher-education tracks ("Biology Grades 8-12"), "School of …" entries and education programs are not majors.
  Political Science, Sociology, Social Work, Equine Science and Sport Management are kept (client decision).
- Program names are cleaned of program-finder card text ("… Major Main campus"). Duplicates are dropped when the
  major and degree are the same ("B.A. in Psychology" = "Bachelor of Arts in Psychology"). Student-life pages are not
  majors. Up to 60 majors per institution are searched for contacts.
- Never selected: admissions or enrollment staff, graduate-program coordinators, study-abroad, registrar and alumni staff.

### 3d. Select contacts (client rules)
- Priority: **chair > program director > dean > undergraduate coordinator** > professor / associate professor.
- A person is selected for a major only if their title fits it. For example, "Department Chair, Art & Design" is never
  selected for Biology. The exceptions are people found on the department's own pages, and titles that name only
  broad areas ("Dean, College of Arts and Sciences").
- Keep all leadership contacts. If there are none, keep 2–3 professors for that program (not administrative staff).
- Also include undergraduate research, advising or career-office emails when found.
- Save **everything extracted** in the Contacts sheet. Column F gets only the selected subset.

### 3e. Verify (no AI, required)
- The email must appear literally in the fetched page (after de-obfuscation).
- The email domain must match the institution domain or one of its subdomains (e.g. `@med.umich.edu`).
- Check syntax, remove duplicates, drop generic no-reply/webmaster addresses.
- No match means the email is not written. **Never guess an email.**

### Output: `output/03_final.xlsx`
- **Sheet 1:** the client's original sheet with D, E, F, I and NOTES filled (e.g. "contact forms only", "JS-blocked", "needs review").
  Original column order and formatting are unchanged.
- **Sheet 2, Contacts:** one row per person: `institution, department, name, title, role, email, selected, programs,
  source_url`. It is sorted by department, so every person in a department sits together and the department name
  repeats on each row. Unwanted departments can be filtered and deleted. Only people who fit at least one major are listed.
- **Sheet 3, Review:** rows with low confidence from any module, or with no degrees or no emails.

---

## 6. Tech stack

| Purpose | Tool |
|---|---|
| Language | Python 3.11+ |
| Google search | **Serper.dev API** (`SERPER_API_KEYS`), with `undetected-chromedriver` as the fallback |
| Browser automation (JS pages, Module 1 searches) | `undetected-chromedriver` |
| HTTP fetching | `httpx` (async) + `selectolax` / BeautifulSoup |
| Fuzzy matching | `rapidfuzz`, `tldextract` |
| AI (Module 3 only) | Groq (paid), `openai/gpt-oss-120b`, with strict JSON schemas and per-task reasoning effort (medium for judging, high for reading whole program pages, low for copying names and titles). `gpt-oss-20b` is the backup if 120b is rate-limited. Switch to Claude Haiku 4.5 with `llm.provider: anthropic` |
| Website cross-check | College Scorecard API (free key), matched by `unitid` |
| Storage / cache | SQLite |
| Excel | `openpyxl` (keeps the client's formatting) |
| Config | `config.yaml`: target majors, synonyms, keywords, blocklist, thresholds |

### Proposed project layout
```
faculty-emails-extractor/
  config.yaml
  run.py                     # python run.py m1|m2|m3 --state KY --limit 10 --resume
  src/
    common/
      browser.py             # undetected Chrome session, CAPTCHA pause
      fetch.py               # httpx + Chrome fallback + SQLite cache
      excel.py               # read input, write module outputs, apply overrides
      scoring.py             # link/URL keyword scoring, confidence
      emails.py              # regex + de-obfuscation + verification
      pagination.py          # next-link / ?page= / load-more handling
      llm.py                 # AI calls (Groq or Claude) with JSON schemas (Module 3 only)
    m1_websites.py
    m2_programs.py
    m3_faculty.py
  output/
    01_websites.xlsx
    02_programs.xlsx
    03_final.xlsx
```

---

## 7. Cost estimate

| Item | 304 institutions |
|---|---|
| Google searches, Module 1 (Chrome, done) | $0 |
| Google searches, Module 2 (Serper): 304 × ~1.7 (programs, sometimes a 2nd query and the academics query) ≈ 520 | within the 2,500 free |
| Google searches, Module 3 (Serper): 304 × ~5 departments not found through links ≈ 1,500 (worst case 304 × 30 = 9,120) | $0 (worst case about $7) |
| College Scorecard API | $0 |
| AI, Module 3 only: Groq `gpt-oss-120b` at $0.15 in / $0.60 out per 1M tokens, about 15–20K tokens per institution | **about $2–4 for all 304** |
| Alternative: Claude Haiku 4.5 for the full 304 | about $5–20 |
| **Total API cost** | **about $5–30** |

These are estimates. The real tokens per institution will be measured on the first 10–20 institutions. At 7k institutions,
Haiku would cost roughly $100–400 depending on how many pages need AI.

---

## 8. Timeline (Sep 24 → Oct 7)

| Dates | Work |
|---|---|
| Sep 25–26 | Module 1 built and run on all 304 (265 High). Review the 39 Medium/Low URLs |
| Sep 26–27 | Module 2 rebuilt as Google-first (Serper). Run on all 304 (about 30–60 min). Review low confidence |
| Sep 28–29 | Module 3 built. Run on 10–15 Kentucky institutions. **Send the sample to the client** |
| Sep 30–Oct 3 | Apply client feedback. Full Module 3 run |
| Oct 4–6 | QA, fill gaps from the Review sheet, final delivery (xlsx + code + README) |
| Oct 7 | Buffer / deadline |

---

## 9. Verification and QA
- **Module 1:** check 20 random High-confidence rows by hand; target ≥ 98% correct. Review every Low/Medium row.
- **Module 2:** check 20 random rows; the page must list undergraduate majors. Use the `steps` column to count how many
  rows Google settled alone and how many needed the crawl.
- **Module 3:** compare against the client's completed states / `Abhi_CRCEP Emails I-M.xlsx` as a gold set: degree
  precision/recall, and email precision (valid + right role). **Zero made-up emails** (enforced by 3e).
- Spot-check 10% of the final rows at random before delivery.

---

## 10. Open questions for the client
1. Please share `Abhi_CRCEP Emails I-M.xlsx` and your completed states, to use as a gold set.
2. What should go in the CRCEP / STRONGER / Basic columns? Should they stay empty?
3. Do you want contact names and titles in the main sheet, or is a separate Contacts sheet OK?
4. Should the Marshall Islands row stay in scope?
5. Deliverable: data only, or a tool you will run again yourselves? Whose API account pays for AI usage?
