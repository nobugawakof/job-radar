---
name: verify-dashboard
description: Start the Job Radar web dashboard against a throwaway data folder and check every tab in headless Chromium (desktop + phone width) for JavaScript errors and horizontal overflow, with screenshots. Use after any change to jobradar/web/index.html or the dashboard API.
---

# Check the dashboard in a real browser

Unit tests don't run the page's JavaScript. A single bad value in `/api/state`
once blanked the whole page ("Disconnected"), so after every UI or API change,
run the dashboard in a browser.

```bash
W=$(mktemp -d) && mkdir -p $W/jobradar-data && : > $W/jobradar-data/config.toml
python3 -m jobradar.cli --config $W/jobradar-data/config.toml web --no-browser --port 8794 > $W/log 2>&1 &
sleep 2 && node .claude/skills/verify-dashboard/check.js $W 8794
pkill -f "port 8794"
```

`check.js` does the following:
- reads the token from `$W/jobradar-data/jobradar-web.token`;
- opens every `nav button[data-tab]` at 1280×860 and at 390×844;
- prints JS errors and whether the phone layout overflows horizontally;
- saves `$W/<tab>-<size>.png`.

It should end with `JS errors: none` and every `overflow` false. Look at the
screenshots of the tab you changed (Read the PNG).

To test a feature that needs data, put the data in before starting the server:
```bash
python3 -c "from jobradar.state import State; s=State('$W/jobradar-data/jobradar.db'); c=s.conversation_id('telegram','@hr_anna',display_name='Anna Li'); s.add_message(c,'in','你好！',external_id='1',unread=True)"
```

Notes:
- Playwright and Chromium are preinstalled; don't run `playwright install`.
- If the shell refuses a heredoc containing JS `${...}`, write the script with
  the Write tool instead.
