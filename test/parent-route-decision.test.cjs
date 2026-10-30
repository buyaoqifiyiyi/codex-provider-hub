'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { resolveParentRoute } = require('../lib/parent-route.cjs');

test('unknown provider requires actual same-turn route proof', async () => {
  const envelope = {session_id:'session', turn_id:'turn'};
  assert.equal(await resolveParentRoute(null, envelope, async input => input === envelope ? 'compatible' : 'unknown'), 'compatible');
  assert.equal(await resolveParentRoute(null, envelope, async () => 'unknown'), 'unknown');
  assert.equal(await resolveParentRoute(null, envelope, async () => { throw Error('unavailable'); }), 'unknown');
});

test('known official and other providers cannot be upgraded by the local route check', async () => {
  let checked = 0;
  const check = async () => { checked++; return 'compatible'; };
  assert.equal(await resolveParentRoute('openai', {}, check), 'official');
  assert.equal(await resolveParentRoute('unrelated', {}, check), 'unknown');
  assert.equal(checked, 0);
  assert.equal(await resolveParentRoute('custom', {}, check), 'compatible');
  assert.equal(checked, 1);
});
