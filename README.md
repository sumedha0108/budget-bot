# 💸 Money: a personal expense tracker you chat with

One web app, opened from your phone's home screen:

- **Chat**: type `200 food`, `coffee 150 yesterday`, or `paid 3000 for dinner with Rahul and Priya`
- **Dashboard**: spend vs. budget, category donut, day-by-day bars, 12-month trend, biggest expenses
- **Table**: filter, edit, delete and export every expense
- **Owed**: who owes you, who you owe, one-tap settle

Simple entries are parsed by plain code (instant, no AI). Only messy messages use Groq's free
Llama model. Python does all the maths and writes to the database; the AI never touches your data.

**Cost: ₹0.** Streamlit Community Cloud (hosting) + Neon (Postgres) + Groq (AI), all on free plans.

---

## Setup

### 1. Database: Neon
1. Sign up at **neon.com**, create a project (any name; pick the region nearest you or Streamlit's servers).
2. On the project dashboard click **Connect** and copy the connection string. It looks like
   `postgresql://user:pass@ep-xxxx-pooler.region.aws.neon.tech/neondb?sslmode=require`.
3. Nothing else: the app creates its own tables on first launch.

### 2. AI: Groq
1. Sign up at **console.groq.com** and create an API key (starts with `gsk_`). No card needed.

### 3. Code: GitHub
1. Create a new **private** repository and upload everything in this folder.
2. Do **not** upload `.streamlit/secrets.toml` (the included `.gitignore` already blocks it).

### 4. Hosting: Streamlit Community Cloud
1. Go to **share.streamlit.io**, sign in with GitHub, click **Create app**.
2. Pick your repo, branch `main`, main file `app.py`.
3. Open **Advanced settings**, choose Python 3.12 if offered, and paste your secrets:
   ```toml
   DATABASE_URL = "postgresql://...your Neon string..."
   GROQ_API_KEY = "gsk_..."
   APP_PASSWORD = "a-long-random-passphrase"
   ```
4. Click **Deploy**. First build takes a couple of minutes.

### 5. Lock it down
- Choose a long `APP_PASSWORD` (16+ characters). Anyone with the URL sees a password prompt.
- Streamlit allows one **private** app on the free plan: in the app's **Share** settings,
  restrict viewing to your own email. This is the stronger lock; the password is a second one.

### 6. Put it on your phone
1. Open your app URL **once** with your password on the end:
   `https://your-app.streamlit.app/?k=YOUR_PASSWORD`
   (this skips the login prompt for that link; treat the link like a password, don't share it).
2. **iPhone**: Safari → Share → *Add to Home Screen*. **Android**: Chrome menu → *Add to Home screen*.

---

## Using it

| Type | What happens |
|---|---|
| `200 food` · `uber 340` · `coffee 150 yesterday` · `₹1,200 groceries` | logged instantly |
| `haircut 300 and auto 80` | two expenses (AI) |
| `paid 3000 for dinner with Rahul and Priya` | splits 3 ways: they owe you ₹1000 each, your ₹1000 is logged as an expense |
| `Rahul paid 1200 for the cab, me and Priya shared it` | you owe Rahul ₹400 |
| `lent Priya 2000` · `Rahul gave me back 500` | debts and repayments |
| `today` · `week` · `month` | spending summary |
| `owed` | who owes what |
| `budget food 5000` · `budget total 40000` · `budget` | set / view monthly budgets |
| `undo` (or the ↩ button) | removes everything the last message added |
| `how much did I spend on transport last month?` | AI answers from your data |

Budget warnings appear in the chat when a category reaches 80% and 100%.

## Things to know

- **Sleeping:** Streamlit Community Cloud puts apps to sleep after ~12 hours without a visit.
  You'll see a "wake this app" button; tap it and wait a few seconds. Using it daily mostly avoids this.
- **Neon** pauses its compute when idle and wakes in a second or two on the next request.
- **Groq free limits** (llama-3.3-70b: ~1,000 requests/day, 100K tokens/day) are far above personal
  use, and the app falls back to a smaller model automatically if you hit them. If Groq retires a model, set
  `GROQ_MODEL` in secrets to a current one.
- **Privacy:** messages that go to the AI (only the non-simple ones) are sent to Groq. Your database
  stays in your own Neon account.
- **Categories:** edit the `CATEGORIES` list in `config.py`, commit, and the app redeploys.
- **Backups:** Neon's free plan keeps only a short restore window. Use **Table → Download CSV** now and then.

## Run it on your computer (optional)
```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then fill it in
streamlit run app.py
```

## Files
`app.py` UI · `nlp.py` message parsing · `engine.py` executes actions · `db.py` all SQL · `config.py` settings
