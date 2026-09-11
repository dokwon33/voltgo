const { chromium } = require('@playwright/test');
const fs = require('node:fs/promises');
const assert = require('node:assert/strict');
const root = require('node:path').resolve(__dirname, '..');
const sdk = `(() => { window.drawn=[]; window.mapCount=0;
class LatLng { constructor(lat,lon){this.lat=lat;this.lon=lon;} }
class Map { constructor(id,options){window.mapCount++;this.options=options;} resize(w,h){if(!w||!h)throw Error('resize dimensions required');} fitBounds(b,m){window.bounds=b.points;} setCenter(){} setZoom(){} }
class Overlay { constructor(options){this.options=options;this.map=options.map;window.drawn.push(this);} setMap(m){this.map=m;} addListener(event,cb){this.click=cb;} }
class Marker extends Overlay {} class Polyline extends Overlay {}
class LatLngBounds { constructor(){this.points=[];} extend(p){this.points.push(p);} }
window.Tmapv2={Map,LatLng,Marker,Polyline,LatLngBounds,Size:class{}}; })();`;
const c = (id,name) => ({plan_id:id, poi_id:id, name, category:'cafe', outbound_sec:240,inbound_sec:300,dwell_sec:600,slack_sec:100,return_at:'2026-09-11T14:20:00+09:00',leave_by:'2026-09-11T14:15:00+09:00',poi_source:'tmap',route_source:'tmap',opening_status:'unknown'});
const line = coords => ({type:'LineString',coordinates:coords});
const geometry = {
 origin:{latitude:37.5665,longitude:126.978},
 places:[{poi_id:'A',latitude:37.5675,longitude:126.979},{poi_id:'B',latitude:37.565,longitude:126.980}],
 routes:[{poi_id:'A',outbound:line([[126.978,37.5665],[126.979,37.5675]]),inbound:{type:'MultiLineString',coordinates:[[[126.979,37.5675],[126.979,37.5665]],[[126.979,37.5665],[126.978,37.5665]]]}},
 {poi_id:'B',outbound:line([[126.978,37.5665],[126.980,37.565]]),inbound:null}]
};
(async()=>{
const browser=await chromium.launch({headless:true});
try {
const page=await browser.newPage({viewport:{width:390,height:844}});
page.setDefaultTimeout(15000);
const errors=[]; page.on('pageerror',e=>errors.push(e.message));
let payload={response:{status:'ok',message:'추천 장소입니다.',candidates:[c('A','추천 카페 A'),c('B','추천 카페 B')]},session:{},map_data:geometry};
let sdkRequests=0, failSDK=false, delaySDK=0, key='test-browser-key', apiCalls=0;
await page.route('**/*',async route=>{
 const url=new URL(route.request().url());
 if(url.hostname==='apis.openapi.sk.com'){sdkRequests++; if(delaySDK)await new Promise(r=>setTimeout(r,delaySDK));return failSDK?route.abort():route.fulfill({contentType:'application/javascript',body:sdk});}
 if(url.hostname!=='voltgo.test')return route.abort();
 if(url.pathname.startsWith('/api/')){apiCalls++;return route.fulfill({json:url.pathname==='/api/session'?{session:{}}:url.pathname==='/api/health'?{}:payload});}
 if(url.pathname==='/map-config.js')return route.fulfill({contentType:'application/javascript',body:`window.VOLTGO_MAP_CONFIG={appKey:${JSON.stringify(key)}};`});
 const path=url.pathname==='/'?'/index.html':url.pathname;
 const type=path.endsWith('.js')?'application/javascript':path.endsWith('.css')?'text/css':path.endsWith('.png')?'image/png':path.endsWith('.woff2')?'font/woff2':'text/html';
 try{return route.fulfill({contentType:type,body:await fs.readFile(root+path)});}catch{return route.fulfill({status:404,body:''});}
});
const submit=async()=>{await page.waitForFunction(()=>threadId && serverInstance !== undefined);await page.locator('#input').fill('카페 추천');await page.locator('#send').click();await page.waitForFunction(()=>!document.querySelector('#send').disabled);if(await page.locator('#routeMap').isHidden())await page.locator('#mapBtn').click();};
const drawn=()=>page.evaluate(()=>window.drawn.filter(x=>x.map).map(x=>({type:x.constructor.name,...x.options,path:x.options.path?.map(p=>[p.lon,p.lat]),map:undefined})));
await page.goto('https://voltgo.test'); await submit(); await page.locator('[data-map-key]').first().click();
await page.waitForFunction(()=>window.drawn?.filter(x=>x.map).length===6);
assert.equal(sdkRequests,1); assert.equal((await drawn()).filter(x=>x.type==='Marker').length,3);
assert.deepEqual(await page.locator('.plan-label').allTextContents(), ['A', 'B']);
assert.deepEqual((await drawn()).filter(x=>x.type==='Marker').map(x=>decodeURIComponent(x.icon).match(/<text[^>]*>(.*?)<\/text>/)[1]), ['⚡', 'A', 'B']);
assert.deepEqual((await drawn()).find(x=>x.type==='Polyline').path,[[126.978,37.5665],[126.979,37.5675]]);
const callsBefore=apiCalls;
await page.locator('[data-direction="inbound"]').click();
await page.waitForFunction(()=>window.drawn.filter(x=>x.map&&x.constructor.name==='Polyline').length===2);
assert.ok((await drawn()).filter(x=>x.type==='Polyline').every(x=>x.strokeStyle==='dash'));
await page.locator('[data-map-key]').nth(1).click();
await page.waitForFunction(()=>document.querySelector('.map-note').textContent.includes('오는 길 경로 미제공'));
assert.equal((await drawn()).filter(x=>x.type==='Polyline').length,0);
assert.equal(apiCalls,callsBefore,'map interactions must not call backend');
await page.locator('[data-direction="both"]').click();
await page.waitForFunction(()=>window.drawn.filter(x=>x.map&&x.constructor.name==='Polyline').length===1);
assert.equal(await page.locator('[data-map-key]').nth(1).getAttribute('aria-pressed'),'true');
// Marker click selects the corresponding card.
await page.evaluate(()=>window.drawn.filter(x=>x.map&&x.constructor.name==='Marker')[1].click());
await page.waitForFunction(()=>document.querySelector('.map-place').textContent==='A · 추천 카페 A');
await page.screenshot({path:'/tmp/voltgo-map-mobile.png',fullPage:true});
assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
// A new response with no coordinates never reuses old geometry; old cards retain their snapshot.
payload={response:{status:'ok',message:'새 결과',candidates:[c('C','좌표 없는 카페')]},session:{}};
await submit();
assert.match(await page.locator('.map-empty').innerText(),/장소 위치를 아직/);
assert.equal((await drawn()).length,0);
await page.locator('[data-map-key]').first().click();
await page.waitForFunction(()=>window.drawn.filter(x=>x.map&&x.constructor.name==='Polyline').length===3);
// Malformed points must not be skipped to create a shortcut; separate valid segments still render.
payload={response:{status:'ok',candidates:[c('A','좌표 검증')]},session:{},map_data:JSON.parse(JSON.stringify(geometry))};
payload.map_data.routes[0].outbound=line([[126.978,37.5665],['bad',37.567],[126.979,37.5675]]);
await submit();
await page.waitForFunction(()=>document.querySelector('.map-note').textContent.includes('가는 길 경로 미제공'));
assert.equal((await drawn()).filter(x=>x.type==='Polyline').length,2);
await page.evaluate(()=>goHome()); assert.equal(await page.locator('#routeMap').isVisible(),false);assert.equal((await drawn()).length,0);
assert.equal(await page.evaluate(()=>window.mapCount),1);
// Missing key, network failure/retry, and reset while SDK is loading.
key=''; payload={response:{status:'ok',candidates:[c('A','카페')]},session:{},map_data:geometry};
await page.reload();await submit();assert.match(await page.locator('.map-empty').innerText(),/지도를 불러올 수 없어요/);
key='test-browser-key'; failSDK=true;
await page.reload();await submit();await page.waitForFunction(()=>document.querySelector('.map-empty').textContent.includes('연결이 원활하지'));
failSDK=false; await page.locator('.map-retry').click();await page.waitForFunction(()=>window.drawn?.filter(x=>x.map).length>0);
delaySDK=500; await page.reload();await submit();await page.evaluate(()=>goHome());await page.waitForTimeout(750);
assert.equal(await page.locator('#routeMap').isVisible(),false);assert.equal(await page.evaluate(()=>window.mapCount),0);
await page.evaluate(()=>{
  addBot({status:'ok',candidates:[
    {plan_id:'place-91',version:1,name:'첫 번째 장소',category:'cafe'},
    {plan_id:'place-37',version:1,name:'두 번째 장소',category:'cafe'},
    {plan_id:'place-58',version:1,name:'세 번째 장소',category:'cafe'}]},{});
});
assert.deepEqual((await page.locator('.plan-label').allTextContents()).slice(-3), ['A', 'B', 'C']);
await page.setViewportSize({width:320,height:844});
await page.evaluate(()=>showChat());
assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
await page.screenshot({path:'/tmp/voltgo-abc-recommendations.png',fullPage:true});
assert.deepEqual(errors,[]);
console.log('PASS: markers, coordinate order, route toggles, card/marker selection, no extra API calls, snapshots, missing/invalid data, mobile layout, single map, reset, SDK failure/retry, async reset.');
} finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
