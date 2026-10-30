'use strict';
// Run with NODE_PATH pointing to the bundled Playwright installation.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const html = fs.readFileSync(path.join(__dirname, '../ui/manager.html'), 'utf8');
const A = 'hub-11111111111141118111111111111111/sample-model-v1';
const B = 'hub-22222222222242228222222222222222/sample-model-v1';
const OLD = 'legacy-route/previous-model';
const candidates = [
  {model: A, display_name: 'sample-model-v1', state: 'available', efforts: ['high','max'], connection: '线路一', critical_eligible:true,
    evidence:[{purpose:'connection',channel:'connection',status:'passed',tested_at:'2026-10-29T13:14:11Z',effort:null}],
    public_reference:{status:'available',source_url:'https://huggingface.co/fixture-org/sample-model-v1',
      results:[{benchmark:'datacurve/deep-swe',score:77,verified:false},{benchmark:'harborframework/terminal-bench',score:42.3,verified:false},
               {benchmark:'<img src=x onerror=alert(1)>',score:1,verified:false},{benchmark:'Fourth benchmark',score:17,verified:false}]}},
  {model: B, display_name: 'sample-model-v1', state: 'available', efforts: ['high','max'], connection: '线路二', critical_eligible:true,
    evidence:[{purpose:'audio',channel:'gemini_media',status:'error',tested_at:'2026-10-30T01:00:00Z',effort:null,error_code:'rate_limit'}],
    public_reference:{status:'ambiguous',source_url:'javascript:alert(1)'}},
  {model: 'native-model-z', display_name: 'native-model-z', state: 'available', efforts: ['high'], critical_eligible: true,
    public_reference:{status:'available',source_kind:'report',source_url:'https://reports.example.invalid/models/sample-model-z',
      results:[{benchmark:'SWE-Bench Pro',score:61.6,unit:'%',verified:null}]}},
  {model: 'native-reserve', state: 'outside_policy', efforts: []},
  {model: 'hub-paused/hidden', state: 'disabled', efforts: []}
];
const preferences = {
  bounded: {model: OLD, effort:'high', fallback_model:B, fallback_effort:'high'},
  boundary: {model:A, effort:'high', fallback_model:B, fallback_effort:'high'},
  critical: {model:'native-model-z', effort:'high', fallback_model:'native-model-z', fallback_effort:'high'}
};
const snapshot = {
  ok:true, routing_mode:"auto", preferences_saved:true, preferences, candidates,
  effective: {
    bounded: {ok:true, model:A, effort:'high', connection:'线路一', source:'automatic', selection_reasons:['符合当前任务'], skipped:[{model:OLD, effort:'high', code:'MODEL_UNAVAILABLE'}]},
    boundary: {ok:true, model:B, effort:'high', connection:'线路二', source:'preferred'},
    critical: {ok:true, model:'native-model-z', effort:'high', source:'preferred'}
  },
  recent:[{event:'spawn.result', requested:{model:OLD,effort:'high'}, actual:{model:A,effort:'high'}}]
};

test('scheduling page hides unavailable models while retaining distinct route IDs', async () => {
  const browser = await chromium.launch({headless:true, args:['--disable-background-networking','--host-resolver-rules=MAP * ~NOTFOUND','--disable-quic'], executablePath: process.env.CHROME_BIN || 'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const page = await browser.newPage();
  const host = `<!doctype html><iframe src="/manager.html"></iframe><script>
    window.fixture=${JSON.stringify(snapshot)}; window.saved=[]; window.modeRequests=[];
    addEventListener('message', event => {
      const m=event.data;if(!m || m.jsonrpc!=='2.0' || !m.id)return;
      let result={};
      if(m.method==='ui/initialize')result={hostContext:{}};
      if(m.method==='tools/call'){
        if(m.params.name==='get_scheduling_overview')result={structuredContent:window.fixture};
        else if(m.params.name==='save_scheduling_preferences'){
          if(m.params.arguments.mode) window.modeRequests.push(m.params.arguments);
          if(m.params.arguments.preferences) window.saved.push(m.params.arguments.preferences);
          if(m.params.arguments.mode==='auto') window.fixture.routing_mode='auto';
          else { if(m.params.arguments.mode==='manual') window.fixture.routing_mode='manual'; if(m.params.arguments.preferences) { window.fixture.preferences=m.params.arguments.preferences; window.fixture.preferences_saved=true; } }
          result={structuredContent:{ok:true,preferences:window.fixture.preferences}};
        } else if(m.params.name==='list_providers')result={structuredContent:{profiles:[],active_models:[],service:{},routing_active:false}};
        else result={structuredContent:{ok:true}};
      }
      event.source.postMessage({jsonrpc:'2.0',id:m.id,result},'*');
    });</script>`;
  await page.route('**/*', route => route.request().url().startsWith('http://fixture.invalid/')
    ? route.fulfill({status:200, contentType:'text/html; charset=utf-8', body:route.request().url().endsWith('/manager.html') ? html : host}) : route.abort());
  try {
    await page.goto('http://fixture.invalid/');
    const frame = page.frameLocator('iframe');
    await frame.locator('#tab-scheduling').click();
    await frame.locator('#effective-updated').getByText('快照', {exact:false}).waitFor();
    assert.match(await frame.locator('#routing-mode').innerText(), /自动调度/);
    assert.match(await frame.locator('.effective-card').first().innerText(), /符合当前任务/);
    await frame.locator('summary').filter({hasText:'设置首选与备选'}).click();
    const choices = await frame.locator('#pref-bounded-model option').evaluateAll(nodes => nodes.map(n=>({value:n.value,text:n.textContent,title:n.title})));
    assert.deepEqual(choices.filter(x=>x.value).map(x=>x.text), ['sample-model-v1','sample-model-v1','native-model-z']);
    assert.deepEqual(choices.filter(x=>x.value).map(x=>x.value), [A,B,'native-model-z']);
    assert.match(choices.find(x=>x.value===A).title, /线路一/);
    assert.match(choices.find(x=>x.value===B).title, /线路二/);
    assert.equal(await frame.locator('#pref-bounded-model').inputValue(), '');
    assert.equal(await frame.locator('#pref-bounded-effort').inputValue(), '');
    assert.match(await frame.locator('#preference-dirty-state').innerText(), /重新选择/);
    const firstCard = frame.locator('.effective-card').first();
    const compact = await firstCard.innerText();
    assert.match(compact, /连接 通过/);
    assert.match(compact, /有参考（未核验）/);
    assert.ok(!compact.includes('DeepSWE 77'));
    assert.ok(!compact.includes('按名称匹配'));
    const opener = firstCard.getByRole('button', {name:'查看详情'});
    const cardHeight = await firstCard.evaluate(e=>e.getBoundingClientRect().height);
    await opener.click();
    const dialog = frame.locator('#evidence-dialog');
    assert.equal(await dialog.getAttribute('open'), '');
    assert.match(await dialog.innerText(), /线路一/);
    assert.match(await dialog.innerText(), /连接短测 · 通过/);
    assert.match(await dialog.innerText(), /DeepSWE\s+77/);
    assert.match(await dialog.innerText(), /Terminal-Bench\s+42\.3/);
    assert.match(await dialog.innerText(), /Fourth benchmark\s+17/);
    assert.match(await dialog.innerText(), /按名称匹配，非本线路实测/);
    assert.equal(await dialog.locator('img[src="x"]').count(), 0);
    assert.equal(await dialog.locator('a[href^="https://huggingface.co/"]').count(), 1);
    assert.equal(await firstCard.evaluate(e=>e.getBoundingClientRect().height), cardHeight);
    assert.equal(await frame.locator('#close-evidence').evaluate(e=>e===document.activeElement), true);
    await page.keyboard.press('Escape');
    assert.equal(await dialog.isHidden(), true);
    assert.equal(await opener.evaluate(e=>e===document.activeElement), true);
    await frame.locator('#candidate-list .candidate-row').nth(1).getByRole('button',{name:'查看详情'}).click();
    assert.match(await dialog.innerText(), /非语音声音 · 媒体工具 · 请求出错（额度或频率受限）/);
    assert.match(await dialog.innerText(), /多个同名模型/);
    assert.equal(await dialog.locator('a[href^="javascript:"]').count(), 0);
    await frame.locator('#close-evidence').click();
    await frame.locator('#candidate-list .candidate-row').nth(2).getByRole('button',{name:'查看详情'}).click();
    assert.match(await dialog.innerText(), /暂无测试记录/);
    assert.match(await dialog.innerText(), /SWE-Bench Pro\s+61\.6%/);
    assert.match(await dialog.innerText(), /公开评测 · 公开报告/);
    assert.equal(await dialog.locator('a[href^="https://reports.example.invalid/"]').count(), 1);
    await frame.locator('#close-evidence').click();
    const visible = await frame.locator('#scheduling-view').innerText();
    assert.match(visible, /关键任务[\s\S]*可参与/);
    assert.ok(!visible.includes('自动跳过'));
    const criticalChoices=await frame.locator('#pref-critical-model option').evaluateAll(nodes=>nodes.filter(x=>x.value).map(x=>x.value));
    assert.ok(criticalChoices.includes(A));
    for (const hidden of [OLD,'native-reserve','hub-paused/hidden',A,B]) assert.ok(!visible.includes(hidden), hidden);
    assert.equal(await frame.locator('.effective-model').first().innerText(), 'sample-model-v1');
    assert.deepEqual(await frame.locator('#candidate-list .model-code').allTextContents(), ['sample-model-v1','sample-model-v1','native-model-z']);
    assert.equal((await page.evaluate(()=>window.saved)).length,0);
    // Editing another preference does not silently rewrite the hidden old one.
    await frame.locator('#pref-boundary-fallback-model').selectOption(A);
    await frame.locator('#save-scheduling').click();
    await frame.locator('#scheduling-message').getByText('偏好已保存', {exact:false}).waitFor();
    assert.match(await frame.locator('#routing-mode').innerText(), /自动调度/);
    await frame.locator('#pref-boundary-fallback-model').selectOption(B);
    await frame.locator('#switch-manual').click();
    await frame.locator('#routing-mode').getByText('手动调度').waitFor();
    assert.deepEqual(await page.evaluate(()=>window.modeRequests.at(-1)),{mode:'manual'});
    assert.equal(await frame.locator('#pref-boundary-fallback-model').inputValue(), B);
    const savedBeforeAuto = await page.evaluate(() => JSON.stringify(window.fixture.preferences));
    await frame.locator('#restore-auto').click();
    await frame.locator('#routing-mode').getByText('自动调度').waitFor();
    assert.equal(await page.evaluate(() => JSON.stringify(window.fixture.preferences)), savedBeforeAuto);
    assert.equal(await frame.locator('#pref-boundary-fallback-model').inputValue(), B);
    await frame.locator('#pref-boundary-fallback-model').selectOption(A);
    await page.setViewportSize({width: 375, height: 800});
    assert.ok(await frame.locator('body').evaluate(el => el.scrollWidth <= el.clientWidth));
    await frame.locator('.effective-card').first().getByRole('button',{name:'查看详情'}).click();
    assert.ok(await dialog.evaluate(e=>e.getBoundingClientRect().width <= innerWidth));
    assert.ok(await dialog.evaluate(e=>e.scrollHeight > e.clientHeight));
    assert.ok(await dialog.evaluate(e=>{e.scrollTop=40;return e.scrollTop>0}));
    await frame.locator('#close-evidence').click();
    let saved = await page.evaluate(()=>window.saved);
    assert.equal(saved.length,1);
    assert.equal(saved[0].bounded.model,OLD);
    assert.equal(saved[0].boundary.fallback_model,A);
    assert.match(await frame.locator('#preference-dirty-state').innerText(), /有未保存修改/);
    await frame.locator('#pref-bounded-model').selectOption(A);
    await frame.locator('#save-scheduling').click();
    await page.waitForFunction(()=>window.saved.length===2);
    saved = await page.evaluate(()=>window.saved);
    assert.equal(saved[1].bounded.model,A);
    assert.equal(saved[1].bounded.fallback_model,B);
    // A later refresh must hide an unavailable saved selection without replacing it.
    await page.evaluate(model=>{window.fixture.candidates.find(x=>x.model===model).state='disabled';},A);
    await frame.locator('.effective-card').first().getByRole('button',{name:'查看详情'}).click();
    await frame.locator('#refresh-scheduling').evaluate(e=>e.click());
    await frame.locator('#scheduling-message').getByText('当前建议已刷新', {exact:false}).waitFor();
    assert.equal(await dialog.isHidden(), true);
    assert.equal(await frame.locator('#pref-bounded-model').inputValue(),'');
    assert.equal((await page.evaluate(()=>window.saved)).length,2);
    await page.evaluate(model=>{window.fixture.candidates.find(x=>x.model===model).state='available';},A);
    await frame.locator('#refresh-scheduling').click();
    await frame.locator('#scheduling-message').getByText('当前建议已刷新', {exact:false}).waitFor();
    assert.equal(await frame.locator('#pref-bounded-model').inputValue(),A);
    // A model changed by the user and then disabled before saving is not silently replaced.
    await frame.locator('#pref-bounded-model').selectOption(B);
    await page.evaluate(model=>{window.fixture.candidates.find(x=>x.model===model).state='disabled';},B);
    await frame.locator('#refresh-scheduling').click();
    await frame.locator('#scheduling-message').getByText('当前建议已刷新', {exact:false}).waitFor();
    assert.equal(await frame.locator('#pref-bounded-model').inputValue(),'');
    await frame.locator('#save-scheduling').click();
    assert.match(await frame.locator('#scheduling-message').innerText(), /请选择首选模型/);
    assert.equal((await page.evaluate(()=>window.saved)).length,2);
    // With no usable models, even untouched unavailable preferences are not submitted.
    await page.evaluate(()=>{window.fixture.candidates.forEach(x=>x.state='disabled');});
    await frame.locator('#refresh-scheduling').click();
    await frame.locator('#scheduling-message').getByText('当前建议已刷新', {exact:false}).waitFor();
    await frame.locator('#save-scheduling').click();
    assert.match(await frame.locator('#scheduling-message').innerText(), /当前没有可用模型/);
    assert.equal((await page.evaluate(()=>window.saved)).length,2);
    // A new installation cannot accidentally turn displayed defaults into a manual preference.
    await page.evaluate(()=>{window.fixture.routing_mode='auto';window.fixture.preferences_saved=false;});
    await frame.locator('#refresh-scheduling').click();
    await frame.locator('#routing-mode').getByText('自动调度').waitFor();
    const beforeSwitch=await page.evaluate(()=>window.modeRequests.length);
    await frame.locator('#switch-manual').click();
    await frame.locator('#routing-message').getByText('请先保存', {exact:false}).waitFor();
    assert.equal(await page.evaluate(()=>window.modeRequests.length),beforeSwitch);
    // A stale host that has not loaded mode support must not claim automation is active.
    await page.evaluate(()=>{delete window.fixture.routing_mode;});
    await frame.locator('#refresh-scheduling').click();
    await frame.locator('#routing-mode').getByText('调度模式未确认').waitFor();
    assert.equal(await frame.locator('#restore-auto').isHidden(),true);
  } finally {
    await browser.close();
  }
});

test('public reference refreshes when background lookup completes without changing preferences', async () => {
  const browser = await chromium.launch({headless:true, args:['--disable-background-networking','--host-resolver-rules=MAP * ~NOTFOUND','--disable-quic'], executablePath: process.env.CHROME_BIN || 'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const page = await browser.newPage();
  const pending = structuredClone(snapshot);
  pending.public_reference_pending = true;
  pending.candidates[0].public_reference = {status:'pending'};
  const ready = {status:'available',source_url:'https://huggingface.co/fixture-org/sample-model-v1',
    results:[{benchmark:'datacurve/deep-swe',score:77,verified:false}]};
  const host = `<!doctype html><iframe src="/manager.html"></iframe><script>
    window.calls=[]; window.fixture=${JSON.stringify(pending)};
    addEventListener('message',event=>{const m=event.data;if(!m||m.jsonrpc!=='2.0'||!m.id)return;
      let result={};if(m.method==='ui/initialize')result={hostContext:{}};
      if(m.method==='tools/call') {window.calls.push(m.params.name);
        if(m.params.name==='get_scheduling_overview')result={structuredContent:window.fixture};
        else if(m.params.name==='get_public_model_references'){window.fixture.public_reference_pending=false; window.fixture.candidates[0].public_reference=${JSON.stringify(ready)}; window.fixture.effective.bounded.selection_reasons=['评测完成']; result={structuredContent:{ok:true,pending:false,references:{[${JSON.stringify(A)}]:${JSON.stringify(ready)}}}};}
        else if(m.params.name==='list_providers')result={structuredContent:{profiles:[],active_models:[],service:{},routing_active:false}};
        else result={structuredContent:{ok:true}};
      } event.source.postMessage({jsonrpc:'2.0',id:m.id,result},'*'); });</script>`;
  await page.route('**/*',route=>route.request().url().startsWith('http://fixture.invalid/')
    ? route.fulfill({status:200,contentType:'text/html; charset=utf-8',body:route.request().url().endsWith('/manager.html')?html:host}) : route.abort());
  try {
    await page.goto('http://fixture.invalid/');
    const frame=page.frameLocator('iframe');
    await frame.locator('#tab-scheduling').click();
    await frame.locator('#candidate-list').getByText('查询中').waitFor();
    await frame.locator('#candidate-list .candidate-row').first().getByRole('button',{name:'查看详情'}).click();
    await frame.locator('#close-evidence').click();
    await frame.locator('summary').filter({hasText:'设置首选与备选'}).click();
    await frame.locator('#pref-bounded-model').selectOption(A);
    await frame.locator('#candidate-list').getByText('有参考（未核验）').waitFor();
    assert.equal(await frame.locator('#pref-bounded-model').inputValue(), A);
    await frame.locator('.effective-card').first().getByText('评测完成').waitFor();
    await frame.locator('#candidate-list .candidate-row').first().getByRole('button',{name:'查看详情'}).click();
    await frame.locator('#evidence-dialog').getByText('DeepSWE', {exact:true}).waitFor();
    const calls=await page.evaluate(()=>window.calls);
    assert.equal(calls.filter(x=>x==='get_scheduling_overview').length,2);
    assert.equal(calls.filter(x=>x==='get_public_model_references').length,1);
    assert.ok(!calls.includes('save_scheduling_preferences'));
  } finally { await browser.close(); }
});

