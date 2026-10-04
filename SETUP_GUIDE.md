# Value Stock Analyzer — Setup & User Guide

Welcome to the **Value Stock Analyzer**! This guide walks you through setting up, configuring, and running the application on your computer (Windows, macOS, or Linux).

---

## 1. Prerequisites

Before getting started, make sure you have:
1. **Python 3.11 or higher** (Python 3.11, 3.12, or 3.13):
   - Check with: `python3 --version` (or `python --version` on Windows).
   - If not installed, download from [python.org](https://www.python.org/downloads/).
2. **Git** (optional, recommended if cloning/versioning):
   - Check with: `git --version`.

---

## 2. Installation & Setup

### Step 1: Extract the Project
Extract the zip file to a directory of your choice, then open a terminal / command prompt in that directory:

```bash
cd value-stock-analyzer
```

### Step 2: Create and Activate a Virtual Environment
Using a virtual environment keeps the project's dependencies isolated:

- **macOS / Linux:**
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  ```

- **Windows (Command Prompt):**
  ```cmd
  python -m venv .venv
  .venv\Scripts\activate.bat
  ```

- **Windows (PowerShell):**
  ```powershell
  python -m venv .venv
  .venv\Scripts\Activate.ps1
  ```

### Step 3: Install Required Dependencies
Upgrade `pip` and install all required packages:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 3. Configuration (`.env`)

The project uses a `.env` file for environment settings and API keys.

1. Copy the example template:
   - **macOS / Linux:**
     ```bash
     cp .env.example .env
     ```
   - **Windows:**
     ```cmd
     copy .env.example .env
     ```

2. Open `.env` in any text editor and fill in the required fields:

```bash
# --- Required: SEC EDGAR Identity ---
# The US SEC requires a user-agent containing your name and email for EDGAR financial filings:
SEC_USER_AGENT="Your Name your_email@example.com"

# --- Optional: AI LLM Analysis (Moat & Devil's Advocate) ---
# If you have an Anthropic API key, paste it here:
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-sonnet-5

# If you don't have an API key, the app still works! Basic quant analysis, ratios,
# signals, screeners, and turnaround metrics work completely offline/without an API key.
# If you use Claude Code CLI, set LLM_BACKEND=auto or LLM_BACKEND=claude_code.
LLM_BACKEND=auto

# --- Optional: Remote Sharing & Authentication ---
# Passwords for sharing the app over the web using `python scripts/share.py`
APP_OWNER_PASSWORD=
APP_VIEWER_PASSWORD=

# --- Optional: Email Alerts (Portfolio & Watchlist triggers) ---
# Leave blank to keep alerts in-app only.
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
ALERT_EMAIL_TO=
ALERT_EMAIL_FROM=
```

---

## 4. Running the Application

To launch the web user interface:

```bash
streamlit run app/main.py
```

Your web browser will automatically open to:
```
http://localhost:8501
```

If it does not open automatically, copy and paste `http://localhost:8501` into your browser.

---

## 5. Application Features & How to Use

### 🔍 Screener (`Screener` Page)
- Screen broad stock universes:
  - S&P 400 (Mid-Cap)
  - S&P 600 (Small-Cap)
  - TSX Composite (Canada)
  - Pacer US Cash Cows (COWZ & CALF)
  - Dataroma super-investor consensus
  - Custom Watchlist
- Uses a rigorous two-stage value filter: Free Cash Flow yield, ROIC/ROE, leverage, and valuation floor.
- You can run screens manually or resume interrupted screens directly from the UI or command line.

### 📊 Stock Analysis (`Stock` Page)
- Type any ticker symbol (e.g., `LULU`, `CNR.TO`, `MELI`, `JPM`) in the sidebar or search box.
- Four-lens comprehensive breakdown:
  1. **Quant Lens**: Valuation ratios, historical percentiles, reverse DCF growth rate expectations, Graham number, EV/EBIT.
  2. **Macro & Balance Sheet**: Debt maturity schedules, leverage trajectory, pension liabilities, cyclicality adjustments.
  3. **Moat Lens**: Qualitative competitive advantage assessment.
  4. **Devil's Advocate**: Adversarial counter-thesis spotlighting potential value-trap risks, secular headwinds, and earnings manipulation checks (Altman Z-Score, Beneish M-Score, Piotroski F-Score).
- **Turnaround Analysis**: Historical drawdown recovery projections and peer benchmarking.
- **Exporting**: Export comprehensive Markdown or Microsoft Word (`.docx`) reports directly.

### 💼 Portfolio & Watchlist (`Portfolio` Page)
- Track real positions and watchlists with written theses.
- Set price alert triggers, trailing stops, or thesis checkpoints.
- Freeze purchase snapshot fundamentals to compare thesis progression over time.

### 🎯 Estimate Accuracy (`Estimate accuracy` Page)
- Review historical consensus accuracy and surprise metrics for analyst revisions.

---

## 6. Helpful Utility Scripts

You can run these scripts directly from your terminal:

- **Run background screener from CLI:**
  ```bash
  python scripts/run_screen.py --lists cowz,sp400,watchlist
  ```

- **Check alert triggers on demand:**
  ```bash
  python scripts/check_alerts.py
  ```

- **Share your app temporarily with a friend:**
  ```bash
  python scripts/share.py
  ```
  *(Creates a secure Cloudflare tunnel and gives you a public link to share, protected by the viewer password you set in `.env`)*

- **Run test suite (offline tests):**
  ```bash
  pytest
  ```

---

## 7. Troubleshooting & FAQ

- **Port already in use?**
  Run on a different port:
  ```bash
  streamlit run app/main.py --server.port 8502
  ```
- **SEC 403 Forbidden errors?**
  Make sure you edited `.env` and set `SEC_USER_AGENT` to your own name and email address. The SEC blocks generic requests without contact info.
- **Missing packages?**
  Make sure your virtual environment is activated before running `pip install -r requirements.txt` and `streamlit run app/main.py`.

Enjoy analyzing value stocks!
