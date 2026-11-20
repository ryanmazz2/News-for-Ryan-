# Twice Daily

A personal news site that rebuilds itself every morning and evening with:

- economics and politics headlines (Guardian, BBC, NPR, Al Jazeera, Phenomenal World)
- the US and UK left press (Jacobin, The Nation, In These Times, Democracy Now!, Tribune, Novara and more)
- the global left (Le Monde diplomatique, Peoples Dispatch, Tricontinental, Africa Is a Country, NACLA, The Wire and more)
- the newest articles in Marxist and socialist journals (via Crossref)
- recent scholarship on Marx, Marxism and socialism from any venue (via OpenAlex)
- a rotating banner of dated events in working-class, socialist and anti-colonial history, with anniversaries shown on the day
- a hero from history each day, with a photo from Wikimedia Commons, chosen by birthday or anniversary when there is one
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

## Make it yours

- **Sources:** edit `sources.json` on GitHub (click the file, then the pencil icon). Add any site (a feed address or just its homepage), any journal by ISSN, or any search term. Saving the file rebuilds the site.
- **Banner facts:** edit `site/data/factoids.json`. Each fact has a `year`, the `text`, and optionally a `month` and `day` (numbers) so it shows the exact date and leads the banner on its anniversary.
- **Daily figures:** edit `site/data/figures.json`. Give `born` and `died` as `"1879-10-07"` for an exact date (used for anniversaries) or as text like `"c. 1822"`.
- **Artwork of the day:** edit `site/data/art.json`. `wiki` is the Wikipedia article whose lead image is shown (only freely licensed images are used). Add `"related": "Hero Name"` to show a work on the day that hero appears, or `"noimage": true` when the article's picture is a portrait rather than the work.
- **Museum searches and art feeds:** edit `museums` and the `artnow` list in `sources.json`.
- **Update times:** change the `cron` line in `.github/workflows/update.yml`. Times are in UTC.

## Good to know

- If a site blocks automated readers, the updater fetches that site's recent articles through Google News instead. Those headlines have no summary. If even that fails, the source is skipped and listed at the bottom of the page.
- GitHub pauses scheduled workflows in a repo that has had no activity for 60 days, and emails you first. Click **Enable workflow** on the Actions tab (or make any small edit) to resume.
- Headlines link out to the original publishers; some (FT-style paywalls aside) may limit free articles.
