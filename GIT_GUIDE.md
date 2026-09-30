# Git & GitHub guide for this project

Two recipes:

1. **First time.** Create a private GitHub repository and upload the whole project.
2. **Every later version (v2, v3...).** The repository already exists; upload your new changes.

Type the commands in a terminal. In VS Code, open one with **Terminal → New Terminal**; it is PowerShell on
Windows. Type (or paste) one command at a time and press **Enter**.

> **Safety:** the file `.gitignore` in this folder stops Git from uploading your API keys (`.env`), the private
> chat files, and the `output/` folder (results, the 222 MB database, Chrome profile). Always look at `git status`
> before a commit (step 1.7). If you ever see `.env` in the list, stop.

---

## Words you will see everywhere

| Word | Meaning |
|---|---|
| **Git** | A program on your computer that records versions ("snapshots") of a folder. |
| **GitHub** | A website that stores a copy of your Git project online (github.com). |
| **repository / repo** | A project folder that Git tracks, together with its history of versions. |
| **commit** | One saved snapshot of the project, with a short message saying what changed. |
| **stage / staging** | Choosing which changed files go into the next commit (`git add`). |
| **branch** | A line of history. You only need one, called `main`. |
| **remote** | A copy of the repo somewhere else. Here, the one on GitHub. |
| **origin** | The usual nickname for that GitHub remote, so you don't have to type its full address. |
| **push** | Upload your new commits from your computer to GitHub. |

---

# Section 1: First time (create the private repo and push everything)

You only do this section once.

### 1.1 Go to the project folder

```powershell
cd "D:\Projects\AI Projects\AI Projects 2026\faculty-emails-extractor"
```

| Word | Meaning |
|---|---|
| `cd` | "change directory": move the terminal into a folder. |
| `"D:\...\faculty-emails-extractor"` | The folder path. The quotes are needed because the path contains spaces. |

**What it's for:** all following commands act on the folder you are "in".
(A terminal opened from VS Code with this project is usually there already.)

### 1.2 Check that Git and the GitHub tool are installed

```powershell
git --version
gh --version
```

| Word | Meaning |
|---|---|
| `git` | Runs the Git program. |
| `gh` | Runs the GitHub CLI, a tool that can create repos on GitHub from the terminal. |
| `--version` | Option: just print the installed version and exit. |

**What it's for:** if both print a version number (e.g. `git version 2.55...`), you are ready. Both are
already installed on this PC. On a new PC, install them from git-scm.com and cli.github.com.

### 1.3 Tell Git who you are (once per computer)

```powershell
git config --global user.name "Talha"
git config --global user.email "talha.ifn@gmail.com"
```

| Word | Meaning |
|---|---|
| `config` | Git's settings command. |
| `--global` | Save the setting for every project on this computer, not just this one. |
| `user.name` | The setting for the author name shown on each commit. |
| `"Talha"` | The value (your name). |
| `user.email` | The setting for the author email shown on each commit. |
| `"talha.ifn@gmail.com"` | The value. Use the email of your GitHub account. |

**What it's for:** every commit is signed with this name and email.

### 1.4 Log in to GitHub from the terminal (once per computer)

```powershell
gh auth login
```

| Word | Meaning |
|---|---|
| `gh` | The GitHub CLI. |
| `auth` | Its "authentication" (login) commands. |
| `login` | Start logging in. |

**What it's for:** lets the terminal create repos and upload to your GitHub account. It asks a few questions.
Pick these answers with the arrow keys and Enter:

1. *Where do you use GitHub?* → **GitHub.com**
2. *Preferred protocol?* → **HTTPS**
3. *Authenticate Git with your GitHub credentials?* → **Yes**
4. *How would you like to authenticate?* → **Login with a web browser**. Copy the one-time code it shows, press
   Enter, paste the code in the browser page, and approve.

Check it worked: `gh auth status` should say "Logged in to github.com".

### 1.5 Turn this folder into a Git repository

```powershell
git init -b main
```

| Word | Meaning |
|---|---|
| `init` | "initialize": create a new, empty Git repository in the current folder. It adds a hidden `.git` folder where the history is kept. |
| `-b main` | Name the first branch `main` (GitHub's standard name). `-b` is short for "branch". |

**What it's for:** from now on Git can track this folder. Nothing is saved yet.

### 1.6 Stage all files for the first snapshot

```powershell
git add .
```

| Word | Meaning |
|---|---|
| `add` | Put files into the "staging area", the list of what goes into the next commit. |
| `.` | A dot means "this folder and everything inside it". Files listed in `.gitignore` are skipped automatically. |

**What it's for:** chooses what the first commit contains (all project files except the ignored ones).

### 1.7 Check what will be saved (safety check)

```powershell
git status
```

| Word | Meaning |
|---|---|
| `status` | Show what has changed and what is staged. |

**What it's for:** you should see about 34 green "new file:" lines (`run.py`, `config.yaml`, `src/...`,
`README.md`...). Make sure these are **not** in the list:

- `.env`
- `output/`
- `fiverr chat.txt`
- `meeting.txt`

If one of them appears, stop and fix `.gitignore` first.

### 1.8 Save the snapshot (the first commit)

```powershell
git commit -m "Version 1: Modules 1-3 faculty emails extractor"
```

| Word | Meaning |
|---|---|
| `commit` | Save the staged files as a snapshot in the history. |
| `-m` | "message": the text that follows describes this snapshot. |
| `"Version 1: ..."` | The message. Write anything that tells you later what this version was. |

**What it's for:** the project now has its first saved version, on your computer only.

### 1.9 Create the private repo on GitHub and upload everything

```powershell
gh repo create faculty-emails-extractor --private --source=. --remote=origin --push
```

| Word | Meaning |
|---|---|
| `gh repo create` | Create a new repository on GitHub. |
| `faculty-emails-extractor` | The repository's name on GitHub. |
| `--private` | Make it **private**: only you (and people you invite) can see it. |
| `--source=.` | Use the current folder (`.`) as the project to upload. |
| `--remote=origin` | Connect this folder to the new GitHub repo under the nickname `origin`. |
| `--push` | Upload the commits right away. |

**What it's for:** this one line creates the private repo, links it, and uploads version 1.

### 1.10 Open the repo in your browser to check

```powershell
gh repo view --web
```

| Word | Meaning |
|---|---|
| `repo view` | Show information about this project's GitHub repo. |
| `--web` | Open it in the web browser instead. |

**What it's for:** confirm the files are on GitHub, the repo is marked **Private**, and `.env` / `output/` are
not there.

<details>
<summary><b>Alternative to steps 1.4 and 1.9, using the GitHub website instead of <code>gh</code></b></summary>

1. On github.com click **+** (top right) → **New repository**. Name: `faculty-emails-extractor`, select
   **Private**, and do **not** tick "Add a README" / ".gitignore" / "license" (the repo must start empty). Click
   **Create repository**.
2. Back in the terminal (after step 1.8):

```powershell
git remote add origin https://github.com/YOUR-GITHUB-USERNAME/faculty-emails-extractor.git
git push -u origin main
```

| Word | Meaning |
|---|---|
| `remote add` | Register a new remote (an online copy of the repo). |
| `origin` | The nickname given to it. |
| `https://github.com/.../faculty-emails-extractor.git` | The repo's address. Replace `YOUR-GITHUB-USERNAME` with your username; GitHub shows the exact address on the new repo's page. |
| `push` | Upload commits. |
| `-u` | "upstream": remember `origin main` as the default, so later you can type just `git push`. |
| `origin main` | Upload to the remote `origin`, branch `main`. |

The first push opens a browser window to log in to GitHub (Git Credential Manager). Approve it once.
</details>

---

# Section 2: Every later version (the repo already exists)

Use this each time you want to save and upload a new version (v2, v3...).

### 2.1 Go to the project folder

```powershell
cd "D:\Projects\AI Projects\AI Projects 2026\faculty-emails-extractor"
```

Same as step 1.1: move the terminal into the project folder.

### 2.2 See what changed

```powershell
git status
```

| Word | Meaning |
|---|---|
| `status` | List files that were changed (red = changed but not staged yet) and new files Git doesn't track yet. |

**What it's for:** a quick look at what you are about to upload. Again, `.env` and `output/` must never appear.

Optional, to see the exact line changes: `git diff`. `diff` shows the lines removed (-) and added (+); press
`q` to exit.

### 2.3 Stage the changes

```powershell
git add .
```

| Word | Meaning |
|---|---|
| `add` | Put changes into the staging area. |
| `.` | All changes in this folder (ignored files are still skipped). |

To stage only one file instead: `git add src/m3_faculty.py`.

### 2.4 Save the new version

```powershell
git commit -m "Version 2: CAPTCHA handling and review fixes"
```

| Word | Meaning |
|---|---|
| `commit` | Save the staged changes as a new snapshot. |
| `-m "..."` | The message describing what changed in this version. Make it specific; it is how you find versions later. |

### 2.5 Upload it to GitHub

```powershell
git push
```

| Word | Meaning |
|---|---|
| `push` | Upload the new commit(s) to GitHub. No extra words needed: step 1.9 (or `-u` in the alternative) already set `origin main` as the default. |

That's the whole routine: **status → add → commit → push**.

### 2.6 Optional extras

| Command | Words | What it's for |
|---|---|---|
| `git log --oneline` | `log` = show the history; `--oneline` = one short line per commit | See all saved versions (newest first). Press `q` to exit. |
| `git tag v2` then `git push --tags` | `tag` = put a permanent label on the current commit; `v2` = the label; `--tags` = also upload labels | Mark "this is version 2" so it is easy to find or download later on GitHub. |
| `git pull` | `pull` = download commits from GitHub that you don't have yet and merge them in | Only needed if you changed something on another PC or on the GitHub website. Do it **before** your own commit. |
| `git restore --staged <file>` | `restore` = undo; `--staged` = only take it out of the staging area | You ran `git add .` and a file slipped in that shouldn't. The file itself is not changed. |

---

## If something goes wrong

- **`.env` was uploaded by mistake:** treat the keys as public. Create new keys at Groq / Serper, put them in
  `.env`, and ask for help removing the file from the repo history.
- **"rejected ... fetch first" when pushing:** GitHub has a change you don't have. Run `git pull`, then
  `git push` again.
- **"Please tell me who you are":** do step 1.3.
- **On a new computer:** `gh repo clone YOUR-GITHUB-USERNAME/faculty-emails-extractor` downloads the project
  (`clone` = make a local copy of a repo). Then copy `.env.example` to `.env` and fill in the keys, and run
  `pip install -r requirements.txt`.
