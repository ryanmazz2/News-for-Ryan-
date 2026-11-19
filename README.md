# Twice Daily

A personal news site that rebuilds itself every morning and evening with:

- economics and politics headlines (Guardian, BBC, NPR, Al Jazeera, Phenomenal World)
- the socialist and labor press (Jacobin, Monthly Review, MR Online, Spectre, Tribune, Dissent, Labor Notes)
- the newest articles in Marxist and socialist journals (via Crossref)
- recent scholarship on Marx, Marxism and socialism from any venue (via OpenAlex)
- a rotating banner of labor and economic history, with anniversaries shown on the day

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

**Hidden folder note:** On a Mac, press Cmd+Shift+. in Finder to show the `.github` folder. If dragging it doesn't work, create the file by hand on GitHub: **Add file → Create new file**, type `.github/workflows/update.yml` as the name, and paste in the contents of that file.

## Make it yours

- **Sources:** edit `sources.json` on GitHub (click the file, then the pencil icon). Add any RSS feed, any journal by ISSN, or any search term. Saving the file rebuilds the site.
- **Your email:** replace `you@example.com` in `sources.json`. Crossref and OpenAlex ask for a contact address and give better service to requests that include one.
- **Banner facts:** edit `site/data/factoids.json`. Each fact has a `year`, the `text`, and optionally `md` (month-day like `"05-01"`) so it leads the banner on its anniversary.
- **Update times:** change the `cron` line in `.github/workflows/update.yml`. Times are in UTC.

## Good to know

- If a source is down or changes its feed address, the site skips it and lists it at the bottom of the page.
- GitHub pauses scheduled workflows in a repo that has had no activity for 60 days, and emails you first. Click **Enable workflow** on the Actions tab (or make any small edit) to resume.
- Headlines link out to the original publishers; some (FT-style paywalls aside) may limit free articles.
