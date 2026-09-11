// node --test tests/test_web_history.cjs (외부 패키지 없음)
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const web = resolve(__dirname, '../web');
const historyScript = readFileSync(resolve(web, 'session.js'), 'utf8');
const appScript = readFileSync(resolve(web, 'index.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const A = 'visitor_' + 'a'.repeat(32), B = 'visitor_' + 'b'.repeat(32);
const key = (user) => 'voltgo.history.v1:' + user;

async function startApp(stored, route) {
  const values = new Map(Object.entries(stored)), reads = [], requests = [], elements = new Map();
  const storage = { getItem(k) { reads.push(k); return values.get(k) ?? null; }, setItem(k, v) { values.set(k, v); } };
  const element = (selector) => {
    if (!elements.has(selector)) elements.set(selector, {
      innerHTML: '', textContent: '', hidden: ['#approval', '#info', '#chat'].includes(selector),
      style: { setProperty() {} }, addEventListener() {}, remove() {}, scrollHeight: 0,
      insertAdjacentHTML(_, html) { this.innerHTML += html; },
    });
    return elements.get(selector);
  };
  const context = vm.createContext({
    document: { querySelector: element, querySelectorAll: () => [], addEventListener() {} },
    localStorage: storage,
    VoltGoRouteMap: class { reset() {} hide() {} register() { return []; } },
    async fetch(path, options) {
      requests.push(path);
      const { status = 200, data } = route(path, options);
      return { ok: status < 400, status, json: async () => data };
    },
  });
  vm.runInContext('window = globalThis', context);
  vm.runInContext(historyScript, context);
  vm.runInContext(appScript, context);
  // start()는 실제 페이지와 같이 자동 실행된다. 비동기 요청을 끝까지 처리한다.
  for (let i = 0; i < 6; i++) await new Promise(setImmediate);
  return { context, values, reads, requests, elements };
}

test('새 브라우저 사용자는 다른 사용자의 저장 기록을 읽지 않는다', async () => {
  const app = await startApp({ [key(A)]: JSON.stringify({ thread_id: 'a-thread', entries: [{ me: 'A 비밀' }] }) },
    (path) => path === '/api/health' ? { data: { user_id: B } }
      : { status: 201, data: { thread_id: 'b-thread', session: { user_id: B } } });
  assert.deepEqual(app.reads, [key(B)]);
  assert.equal(JSON.parse(app.values.get(key(A))).entries[0].me, 'A 비밀');
  assert.equal(JSON.parse(app.values.get(key(B))).thread_id, 'b-thread');
  assert.equal(app.elements.get('#messages').innerHTML, '');
});

test('브라우저 기록의 대화 ID가 변조되어도 서버 소유권 확인 전에는 표시하지 않는다', async () => {
  const app = await startApp({ [key(B)]: JSON.stringify({ thread_id: 'a-thread', entries: [{ me: '보이면 안 되는 기록' }] }) },
    (path) => path === '/api/health' ? { data: { user_id: B } }
      : path.startsWith('/api/session?') ? { status: 404, data: { error: '대화를 찾을 수 없습니다' } }
      : { status: 201, data: { thread_id: 'b-thread', session: { user_id: B } } });
  assert(app.requests.includes('/api/session?thread_id=a-thread'));
  assert.equal(app.elements.get('#messages').innerHTML, '');
  assert.deepEqual(JSON.parse(app.values.get(key(B))).entries, []);
});

test('초기화 중 쿠키 사용자가 바뀌면 다른 사용자 ID 아래에 기록을 저장하지 않는다', async () => {
  const app = await startApp({}, (path) => path === '/api/health' ? { data: { user_id: A } }
    : { status: 201, data: { thread_id: 'b-thread', session: { user_id: B } } });
  assert.equal(app.values.has(key(A)), false);
  assert.equal(app.values.has(key(B)), false);
  assert.equal(app.elements.get('#messages').innerHTML, '');
  assert.match(app.elements.get('#carMain').textContent, /접속 세션이 바뀌었습니다/);
});

test('같은 사용자도 서버에서 대화를 확인한 뒤에만 자기 기록을 복원한다', async () => {
  const app = await startApp({ [key(A)]: JSON.stringify({ thread_id: 'a-thread', entries: [{ me: '내 기록' }] }) },
    (path) => path === '/api/health' ? { data: { user_id: A } }
      : { data: { thread_id: 'a-thread', session: { user_id: A }, request_id: null } });
  assert.deepEqual(app.requests, ['/api/health', '/api/session?thread_id=a-thread']);
  assert.match(app.elements.get('#messages').innerHTML, /내 기록/);
});


test('거절된 질문은 브라우저 영구 기록에 추가하지 않는다', async () => {
  const app = await startApp({}, (path) => path === '/api/health' ? { data: { user_id: A } }
    : path === '/api/ask' ? { status: 400, data: { error: '비밀값은 입력하지 마세요' } }
    : { status: 201, data: { thread_id: 'a-thread', session: { user_id: A } } });
  await vm.runInContext('send("sk-fake_test_token_only_1234567890")', app.context);
  assert.deepEqual(JSON.parse(app.values.get(key(A))).entries, []);
  assert.match(app.elements.get('#messages').innerHTML, /비밀값은 입력하지 마세요/);
});

test('승인 재전송 응답 뒤에도 새 승인 화면과 request_id를 유지한다', async () => {
  const pending = { status: 'awaiting_approval', request_id: 'old', message: '선호 저장 승인', candidates: [], warnings: [] };
  const next = { ...pending, request_id: 'new', message: '새 선호 저장 승인' };
  let posted;
  const app = await startApp({ [key(A)]: JSON.stringify({ thread_id: 'a-thread', entries: [] }) },
    (path, options) => {
      if (path === '/api/health') return { data: { user_id: A } };
      if (path === '/api/decide') {
        posted = JSON.parse(options.body);
        return { data: { thread_id: 'a-thread', session: { user_id: A }, request_id: 'new',
          response: { ...pending, status: 'ok', message: '이전 처리 결과' }, pending_response: next } };
      }
      return { data: { thread_id: 'a-thread', session: { user_id: A }, request_id: 'old', pending_response: pending } };
    });
  await vm.runInContext('decideNow("approve")', app.context);
  assert.equal(posted.request_id, 'old');
  assert.equal('approval_id' in posted, false);
  assert.equal(vm.runInContext('pendingRequestId', app.context), 'new');
  assert.equal(app.elements.get('#approval').hidden, false);
});
