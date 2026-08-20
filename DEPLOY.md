# Deploying the demo (public link)

The console (`app.py`) is a **Streamlit server app** — it runs Python, so it
can't live on GitHub Pages (which only serves static files). It opens in **Demo
mode** when no `DATABASE_URL` is set, so it runs publicly with **no secrets**.

## Option 1 — Streamlit Community Cloud (recommended)

Free, ~2 minutes, real Python, gives you a `*.streamlit.app` URL.

1. Go to **https://share.streamlit.io** and sign in with GitHub.
2. **New app** → pick:
   - Repository: `bcaudillo/mar-guardrail`
   - Branch: `feature/mar-guardrail`
   - Main file path: `app.py`
3. **Deploy.** It installs `requirements.txt` and starts the app in Demo mode.

Pre-filled deploy link (still requires sign-in):

```
https://share.streamlit.io/deploy?repository=bcaudillo/mar-guardrail&branch=feature/mar-guardrail&mainModule=app.py
```

**Going live (real data) later:** in the app's **Settings → Secrets** on
Streamlit Cloud, add `DATABASE_URL` and `FIVETRAN_API_KEY` / `FIVETRAN_API_SECRET`
(same names as `.env`). With those set, toggle Demo off for live. Without them,
it stays a safe public demo.

## Option 2 — Hugging Face Spaces (alternative, also free)

Create a **Streamlit** Space and push this repo to it (or point it at the repo).
Same `requirements.txt` + `app.py`; runs in Demo mode with no secrets.

## Option 3 — GitHub Pages via stlite (a `github.io` link, experimental)

[stlite](https://github.com/whitphx/stlite) runs Streamlit fully in the browser
(WebAssembly/Pyodide), so it *can* be hosted on GitHub Pages as static files —
no server, no signup. Caveats: every dependency must be Pyodide-compatible
(this repo's `psycopg2` is **not**, but it's only needed for live Postgres, not
the demo), and it needs a small `index.html` loader that mounts the `.py` files.
Ask and this can be wired up under `/docs` with Pages enabled.

---

**Note:** none of these can be created *for* you without your GitHub/Streamlit
login — but each is a few clicks, and the app needs zero configuration to run as
a demo.
