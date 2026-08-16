# The results site

A static site — HTML, CSS and hand-written JS. No framework, no bundler, no
dependencies, no build step. Open the files and they are the site.

It never reads the database. It reads `site/data/*.json`, which
[`export_site_data.py`](../export_site_data.py) writes.

## Run it locally

The pages fetch their data, and browsers block `fetch` from `file://`, so it has
to be served rather than double-clicked. From the repo root:

```bash
python site/serve.py
```

Then open <http://localhost:8765>. (If you open a page from the filesystem by
mistake, it says so and tells you this command.) Pass a port to use another one:
`python site/serve.py 9000`.

Use this rather than `python -m http.server`. That one sends no cache headers at
all, so the browser applies its own guesswork and keeps serving the CSS and JS
you just edited — you reload and nothing changes, which is maddening when you are
art-directing. `serve.py` is the same server plus `Cache-Control: no-store`.

If you have already been bitten by that (a stale page that will not update),
the cached copies clear with a hard reload: **Ctrl+Shift+R**.

## Rebuild the data

```bash
python export_site_data.py --mock
```

Synthetic data in the exact schema of a real export — 150 threads, plausible
rates, obviously-fake message text. This is what the site is built and
art-directed against.

```bash
python export_site_data.py
```

The real thing. Reads `data/whoami.duckdb`, or falls back to the runner's
snapshot when the runner holds the write lock, so it is safe to run mid-flight.
Point somewhere else with `--db`.

**While the fleet is running, export from the snapshot explicitly:**

```bash
python export_site_data.py --db data/dashboard_snapshot.duckdb
```

The runner buffers writes and refreshes `dashboard_snapshot.duckdb` every few
seconds, so mid-run the snapshot is *fresher* than the live file on disk, not
staler. An export taken while threads are still in flight sets `complete: false`
in `meta.json` and the site raises a "preliminary" banner saying how many threads
have settled. That banner clears itself on the first export taken after the run
finishes — nobody has to remember to take it down.

Mock and real go through the **same** aggregation code — `--mock` fabricates
thread rows, not JSON — so the two cannot drift apart. The site shows a
"mock data" banner until a real export overwrites the files; nothing needs to be
switched off.

## Adding a numeric result

Don't add a page. Add an entry to `RESULTS` in `export_site_data.py`, re-export,
and the site grows a directory tile and a chart page at
`result.html?id=<your-id>` by itself.

```python
ResultSpec(
    id="my-result",
    title="Subjects who did the thing",
    description="One line, plain language.",
    source="threads.some_column — the gate on pNN-some-prompt",
    num=is_true("some_column"),
    den=recorded_("pNN-some-prompt", "some_column"),
    breakdowns=["by-condition", "by-family"],
)
```

A result that needs the qualitative coding pass is the same entry with
`status="pending"` and a `note` instead of predicates. It shows up in the
directory immediately as "analysis in progress" and turns into a chart when the
coding lands — a re-export, not a rebuild.

Nothing about the roster is hardcoded anywhere. Models, families and tiers come
from `config/models.yaml`, so an eleventh model becomes an eleventh bar on every
chart with no code change here.

## Art direction

Every colour, size, space and typeface on the site is a CSS custom property
declared in [`assets/theme.css`](assets/theme.css), and **nothing else declares
one**. `site.css` and the SVG charts only consume them. So the whole site
restyles from that one file without touching markup or JS — including the chart
bars, which take their fill from the family palette (`--fam-claude`, `--fam-gpt`,
…) through a class rather than an inline attribute.

The charts are plain SVG built in [`assets/chart.js`](assets/chart.js). That file
decides geometry only; it sets no fill, stroke or font.

## The pages

Menu order is Home · Numerical Results · Qualitative Results · Messages.

| File | What it is |
| --- | --- |
| `index.html` | Home: blurb, paper link, three highlight tiles, video slot |
| `results.html` | The numeric-results directory, generated from the manifest |
| `result.html?id=…` | The one numeric-result template, driven by the manifest |
| `qualitative.html` | The coding topics, as stubs |
| `messages.html` | Community messages (survey question 2) as chat bubbles |

Each page loads `assets/data.js` and then its own script of the same name.
The home page's only data dependency is the three headline rates.

## Deploying later

The site is a plain folder of static files with only relative links, so it will
serve from GitHub Pages as-is. When the repo is ready to publish, either point
Pages at `/site` on the default branch, or add a workflow that uploads the
folder. Nothing in the site needs to change for that.
