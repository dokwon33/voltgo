// node --test web/tests/runtime.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {chromium} = require('@playwright/test');
const fs = require('node:fs/promises');
const path = require('node:path');

async function withPage(run, respond) {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({reducedMotion: 'reduce'});
    const errors = [], requests = [];
    page.on('pageerror', e => errors.push(e.message));
    const now = '2026-09-11T14:00:00+09:00';
    const base = {thread_id: 'runtime', instance_id: 'runtime-server', session: {
      now, user_id: 'runtime-user', condition_version: 1, candidates: [],
      charging: {charging: true, soc_pct: 40, target_soc_pct: 80},
    }};
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.hostname !== 'voltgo.test') return route.abort();
      if (url.pathname === '/api/health') return route.fulfill({json: {
        user_id: 'runtime-user', instance_id: base.instance_id, clock: 'fixed',
      }});
      if (url.pathname.startsWith('/api/')) {
        const request = {path: url.pathname, method: route.request().method(), body: route.request().postDataJSON()};
        requests.push(request);
        return route.fulfill(respond(request, base));
      }
      const file = url.pathname === '/' ? '/index.html' : url.pathname;
      const types = {'.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.png': 'image/png', '.woff2': 'font/woff2'};
      return route.fulfill({contentType: types[path.extname(file)], body: await fs.readFile(path.join(__dirname, '..', file))});
    });
    await page.goto('https://voltgo.test');
    await page.waitForFunction(() => threadId === 'runtime');
    await run(page, requests);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
}

test('거부한 질문은 현재 UI의 대화 기록과 localStorage에 저장하지 않는다', async () => {
  const secret = 'sk-fake_test_token_only_1234567890';
  await withPage(async page => {
    await page.locator('#input').fill(secret);
    await page.locator('#send').click();
    await page.waitForFunction(() => !busy && timeline.some(t => t.role === 'error'));
    assert.equal(await page.evaluate(() => timeline.some(t => t.role === 'user')), false);
    const stored = await page.evaluate(() => JSON.stringify(localStorage));
    assert.equal(stored.includes(secret), false);
    await page.reload();
    await page.waitForFunction(() => threadId === 'runtime');
    assert.equal((await page.locator('body').innerText()).includes(secret), false);
  }, (request, base) => request.path === '/api/ask'
    ? {status: 400, json: {error: '비밀값은 입력하지 마세요'}} : {json: base});
});

test('이전 승인 응답을 받아도 현재 요청 ID와 새 승인 내용을 복원한다', async () => {
  const pending = id => ({request_id: id, expires_at: '2026-09-11T14:02:00+09:00',
    actions: [{name: 'save_preferences', args: {category: id === 'old' ? 'cafe' : 'mart', dwell_min: 15}}]});
  const prompt = id => ({status: 'awaiting_approval', request_id: id, message: '선호를 저장할까요?', candidates: []});
  await withPage(async (page, requests) => {
    await page.locator('#input').fill('카페 취향 기억해줘');
    await page.locator('#send').click();
    await page.waitForFunction(() => !busy && !document.querySelector('#approval').hidden);
    await page.locator('#approve').click();
    await page.waitForFunction(() => !busy && pendingApproval?.request_id === 'new');
    const posted = requests.find(r => r.path === '/api/decide').body;
    assert.equal(posted.request_id, 'old');
    assert.equal('approval_id' in posted, false);
    assert.equal(await page.locator('#approval').isVisible(), true);
    assert.match(await page.locator('#apBody').innerText(), /마트/);
    await page.reload();
    await page.waitForFunction(() => threadId === 'runtime');
    await page.locator('#home .history-open').click();
    await page.locator('[data-thread="runtime"]').click();
    await page.waitForFunction(() => !busy && !document.querySelector('#approval').hidden);
    assert.equal(await page.evaluate(() => pendingApproval.request_id), 'new');
    assert.match(await page.locator('#apBody').innerText(), /마트/);
  }, (request, base) => {
    if (request.path === '/api/ask') return {json: {...base, response: prompt('old'), pending_response: prompt('old'), pending_approval: pending('old')}};
    if (request.path === '/api/decide') return {json: {...base,
      response: {status: 'ok', request_id: 'old', message: '이전 처리 결과', candidates: []},
      pending_response: prompt('new'), pending_approval: pending('new')}};
    return {json: request.method === 'GET' ? {...base, pending_response: prompt('new'), pending_approval: pending('new')} : base};
  });
});
