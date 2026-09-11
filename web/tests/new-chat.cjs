const {chromium}=require('@playwright/test');
const fs=require('node:fs/promises');
const path=require('node:path');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:390,height:844},reducedMotion:'reduce'});
  page.setDefaultTimeout(12000);
  const errors=[], asks=[];page.on('pageerror',e=>errors.push(e.message));
  const root=path.resolve(__dirname,'..');
  const snapshot={now:'2026-09-11T14:00:00+09:00',condition_version:1,candidates:[],charging:{charging:true,soc_pct:40,target_soc_pct:80}};
  let releaseInitial, threadCount=0;
  await page.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(url.hostname!=='voltgo.test')return route.abort();
   if(url.pathname==='/api/health')return route.fulfill({json:{user_id:'new-chat',instance_id:'server',clock:'fixed'}});
   if(url.pathname==='/api/session'){
    if(route.request().method()!=='POST')return route.fulfill({json:{instance_id:'server',session:snapshot,thread_id:url.searchParams.get('thread_id')}});
    const id='chat-'+(++threadCount);
    if(threadCount===1)await new Promise(resolve=>{releaseInitial=resolve;});   // 첫 대화의 세션 응답만 늦게 온다
    return route.fulfill({json:{instance_id:'server',session:snapshot,thread_id:id}});
   }
   if(url.pathname==='/api/ask'){
    const body=route.request().postDataJSON();asks.push(body);
    return route.fulfill({json:{instance_id:'server',session:{...snapshot,condition_version:7},response:{status:'need_input',message:'어떤 활동을 원하세요?',candidates:[]}}});
   }
   const file=url.pathname==='/'?'/index.html':url.pathname;
   return route.fulfill({contentType:{'.html':'text/html','.js':'application/javascript','.css':'text/css','.png':'image/png','.woff2':'font/woff2'}[path.extname(file)],body:await fs.readFile(root+file)});
  });
  const send=async text=>{await page.locator('#input').fill(text);await page.locator('#send').click();await page.waitForFunction(()=>!busy&&lastRes);};
  const home=async()=>{await page.locator('#brandHome').click();await page.waitForFunction(()=>!document.querySelector('#home').hidden);};
  await page.goto('https://voltgo.test');await page.waitForFunction(()=>typeof send==='function');
  while(!releaseInitial)await new Promise(resolve=>setTimeout(resolve,10));   // 첫 세션 요청이 서버(가짜)에 도착
  // 사용자가 먼저 입력한다. 질문은 서버가 발급한 대화 ID 를 기다렸다가 보낸다.
  await page.locator('#input').fill('첫 번째 대화');await page.locator('#send').click();
  releaseInitial();await page.waitForFunction(()=>!busy&&lastRes);const first=asks.at(-1).thread_id;
  assert.equal(first,'chat-1');await page.waitForTimeout(100);
  assert.equal(await page.evaluate(()=>session.condition_version),7,'Late initial session must not overwrite the chat response');
  await send('같은 대화의 후속 질문');assert.equal(asks.at(-1).thread_id,first);
  await home();await send('두 번째 새 대화');const second=asks.at(-1).thread_id;
  assert.notEqual(second,first);assert.equal(await page.locator('#messages .me').count(),1);
  assert.equal(await page.evaluate(()=>conversations.length),2);
  await home();await page.locator('#intents button').first().click();await page.waitForFunction(()=>!busy&&lastRes);
  const third=asks.at(-1).thread_id;assert.notEqual(third,second);assert.equal(await page.locator('#messages .me').count(),1);
  await page.locator('#chat .history-open').click();await page.locator(`[data-thread="${first}"]`).click();
  await page.waitForFunction(id=>!busy&&threadId===id,first);
  assert.equal(await page.locator('#messages .me').count(),2);
  await send('기록에서 이어서 보내기');assert.equal(asks.at(-1).thread_id,first);
  assert.equal(await page.locator('#messages .me').count(),3);
  assert.deepEqual(errors,[]);
  console.log('PASS: home input/activity creates new thread, chat input continues, old history preserved/resumable, late initial session ignored.');
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
