// Usage: node check.js <workdir> [port]
// Opens every dashboard tab on desktop and phone sizes; reports JS errors and
// horizontal overflow; writes <workdir>/<tab>-<size>.png.
const fs = require('fs');
const { chromium } = require('playwright');

(async () => {
  const W = process.argv[2];
  const port = process.argv[3] || '8794';
  const tok = fs.readFileSync(W + '/jobradar-data/jobradar-web.token', 'utf8').trim();
  const browser = await chromium.launch();
  const errors = [];
  for (const [size, viewport] of [['desktop', { width: 1280, height: 860 }], ['phone', { width: 390, height: 844 }]]) {
    const page = await (await browser.newContext({ viewport })).newPage();
    page.on('pageerror', (e) => errors.push(size + ': ' + e.message));
    page.on('console', (m) => { if (m.type() === 'error') errors.push(size + ' console: ' + m.text()); });
    await page.goto('http://127.0.0.1:' + port + '/?t=' + tok);
    await page.waitForFunction(() => document.querySelector('#status-text').textContent !== 'Loading…');
    const tabs = await page.$$eval('nav button[data-tab]', (b) => b.map((x) => x.dataset.tab));
    for (const tab of tabs) {
      const btn = await page.$('nav button[data-tab=' + tab + ']');
      if (!(await btn.isVisible())) continue;
      await btn.click();
      await page.waitForTimeout(250);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      console.log(size, tab, 'overflow:', overflow);
      await page.screenshot({ path: W + '/' + tab + '-' + size + '.png' });
    }
  }
  console.log('JS errors:', errors.length ? errors : 'none');
  await browser.close();
})();
