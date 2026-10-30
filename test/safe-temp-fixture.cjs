'use strict';
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');

function samePath(first,second) {
  const normalize=value=>process.platform==='win32' ? path.normalize(value).toLowerCase() : path.normalize(value);
  return normalize(first)===normalize(second);
}

function sameIdentity(first,second) {
  return first.dev===second.dev && first.ino===second.ino && first.birthtimeNs===second.birthtimeNs;
}

// Only a fresh direct child created by this call can ever be removed.
function makeTestDir(owner,prefix) {
  if (!owner || typeof owner.after!=='function' || typeof prefix!=='string' || !/^[A-Za-z0-9._-]+-$/.test(prefix)) {
    throw new Error('invalid temporary fixture owner or prefix');
  }
  const requestedRoot=process.env.NEUTRAL_TEST_TMP || os.tmpdir();
  if (!path.isAbsolute(requestedRoot)) throw new Error('temporary fixture root must be absolute');
  const realRoot=fs.realpathSync(requestedRoot);
  const rootIdentity=fs.statSync(realRoot,{bigint:true});
  if (!rootIdentity.isDirectory()) throw new Error('temporary fixture root must be a directory');
  const target=fs.mkdtempSync(path.join(realRoot,prefix));
  const originalReal=fs.realpathSync(target);
  const originalIdentity=fs.lstatSync(target,{bigint:true});
  if (!path.isAbsolute(originalReal) || !samePath(target,originalReal) ||
      !samePath(path.dirname(originalReal),realRoot) || !path.basename(originalReal).startsWith(prefix) ||
      path.basename(originalReal).length<=prefix.length || !originalIdentity.isDirectory() || originalIdentity.isSymbolicLink()) {
    throw new Error('unsafe temporary fixture creation');
  }
  let removed=false;
  owner.after(()=>{
    if (removed) return;
    let currentIdentity;
    try { currentIdentity=fs.lstatSync(target,{bigint:true}); }
    catch (error) { if(error.code==='ENOENT') return; throw error; }
    const currentRoot=fs.realpathSync(requestedRoot);
    const currentRootIdentity=fs.statSync(currentRoot,{bigint:true});
    const currentReal=fs.realpathSync(target);
    if (!path.isAbsolute(currentReal) || !samePath(currentRoot,realRoot) || !sameIdentity(currentRootIdentity,rootIdentity) ||
        !samePath(currentReal,originalReal) || !samePath(path.dirname(currentReal),realRoot) ||
        !path.basename(currentReal).startsWith(prefix) || path.basename(currentReal).length<=prefix.length ||
        !currentIdentity.isDirectory() || currentIdentity.isSymbolicLink() || !sameIdentity(currentIdentity,originalIdentity)) {
      throw new Error('unsafe temporary fixture cleanup');
    }
    fs.rmSync(currentReal,{recursive:true,force:true});
    removed=true;
  });
  return target;
}

module.exports={makeTestDir};
