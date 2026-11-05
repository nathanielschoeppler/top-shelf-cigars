# Top Shelf Cigars

Top-shelf handmade cigars — **single cigars from $20 and box deals at 15%+ off** — from online cigar stores, checked every hour, plus a ZIP-code finder for local cigar shops and lounges.

## How it works

| Part | What it does |
| --- | --- |
| `index.html` | The website. Reads `deals.json` and shows the deals. |
| `deals.json` | The current deal list. Rewritten every hour by the deal checker. |
| `scraper/scrape.py` | The deal checker. Visits store deal pages and keeps only top-shelf cigars on sale. |
| `.github/workflows/update-deals.yml` | Runs the deal checker every hour on GitHub, for free. |

## One-time setup

1. **Make the repo public** (free GitHub Pages needs it): Settings → General → scroll to *Danger Zone* → **Change visibility** → Public.
2. **Turn on the website:** Settings → Pages → *Deploy from a branch* → `main` / `(root)` → Save. The site appears at `https://nathanielschoeppler.github.io/top-shelf-cigars/` a minute later.
3. **Prices update by themselves** every hour (Actions tab → *Update cigar deals*). To run it right away, open it and tap **Run workflow**.

## Changing the rules

In `scraper/scrape.py`:

- `MIN_MSRP_PER_CIGAR = 20.00` — the minimum regular price per cigar.
- `SOURCES` — which store pages are checked.

## Pro plan ($5/month)

1. Create a Stripe Payment Link for a $5/month subscription.
2. In Stripe, set **After payment → Redirect** to `https://<your-site>/?welcome=pro`.
3. Paste the payment link into `PRO_CHECKOUT_URL` near the top of the `<script>` in `index.html`.

This unlocks Pro on the buyer's device. It's fine for launch, but it's an honor system (someone could visit the welcome link without paying). For real accounts that check Stripe, add a login service such as Supabase or Firebase later.

## Notes

- Some stores block automated visits. When a store can't be read, its last known deals stay up for 48 hours.
- If a store shows 0 items, the page it returned is saved in `scraper/debug/` so the parser can be fixed.
- GitHub pauses scheduled workflows in repos with no activity for 60 days; the hourly deal commits count as activity.
- Cigars are for adults 21+.
