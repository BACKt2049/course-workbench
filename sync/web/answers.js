(function () {
  'use strict';
  var labels = {unknown:'未检查',available:'答案完整',partial:'答案部分可得',closed:'未公布',no_paper:'无试卷',need_login:'需要登录',error:'获取失败'};
  var state = {records:[],courses:[],selected:null,detail:null,checked:new Set(),account:'',job:null,sequence:0,detailSequence:0};
  var deps, root;
  function escape(value) { return String(value == null ? '' : value).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];}); }
  function node(id) { return document.getElementById(id); }
  function message(text) { node('answerStatus').textContent = text || ''; }
  function time(value) { return value ? String(value).replace('T',' ').slice(0,19) : '尚未获取'; }
  function notice(text) { deps.toast(text); }
  function filtered() {
    var course = node('answerCourse').value, status = node('answerState').value;
    var query = node('answerSearch').value.trim().toLowerCase();
    return state.records.filter(function(r){return (!course || r.courseId===course) && (!status || r.state===status) && (r.title+' '+r.courseName).toLowerCase().includes(query);});
  }
  function refreshButtons() {
    root.querySelectorAll('[data-refresh]').forEach(function(button){button.disabled=!!state.job || !deps.canSync;});
  }
  function renderList() {
    var records = filtered(), list = node('answerList');
    node('answerExport').textContent = state.checked.size ? '导出所选 '+state.checked.size+' 份' : '导出筛选结果';
    list.replaceChildren();
    if (!records.length) { list.innerHTML='<div class="answer-empty">没有符合条件的作业。<br>首次使用请先点“一键拉取”或“刷新全部”。</div>'; return; }
    records.forEach(function(record){
      var row=document.createElement('div'); row.className='answer-row'+(state.selected===record.key?' selected':'');
      var check=document.createElement('input'); check.type='checkbox'; check.checked=state.checked.has(record.key);
      check.setAttribute('aria-label','选择 '+record.title);
      check.addEventListener('change',function(){if(check.checked)state.checked.add(record.key);else state.checked.delete(record.key);renderList();});
      var open=document.createElement('button'); open.className='answer-open'; open.type='button';
      open.innerHTML='<span class="answer-title">'+escape(record.title)+'</span><span class="answer-meta">'+escape(record.courseName)+' · '+(record.answeredCount||0)+'/'+(record.questionCount||0)+' 题有答案<br>检查于 '+escape(time(record.checkedAt))+'</span><span class="answer-state '+escape(record.state)+'">'+escape(labels[record.state]||record.state)+(record.historical?' · 有历史缓存':'')+'</span>';
      open.addEventListener('click',function(){select(record);});
      row.append(check,open);list.append(row);
    });
  }
  function renderStats(counts) {
    node('answerStats').innerHTML=['available','partial','closed','no_paper','error'].map(function(s){return '<div class="answer-stat">'+labels[s]+'<b>'+(counts[s]||0)+'</b></div>';}).join('');
  }
  function report(result) {
    var box=node('answerResult');box.replaceChildren();
    if(!result)return;
    var summary=document.createElement('p'),counts=result.counts||{};
    summary.textContent='本次检查：'+Object.keys(labels).filter(function(s){return counts[s];}).map(function(s){return labels[s]+' '+counts[s];}).join(' · ')+(result.coveredCourses!=null?'；覆盖 '+result.coveredCourses+' 门已发现课程':'');box.append(summary);
    if((result.errors||[]).length){var list=document.createElement('ul');result.errors.forEach(function(error){var item=document.createElement('li');var course=(result.courses||[]).find(function(c){return c.courseId===error.courseId;});item.textContent=(error.courseId?'课程 '+(course?course.courseName:error.courseId)+' · ':'')+(error.homeworkId?'作业 '+error.homeworkId+'：':'')+(error.message||'获取失败');list.append(item);});box.append(list);}
  }
  async function load(target) {
    if(!deps.canSync) { message('请打开本地课程工作台查看答案缓存；离线文件和普通快照不包含账号答案。');return; }
    var sequence=++state.sequence;
    try {
      var data=await deps.request('/api/answers');
      if(sequence!==state.sequence)return;
      if(!data.ok)throw new Error(data.error||'读取答案失败');
      if(state.account!==data.accountKey) {++state.detailSequence;state.selected=null;state.detail=null;state.checked.clear();report(null);node('answerDetail').innerHTML='<div class="answer-empty">选择左侧作业查看题目与答案。</div>';}
      state.account=data.accountKey||'';state.records=data.records||[];state.courses=data.courses||[];
      var course=node('answerCourse').value;
      node('answerCourse').innerHTML='<option value="">全部课程</option>'+state.courses.map(function(c){return '<option value="'+escape(c.courseId)+'">'+escape(c.courseName)+'</option>';}).join('');
      node('answerCourse').value=course;
      node('answerCoverage').textContent=data.coverage||'';renderStats(data.counts||{});renderList();
      if(data.needLogin) {message('尚未连接智慧树账号，请在“今天”页扫码连接。');return;}
      if(!state.job)message('已读取本地缓存，共 '+state.records.length+' 份作业；一键拉取会自动更新答案。');
      if(target) {
        var match=state.records.find(function(r){return r.courseId===String(target.courseId)&&r.homeworkId===String(target.homeworkId);});
        if(match) {node('answerCourse').value='';node('answerState').value='';node('answerSearch').value='';await select(match);}
        else message('这份作业尚未缓存，可刷新全部后再查看。');
      } else if(state.selected) {
        var selected=state.records.find(function(r){return r.key===state.selected;});
        if(selected)await select(selected);
      }
    } catch(error) {message(error.message||'无法连接本地服务，已有缓存文件不会被删除。');}
  }
  function answer(question) {return (question.answer||[]).join('、')||'未知';}
  function renderDetail(record) {
    var box=node('answerDetail');box.replaceChildren();
    var header=document.createElement('div');
    header.innerHTML='<h2>'+escape(record.title)+'</h2><div class="answer-meta">'+escape(record.courseName)+' · '+escape(labels[record.state])+'<br>最近检查 '+escape(time(record.checkedAt))+' · 答案获取 '+escape(time(record.answersAt))+'</div>';
    box.append(header);
    if(record.historical){var history=document.createElement('div');history.className='answer-history';history.textContent='以下为本地历史缓存。本次检查结果为“'+labels[record.state]+'”，不代表当前仍可获取。';box.append(history);}
    var actions=document.createElement('div');actions.className='answer-detail-actions';
    [['刷新本份',function(){refresh({courseId:record.courseId,homeworkId:record.homeworkId});},true],['复制答案',copy,false],['导出本份',function(){download([record.key]);},false]].forEach(function(spec){var b=document.createElement('button');b.className='btn small';b.textContent=spec[0];if(spec[2])b.dataset.refresh='1';b.onclick=spec[1];actions.append(b);});
    box.append(actions);
    if(record.message){var p=document.createElement('p');p.className='answer-meta';p.textContent=record.message;box.append(p);}
    var files=record.attachments||[];
    files.forEach(function(file){if(!/^https?:\/\//i.test(file.url||''))return;var a=document.createElement('a');a.textContent='附件：'+(file.name||'查看附件');a.href=file.url;a.target='_blank';a.rel='noopener';a.style.display='block';box.append(a);});
    var questions=record.questions||[];
    if(!questions.length){var empty=document.createElement('div');empty.className='answer-empty';empty.textContent=record.state==='no_paper'?'这是提交文件或文字的作业，没有选择题试卷。':'尚未取得题目和选项答案。';box.append(empty);}
    questions.forEach(function(question,index){
      var section=document.createElement('section');section.className='answer-question';
      var title=document.createElement('h3');title.textContent=(index+1)+'. 原题号 '+question.no+' · '+(question.content||'平台未提供题干，请参见附件');section.append(title);
      (question.options||[]).forEach(function(option){var p=document.createElement('div');p.className='answer-option'+(option.correct===true?' correct':'');p.textContent=option.label+'. '+(option.text||'（平台未提供选项文本）')+(option.correct===true?' ✓':'');section.append(p);});
      var value=document.createElement('div');value.className='answer-value';value.textContent='答案：'+answer(question)+(question.state==='unsupported'?'（本期不支持该题型）':'');section.append(value);box.append(section);
    });
    if(record.gradeCard&&record.gradeCard.length){var grade=document.createElement('p');grade.className='answer-meta';grade.textContent='成绩卡有 '+record.gradeCard.length+' 条作答记录；成绩对错不用于推断选项答案。';box.append(grade);}
    refreshButtons();
  }
  async function select(record) {
    var sequence=++state.detailSequence;state.selected=record.key;renderList();
    try {var data=await deps.request('/api/answers/'+encodeURIComponent(record.courseId)+'/'+encodeURIComponent(record.homeworkId));if(sequence!==state.detailSequence)return;if(!data.ok)throw new Error(data.error);state.detail=data.record;renderDetail(data.record);}
    catch(error){message(error.message||'无法读取作业');}
  }
  async function refresh(scope) {
    if(!deps.canSync||state.job)return;
    try {var data=await deps.request('/api/answers/refresh',{method:'POST',body:scope||{}});if(!data.ok)throw new Error(data.error);state.job=data.jobId;refreshButtons();message('答案同步已开始…');poll();}
    catch(error){message(error.message||'未能开始刷新');}
  }
  async function poll() {
    if(!state.job)return;
    try {var data=await deps.request('/api/answers/jobs/'+state.job);if(data.error&&data.state!=='error')throw new Error(data.error);
      if(data.state==='running'){message((data.step||'正在获取答案')+(data.total?' '+data.current+'/'+data.total:''));setTimeout(poll,1000);return;}
      state.job=null;refreshButtons();await load();report(data.result);message(data.state==='done'?'答案同步完成。':(data.error||'部分答案未取得，已保留可用缓存。'));
    }catch(error){state.job=null;refreshButtons();message(error.message||'连接中断，请刷新缓存列表。');}
  }
  async function copy() {
    if(!state.detail)return;
    var record=state.detail;
    var text=record.title+'\n'+(record.historical?'本地历史缓存 · ':'')+'答案获取：'+time(record.answersAt)+'\n'+(record.questions||[]).map(function(q,i){return (i+1)+'. 原题号'+q.no+'：'+answer(q);}).join('\n');
    try {if(navigator.clipboard)await navigator.clipboard.writeText(text);else{var ta=document.createElement('textarea');ta.value=text;document.body.append(ta);ta.select();if(!document.execCommand('copy'))throw new Error();ta.remove();}notice('答案已复制');}catch(error){notice('复制失败，可使用导出功能');}
  }
  async function download(keys) {
    keys=keys|| (state.checked.size?Array.from(state.checked):filtered().map(function(r){return r.key;}));
    if(!keys.length){notice('先选择或筛选作业');return;}
    try {var format=node('answerFormat').value;
      var response=await fetch(deps.api+'/api/answers/export',{method:'POST',headers:{'X-WB-Token':deps.token,'Content-Type':'application/json'},body:JSON.stringify({keys:keys,format:format})});
      if(!response.ok){var error=await response.json();throw new Error(error.error||'导出失败');}
      var blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='智慧树答案.'+format;document.body.append(a);a.click();a.remove();setTimeout(function(){URL.revokeObjectURL(url);},1000);notice('导出完成');
    }catch(error){notice(error.message||'导出失败');}
  }
  function init(options) {
    deps=options;root=node('view-answers');
    root.innerHTML='<div class="greeting"><div><h1>答案中心</h1><div class="lead">按课程整理作业与答案 · 同步时自动更新 · 支持已完成作业</div></div><div><button class="btn primary" id="answerRefresh" data-refresh>刷新全部</button> <button class="btn" id="answerReload">读取缓存</button></div></div><div id="answerStats" class="answer-stats"></div><div class="answer-coverage" id="answerCoverage"></div><div class="answer-tools"><select id="answerCourse" aria-label="筛选课程"><option value="">全部课程</option></select><input id="answerSearch" type="search" aria-label="搜索作业" placeholder="搜索作业或课程"><select id="answerState" aria-label="筛选答案状态"><option value="">全部状态</option>'+Object.keys(labels).map(function(s){return '<option value="'+s+'">'+labels[s]+'</option>';}).join('')+'</select><select id="answerFormat" aria-label="导出格式"><option value="txt">TXT</option><option value="md">Markdown</option><option value="json">JSON</option></select><button class="btn" id="answerExport">导出筛选结果</button></div><div id="answerStatus" role="status" aria-live="polite"></div><div class="answer-grid"><div class="answer-list" id="answerList"></div><div class="card answer-detail" id="answerDetail"><div class="answer-empty">选择左侧作业查看题目与答案。</div></div></div>';
    var resultBox=document.createElement('div');resultBox.id='answerResult';resultBox.className='answer-result';node('answerStatus').after(resultBox);
    node('answerRefresh').onclick=function(){refresh({});};node('answerReload').onclick=function(){load();};node('answerExport').onclick=function(){download();};
    ['answerCourse','answerState'].forEach(function(id){node(id).onchange=renderList;});node('answerSearch').oninput=renderList;
    refreshButtons();
    if(!deps.canSync){node('answerReload').disabled=true;node('answerExport').disabled=true;}
  }
  window.AnswerCenter={show:load,synced:async function(result){if(!root.hidden)await load();if(result)report(result);},refresh:refresh};
  init({request:wbApi,canSync:WB.canSync,api:WB.api,token:WB.token,toast:toast});
})();
