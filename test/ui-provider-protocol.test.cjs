'use strict';
// Run with NODE_PATH pointing to the bundled Playwright installation.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const html = fs.readFileSync(path.join(__dirname, '../ui/manager.html'), 'utf8');

test('protocol picker adds Gemini native guidance without changing existing values', async () => {
  const browser = await chromium.launch({headless:true, args:['--disable-background-networking','--host-resolver-rules=MAP * ~NOTFOUND','--disable-quic'], executablePath: process.env.CHROME_BIN || 'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const page = await browser.newPage();
  const host = `<!doctype html><iframe src="/manager.html"></iframe><script>
    window.providerListCalled=false; window.capturedSave=null;
    addEventListener('message', event => {
      const m=event.data;if(!m || m.jsonrpc!=='2.0' || !m.id)return;
      let result={};
      if(m.method==='ui/initialize')result={hostContext:{}};
      if(m.method==='tools/call'){
        if(m.params.name==='list_providers'){
          window.providerListCalled=true;
          result={structuredContent:{profiles:[{id:'legacy',name:'旧订阅',protocol:'codex_subscription',model:'native-model-z',models:['native-model-z'],has_key:false}],active_models:[],service:{},routing_active:false}};
        } else if(m.params.name==='start_provider_save') {
          window.capturedSave=m.params.arguments;
          result={structuredContent:{ok:false,message:'fixture stop'}};
        } else result={structuredContent:{ok:true}};
      }
      event.source.postMessage({jsonrpc:'2.0',id:m.id,result},'*');
    });</script>`;
  await page.route('**/*', route => route.request().url().startsWith('http://fixture.invalid/')
    ? route.fulfill({status:200, contentType:'text/html; charset=utf-8', body:route.request().url().endsWith('/manager.html') ? html : host}) : route.abort());
  try {
    await page.goto('http://fixture.invalid/');
    await page.waitForFunction(() => window.providerListCalled);
    const frame = page.frameLocator('iframe');
    const protocol = frame.locator('#protocol');
    await protocol.waitFor();

    const expected = [
      ['chat_completions', 'Chat Completions｜通用对话'],
      ['responses', 'Responses｜OpenAI 响应'],
      ['anthropic_messages', 'Claude Messages｜Claude 原生'],
      ['gemini_native', 'Gemini 原生｜多模态'],
      ['system_one', 'Jev · System One｜Jev 专用']
    ];
    const options = await protocol.locator('option').evaluateAll(nodes => nodes.map(node => [node.value, node.textContent]));
    assert.deepEqual(options, expected);
    for (const [, label] of expected) assert.match(label, /[\u3400-\u9fff]/, `缺少中文用途说明：${label}`);

    await protocol.selectOption('gemini_native');
    assert.equal(await frame.locator('#base-url').isEnabled(), true);
    assert.equal(await frame.locator('#base-url').getAttribute('required'), '');
    assert.equal(await frame.locator('#key-group').isVisible(), true);
    assert.match(await frame.locator('#endpoint-hint').innerText(), /https:\/\/generativelanguage\.googleapis\.com/);
    assert.match(await frame.locator('#endpoint-hint').innerText(), /图片、声音和视频支持情况以实际线路为准/);
    assert.match(await frame.locator('#model-hint').innerText(), /Gemini 模型 ID/);
    assert.match(await frame.locator('#model-hint').innerText(), /账户实际可用范围/);

    await frame.locator('#key').fill('ui-test-key-not-secret');
    await frame.locator('#profiles button').getByText('旧订阅').click();
    assert.equal(await protocol.inputValue(), 'codex_subscription');
    assert.equal(await frame.locator('#base-url').isDisabled(), true);
    assert.equal(await frame.locator('#base-url').getAttribute('required'), null);
    assert.equal(await frame.locator('#key-group').isHidden(), true);
    assert.equal(await frame.locator('#key').inputValue(), '');
    assert.match(await frame.locator('#endpoint-hint').innerText(), /已登录的 OpenAI 订阅/);
    assert.match(await frame.locator('#model-hint').innerText(), /本机订阅目录/);

    await frame.locator('#new').click();
    assert.equal(await protocol.locator('option[value="codex_subscription"]').count(), 0);

    await protocol.selectOption('system_one');
    assert.equal(await frame.locator('#base-url').isEnabled(), true);
    assert.equal(await frame.locator('#base-url').getAttribute('required'), '');
    assert.equal(await frame.locator('#key-group').isVisible(), true);
    assert.equal(await frame.locator('#model-tuning').isHidden(), true);
    assert.match(await frame.locator('#endpoint-hint').innerText(), /System One 地址/);

    await protocol.selectOption('anthropic_messages');
    assert.equal(await frame.locator('#model-tuning').isVisible(), true);
    assert.match(await frame.locator('#endpoint-hint').innerText(), /Claude API/);

    await protocol.selectOption('responses');
    assert.match(await frame.locator('#endpoint-hint').innerText(), /https:\/\/example\.com\/v1/);
    assert.match(await frame.locator('#model-hint').innerText(), /模型列表多选/);

    await protocol.selectOption('chat_completions');
    assert.equal(await frame.locator('#base-url').isEnabled(), true);
    assert.equal(await frame.locator('#key-group').isVisible(), true);
    assert.equal(await frame.locator('#model-tuning').isVisible(), true);
    assert.match(await frame.locator('#endpoint-hint').innerText(), /服务商提供的基础地址/);

    await protocol.selectOption('gemini_native');
    await frame.locator('#name').fill('合成连接');
    await frame.locator('#base-url').fill('https://fixture.invalid/v1');
    await frame.locator('#key').fill('synthetic-key');
    await frame.locator('#model').fill('gemini-fixture');
    await frame.locator('#save').click();
    await page.waitForFunction(() => window.capturedSave !== null);
    const submitted = await page.evaluate(() => window.capturedSave);
    assert.equal(submitted.protocol, 'gemini_native');
    assert.equal(submitted.base_url, 'https://fixture.invalid/v1');
    assert.deepEqual(submitted.models, ['gemini-fixture']);
  } finally {
    await browser.close();
  }
});
