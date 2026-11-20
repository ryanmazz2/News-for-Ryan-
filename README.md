# Twice Daily

A personal news site that rebuilds itself every morning and evening with:

- Economy and Politics headlines from PBS NewsHour, CNBC, Marketplace, NPR, the Guardian, Politico, Stateline, AP and Al Jazeera, mixed with ProPublica, the Marshall Project, The Intercept, Mother Jones, the American Prospect, the Lever, Capital & Main, EPI, CEPR and 30-odd left and international outlets. Outlets that cover everything are sorted article by article; sports, entertainment and other non-news are dropped; no outlet gets more than six stories per tab; a filter on the page switches between all sources, newsrooms and wires, or left and independent outlets
- the newest articles in Marxist and socialist journals (via Crossref)
- recent scholarship on Marx, Marxism and socialism from any venue (via OpenAlex)
- a rotating banner of dated events in working-class, socialist and anti-colonial history, with anniversaries shown on the day
- a hero from history each day, with a photo from Wikimedia Commons, chosen by birthday or anniversary when there is one
- the economy in numbers: ten-year charts of unemployment, inflation, real pay, workers' share and wealth inequality from FRED
- a strike tracker and a local news section (set to Colorado; change `local_place` and `local_searches` in `sources.json`)
- a text of the week from the socialist canon, with a reading-progress tracker, and a song of the day
- listen & watch: new episodes from left podcasts (playable on the page) and YouTube channels
- a reading list (save any headline) and back issues of every edition for a year
- radical art: an artwork of the day, public-domain prints and posters from three museums' open collections, and the latest from artist collectives

It runs entirely on free GitHub features. Nothing to install on your computer.

## Set it up (about 10 minutes)

1. Make a free account at github.com if you don't have one.
2. Create a new repository (the **+** menu, top right, then **New repository**). Name it anything, e.g. `twice-daily`. Choose **Public** (GitHub Pages is free for public repos). Click **Create repository**.
3. On the new repo's page, click **uploading an existing file**. Unzip the download, then drag **the contents** of the `twice-daily` folder into the browser (including the hidden `.github` folder — see the note below). Click **Commit changes**.
4. Go to **Settings → Pages**. Under **Build and deployment → Source**, choose **GitHub Actions**.
5. Go to the **Actions** tab. If asked, click the button to enable workflows. Open **Update site**, click **Run workflow**, then **Run workflow** again.
6. When the run shows a green check (a minute or two), your site is live at
   `https://YOUR-USERNAME.github.io/twice-daily/`. The link also appears in the run summary.

After that it updates itself at about 7 am and 7 pm Mountain time.

## Add your two secrets (needed for the scholarship sections)

OpenAlex, which supplies the "Recent work" tab, requires a free API key. Crossref, which supplies the journals, works faster when it knows who's asking.

1. Make a free account at openalex.org, then copy your key from openalex.org/settings/api.
2. In your repo, go to **Settings → Secrets and variables → Actions → New repository secret**.
3. Name: `OPENALEX_API_KEY`. Secret: paste the key. Click **Add secret**.
4. Click **New repository secret** again. Name: `CONTACT_EMAIL`. Secret: your email address. Click **Add secret**.

Secrets stay private even though the repository is public.

**Hidden folder note:** On a Mac, press Cmd+Shift+. in Finder to show the `.github` folder. If dragging it doesn't work, create the file by hand on GitHub: **Add file → Create new file**, type `.github/workflows/update.yml` as the name, and paste in the contents of that file.

## Four pages

- **Today:** history of struggle, hero from history, strike tracker, local news, and the Economy / Politics / Essays & Ideas headlines.
- **Economy & research:** US numbers (FRED), the world economy (IMF World Economic Outlook), official releases from the BLS, IMF and World Bank with the BLS release calendar, journals, working papers (PERI, INET, Levy, EPI) and recent research, and the text of the week.
- **Culture:** artwork of the day, museum archives, posters from MSU's Leftist Political Posters and Radicalism collections, radical art now, song of the day, and listen & watch.
- **My shelf:** what you're currently reading, reading stats, recommendations from the canon (`site/data/canon.json`), your saved reading list and back issues.

## Posters: one more free key

The MSU posters come through the Digital Public Library of America, which needs a free key.
1. Make sure the `CONTACT_EMAIL` secret is set.
2. Actions tab → **Request a DPLA key** → Run workflow. DPLA emails you a key.
3. Add it as a new repository secret named `DPLA_API_KEY`.

## Make it yours

- **Sources:** edit `sources.json` on GitHub (click the file, then the pencil icon). Add any site (a feed address or just its homepage), any journal by ISSN, or any search term. Saving the file rebuilds the site.
- **Banner facts:** edit `site/data/factoids.json`. Each fact has a `year`, the `text`, and optionally a `month` and `day` (numbers) so it shows the exact date and leads the banner on its anniversary.
- **Daily figures:** edit `site/data/figures.json`. Give `born` and `died` as `"1879-10-07"` for an exact date (used for anniversaries) or as text like `"c. 1822"`.
- **Artwork of the day:** edit `site/data/art.json`. `wiki` is the Wikipedia article whose lead image is shown (only freely licensed images are used). Add `"related": "Hero Name"` to show a work on the day that hero appears, or `"noimage": true` when the article's picture is a portrait rather than the work.
- **Museum searches and art feeds:** edit `museums` and the `artnow` list in `sources.json`.
- **Texts and songs:** edit `site/data/texts.json` and `site/data/songs.json`.
- **Podcasts and YouTube:** add to `podcasts` or `videos` in `sources.json`. A YouTube channel can be given as its `https://www.youtube.com/@handle` address.
- **The canon:** edit `site/data/canon.json`. Each work has a `shelf` (theory, articles, fiction or poetry), a line on `why` it matters, `topics`, and `after`: titles it builds on, which power "because you read".
- **Your books:** replace `books/storygraph.csv` with a fresh StoryGraph export whenever you like.
- **Update times:** change the `cron` line in `.github/workflows/update.yml`. Times are in UTC.

## Good to know

- If a site blocks automated readers, the updater fetches that site's recent articles through Google News instead. Those headlines have no summary. If even that fails, the source is skipped and listed at the bottom of the page.
- GitHub pauses scheduled workflows in a repo that has had no activity for 60 days, and emails you first. Click **Enable workflow** on the Actions tab (or make any small edit) to resume.
- Headlines link out to the original publishers; some (FT-style paywalls aside) may limit free articles.
