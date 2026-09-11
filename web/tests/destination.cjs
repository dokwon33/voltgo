// Confirmed destination actions; API responses are fixtures, with no model or map API calls.
const { chromium } = require('@playwright/test');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}, reducedMotion: 'reduce'});
    const errors = [], asks = [];
    page.on('pageerror', e => errors.push(e.message));
    const now = '2026-09-11T10:48:00+09:00';
    const candidate = (id, name) => ({plan_id: id, poi_id: id, name, version: 1, category: 'cafe',
      outbound_sec: 240, inbound_sec: 300, dwell_sec: 600, slack_sec: 600, total_sec: 1140,
      return_at: '2026-09-11T11:07:00+09:00', leave_by: '2026-09-11T11:10:00+09:00',
      evaluated_at: now, poi_source: 'mock', route_source: 'mock', opening_status: 'unknown'});
    const a = candidate('A', '잠깐의 커피'), b = candidate('B', '충전소 옆 작은 골목 테이크아웃 카페');
    const session = {now, condition_version: 1, candidates: [], confirmed: null, origin: '출발 충전소'};
    let threadCount = 0;
    const envelope = response => ({session, instance_id: 'destination-test', exists: true, response,
      map_data: {origin: {latitude: 37.5, longitude: 127.03},
        places: [a, b].map((c, i) => ({poi_id: c.poi_id, latitude: 37.501 + i / 1000, longitude: 127.032})), routes: []}});
    await page.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.hostname !== 'voltgo.test') return route.abort();
      if (u.pathname === '/api/health') return route.fulfill({json: {clock: 'fixed', instance_id: 'destination-test'}});
      if (u.pathname === '/api/ask') {
        const body = route.request().postDataJSON(); asks.push(body);
        session.candidates = [a, b];
        const selected = body.selection && session.candidates.find(c => c.plan_id === body.selection.id);
        session.confirmed = selected ? {plan_id: selected.plan_id, version: 1} : null;
        return route.fulfill({json: envelope({status: selected ? 'confirmed' : 'ok', message: selected ? '목적지를 정했어요.' : '다녀올 수 있는 곳이에요.', candidates: selected ? [selected] : [a, b]})});
      }
      if (u.pathname === '/api/session') return route.fulfill({json: {...envelope(null), thread_id: route.request().method() === 'POST' ? 'destination-' + (++threadCount) : u.searchParams.get('thread_id')}});
      const file = u.pathname === '/' ? '/index.html' : u.pathname;
      const type = {'.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript', '.png': 'image/png', '.woff2': 'font/woff2'}[path.extname(file)];
      try { return route.fulfill({contentType: type || 'application/octet-stream', body: await fs.readFile(root + file)}); }
      catch { return route.fulfill({status: 404, body: ''}); }
    });
    await page.goto('https://voltgo.test');
    await page.waitForFunction(() => Boolean(threadId));
    await page.locator('#input').fill('카페 추천해줘'); await page.locator('#send').click();
    await page.locator('[data-plan-id="A"]').click();
    await page.waitForFunction(() => !busy && document.querySelector('.plan.done'));
    const done = page.locator('.plan.done');
    assert.match(await done.innerText(), /현재 목적지.*잠깐의 커피/s);
    const cta = await done.locator('.destination-map').boundingBox();
    assert.ok(cta.height >= 48 && cta.width > 220);
    const before = asks.length;
    await done.locator('[data-change-destination]').click();
    assert.match(await page.locator('.destination-current').innerText(), /잠깐의 커피/);
    await page.locator('#keepDestination').click();
    await page.waitForFunction(() => !document.querySelector('#featureDialog').open);
    assert.equal(asks.length, before, 'keeping the destination must not call the agent');
    await done.locator('[data-change-destination]').click();
    await page.locator('[data-destination-plan="B"]').click();
    await page.waitForFunction(() => !busy && document.querySelector('.plan.done')?.dataset.confirmedPlan === 'B');
    assert.equal(asks.at(-1).thread_id, asks[0].thread_id);
    assert.deepEqual(asks.at(-1).selection, {kind: 'plan', id: 'B', version: 1, evaluated_at: now});
    assert.equal(await page.locator('.plan.done').count(), 1);
    assert.equal(await page.locator('.plan.done .plan-label').innerText(), 'B', 'confirmation must retain recommendation label');
    assert.match(await page.locator('[data-confirmed-plan="A"]').innerText(), /이전에 선택한 목적지/);
    assert.equal(await page.locator('[data-confirmed-plan="A"] [data-change-destination]').isVisible(), false);
    await done.locator('.destination-map').click();
    assert.equal(await page.locator('.map-place').innerText(), 'B · ' + b.name);
    await page.locator('.map-back').click();
    await page.locator('[data-confirmed-plan="A"] .map-open').click();
    await page.locator('#mapBtn').click();
    assert.equal(await page.locator('.map-place').innerText(), 'B · ' + b.name, 'header map must prefer the confirmed destination');
    assert.equal(asks.length, before + 1, 'opening maps must not call the agent');
    await page.evaluate(() => navigateTo('chat'));
    await done.scrollIntoViewIfNeeded();
    await page.screenshot({path: '/tmp/voltgo-confirmed-destination.png', fullPage: true});
    await done.locator('[data-change-destination]').click();
    await page.setViewportSize({width: 320, height: 740});
    assert.equal(await page.locator('#featureDialog').evaluate(e => e.scrollWidth > e.clientWidth), false);
    await page.screenshot({path: '/tmp/voltgo-change-destination.png', fullPage: true});
    // Expired alternatives cannot be picked; a fresh search remains available.
    await page.evaluate(() => { session.now = '2026-09-11T10:54:00+09:00'; updatePlanButtons(); });
    assert.equal(await page.locator('[data-destination-plan="A"]').isDisabled(), true);
    await page.locator('#destinationForm [name=request]').fill('더 가까운 곳');
    await page.locator('#destinationForm button').click();
    await page.waitForFunction(() => !busy && !document.querySelector('#featureDialog').open);
    assert.equal(asks.at(-1).selection, null);
    assert.match(asks.at(-1).text, /활동 종류는 유지.*더 가까운 곳.*고르기 전에는 새 목적지를 확정하지 마/s);
    assert.equal(await page.locator('.plan.done').count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    // Historical confirmations stay viewable but cannot change the current plan.
    await page.evaluate(() => { archived = true; syncFeatures(); });
    assert.equal(await page.locator('[data-change-destination]:visible').count(), 0);
    assert.deepEqual(errors, []);
    console.log('PASS: confirmed destination CTA, keep without API, change selection metadata, current map priority, stale options, consent before search, archive and 320px layout.');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
