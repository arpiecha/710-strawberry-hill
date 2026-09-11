# GC Tracker Template (`gc-tracker-template`)

One receipt tracker for every job. This is the template repo the live tracker is deployed from. Snap a receipt on your phone, Claude reads
it, it lands on the right job's dashboard with the photo attached.

Adding a new job is a name and an address on a form. No API keys, no Google
Sheet, no Dropbox, no Telegram bot, no new deployment.

## The two pages

| Page | What it's for |
| --- | --- |
| `/` | Dashboard: pick a job, see totals and the receipts table, upload from a computer, export to Excel |
| `/add` | Phone page: pick a job, tap **Add receipt**, the camera opens |
| `/c/<job-slug>` | The dashboard opened straight to one job, e.g. `/c/1041-arbor-ln` |

Both pages ask for the password once and remember it on that device.

### Put it on your phone

Open `/add` in Safari → Share → **Add to Home Screen**. It gets its own icon
and opens full screen like an app.

## Adding a job

Dashboard → **+ New client** → name (and optionally address and notes) → Add.
That is the whole setup. The job appears in the phone page's dropdown straight
away.

## Project tab

Rename the project (the name at the top of the page), change the passcode,
and pick light, dark or auto for the theme. The theme choice is per device
and the phone page follows it.

The passcode lives in the database once it has been changed here;
`ADMIN_PASSWORD` is only the starting value. Changing it signs out every
other device.

## Bills due

Each job has a **Bills due** list: a name, the day of the month it's due, and
optionally an amount. The dashboard shows what's coming and how many days
away. Nothing is sent anywhere — it's a list to look at.

## Running it

Everything is one Railway project:

- a **Flask** service (this repo)
- a **Postgres** database
- a **volume** mounted at `/data` for receipt photos

### Environment variables

| Variable | What it is |
| --- | --- |
| `DATABASE_URL` | Postgres connection string |
| `ADMIN_PASSWORD` | Starting passcode. Once changed in the Project tab, the stored one wins |
| `ANTHROPIC_API_KEY` | Used to read receipts |
| `RECEIPT_STORAGE_DIR` | Where photos are written. `/data/receipts` |

`PORT` is provided by Railway.

If `ANTHROPIC_API_KEY` is missing the site still runs — only receipt reading
fails, and it says so. You can still enter receipts by hand.

## How the pieces fit

```
app.py               Flask: API + serves the two pages
db.py                Tables: clients, receipts, bills
storage.py           Receipt photos on the volume
claude_receipts.py   The prompt and the Claude call
static/index.html    Dashboard
static/add.html      Phone page
static/manifest.json Home-screen app details
```

Tables are created on startup, so a fresh database needs no migration step.

## API

Everything except `/`, `/add`, `/c/<slug>`, `/static/*` and `/health` needs the
password, sent as an `X-Admin-Password` header. `/receipts/<id>/image` also
accepts `?key=`, which is what lets the **View** link be a plain link.

```
GET    /health
GET    /auth-check                 is this password right?

GET    /clients                    with receipt counts
POST   /clients                    {name, address?, notes?}
GET    /clients/<id>
PATCH  /clients/<id>
DELETE /clients/<id>               also deletes its receipts and bills

GET    /clients/<id>/receipts      newest first
POST   /analyze                    photo -> Claude -> receipt fields
POST   /save                       {client_id, date, store, category, type,
                                    amount, items?, notes?, image_base64?,
                                    source, force?}
DELETE /receipts/<id>
GET    /receipts/<id>/image        the photo behind View

POST   /settings/password          {current_password, new_password}

GET    /clients/<id>/bills         soonest first
POST   /clients/<id>/bills         {name, due_day, amount?, notes?}
DELETE /bills/<id>
```

## Notes

- **Returns** are stored as negative amounts, so the totals are net spend.
- **Duplicates** are caught per job by matching the amount and the first word
  of the store name. Receipts write store names inconsistently and the printed
  date is unreliable, so this is what the original bot settled on. You always
  get the choice to save anyway.
- **Photos** are shrunk to 1600px in the browser before upload. Phone photos
  are 5-10MB and the API rejects images over 5MB.
- Photos live on the volume, not in the database. Keep the volume when moving
  the service or the **View** links stop working.
