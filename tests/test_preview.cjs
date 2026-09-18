/* Dependency-free preview state/DOM-contract regression tests. Run: node tests/test_preview.cjs */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'../tools/preview/index.html'),'utf8');
assert.equal((html.match(/<script>/g)||[]).length,2,'one executable script plus harmless escaped test text');
assert.equal((html.match(/<\/script>/g)||[]).length,1,'only one HTML closing script');
assert(!/(?:src|href)=["']https?:/i.test(html));
assert(html.includes("connect-src 'none'"));
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes=new Map(),listeners={},saved={};
const doc={documentElement:{dataset:{},lang:''},activeElement:null,addEventListener(k,fn){listeners[k]=fn;},getElementById(id){if(!nodes.has(id))nodes.set(id,{id,hidden:id==='modal-layer',innerHTML:'',textContent:'',value:'',dataset:{},style:{},options:[{},{}],isConnected:true,addEventListener(k,fn){this[k]=fn;},focus(){doc.activeElement=this;},querySelector(){return this;},setPointerCapture(){},getBoundingClientRect(){return {left:0,top:0,width:600,height:180};}});return nodes.get(id);}};
const context={document:doc,window:{},localStorage:{getItem(k){return saved[k]||null;},setItem(k,v){saved[k]=v;}},console};
vm.runInNewContext(script,context,{filename:'preview-inline.js'});
const api=context.window.MixPreview,s=api.state;
let count=0;function check(name,fn){fn();count++;console.log('PASS',name);}
const app=()=>nodes.get('app').innerHTML;
const key=(k,extra={})=>listeners.keydown({key:k,preventDefault(){},stopPropagation(){},...extra});

check('three pages, five settings sections, both languages, all four themes and two scenarios',()=>{
 for(const scenario of ['offline','mock'])for(const theme of ['graphite','paper','midnight','ember'])for(const lang of ['en','zh'])
  for(const page of ['home','app','settings'])for(const section of ['appearance','network','power','device','system']){
   Object.assign(s,{scenario,theme,lang,page,section});api.render();
   assert(app().length>100);
   assert.equal(doc.documentElement.dataset.theme,theme);
   assert.equal((nodes.get('nav').innerHTML.match(/data-page=/g)||[]).length,3);
   assert(nodes.get('statusbar').innerHTML.length>40);
  }});

check('the launcher offers exactly four buttons and starts one application',()=>{
 Object.assign(s,{scenario:'mock',lang:'en',page:'home',session:false,app:'shell'});api.render();
 assert.equal((app().match(/data-launch=/g)||[]).length,4);
 api.launch(0);
 assert.equal(s.page,'app');assert.equal(s.app,'translate');assert.equal(s.session,true);
 api.launch(3);
 assert.equal(s.page,'settings');});

check('the agent button opens a placeholder and starts nothing',()=>{
 Object.assign(s,{scenario:'mock',page:'home',session:false});api.render();
 const n=s.actions.length;api.launch(2);
 assert.equal(s.app,'agent');assert.equal(s.session,false);assert.equal(s.actions.length,n);
 assert(app().includes('placeholder'));});

check('offline refuses a session and unknown telemetry stays unknown',()=>{
 Object.assign(s,{scenario:'offline',session:false,lang:'en',page:'home'});
 api.launch(1);
 assert.equal(s.session,false);
 s.page='settings';s.section='power';api.render();
 assert(app().includes('Unavailable'));
 assert(!app().includes('<polyline'));
 assert(app().includes('Calibration has not been verified'));});

check('a mock session survives page changes and background output',()=>{
 s.scenario='mock';s.page='app';s.app='shell';api.dispatch('open-session');assert(s.session);
 s.page='home';api.feed('background output');api.render();
 assert(s.session);assert(s.history.includes('background output'));});

check('both terminal geometries are honest about columns, rows and CJK width',()=>{
 for(const [index,cols,rows] of [[0,80,28],[1,64,22]]){
  api.setGeometry(index);api.render();
  assert.equal(api.geom().cols,cols);
  const cells=api.terminalCells('A中文B');
  assert.equal(cells.reduce((n,c)=>n+c.width,0),cols);
  assert.equal(cells[1].width,2);
  assert.equal(api.terminalCells('A'.repeat(cols-1)+'中').filter(c=>c.width===2).length,0);
  assert.equal(api.terminalRows().length,rows);
 }});

check('changing the cell size clears the grid rather than guessing a reflow',()=>{
 api.setGeometry(1);s.history=[];s.scroll=0;
 for(let i=0;i<40;i++)api.feed('line '+i);
 assert(s.history.length>0);
 api.setGeometry(0);
 assert.equal(s.history.length,0);assert.equal(s.scroll,0);
 api.setGeometry(1);});

check('100 history lines plus one screen, bounded scroll',()=>{
 s.history=[];s.scroll=0;
 for(let i=0;i<200;i++)api.feed('line '+i);
 assert.equal(s.history.length,100+api.geom().rows);
 s.page='app';s.app='shell';
 for(let i=0;i<20;i++)api.dispatch('older');
 assert.equal(s.scroll,100);
 assert.equal(api.terminalRows().length,api.geom().rows);
 api.dispatch('live');assert.equal(s.scroll,0);});

check('untrusted output is escaped and cannot start or confirm actions',()=>{
 s.page='app';s.app='shell';const n=s.actions.length;
 api.feed('<img src=x onerror=alert(1)>\n\x1b[confirm]\r\nYES');api.render();
 assert.equal(s.actions.length,n);assert.equal(s.modal,null);
 assert(app().includes('&lt;'));assert(!app().includes('<img src=x'));
 api.request('update');api.feed('Enter YES\n');
 assert.equal(s.modal,'update');assert.equal(s.actions.length,n);
 api.resolve(false);assert.equal(s.actions.at(-1).accepted,false);});

check('independent modal requires local Enter and Escape rejects',()=>{
 api.request('maintenance');assert.equal(s.job,false);assert.equal(nodes.get('app').inert,true);
 let prevented=false;listeners.keydown({key:'Escape',preventDefault(){prevented=true;},stopPropagation(){}});
 assert(prevented);assert.equal(s.job,false);
 api.request('maintenance');key('Enter');
 assert.equal(s.job,true);assert.equal(s.modal,null);assert.equal(nodes.get('app').inert,false);
 api.request('cancel-job');api.resolve(true);assert.equal(s.job,false);});

check('modal blocks navigation, launching and duplicate requests',()=>{
 s.page='settings';api.request('reset');api.request('maintenance');
 assert.equal(s.modal,'reset');
 api.launch(0);assert.equal(s.page,'settings');
 api.dispatch('open-shell');assert.equal(s.page,'settings');
 api.resolve(false);});

check('the Wi-Fi passphrase becomes one request and is then gone',()=>{
 Object.assign(s,{scenario:'mock',page:'settings',section:'network',netView:'list'});
 api.dispatch('net-scan');
 assert(s.networks.length>0);
 assert.equal((app().match(/data-network=/g)||[]).length,s.networks.length);
 api.selectNetwork(0);
 assert.equal(s.netView,'connect');assert.equal(s.netSsid,'cafe');
 listeners.input?.({target:{id:'passphrase',value:'hunter2'}});
 s.netPass='hunter2';
 const n=s.actions.length;
 api.dispatch('net-connect');
 assert.equal(s.actions.length,n+1);
 assert.equal(s.actions.at(-1).kind,'net-connect');
 assert.equal(s.actions.at(-1).ssid,'cafe');
 assert.equal(s.netPass,'');
 assert.equal(s.netView,'list');
 /* The passphrase must not survive anywhere in the rendered page. */
 assert(!app().includes('hunter2'));});

check('forgetting a saved network needs local consent',()=>{
 api.selectNetwork(0);
 api.dispatch('net-forget');
 assert.equal(s.modal,'net-forget');
 api.resolve(false);
 assert.equal(s.netView,'connect');
 api.dispatch('net-forget');api.resolve(true);
 assert.equal(s.netView,'list');});

check('single pointer rejects second finger and releases on cancel',()=>{
 s.page='settings';s.section='device';s.touch=true;api.render();
 const pad=nodes.get('touch-pad');
 pad.onpointerdown({pointerId:1,clientX:20,clientY:30});assert.equal(s.pointer,1);
 pad.onpointerdown({pointerId:2,clientX:40,clientY:50});assert.equal(s.pointer,1);
 pad.onpointercancel({pointerId:1});assert.equal(s.pointer,null);
 assert(nodes.get('touch-coordinates').textContent.includes('UP'));});

check('theme, language and cell size are persisted and validated',()=>{
 api.dispatch('theme-ember');api.dispatch('lang-zh');api.dispatch('geometry-0');
 assert.deepEqual(JSON.parse(saved['mixui-preview']),{theme:'ember',lang:'zh',geometry:0});
 api.dispatch('theme-invalid');assert.equal(s.theme,'ember');
 api.dispatch('geometry-9');assert.equal(s.geometry,0);
 api.dispatch('geometry-1');api.dispatch('lang-en');});

check('brightness clamps, volume and keyboard step, reset confirms locally',()=>{
 s.page='settings';s.section='appearance';api.render();
 listeners.input({target:{id:'brightness',value:'500'}});assert.equal(s.brightness,100);
 for(let i=0;i<4;i++)api.dispatch('kbd');assert.equal(s.kbd,0);
 s.volume=60;api.dispatch('volume-up');api.dispatch('volume-up');assert.equal(s.volume,70);
 for(let i=0;i<30;i++)api.dispatch('volume-down');assert.equal(s.volume,0);
 s.touch=true;api.request('reset');api.resolve(true);assert.equal(s.touch,false);});

check('number and arrow keys reach the same four launcher buttons',()=>{
 Object.assign(s,{scenario:'mock',page:'home',focus:0,session:false});api.render();
 key('ArrowRight');assert.equal(s.focus,1);
 key('ArrowDown');assert.equal(s.focus,3);
 key('Enter');assert.equal(s.page,'settings');
 s.page='home';api.render();
 key('3');assert.equal(s.app,'agent');assert.equal(s.session,false);});

check('incoming output preserves the scrolled history position',()=>{
 s.page='app';s.app='shell';s.history=[];s.scroll=0;
 for(let i=0;i<70;i++)api.feed('stable '+i);
 api.dispatch('older');
 const first=api.terminalRows()[0];
 api.feed('new background line');
 assert.equal(api.terminalRows()[0],first);
 api.dispatch('live');});

check('confirmed mock maintenance progresses to completion',()=>{
 s.page='settings';s.section='system';
 api.request('maintenance');api.resolve(true);
 for(let i=0;i<4;i++)api.dispatch('advance-job');
 assert.equal(s.jobPercent,100);assert.equal(s.job,false);assert(s.notice.length>0);});

console.log(`${count} preview regression groups passed (240 page/section/theme/language/scenario renders).`);
