// Run: NODE_PATH=$(npm root -g) node web/tests/home-character.cjs
const {chromium} = require('@playwright/test');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}});
    const errors = [], missing = [], sessions = new Map();
    let selectedCategory = 'meal', calls = 0;
    const now = '2026-09-11T12:00:00+09:00';
    const candidate = category => ({
      plan_id: category, poi_id: category, version: 1, category, name: category + ' 추천 장소',
      outbound_sec: 180, inbound_sec: 180, dwell_sec: 600, total_sec: 960, slack_sec: 600,
      return_at: '2026-09-11T12:16:00+09:00', leave_by: '2026-09-11T12:13:00+09:00',
      evaluated_at: now, route_summary: '가까운 산책길', opening_status: 'open', poi_source: 'mock', route_source: 'mock',
    });
    const fresh = () => ({now, condition_version: 1, confirmed: null, candidates: []});
    page.on('pageerror', error => errors.push(error.message));
    let threadCount = 0;
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.hostname !== 'voltgo.test') return route.abort();
      if (url.pathname === '/api/health') return route.fulfill({json: {user_id: 'character-test', clock: 'fixed', instance_id: 'one'}});
      if (url.pathname.startsWith('/api/')) {
        calls++;
        const body = route.request().postDataJSON();
        const isNew = url.pathname === '/api/session' && route.request().method() === 'POST';
        const id = isNew ? 'character-' + (++threadCount) : (body?.thread_id || url.searchParams.get('thread_id'));
        if (!sessions.has(id)) sessions.set(id, fresh());
        const session = sessions.get(id);
        let response = null;
        if (url.pathname === '/api/ask') {
          if (body.selection?.kind === 'plan') {
            session.confirmed = {plan_id: body.selection.id, version: 1, confirmed_at: now};
            response = {status: 'confirmed', candidates: session.candidates, message: '계획을 확정했어요.'};
          } else {
            session.confirmed = null;
            // A different category is deliberately first: use the selected plan, not candidates[0].
            session.candidates = [candidate(selectedCategory === 'meal' ? 'cafe' : 'meal'), candidate(selectedCategory)];
            response = {status: 'ok', candidates: session.candidates, message: '마음에 드는 곳을 골라 주세요.'};
          }
        }
        return route.fulfill({json: {session, response, thread_id: id, instance_id: 'one', pending_approval: null, exists: true}});
      }
      const file = url.pathname === '/' ? '/index.html' : url.pathname;
      try {
        return route.fulfill({contentType: {'.js':'application/javascript', '.css':'text/css', '.html':'text/html', '.png':'image/png', '.woff2':'font/woff2'}[path.extname(file)] || 'application/octet-stream', body: await fs.readFile(root + file)});
      } catch {
        missing.push(file); return route.fulfill({status: 404, body: ''});
      }
    });
    await page.goto('https://voltgo.test');
    await page.waitForFunction(() => !!threadId);
    assert.equal(await page.locator('#homeVolty').getAttribute('data-activity'), 'hello');
    await page.evaluate(() => { window.sharedHomeShadow = document.querySelector('.volty-ground-shadow'); });
    assert.equal(await page.locator('.volty-ground-shadow').count(), 1);
    await page.screenshot({path: '/tmp/voltgo-home-hello-smooth.png', fullPage: true});
    const captions = new Set();
    for (const [category, text, line] of [
      ['meal', '간단히 밥 먹고 오고 싶어', '든든하게 한 끼'],
      ['cafe', '커피 한잔 하고 싶어', '커피 한 모금'],
      ['convenience', '편의점에 다녀오고 싶어', '편의점에 다녀와요'],
      ['mart', '마트에서 장 보고 싶어', '쏙쏙 담아볼까요'],
    ]) {
      selectedCategory = category;
      await page.locator(`#intents [data-text="${text}"]`).click();
      await page.waitForFunction(() => !busy && !!lastRes);
      assert.equal(await page.locator('#homeVolty').getAttribute('data-activity'), 'hello', 'a recommendation is not a confirmation');
      await page.locator(`button[data-plan-id="${category}"]`).last().click();
      await page.waitForFunction(() => !busy && session.confirmed);
      const thread = await page.evaluate(() => threadId), beforeHome = calls;
      await page.locator('#brandHome').click();
      await page.locator('.volty-activity').waitFor({state: 'visible'});
      assert.equal(await page.evaluate(() => threadId), thread, 'home navigation preserves the confirmed thread');
      assert.equal(calls, beforeHome, 'home navigation does not send a new request');
      assert.equal(await page.locator('#homeVolty').getAttribute('data-activity'), category);
      assert.equal(await page.locator('#homeVolty > svg').isVisible(), false);
      assert.equal(await page.locator('.volty-ambient').isVisible(), true);
      assert.equal(await page.locator('.volty-ground-shadow').isVisible(), true);
      assert.equal(await page.evaluate(() => window.sharedHomeShadow === document.querySelector('.volty-ground-shadow')), true);
      assert.equal(await page.locator('.volty-ground-shadow image').getAttribute('href'), 'img/volty_hello.png');
      assert.equal(await page.locator('.volty-activity-glitter').evaluate(el => getComputedStyle(el).visibility), 'visible');
      assert.equal(await page.locator('.volty-activity').evaluate(el => getComputedStyle(el).mixBlendMode), 'multiply');
      assert.ok((await page.locator('.hero-say').innerText()).includes(line));
      captions.add(await page.locator('.hero .hand').innerText());
      // Every pose is addressable, including row changes and the last → first seam.
      const motion = await page.locator('.volty-activity-sprite').evaluate(el => {
        const animations = el.getAnimations({subtree: true});
        const frames = animations.find(a => a.animationName === 'volty-activity-frames');
        const next = animations.find(a => a.animationName === 'volty-activity-next');
        const blend = animations.find(a => a.animationName === 'volty-frame-blend');
        animations.forEach(a => a.pause());
        const duration = frames.effect.getTiming().duration;
        const positions = Array.from({length: 16}, (_, index) => {
          // Sample inside each step; exact boundaries can round into the preceding frame.
          frames.currentTime = duration * (index + .5) / 16;
          return getComputedStyle(el).backgroundPosition;
        });
        next.currentTime = duration * 15.5 / 16;
        const seam = getComputedStyle(el, '::after').backgroundPosition;
        const opacities = [0, .5, .99].map(fraction => {
          blend.currentTime = duration / 16 * fraction;
          return Number(getComputedStyle(el, '::after').opacity);
        });
        return {positions, seam, opacities};
      });
      assert.equal(new Set(motion.positions).size, 16);
      assert.equal(motion.seam, motion.positions[0], 'last pose blends back into the first');
      assert.ok(motion.opacities[0] < .01 && motion.opacities[1] > .45 && motion.opacities[1] < .55 && motion.opacities[2] > .98);
      await page.locator('#homeVolty').click();
      assert.equal(await page.locator('.volty-activity-sprite').evaluate(el => el.getAnimations({subtree: true}).every(a => a.playState === 'running')), true);
      const glitter = await page.locator('.volty-glitter').evaluateAll(elements => elements.map(el => {
        const a = el.getAnimations()[0];
        const timing = a.effect.getTiming();
        a.pause(); a.currentTime = -timing.delay;
        const before = getComputedStyle(el).transform;
        a.currentTime = timing.duration * .45 - timing.delay;
        const after = getComputedStyle(el).transform;
        a.play();
        return {before, after, duration: timing.duration, delay: timing.delay};
      }));
      assert.equal(glitter.length, 5);
      assert.ok(glitter.every(light => light.before !== light.after));
      assert.equal(new Set(glitter.map(light => light.delay)).size, 5, 'lights do not blink in unison');
      await page.setViewportSize({width: 320, height: 844});
      await page.locator('#homeVolty').blur();
      await page.screenshot({path: `/tmp/voltgo-home-${category}.png`, fullPage: true});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.emulateMedia({reducedMotion: 'reduce'});
      assert.equal(await page.locator('#homeVolty').evaluate(el => el.getAnimations({subtree: true}).length), 0);
      await page.emulateMedia({reducedMotion: 'no-preference'});
      await page.setViewportSize({width: 390, height: 844});
    }
    assert.equal(captions.size, 4);
    // Invalidated confirmations, preference approvals and unknown categories cannot leave stale art.
    await page.evaluate(() => homeCharacter.render({...session, condition_version: 2}));
    assert.equal(await page.locator('#homeVolty').getAttribute('data-activity'), 'hello');
    await page.evaluate(() => homeCharacter.render({confirmed: null}, {status: 'awaiting_approval', candidates: [{category: 'cafe'}]}));
    assert.equal(await page.locator('#homeVolty').getAttribute('data-activity'), 'hello');
    await page.evaluate(() => homeCharacter.render({...session, candidates: [{...session.candidates[1], category: 'unknown'}]}));
    assert.equal(await page.locator('#homeVolty').getAttribute('data-activity'), 'hello');
    // A pending image load cannot reappear after resetting the plan.
    await page.evaluate(() => {homeCharacter.render(session); homeCharacter.render({});});
    await page.waitForTimeout(100);
    assert.equal(await page.locator('.volty-activity').isVisible(), false);
    await page.evaluate(() => homeCharacter.render(session));
    await page.locator('.volty-activity').waitFor({state: 'visible'});
    await page.locator('#home .history-open').click();
    await page.locator('#newConversation').click();
    await page.waitForFunction(() => !session.confirmed && !document.querySelector('#home').hidden);
    assert.equal(await page.locator('#homeVolty').getAttribute('data-activity'), 'hello');
    assert.match(await page.locator('.hero-say').innerText(), /잠깐 쉬어갈까요/);
    assert.equal(await page.locator('.volty-ground-shadow').count(), 1);
    assert.equal(await page.locator('.volty-activity-glitter').evaluate(el => getComputedStyle(el).visibility), 'visible');
    assert.deepEqual(errors, []); assert.deepEqual(missing, []);
    console.log('PASS: four confirmed activities, selected-plan matching, home/thread preservation, sixteen action frames, continuous blending/loop seam, independent glitter, distinct messages, replay, mobile, reduced motion, invalidation, async reset and new conversation.');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
