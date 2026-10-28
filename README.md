# Job Application Bot

A Python automation bot that discovers job listings across multiple boards, scores them against your profile using an LLM, generates tailored cover letters, and (optionally) submits applications automatically.

Built for technical job seekers who want a focused, high-signal job search running on autopilot.

---

## Overview

The bot runs in six modes:

| Mode | What it does |
| --- | --- |
| `find` | Scrape job boards, score matches, save ranked jobs to disk |
| `apply` | Assisted apply — opens each match in Selenium, fills forms, waits for your review |
| `auto` | Fully automated apply — fills and submits without intervention |
| `inbox` | Check email for replies and update application status |
| `summary` | Send a daily summary of activity |
| `review` | Review drafts of cover letters and answer questions |

Every action is logged to `activity_log.jsonl` with fit score, success state, and timestamp. All submissions are dry-run by default. Set `BOT_LIVE=1` to actually submit.

---

## Features

- **Multi-source scraping** — Pulls listings from Greenhouse, Lever, Ashby, RemoteOK, Remotive, Jobicy, Arbeitnow, Himalayas, We Work Remotely, and Hacker News "Who is hiring".
- **LLM scoring** — Each job is scored against your resume and preferences using Cerebras (default) or Anthropic Claude.
- **Cover letter generation** — LLM-generated, role-specific cover letters saved to `cover_letters/`.
- **Custom question answers** — Automatically drafts answers to common application questions using the LLM.
- **Dry run by default** — Nothing is submitted until you explicitly set `BOT_LIVE=1`.
- **Notifications** — Optional ntfy.sh and Telegram push notifications for new matches and application status.
- **Inbox monitoring** — IMAP-based email checking for application replies.
- **Activity log** — Every action is logged to `activity_log.jsonl` (JSONL format).

---

## Requirements

- Python 3.10+
- Google Chrome (for Selenium)
- At least one LLM API key:
  - Cerebras (`CEREBRAS_API_KEY`)
  - Anthropic (`ANTHROPIC_API_KEY`)

---

## Installation

```bash
git clone https://github.com/muyaallan/job-application-bot.git
cd "job application bot"
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Place your resume at the path specified in `PROFILE["resume_path"]` (default: `Muya_Allan_Resume.pdf` in the project root).

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

See `.env.example` for the full list of variables. At minimum you need:

- `CEREBRAS_API_KEY` **or** `ANTHROPIC_API_KEY`
- A resume PDF in the project root

Optional:

- `NTFY_TOPIC` for push notifications
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` for Telegram
- `IMAP_APP_PASSWORD` + `IMAP_USER` for inbox checking

---

## Usage

### Discover jobs

```bash
python apply_bot.py find
```

Scrapes every configured source, scores each listing, saves results to `ranked_jobs.json` and `ranked_jobs.csv`.

### Assisted apply (recommended)

```bash
python apply_bot.py apply --top 10
```

Opens the top 10 matches in Selenium, fills what it can, and pauses for your review before submitting.

### Fully automated apply

```bash
BOT_LIVE=1 python apply_bot.py auto --max 5 --min-chance 60
```

Submits up to 5 applications with at least 60% estimated success chance.

Only use `auto` mode with `BOT_LIVE=1` if you have already tested `apply` and `review` and trust the bot on your target sites.

### Check inbox

```bash
python apply_bot.py inbox
```

Reads IMAP, matches replies to applications, updates status.

### Daily summary

```bash
python apply_bot.py summary
```

Sends a notification summarizing the day's activity.

### Review drafts

```bash
python apply_bot.py review
```

Prints any cover letters and answer drafts that are queued for approval.

---

## Project structure

```text
job application bot/
├── apply_bot.py                 # Main bot (find / apply / auto / inbox / summary / review)
├── requirements.txt             # Python dependencies
├── .env.example                 # Environment variable template
├── .gitignore
├── LICENSE
├── README.md
└── (generated at runtime)
    ├── .env                     # Your secrets (not committed)
    ├── ranked_jobs.json         # Scored listings
    ├── ranked_jobs.csv          # Same, CSV export
    ├── drafts.json              # Pending cover letters + answers
    ├── answers.json             # Saved answer templates
    ├── cover_letters/           # Generated PDFs
    ├── activity_log.jsonl       # Every action logged
    └── job_bot.log              # Debug log
```

---

## How scoring works

For each scraped listing the bot:

1. Extracts title, company, location, description, and remote status
2. Filters out obvious rejects (non-remote, wrong region, blocked keywords)
3. Sends the job description and your resume to the LLM
4. Gets back a fit score (0-100), a chance estimate, and a one-line reason
5. Keeps jobs above `MIN_CHANCE` (default 40)

The LLM prompt and scoring heuristics are in `analyse()` and `_llm_raw()`.

---

## Safety notes

- **Dry run by default.** The bot does nothing destructive unless you set `BOT_LIVE=1`.
- **Never commit your `.env`.** It is gitignored. Do not paste real keys into GitHub issues or PRs.
- **Rate limits.** Scraping every source runs roughly 10 HTTPS requests. Respect the sites' terms of service.
- **Selenium is fragile.** Each job site's form changes over time. The `apply` mode is intentionally interactive to catch failures.
- **Personal data.** `activity_log.jsonl`, `ranked_jobs.json`, `drafts.json`, `answers.json`, and `cover_letters/` contain personal application data. They are gitignored and should never be committed.

---

## Development

The bot is a single-file application. Edit `apply_bot.py` directly. To test:

```bash
# Dry run find
python apply_bot.py find --top 5

# Check that scoring returns sensible output
python apply_bot.py review
```

There is no automated test suite.

---

## License

MIT License

Copyright (c) 2026 Muya Allan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
