'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {makeTestDir}=require('./safe-temp-fixture.cjs');

function underRoot(t,root,callback) {
  const old=process.env.NEUTRAL_TEST_TMP;
  process.env.NEUTRAL_TEST_TMP=root;
  try { return callback(); }
  finally { if(old===undefined) delete process.env.NEUTRAL_TEST_TMP; else process.env.NEUTRAL_TEST_TMP=old; }
}

function captureOwner() {
  const callbacks=[];
  return {owner:{after:fn=>callbacks.push(fn)},cleanup:()=>callbacks.forEach(fn=>fn())};
}

test('configured temporary root is used and only the freshly created prefixed child is removed',t=>{
  const parent=makeTestDir(t,'fixture-safety-parent-'),captured=captureOwner();
  const child=underRoot(t,parent,()=>makeTestDir(captured.owner,'fixture-safety-child-'));
  assert.equal(fs.realpathSync(path.dirname(child)),fs.realpathSync(parent));
  assert.ok(path.basename(child).startsWith('fixture-safety-child-'));
  fs.writeFileSync(path.join(child,'synthetic.txt'),'fixture');
  captured.cleanup();
  assert.equal(fs.existsSync(child),false);
  assert.equal(fs.existsSync(parent),true);
  captured.cleanup();
});

test('invalid prefixes and a relative configured root cannot allocate a cleanup target',t=>{
  const parent=makeTestDir(t,'fixture-safety-invalid-');
  for(const prefix of ['../unsafe-','/unsafe-','unsafe','']) {
    assert.throws(()=>underRoot(t,parent,()=>makeTestDir(t,prefix)),/invalid temporary fixture/);
  }
  assert.throws(()=>underRoot(t,'relative-root',()=>makeTestDir(t,'valid-prefix-')),/root must be absolute/);
  assert.deepEqual(fs.readdirSync(parent),[]);
});

test('cleanup rejects a replacement directory at the same path',t=>{
  const parent=makeTestDir(t,'fixture-safety-replacement-');
  const first=captureOwner(),replacement=captureOwner();
  const owned=underRoot(t,parent,()=>makeTestDir(first.owner,'owned-dir-'));
  const other=underRoot(t,parent,()=>makeTestDir(replacement.owner,'other-dir-'));
  const moved=path.join(parent,'moved-'+path.basename(owned));
  assert.equal(fs.realpathSync(path.dirname(owned)),fs.realpathSync(parent));
  assert.equal(fs.realpathSync(path.dirname(other)),fs.realpathSync(parent));
  assert.equal(path.dirname(moved),parent);
  assert.equal(fs.existsSync(moved),false);
  fs.writeFileSync(path.join(other,'sentinel.txt'),'do not remove');
  fs.renameSync(owned,moved);
  fs.renameSync(other,owned);
  try {
    assert.throws(()=>first.cleanup(),/unsafe temporary fixture cleanup/);
    assert.equal(fs.readFileSync(path.join(owned,'sentinel.txt'),'utf8'),'do not remove');
  } finally {
    fs.renameSync(owned,other);
    fs.renameSync(moved,owned);
    first.cleanup();
    replacement.cleanup();
  }
});

test('cleanup rejects a target whose resolved path is outside the recorded root',t=>{
  const parent=makeTestDir(t,'fixture-safety-realpath-'),captured=captureOwner();
  const child=underRoot(t,parent,()=>makeTestDir(captured.owner,'owned-realpath-'));
  fs.writeFileSync(path.join(child,'sentinel.txt'),'fixture');
  const realpath=fs.realpathSync;
  const mocked=t.mock.method(fs,'realpathSync',value=>value===child ? path.resolve(parent,'..','not-the-created-child') : realpath(value));
  try {
    assert.throws(()=>captured.cleanup(),/unsafe temporary fixture cleanup/);
    assert.equal(fs.readFileSync(path.join(child,'sentinel.txt'),'utf8'),'fixture');
  } finally {
    mocked.mock.restore();
    captured.cleanup();
  }
});

test('cleanup refuses a directory link that replaces the created fixture',t=>{
  const parent=makeTestDir(t,'fixture-safety-link-');
  const captured=captureOwner(),victimOwner=captureOwner();
  const child=underRoot(t,parent,()=>makeTestDir(captured.owner,'owned-link-'));
  const victim=underRoot(t,parent,()=>makeTestDir(victimOwner.owner,'victim-link-'));
  const moved=path.join(parent,'moved-'+path.basename(child));
  assert.equal(fs.realpathSync(path.dirname(child)),fs.realpathSync(parent));
  assert.equal(fs.realpathSync(path.dirname(victim)),fs.realpathSync(parent));
  assert.equal(path.dirname(moved),parent);
  assert.equal(fs.existsSync(moved),false);
  fs.writeFileSync(path.join(victim,'sentinel.txt'),'do not remove');
  fs.renameSync(child,moved);
  let linked=false;
  try {
    fs.symlinkSync(victim,child,process.platform==='win32'?'junction':'dir');
    linked=true;
    assert.throws(()=>captured.cleanup(),/unsafe temporary fixture cleanup/);
    assert.equal(fs.readFileSync(path.join(victim,'sentinel.txt'),'utf8'),'do not remove');
  } finally {
    if(linked) {
      assert.equal(fs.lstatSync(child).isSymbolicLink(),true);
      assert.equal(fs.realpathSync(child),fs.realpathSync(victim));
      fs.unlinkSync(child);
    }
    fs.renameSync(moved,child);
    captured.cleanup();
    victimOwner.cleanup();
  }
});

test('recursive cleanup does not follow directory links inside the created fixture',t=>{
  const parent=makeTestDir(t,'fixture-safety-nested-link-');
  const captured=captureOwner(),victimOwner=captureOwner();
  const child=underRoot(t,parent,()=>makeTestDir(captured.owner,'owned-nested-'));
  const victim=underRoot(t,parent,()=>makeTestDir(victimOwner.owner,'victim-nested-'));
  const link=path.join(child,'synthetic-link');
  assert.equal(path.dirname(link),child);
  fs.writeFileSync(path.join(victim,'sentinel.txt'),'do not remove');
  fs.symlinkSync(victim,link,process.platform==='win32'?'junction':'dir');
  assert.equal(fs.realpathSync(link),fs.realpathSync(victim));
  captured.cleanup();
  assert.equal(fs.readFileSync(path.join(victim,'sentinel.txt'),'utf8'),'do not remove');
  victimOwner.cleanup();
});

test('cleanup rejects a temporary root link that is redirected after creation',t=>{
  const parent=makeTestDir(t,'fixture-safety-root-link-');
  const firstOwner=captureOwner(),secondOwner=captureOwner(),childOwner=captureOwner();
  const first=underRoot(t,parent,()=>makeTestDir(firstOwner.owner,'root-first-'));
  const second=underRoot(t,parent,()=>makeTestDir(secondOwner.owner,'root-second-'));
  const link=path.join(parent,'root-link');
  assert.equal(path.dirname(link),parent);
  fs.symlinkSync(first,link,process.platform==='win32'?'junction':'dir');
  const child=underRoot(t,link,()=>makeTestDir(childOwner.owner,'root-child-'));
  fs.writeFileSync(path.join(child,'sentinel.txt'),'fixture');
  assert.equal(fs.realpathSync(link),fs.realpathSync(first));
  fs.unlinkSync(link);
  fs.symlinkSync(second,link,process.platform==='win32'?'junction':'dir');
  try {
    assert.throws(()=>childOwner.cleanup(),/unsafe temporary fixture cleanup/);
    assert.equal(fs.readFileSync(path.join(child,'sentinel.txt'),'utf8'),'fixture');
  } finally {
    assert.equal(fs.realpathSync(link),fs.realpathSync(second));
    fs.unlinkSync(link);
    fs.symlinkSync(first,link,process.platform==='win32'?'junction':'dir');
    childOwner.cleanup();
    fs.unlinkSync(link);
    firstOwner.cleanup();
    secondOwner.cleanup();
  }
});
