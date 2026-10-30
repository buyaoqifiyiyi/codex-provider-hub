'use strict';
const path = require('node:path');
const { execFile } = require('node:child_process');

function resolveBindingKind(alias) {
  return new Promise(resolve => execFile('python',[path.join(__dirname,'..','scripts','binding-kind.py'), ...(alias ? [alias] : [])],
    {encoding:'utf8',timeout:2500,maxBuffer:4096,windowsHide:true,stdio:['ignore','pipe','pipe']}, (error,stdout) => {
      try {
        const result=JSON.parse(stdout);
        if (!error && ['bound','disabled','unbound','unknown'].includes(result.kind) &&
            (result.revision === null || /^[a-f0-9]{64}$/.test(result.revision))) return resolve(result);
      } catch { /* Unreadable binding is unknown. */ }
      resolve({kind:'unknown',revision:null});
    }));
}
module.exports = { resolveBindingKind };
