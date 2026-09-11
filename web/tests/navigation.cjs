const {chromium} = require('@playwright/test');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
(async () => {
 const browser = await chromium.launch({headless:true});
 try {
  const page = await browser.newPage({viewport:{width:390,height:844},reducedMotion:'reduce'});
  page.setDefaultTimeout(12000);
  const errors = []; page.on('pageerror', e => {errors.push(e.message); console.error(e.message);});
  const now = '2026-09-11T14:00:00+09:00';
  const candidate = id => ({plan_id:id,poi_id:id,name:'카페 '+id,category:'cafe',version:1,evaluated_at:now,outbound_sec:240,inbound_sec:300,dwell_sec:600,slack_sec:600,leave_by:'2026-09-11T14:25:00+09:00',return_at:'2026-09-11T14:20:00+09:00',poi_source:'mock',route_source:'mock'});
  const candidates = [candidate('A'),candidate('B')];
  let pending = null, nextApproval = false, release = null, delay = false, decisions = 0;
  const session = {now,condition_version:1,candidates,charging:{charging:true,soc_pct:40,target_soc_pct:80,observed_at:now,reported_remaining_sec:2400}};
  let threadCount = 0;
  await page.route('**/*', async route => {
   const url = new URL(route.request().url());
   if (url.hostname !== 'voltgo.test') return route.abort();
   if (url.pathname === '/api/health') return route.fulfill({json:{clock:'fixed',user_id:'navigation',instance_id:'server'}});
   if (url.pathname.startsWith('/api/')) {
    let response = null;
    if (url.pathname === '/api/ask') {
     if (delay) await new Promise(resolve=>{release=resolve;});
     if (nextApproval) {
      pending={request_id:'request-1',expires_at:'2026-09-11T14:02:00+09:00',actions:[{name:'save_preferences',args:{category:'cafe'}}]};
      response={status:'awaiting_approval',message:'선호를 저장할까요?',candidates:[]}; nextApproval=false;
     } else response={status:'ok',message:'추천했어요.',candidates};
    }
    if (url.pathname === '/api/decide') { decisions++;pending=null;response={status:'ok',message:'처리했어요.',candidates:[]}; }
    const thread_id = url.pathname === '/api/session' && route.request().method() === 'POST' ? 'nav-' + (++threadCount) : undefined;
    return route.fulfill({json:{session,thread_id,instance_id:'server',exists:true,response,pending_approval:pending}});
   }
   const file = url.pathname === '/' ? '/index.html' : url.pathname;
   return route.fulfill({contentType:{'.html':'text/html','.js':'application/javascript','.css':'text/css','.png':'image/png','.woff2':'font/woff2'}[path.extname(file)],body:await fs.readFile(root+file)});
  });
  const view = async name => page.waitForFunction(name=>history.state?.view===name,name);
  const back = async name => {await page.goBack();await view(name);};
  const submit = async text => {await page.locator('#input').fill(text);await page.locator('#send').click();await page.waitForFunction(()=>!busy);};
  await page.goto('https://voltgo.test');await page.waitForFunction(()=>serverInstance==='server');
  const initialId=await page.evaluate(()=>threadId), initialLength=await page.evaluate(()=>history.length);
  await page.locator('#brandHome').click();await page.locator('#brandHome').press('Enter');
  assert.equal(await page.evaluate(()=>threadId),initialId);assert.equal(await page.evaluate(()=>history.length),initialLength);
  await page.locator('#carBtn').click();await view('car');await page.keyboard.press('Escape');await view('home');
  await page.goForward();await view('car');assert.equal(await page.locator('#home').isVisible(),true);
  await page.locator('#carClose').click();await view('home');
  await page.evaluate(() => setMood('error'));
  const turnsBeforeMap = await page.locator('#messages').innerHTML();
  await page.locator('#mapBtn').click();await view('map');
  assert.equal(await page.locator('.chat-top').isVisible(),false,'old conversation errors must not appear on the map');
  assert.equal(await page.locator('#messages').isVisible(),false);
  assert.equal(await page.locator('#composer').isVisible(),false);
  assert.equal(await page.locator('.map-controls').isVisible(),false,'no route controls before a place is available');
  assert.equal(await page.locator('.map-start').isVisible(),true);
  assert.equal(await page.locator('#messages').innerHTML(),turnsBeforeMap,'map navigation must not add an error turn');
  await page.screenshot({path:'/tmp/voltgo-empty-map-after.png',fullPage:true});
  await page.locator('.map-start').click();await view('home');
  await page.locator('#mapBtn').click();await view('map');await page.locator('#backBtn').click();await view('home');
  await submit('카페 추천해줘');await view('chat');
  await page.locator('[data-map-key]').first().click();await view('map');
  const firstKey=await page.evaluate(()=>routeMap.selected);
  const mapDepth=await page.evaluate(()=>history.state.depth);
  await page.locator('[data-map-choice]').nth(1).click();
  assert.equal(await page.evaluate(()=>history.state.depth),mapDepth);
  const secondKey=await page.evaluate(()=>routeMap.selected);assert.notEqual(firstKey,secondKey);
  await page.locator('#carBtn').click();await page.locator('#carClose').click();await view('map');
  assert.equal(await page.evaluate(()=>routeMap.selected),secondKey);
  await page.goForward();await view('car');await back('map');
  assert.equal(await page.locator('#carDialog').isVisible(),false);
  await page.locator('#chat .history-open').click();await view('panel');await page.keyboard.press('Escape');await view('map');
  assert.equal(await page.evaluate(()=>history.state.mapKey),secondKey);
  await page.locator('#infoBtn').click();await view('info');await back('map');assert.equal(await page.locator('#info').isVisible(),false);
  await page.goForward();await view('info');await page.keyboard.press('Escape');await view('map');
  await page.locator('#brandHome').click();await view('home');
  assert.equal(await page.evaluate(()=>threadId),initialId);assert.equal(await page.locator('#messages .me').count(),1);
  await back('map');assert.equal(await page.evaluate(()=>routeMap.selected),secondKey);
  await page.locator('.map-back').click();await view('chat');
  assert.equal(await page.locator('#messages').isVisible(),true);
  assert.equal(await page.locator('#composer').isVisible(),true);
  assert.equal(await page.locator('.chat-top').isVisible(),true);
  assert.equal(await page.locator('#messages .me').count(),1);
  await page.locator('#conditionsBtn').click();await view('panel');
  await page.locator('[name=dwell]').fill('15');await page.locator('#conditionForm button').click();await page.waitForFunction(()=>!busy && history.state.view==='chat');
  await page.locator('#backBtn').click();await view('home'); // Closing/submitting the sheet didn't add a duplicate chat entry.
  await page.locator('#home .history-open').click();await page.locator('.history-item').first().click();await page.waitForFunction(()=>!busy && history.state.view==='chat');
  assert.equal(await page.evaluate(()=>history.state.depth),0);
  await page.locator('#backBtn').click();await view('home');assert.equal(page.url(),'https://voltgo.test/');
  // The logo remains usable while a reply is pending; completion must not reopen chat/map.
  delay=true;nextApproval=true;
  await page.locator('#input').fill('카페 취향 기억해줘');await page.locator('#send').click();
  await page.waitForFunction(()=>busy);await page.locator('#brandHome').click();await view('home');
  while(!release) await new Promise(resolve=>setTimeout(resolve,10));
  delay=false;release();await page.waitForFunction(()=>!busy);
  await view('home');assert.equal(await page.locator('#approval').isVisible(),false);
  assert.equal(await page.locator('#pendingReview').isVisible(),true);
  await page.locator('#pendingReview').click();await view('approval');await back('home');
  assert.equal(decisions,0);assert.equal(await page.locator('#pendingReview').isVisible(),true);
  await page.locator('#pendingReview').click();await page.locator('#approve').click();await page.waitForFunction(()=>!busy && !pendingApproval);
  assert.equal(decisions,1);await view('home');
  await page.goForward();await view('home');assert.equal(await page.locator('#approval').isVisible(),false);
  // New conversation starts a fresh navigation scope, independent of older thread entries.
  await page.locator('#home .history-open').click();await page.locator('#newConversation').click();await view('home');
  await page.waitForFunction(()=>!!threadId);const newId=await page.evaluate(()=>threadId);assert.notEqual(newId,initialId);
  await back('home');assert.equal(await page.evaluate(()=>threadId),newId);
  assert.deepEqual(errors,[]);
  console.log('PASS: clickable logo/no reset, repeat home, home-map back, map selection, dialog close/Esc/back/forward, no duplicate submit history, restored root back, late reply, approval dismissal/review, new-thread scope.');
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
