# SaltScope (prototype / test)

Draw an area on a map, get its center coordinates, check a Sentinel-2 based
salinity status, and ask AI for soil advice and the best 3 crops to grow now.
Made by Khaled Islam and Mohammed Waleed.

## Files

- `main.py` – your salinity screening code (unchanged)
- `advice.py` – the AI advice code (Gemini API, season and hemisphere logic)
- `server.py` – local server: serves the website and calls `main.py` and `advice.py`
- `web/` – the website (HTML, CSS, JS, bundled map libraries and fonts)

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export CDSE_CLIENT_ID="your-copernicus-client-id"
export CDSE_CLIENT_SECRET="your-copernicus-client-secret"
export GEMINI_API_KEY="your-gemini-key"      # optional: turns on AI advice

python server.py
```

Open http://127.0.0.1:8000

Keys stay in the server process. The browser never sees them. Without
`GEMINI_API_KEY` everything works except the AI advice button, which shows how to
turn it on.

## Getting a free Gemini API key

1. Go to https://aistudio.google.com and sign in with a Google account.
2. Click **Get API key**, then **Create API key**.
3. Copy the key (it starts with `AIza`) and set it as `GEMINI_API_KEY`.

No credit card is needed for the free tier. The free tier has per-minute and
per-day limits, and Google may use free-tier content to improve its products. The
app only sends the coordinates, the satellite index numbers, and the date. If the
model name stops working, set `GEMINI_MODEL` to a current Flash model shown in
AI Studio (the default is `gemini-2.5-flash`).

## If AI advice fails (502, 503, 504)

Look at the terminal where `python server.py` runs. Every Gemini attempt is printed on a line
starting with `[advice]`, with the real reason from Google.

- **HTTP 503 or "overloaded"**: Google's side. New models such as `gemini-3.8-flash` have had
  repeated capacity errors, even on free keys under their limits. The server already retries once,
  then tries backup models (`gemini-3.7-flash`, `gemini-3.5-flash-lite`, `gemini-2.5-flash`), and the
  page says when a backup answered. If everything is busy, wait a few minutes and press **Ask again**.
- **429**: the free limit for that model was reached. The server moves to the next model.
- **504 or "took too long"**: Gemini did not answer in time. Press **Ask again**.
- **"rejected the API key"**: check `GEMINI_API_KEY` in the same terminal that starts the server.
- **"does not know the model"**: set `GEMINI_MODEL` to a current model from AI Studio.

Optional settings: `GEMINI_MODEL` (main model) and `GEMINI_FALLBACK_MODELS` (comma-separated backup
list, or empty to turn backups off). Gemini 3 models are asked to think at a low level, which keeps
this short task fast.

## How it works

1. Draw a circle, polygon or rectangle. The page computes its center (the area-weighted centroid).
2. Press **Start salinity scan**. The page shows **Loading** while the server downloads a
   Sentinel-2 image around the center and runs `compute_indices` and `classify_salinity` from `main.py`.
3. **Refresh** repeats the scan for the same area.
4. After a scan, press **Get soil advice**. The server sends Gemini the coordinates, hemisphere,
   month and season, the salinity score and class, the spectral indices, and the data reliability.
   It returns a short assessment, 4 to 6 steps to improve the soil, and the top 3 crops for the
   current season and likely climate.

The scan uses a square around the center (the dashed outline on the map), sized to
fit your shape between 100 m and 4 km wide. It does not clip to the exact outline,
because `main.py` fetches a square.

## Limits to keep in mind

- The percentage is a spectral similarity score against your reference sites. It is not
  a measured salt concentration (ECe, dS/m). With three reference sites it is a rough screening.
  Add verified references to `REFERENCES` in `main.py` as you get them.
- Soil type (texture, clay, sand) cannot be read from the bands `main.py` downloads, so the
  crop list is matched to the salinity level, the climate at the coordinates, and the season.
- AI advice is generated text. Confirm it with a soil test and a local agronomist.
