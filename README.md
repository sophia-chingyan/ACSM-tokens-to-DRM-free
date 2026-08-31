# ACSM tokens to DRM-free

Convert Adobe ACSM ebook tokens into DRM-free **EPUB** or **PDF** files for
personal offline reading, through a small single-user web app.

This repository consolidates two earlier projects that were the same
application specialised for one format each:

- `EPUB-sourced-ACSM-tokens-to-DRM-free-EPUB`
- `PDF-sourced-ACSM-tokens-to-DRM-free-PDF`

Both are superseded by this one, which handles either format.

## How it works

An `.acsm` file is not a book — it is a short Adobe fulfilment token. Three
[libgourou](https://forge.soutade.fr/soutade/libgourou) tools turn it into one:

1. **`adept_activate`** registers an anonymous Adobe device (once per volume).
2. **`acsmdownloader`** fulfils the token and downloads the encrypted book.
3. **`adept_remove`** decrypts it.

Step 3 is decryption only — nothing is re-encoded, re-rendered or transcoded:

- **EPUB** is decrypted entry-by-entry inside the ZIP container and
  `META-INF/encryption.xml` is dropped. Images, links, CSS (including
  `writing-mode` for vertical CJK text), fonts and paragraph structure survive
  byte-for-byte, as do Traditional/Simplified Chinese, Japanese and Korean text.
- **PDF** is decrypted as a document, so images, selectable text, fonts,
  bookmarks, links and annotations are preserved exactly.

Every conversion runs six steps, the last of which verifies the result: PDFs are
scanned for extractable text, bookmarks and links; EPUBs are checked for a valid
container and the *absence* of `encryption.xml`, which is the proof that DRM is
really gone.

## Choosing the format

The converter page has an **EPUB / PDF** picker. Your choice is checked against
the token before anything is downloaded — pick PDF for an EPUB token and the job
stops immediately, telling you which format to choose instead. The token is
always the authority; the picker just makes the intent explicit.

## Deploying to Railway

1. Create a new Railway service from this repository. The `Dockerfile` is
   detected via `railway.json`; libgourou is built during the image build.
2. **Add one volume mounted at `/app/data`.** Everything mutable lives there —
   converted books, covers, uploads, and the Adobe device registration under
   `/app/data/.adept`. Without it, your library and the device registration are
   lost on every redeploy.
3. Set the service variables:

   | Variable | Purpose |
   |---|---|
   | `SECRET_KEY` | Flask session secret. Use a fixed random value so sessions survive restarts. |
   | `GOOGLE_CLIENT_ID` | From Google Cloud Console → APIs & Services → Credentials. |
   | `GOOGLE_CLIENT_SECRET` | Same place. |
   | `ALLOWED_EMAIL` | The only Google account permitted to sign in. |
   | `APP_BASE_URL` | Optional. Defaults to `https://$RAILWAY_PUBLIC_DOMAIN`. |

4. In Google Cloud Console → Credentials → your OAuth 2.0 Client, add this
   authorised redirect URI:

   ```
   https://<your-app>.up.railway.app/auth/google/callback
   ```

   `/auth/callback` is also served, so a client registered against the older
   path keeps working.

`PORT` is injected by Railway and honoured automatically.

## Running locally

```bash
pip install -r requirements.txt
python app.py                     # http://localhost:8080
```

libgourou must be on `PATH` or built into `./libgourou/utils/`. Without OAuth
variables set, `/login` explains what is missing rather than failing blankly.

State is written to `./data/` locally; override with `DATA_DIR`.

## Command line

```bash
python converter.py book.acsm -o output          # format taken from the token
python converter.py book.acsm --format epub      # refuse if the token is a PDF
python converter.py --verify-only output/book.pdf
python converter.py --verify-only output/book.epub
```

## Notes

- Access is single-user by design: exactly one `ALLOWED_EMAIL` may sign in.
- Use this only for books you have legitimately purchased, to read on your own
  devices.
