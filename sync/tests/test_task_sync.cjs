const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../app.html'), 'utf8');
const code = html.slice(html.indexOf('function normalizeTask('), html.indexOf('/* ================= 同步：卡片'));
const sandbox = {save(){}, uid(){return 'generated';}};
vm.createContext(sandbox); vm.runInContext(code, sandbox);
const task = {id:'task-a',title:'设计模式习题二',courseName:'设计模式',src:'zhs',done:false,
  sourceRef:{provider:'zhs',accountKey:'a',courseId:'c',kind:'homework',homeworkId:'h'}};
function merge(old,incoming){return sandbox.mergeSync({ddl:old,courses:[]},{ddl:incoming},true).ddl;}
let rows=merge([{...task,done:true}],[{...task,title:'新标题',due:'2030-11-20'}]);
assert.equal(rows.length,1);assert.equal(rows[0].done,true);assert.equal(rows[0].title,'新标题');
rows=merge(rows,[{...task,id:'task-b',sourceRef:{...task.sourceRef,homeworkId:'b'}}]);
assert.equal(rows.length,2);assert.equal(rows[0].done,true);
rows=merge([task],[{...task,submissionStatus:'submitted'}]);assert.equal(rows[0].done,true);
rows=merge([{...task,workStatusExplicit:true,workStatus:'todo',workStatusUpdatedAt:20}],[{...task,submissionStatus:'submitted'}]);
assert.equal(rows[0].done,false);assert.equal(rows[0].submissionStatus,'submitted');
rows=merge([{...task,done:true}],[{...task,workStatusExplicit:true,workStatus:'todo',workStatusUpdatedAt:30}]);assert.equal(rows[0].done,false);
rows=merge([{...task,id:'legacy',sourceRef:undefined,done:true}],[task]);assert.equal(rows.length,1);assert.equal(rows[0].id,'legacy');
rows=merge([{...task,id:'legacy-exam',sourceRef:undefined,type:'考试',done:true}],[{...task,sourceRef:{...task.sourceRef,kind:'exam'}}]);assert.equal(rows.length,1);assert.equal(rows[0].done,true);
rows=merge([task],[{...task,id:'different',sourceRef:{...task.sourceRef,accountKey:'other'}}]);assert.equal(rows.length,2);
const initial=merge([], [task]);const again=merge(initial, [task]);
assert.equal(again.length,1);assert.equal(again[0].done,false);
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
console.log('PASS: 8 frontend task regressions and all inline script syntax');
