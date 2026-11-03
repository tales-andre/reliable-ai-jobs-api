// Screenshots of local HTML files with a headless Edge/Chromium started by this script.
// Usage: node scripts/capture.js <file-or-url> <out.png> [width] [height|full] [selector]
// Needs playwright-core (npm i playwright-core) and a local Edge or Chrome (set BROWSER_PATH).
const path = require('path');
const { chromium } = require('playwright-core');

(async () => {
  const [src, out, width = '1200', height = 'full', selector] = process.argv.slice(2);
  const executablePath = process.env.BROWSER_PATH ||
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
  const browser = await chromium.launch({ executablePath, headless: true });
  const page = await browser.newPage({
    viewport: { width: Number(width), height: height === 'full' ? 900 : Number(height) },
    deviceScaleFactor: 1,
  });
  const url = /^https?:/.test(src) ? src : 'file:///' + path.resolve(src).split(path.sep).join('/');
  await page.goto(url, { waitUntil: 'load' });
  if (selector) await page.locator(selector).first().screenshot({ path: out });
  else await page.screenshot({ path: out, fullPage: height === 'full' });
  await browser.close();
  console.log('wrote', out);
})();
