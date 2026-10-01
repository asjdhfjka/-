let currentUser = null;
let currentView = '';
let reviewHistory = [];
let chatHistory = [];
let submissions = [];
let submissionFilter = 'all';
let ruleDraftBatches = [];

const roleNames = {student:'学生', reviewer:'审核员', admin:'管理员'};
const statusNames = {submitted:'待处理', reviewing:'审核中', changes_requested:'需修改', approved:'已通过'};
const navByRole = {
    student: [
        ['student-home','⌂','首页'], ['chat','▣','智能问答'], ['precheck','▤','材料预审'], ['records','◷','我的记录']
    ],
    reviewer: [
        ['reviewer-home','⌂','审核工作台'], ['submissions','▣','材料审核'], ['rules','◇','规则中心'],
        ['knowledge','▤','知识库维护'], ['chat','◷','智能助手']
    ],
    admin: [
        ['admin-home','⌂','系统总览'], ['users','♙','用户管理'], ['submissions','▣','材料流转'],
        ['rules','◇','规则中心'], ['knowledge','▤','知识库维护']
    ]
};

function esc(value) {
    return String(value == null ? '' : value)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function formatDate(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value).replace('T',' ').slice(0,16);
    return new Intl.DateTimeFormat('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}).format(date);
}

function toast(message, type='') {
    const el = document.getElementById('toast');
    el.textContent = message;
    el.className = `toast show ${type}`;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.className = 'toast', 2800);
}

async function api(url, options={}) {
    const response = await fetch(url, options);
    let data = {};
    try { data = await response.json(); } catch (_) { data = {}; }
    if (response.status === 401) {
        showLogin('登录状态已失效，请重新登录');
        throw new Error('请先登录');
    }
    if (!response.ok) throw new Error(data.detail || data.error || `请求失败（${response.status}）`);
    return data;
}

function fillDemoAccount(username) {
    document.getElementById('login-username').value = username;
    document.getElementById('login-password').value = 'Demo@123456';
}

function showLogin(message='') {
    currentUser = null;
    document.getElementById('app-shell').classList.add('hidden');
    document.getElementById('login-screen').classList.remove('hidden');
    document.getElementById('login-error').textContent = message;
}

async function initializeAuth() {
    bindStaticEvents();
    try {
        const data = await api('/auth/me');
        applyUser(data.user);
    } catch (_) {
        showLogin();
    }
}

async function login(event) {
    event.preventDefault();
    const button = document.getElementById('login-submit');
    const error = document.getElementById('login-error');
    button.disabled = true; button.textContent = '正在登录…'; error.textContent = '';
    try {
        const data = await api('/auth/login', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({
                username:document.getElementById('login-username').value.trim(),
                password:document.getElementById('login-password').value
            })
        });
        applyUser(data.user);
    } catch (err) {
        error.textContent = err.message || '登录失败';
    } finally {
        button.disabled = false; button.textContent = '进入系统';
    }
}

async function logout() {
    try { await fetch('/auth/logout', {method:'POST'}); } catch (_) {}
    document.querySelectorAll('.modal').forEach(el => el.classList.remove('open'));
    showLogin('已安全退出');
}

function applyUser(user) {
    currentUser = user;
    document.getElementById('login-screen').classList.add('hidden');
    document.getElementById('app-shell').classList.remove('hidden');
    document.querySelectorAll('[data-user-name]').forEach(el => el.textContent = user.display_name || user.username);
    document.getElementById('profile-name').textContent = user.display_name || user.username;
    document.getElementById('profile-meta').textContent = `${user.college || '广东工业大学'} · ${roleNames[user.role] || user.role}`;
    document.getElementById('profile-avatar').textContent = (user.display_name || user.username || '同').slice(-1);
    document.getElementById('mobile-role').textContent = `${roleNames[user.role] || ''}工作台`;
    document.getElementById('rule-reviewer').value = user.display_name || user.username;
    renderNavigation();
    loadRecentChats();
    const first = navByRole[user.role]?.[0]?.[0] || 'chat';
    showView(first);
}

function renderNavigation() {
    const nav = document.getElementById('role-nav');
    nav.innerHTML = (navByRole[currentUser.role] || []).map(([view,icon,label]) =>
        `<button class="nav-item" data-view-target="${view}" onclick="showView('${view}')"><span class="nav-icon">${icon}</span>${label}</button>`
    ).join('');
    document.getElementById('new-chat-button').style.display = currentUser.role === 'admin' ? 'none' : '';
    document.getElementById('recent-section').style.display = currentUser.role === 'admin' ? 'none' : '';
}

function allowedView(view) {
    return (navByRole[currentUser?.role] || []).some(item => item[0] === view);
}

function showView(view) {
    if (!currentUser || !allowedView(view)) return;
    currentView = view;
    document.querySelectorAll('.page-view').forEach(el => el.classList.add('hidden'));
    const target = document.getElementById(`view-${view}`);
    if (target) target.classList.remove('hidden');
    document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.viewTarget === view));
    document.getElementById('sidebar').classList.remove('open');
    if (view === 'student-home' || view === 'precheck') loadReviewHistory();
    if (view === 'records') loadReviewHistory().then(() => renderRecordReviews());
    if (view === 'reviewer-home') loadReviewerDashboard();
    if (view === 'submissions') loadSubmissions();
    if (view === 'rules') loadRuleSummary();
    if (view === 'admin-home') loadAdminDashboard();
    if (view === 'users') loadUsers();
}

function toggleSidebar() { document.getElementById('sidebar').classList.toggle('open'); }
function showProfileMenu() { if (window.confirm(`当前账号：${currentUser.display_name}\n是否退出登录？`)) logout(); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
function openReviewPicker() { document.getElementById('review-upload').click(); }

function bindStaticEvents() {
    document.getElementById('submission-filters').addEventListener('click', event => {
        const button = event.target.closest('button[data-status]');
        if (!button) return;
        submissionFilter = button.dataset.status;
        document.querySelectorAll('#submission-filters button').forEach(el => el.classList.toggle('active', el === button));
        renderSubmissions();
    });
}

/* 问答 */
function startNewChat() {
    if (!currentUser || currentUser.role === 'admin') return;
    document.getElementById('messages').innerHTML = `<div class="chat-row assistant"><span class="chat-avatar">AI</span><div class="message-bubble"><b>你好，我是工大智政 AI 助手。</b><p>你可以询问学校政策、报名通知和办事流程。</p></div></div>`;
    showView('chat');
    document.getElementById('user-input').focus();
}

function appendChat(role, content, isHtml=false) {
    const messages = document.getElementById('messages');
    const row = document.createElement('div');
    row.className = `chat-row ${role}`;
    if (role === 'assistant') row.innerHTML = `<span class="chat-avatar">AI</span><div class="message-bubble"></div>`;
    else row.innerHTML = `<div class="message-bubble"></div>`;
    const bubble = row.querySelector('.message-bubble');
    if (isHtml) bubble.innerHTML = content; else bubble.textContent = content;
    messages.appendChild(row); messages.scrollTop = messages.scrollHeight;
    return bubble;
}

function safeMarkdown(text, sources=[]) {
    let escaped = esc(text);
    escaped = escaped.replace(/\[来源(\d+)\]/g, (match, number) => {
        const source = sources.find(item => Number(item.index) === Number(number));
        const url = source && typeof source.url === 'string' ? source.url : '';
        if (!url.startsWith('/preview/') && !/^https?:\/\//i.test(url)) return match;
        return `<a href="${esc(url)}" target="_blank" rel="noopener">[来源${number}]</a>`;
    });
    const html = window.marked ? marked.parse(escaped) : escaped.replace(/\n/g,'<br>');
    const template = document.createElement('template'); template.innerHTML = html;
    template.content.querySelectorAll('a').forEach(link => {
        const href = link.getAttribute('href') || '';
        if (!href.startsWith('/preview/') && !/^https?:\/\//i.test(href)) link.replaceWith(document.createTextNode(link.textContent));
        else { link.target = '_blank'; link.rel = 'noopener'; }
    });
    return template.innerHTML;
}

function sendHomeQuestion(event) {
    event.preventDefault();
    const input = document.getElementById('home-question');
    const text = input.value.trim(); if (!text) return;
    input.value = ''; showView('chat');
    document.getElementById('user-input').value = text;
    sendMessage();
}

async function sendMessage(event) {
    if (event) event.preventDefault();
    const input = document.getElementById('user-input');
    const button = document.getElementById('send-btn');
    const question = input.value.trim(); if (!question) return;
    appendChat('user', question); input.value=''; input.disabled=true; button.disabled=true; button.textContent='思考中';
    const bubble = appendChat('assistant','<span class="loading-dots">正在检索知识库 </span>',true);
    let buffer='', answer='', sources=[], parsedSources=false;
    try {
        const recent = Array.from(document.querySelectorAll('#messages .message-bubble')).slice(-6,-1).map(el => el.innerText.slice(0,260)).join('\n---\n');
        const response = await fetch(`/chat/stream?user_input=${encodeURIComponent(question)}&history=${encodeURIComponent(recent)}`, {method:'POST'});
        if (response.status === 401) { showLogin('登录状态已失效，请重新登录'); throw new Error('请先登录'); }
        if (!response.ok || !response.body) throw new Error(`服务响应异常（${response.status}）`);
        const reader=response.body.getReader(), decoder=new TextDecoder('utf-8'); bubble.innerHTML='';
        while (true) {
            const {done,value}=await reader.read(); if (done) break;
            buffer += decoder.decode(value,{stream:true});
            if (!parsedSources) {
                const start=buffer.indexOf('__SOURCES__'), end=buffer.indexOf('__END__');
                if (start >= 0 && end >= 0) {
                    try { sources=JSON.parse(buffer.slice(start+11,end)); } catch (_) { sources=[]; }
                    buffer=buffer.slice(end+7); parsedSources=true;
                } else if (start >= 0) continue; else parsedSources=true;
            }
            if (buffer) { answer+=buffer; buffer=''; bubble.innerHTML=safeMarkdown(answer,sources); document.getElementById('messages').scrollTop=999999; }
        }
        if (buffer) answer+=buffer;
        bubble.innerHTML=safeMarkdown(answer || '暂时没有生成答案，请稍后重试。',sources);
        if (sources.length) {
            const used=new Set(Array.from(answer.matchAll(/\[来源(\d+)\]/g)).map(item=>Number(item[1])));
            const final=(used.size?sources.filter(item=>used.has(Number(item.index))):sources.slice(0,3));
            const sourceBox=document.createElement('div'); sourceBox.className='sources';
            sourceBox.innerHTML='<b>参考来源</b><br>'+final.map(item=>{
                const url=String(item.url||''); if(!url.startsWith('/preview/')&&!/^https?:\/\//i.test(url)) return esc(item.title||'未命名来源');
                return `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(item.title||'查看原文')}</a>`;
            }).join(''); bubble.appendChild(sourceBox);
        }
        loadRecentChats();
    } catch (err) {
        bubble.textContent=err.message || '网络错误，请稍后重试。';
    } finally {
        input.disabled=false; button.disabled=false; button.textContent='发送'; input.focus();
    }
}

async function loadRecentChats() {
    if (!currentUser || currentUser.role === 'admin') return;
    try {
        const data=await api('/me/chats'); chatHistory=data.items||[];
        const container=document.getElementById('recent-chats');
        container.innerHTML=chatHistory.slice(0,5).map((item,index)=>`<div class="recent-chat" onclick="openPastChat(${index})">▣　${esc(item.question)}</div>`).join('') || '<div class="side-empty">暂无记录</div>';
    } catch (_) {}
}

function openPastChat(index) {
    const item=chatHistory[index]; if(!item)return; startNewChat();
    appendChat('user',item.question); const bubble=appendChat('assistant','',true); bubble.innerHTML=safeMarkdown(item.answer,item.sources||[]);
}

/* 材料预审与个人记录 */
async function reviewFile() {
    const input=document.getElementById('review-upload'), file=input.files[0]; if(!file)return;
    showView(currentUser.role==='student'?'precheck':'chat');
    const progress=document.getElementById('review-progress');
    progress.classList.remove('hidden'); progress.innerHTML='<span class="loading-dots">正在识别材料类型、抽取事实并执行规则校验 </span>';
    const form=new FormData(); form.append('file',file);
    try {
        const data=await api('/review',{method:'POST',body:form});
        if(!data.review_result)throw new Error(data.error||'审查没有返回结果');
        progress.innerHTML=`完成：${esc(data.review_result.审查结论)}，综合评分 ${esc(data.review_result.总评分 ?? '—')}`;
        await loadReviewHistory();
        openReviewReport(data.review_result.记录ID, data.review_result, file.name);
    } catch(err){ progress.textContent=err.message||'审查失败'; toast(progress.textContent,'error'); }
    finally{input.value='';}
}

async function loadReviewHistory() {
    if(!currentUser)return;
    try {
        const data=await api('/me/reviews'); reviewHistory=data.items||[];
        renderPrecheckHistory(); renderHomeLatest();
    } catch(err) {
        const target=document.getElementById('precheck-history'); if(target)target.innerHTML=`<div class="empty-state">${esc(err.message)}</div>`;
    }
}

function reviewStatus(item) {
    if(item.submission_status)return [statusNames[item.submission_status]||item.submission_status,item.submission_status];
    return ['仅预审','none'];
}

function renderPrecheckHistory() {
    const target=document.getElementById('precheck-history'); if(!target)return;
    target.innerHTML=reviewHistory.slice(0,9).map((item,index)=>{
        const [status,cls]=reviewStatus(item);
        return `<article class="record-card" onclick="openReviewReportByIndex(${index})"><div class="record-card-head"><h4>${esc(item.filename)}</h4><span class="status-badge status-${cls}">${esc(status)}</span></div><p>${esc(item.material_type||'待识别')} · ${formatDate(item.created_at)}</p><div class="record-card-foot"><span>${esc(item.outcome||'待核验')}</span><b class="score-pill">${esc(item.total_score??'—')}<small> 分</small></b></div></article>`;
    }).join('')||'<div class="empty-state">还没有预审记录，上传一份材料开始检查吧。</div>';
}

function renderHomeLatest() {
    const target=document.getElementById('home-review-latest'); if(!target)return;
    const item=reviewHistory[0];
    if(!item){target.innerHTML='<div class="empty-state compact">还没有预审记录</div>';return;}
    const [status,cls]=reviewStatus(item);
    target.innerHTML=`<div class="latest-review-item" onclick="openReviewReportByIndex(0)" role="button"><span class="file-badge">W</span><div><b>${esc(item.filename)}</b><small>${esc(status)} · ${formatDate(item.created_at)}</small></div><span class="score-pill">${esc(item.total_score??'—')}</span></div>`;
}

function renderRecordReviews() {
    const container=document.getElementById('record-content');
    container.innerHTML=reviewHistory.map((item,index)=>{const [status,cls]=reviewStatus(item);return `<div class="history-row"><div class="history-title"><b>${esc(item.filename)}</b><small>${esc(item.material_type||'材料')} · ${formatDate(item.created_at)}</small></div><span>${esc(item.outcome||'待核验')} · ${esc(item.total_score??'—')}分</span><span class="status-badge status-${cls}">${esc(status)}</span><button class="row-action" onclick="openReviewReportByIndex(${index})">查看详情</button></div>`}).join('')||'<div class="empty-state">暂无材料记录</div>';
}

function renderRecordChats() {
    const container=document.getElementById('record-content');
    container.innerHTML=chatHistory.map((item,index)=>`<div class="history-row"><div class="history-title"><b>${esc(item.question)}</b><small>${formatDate(item.created_at)}</small></div><span>${(item.sources||[]).length} 个来源</span><span></span><button class="row-action" onclick="openPastChat(${index})">打开对话</button></div>`).join('')||'<div class="empty-state">暂无问答记录</div>';
}

function switchRecordTab(kind,button){document.querySelectorAll('.record-tabs button').forEach(el=>el.classList.toggle('active',el===button));kind==='reviews'?renderRecordReviews():renderRecordChats();}
function openReviewReportByIndex(index){const item=reviewHistory[index];if(item)openReviewReport(item.id,item.result,item.filename,item);}

function renderReviewResult(result, historyItem={}) {
    const hard=result.硬规则校验||[], failed=hard.filter(x=>!x.passed&&!x.not_applicable&&!x.needs_review), pending=hard.filter(x=>x.needs_review), passed=hard.filter(x=>x.passed);
    const grade=result.审查结论||'待核验', heroClass=grade==='通过'?'':(grade==='待核验'?'pending':'warning');
    const issues=failed.map(item=>`<div class="issue-card"><b>${esc(item.rule_name||'未通过规则')}</b><p>${esc(item.message||item.reason||'未满足规则要求')}</p><div class="fix-box"><b>怎么改：</b>${esc(item.fix_suggestion||'请按规则要求补充或修改材料后重新预审。')}</div></div>`).join('');
    const pendings=pending.map(item=>`<div class="issue-card pending"><b>${esc(item.rule_name||'待核验规则')}</b><p>${esc(item.message||'当前材料信息不足，需要人工确认。')}</p><div class="fix-box"><b>建议：</b>${esc(item.fix_suggestion||'请补充清晰、明确的证明信息。')}</div></div>`).join('');
    const sources=(result.审查依据||[]).map(source=>source.available&&String(source.url||'').startsWith('/preview/')?`<a href="${esc(source.url)}" target="_blank" rel="noopener">▤ ${esc(source.title||'制度原文')}</a>`:`<span>▤ ${esc(source.title||'制度依据')}（原文未归档）</span>`).join('');
    const status=historyItem.submission_status;
    const canSubmit=currentUser?.role==='student'&&result.记录ID&&(status==null||status==='changes_requested');
    return `<div class="report-hero ${heroClass}"><div class="report-score">${esc(result.总评分??'—')}<small> 分</small></div><h3>${esc(grade)}</h3><div class="report-meta">${esc(result.材料类型||'通用材料')} · 硬规则 ${esc(result.硬规则得分??'—')}/100 · 待核验 ${pending.length} 项</div></div>
        ${failed.length?`<details class="report-section" open><summary><span>需要修改</span><span>${failed.length} 项</span></summary><div class="report-body">${issues}</div></details>`:''}
        ${pending.length?`<details class="report-section"><summary><span>待人工核验</span><span>${pending.length} 项</span></summary><div class="report-body">${pendings}</div></details>`:''}
        <details class="report-section"><summary><span>已完成核验</span><span>${passed.length} 项通过</span></summary><div class="report-body">${passed.map(item=>`<p>✓ ${esc(item.rule_name)}</p>`).join('')||'<p>暂无已通过规则</p>'}</div></details>
        ${sources?`<details class="report-section"><summary><span>审查依据</span><span>${(result.审查依据||[]).length} 份</span></summary><div class="report-body source-list">${sources}</div></details>`:''}
        <div class="report-actions"><button class="secondary-button" onclick="closeModal('review-modal')">关闭</button>${canSubmit?`<button class="primary-button" onclick="submitReview('${esc(result.记录ID)}')">提交正式审核</button>`:''}</div>`;
}

function openReviewReport(reviewId,result,filename,historyItem={}) {
    document.getElementById('review-modal-title').textContent=filename||'材料审查报告';
    document.getElementById('review-modal-body').innerHTML=renderReviewResult(result,historyItem);
    document.getElementById('review-modal').classList.add('open');
}

async function submitReview(reviewId) {
    try { await api(`/me/reviews/${encodeURIComponent(reviewId)}/submit`,{method:'POST'}); toast('已提交负责人审核','success'); closeModal('review-modal'); await loadReviewHistory(); }
    catch(err){toast(err.message,'error');}
}

/* 正式审核队列 */
async function loadSubmissions() {
    try {const data=await api('/submissions');submissions=data.items||[];renderSubmissions();}
    catch(err){document.getElementById('submission-list').innerHTML=`<div class="empty-state">${esc(err.message)}</div>`;}
}

function renderSubmissions() {
    const target=document.getElementById('submission-list'), list=submissionFilter==='all'?submissions:submissions.filter(item=>item.status===submissionFilter);
    target.innerHTML=`<div class="submission-table-row header"><span>材料 / 提交人</span><span>类型</span><span>AI 评分</span><span>状态</span><span>操作</span></div>`+list.map(item=>`<div class="submission-table-row"><div class="submission-person"><b>${esc(item.filename)}</b><small>${esc(item.student_name||'当前用户')} · ${formatDate(item.submitted_at)}</small></div><span>${esc(item.material_type||'待识别')}</span><b>${esc(item.total_score??'—')} 分</b><span class="status-badge status-${esc(item.status)}">${esc(statusNames[item.status]||item.status)}</span><button class="row-action" onclick="openSubmission('${esc(item.id)}')">${currentUser.role==='student'?'查看':'处理'}</button></div>`).join('');
    if(!list.length)target.innerHTML='<div class="empty-state">当前筛选条件下没有材料</div>';
}

async function openSubmission(id) {
    try {
        const data=await api(`/submissions/${encodeURIComponent(id)}`), item=data.submission;
        const report=renderReviewResult(item.result,{submission_status:item.status});
        const controls=currentUser.role==='student'?`<div class="report-actions"><button class="secondary-button" onclick="closeModal('submission-modal')">关闭</button></div>`:
            `<textarea id="submission-note" class="review-note" placeholder="填写人工审核意见；退回修改时请说明需要补充的内容">${esc(item.reviewer_note||'')}</textarea><div class="decision-actions"><button onclick="decideSubmission('${esc(id)}','reviewing')">标记审核中</button><button class="request-change" onclick="decideSubmission('${esc(id)}','changes_requested')">退回修改</button><button class="approve" onclick="decideSubmission('${esc(id)}','approved')">审核通过</button></div>`;
        document.getElementById('submission-modal-body').innerHTML=`<div class="submission-detail"><div class="detail-box"><b>${esc(item.student_name)}</b><small>${esc(item.college||'未填写学院')}</small></div><div class="detail-box"><b>${esc(statusNames[item.status]||item.status)}</b><small>提交于 ${formatDate(item.submitted_at)}</small></div></div><div class="report-actions" style="justify-content:flex-start"><a class="secondary-button" href="/me/materials/${encodeURIComponent(item.review_id)}/file" target="_blank">查看原始材料</a></div>${report}${controls}`;
        document.getElementById('submission-modal').classList.add('open');
    } catch(err){toast(err.message,'error');}
}

async function decideSubmission(id,status) {
    const note=document.getElementById('submission-note')?.value.trim()||'';
    if(status==='changes_requested'&&!note){toast('退回修改时请填写具体修改意见','error');return;}
    try {await api(`/submissions/${encodeURIComponent(id)}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status,note})});toast('审核状态已更新','success');closeModal('submission-modal');await loadSubmissions();if(currentView==='reviewer-home')loadReviewerDashboard();}
    catch(err){toast(err.message,'error');}
}

async function loadReviewerDashboard() {
    try {
        const [submissionData,statsData]=await Promise.all([api('/submissions'),api('/stats')]); submissions=submissionData.items||[];
        const counts=Object.fromEntries(['submitted','reviewing','changes_requested','approved'].map(key=>[key,submissions.filter(item=>item.status===key).length]));
        document.getElementById('reviewer-kpis').innerHTML=kpiCards([['待处理',counts.submitted,'需要尽快处理',true],['审核中',counts.reviewing,'正在人工核验'],['需修改',counts.changes_requested,'已退回学生'],['模型调用',statsData.llm?.calls??0,'本次服务启动后']]);
        document.getElementById('reviewer-latest').innerHTML=submissions.slice(0,5).map(item=>`<div class="submission-row"><div class="submission-person"><b>${esc(item.filename)}</b><small>${esc(item.student_name)} · ${formatDate(item.submitted_at)}</small></div><span>${esc(item.material_type||'待识别')}</span><span class="status-badge status-${item.status}">${esc(statusNames[item.status])}</span><button class="row-action" onclick="openSubmission('${item.id}')">处理</button></div>`).join('')||'<div class="empty-state compact">暂无待审核材料</div>';
    } catch(err){toast(err.message,'error');}
}

function kpiCards(items){return items.map(([label,value,note,accent])=>`<div class="kpi-card ${accent?'accent':''}"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`).join('');}

/* 用户与管理总览 */
async function loadUsers() {
    const target=document.getElementById('user-list');
    try {const data=await api('/auth/users');renderUsers(data.users||[],target);}
    catch(err){target.innerHTML=`<div class="empty-state">${esc(err.message)}</div>`;}
}

function renderUsers(users,target=document.getElementById('user-list')) {
    target.innerHTML=users.map(user=>{const self=user.id===currentUser.id;return `<div class="user-row"><div class="user-main"><b>${esc(user.display_name)} <small>@${esc(user.username)}</small></b><small>${esc(user.college||'未填写部门')}</small></div><span>${esc(roleNames[user.role]||user.role)}</span><span class="status-badge ${user.is_active?'status-approved':'status-changes_requested'}">${user.is_active?'正常':'已停用'}</span><button class="user-toggle ${user.is_active?'':'enable'}" ${self?'disabled':''} onclick="setUserActive('${esc(user.id)}',${user.is_active?'false':'true'})">${self?'当前账号':(user.is_active?'停用':'启用')}</button></div>`}).join('')||'<div class="empty-state">暂无用户</div>';
}

async function createUser(event) {
    event.preventDefault();const button=document.getElementById('create-user-btn'),message=document.getElementById('user-admin-message');button.disabled=true;message.textContent='正在创建…';
    try {const data=await api('/auth/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.getElementById('new-username').value.trim(),display_name:document.getElementById('new-display-name').value.trim(),password:document.getElementById('new-password').value,role:document.getElementById('new-role').value,college:document.getElementById('new-college').value.trim()})});event.target.reset();message.style.color='#059669';message.textContent=`账号 ${data.user.username} 已创建`;await loadUsers();}
    catch(err){message.style.color='#c2414c';message.textContent=err.message;}finally{button.disabled=false;}
}

async function setUserActive(id,active){try{await api(`/auth/users/${encodeURIComponent(id)}/active`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({active})});await loadUsers();toast(active?'账号已启用':'账号已停用','success');}catch(err){toast(err.message,'error');}}

async function loadAdminDashboard() {
    try {
        const [usersData,submissionData,statsData]=await Promise.all([api('/auth/users'),api('/submissions'),api('/stats')]);
        const users=usersData.users||[], records=submissionData.items||[];
        document.getElementById('admin-kpis').innerHTML=kpiCards([['用户总数',users.length,'当前已建立账号',true],['待审材料',records.filter(x=>x.status==='submitted').length,'等待负责人处理'],['已通过',records.filter(x=>x.status==='approved').length,'正式审核通过'],['模型调用',statsData.llm?.calls??0,'本次服务启动后']]);
        renderUsers(users.slice(0,5),document.getElementById('admin-user-preview'));
    } catch(err){toast(err.message,'error');}
}

/* 知识库批量上传 */
async function uploadFiles() {
    const input=document.getElementById('file-upload'),files=Array.from(input.files||[]),target=document.getElementById('upload-progress');if(!files.length)return;
    const results=[];target.innerHTML=`<div class="review-progress">准备上传 ${files.length} 个文件…</div>`;
    for(let i=0;i<files.length;i++){
        const form=new FormData();form.append('file',files[i]);
        try{const data=await api('/upload',{method:'POST',body:form});results.push({name:files[i].name,ok:!data.error,message:data.message||data.error||'上传完成'});}catch(err){results.push({name:files[i].name,ok:false,message:err.message});}
        target.innerHTML=`<div class="review-progress">进度 ${i+1}/${files.length}</div>`+results.map(item=>`<div class="latest-review-item"><span>${item.ok?'✓':'×'}</span><div><b>${esc(item.name)}</b><small>${esc(item.message)}</small></div></div>`).join('');
    }
    input.value='';toast(`批量上传完成：成功 ${results.filter(x=>x.ok).length}，失败 ${results.filter(x=>!x.ok).length}`,results.some(x=>!x.ok)?'error':'success');
}

/* 规则草稿 */
function closeRuleDrafts(){document.getElementById('rule-modal').classList.remove('open');}
async function openRuleDrafts(){document.getElementById('rule-modal').classList.add('open');await loadRuleDrafts();}
function draftStatusText(status){return({pending:'待确认',approved:'已通过',rejected:'已驳回',pending_review:'审核中',ready_to_publish:'待发布',published:'已发布'})[status]||status||'未知';}
function ruleExtra(rule){const common=new Set(['id','name','type','field','severity','message','scene','condition','source_excerpt','source_page']);return Object.fromEntries(Object.entries(rule||{}).filter(([key])=>!common.has(key)));}
function renderRuleDrafts(){
    const container=document.getElementById('rule-draft-list');if(!ruleDraftBatches.length){container.innerHTML='<div class="draft-empty">目前没有规则草稿。上传制度文件后，候选规则会出现在这里。</div>';return;}
    const activeIndex=ruleDraftBatches.findIndex(batch=>batch.status!=='published');
    container.innerHTML=ruleDraftBatches.map((batch,batchIndex)=>{const published=batch.status==='published',rules=Array.isArray(batch.rules)?batch.rules:[],approved=rules.filter(x=>x.status==='approved').length,rejected=rules.filter(x=>x.status==='rejected').length,pending=rules.length-approved-rejected;
        const cards=rules.map(item=>{const rule=item.rule||{},issues=Array.isArray(item.precheck)?item.precheck:[],disabled=published?'disabled':'',typeOptions=['required','regex','range','keyword','forbidden_keyword','enum','status'].map(v=>`<option value="${v}" ${rule.type===v?'selected':''}>${v}</option>`).join(''),severityOptions=['critical','error','warning'].map(v=>`<option value="${v}" ${rule.severity===v?'selected':''}>${v}</option>`).join(''),issueHtml=issues.length?issues.map(issue=>`<div class="${issue.blocking?'draft-issue-block':'draft-issue-warn'}">${issue.blocking?'⛔':'⚠'} ${esc(issue.message)}</div>`).join(''):'<div style="color:#059669">✓ 自动预检通过</div>';
            return `<div class="draft-rule ${esc(item.status)}" id="draft-${esc(item.draft_id)}"><div class="draft-rule-head"><b>${esc(rule.name||'未命名规则')}</b><span class="draft-status">${esc(draftStatusText(item.status))} · ${esc(rule.id||'')}</span></div><div class="draft-grid"><div class="draft-field"><label>规则名称</label><input data-key="name" value="${esc(rule.name||'')}" ${disabled}></div><div class="draft-field"><label>核验字段</label><input data-key="field" value="${esc(rule.field||'')}" ${disabled}></div><div class="draft-field"><label>规则类型</label><select data-key="type" ${disabled}>${typeOptions}</select></div><div class="draft-field"><label>严重程度</label><select data-key="severity" ${disabled}>${severityOptions}</select></div><div class="draft-field"><label>适用材料类型</label><input data-key="scene" value="${esc((rule.scene||[]).join('，'))}" ${disabled}></div><div class="draft-field"><label>适用条件</label><input data-key="condition" value="${esc(rule.condition||'')}" ${disabled}></div><div class="draft-field full"><label>不通过时提示</label><input data-key="message" value="${esc(rule.message||'')}" ${disabled}></div><div class="draft-field full"><label>判定参数（JSON）</label><textarea data-key="extra" ${disabled}>${esc(JSON.stringify(ruleExtra(rule),null,2))}</textarea></div><div class="draft-field full"><label>制度原文依据</label><textarea data-key="source_excerpt" ${disabled}>${esc(rule.source_excerpt||'')}</textarea></div>${published?'':`<div class="draft-field full"><label>审核备注</label><input data-key="comment"></div>`}</div><div class="draft-issues">${issueHtml}</div>${published?'':`<div class="draft-actions"><button onclick="decideDraft('${esc(batch.batch_id)}','${esc(item.draft_id)}','approve')">通过 / 保存修改</button><button class="reject" onclick="decideDraft('${esc(batch.batch_id)}','${esc(item.draft_id)}','reject')">驳回</button></div>`}</div>`;
        }).join('');const canPublish=!published&&pending===0;
        return `<details class="draft-batch" ${batchIndex===activeIndex||batch.status==='ready_to_publish'?'open':''}><summary><span>${esc(batch.domain)} · ${esc(batch.source_doc)}</span><span>${esc(draftStatusText(batch.status))}｜待确认 ${pending} · 通过 ${approved} · 驳回 ${rejected}</span></summary><div class="draft-batch-body"><div class="draft-meta">创建于 ${esc(batch.created_at||'')}${batch.source_url?` · <a href="${esc(batch.source_url)}" target="_blank">查看制度原文</a>`:''}</div>${cards}${published?'':`<button class="draft-publish" ${canPublish?'':'disabled'} onclick="publishDraftBatch('${esc(batch.batch_id)}')">发布已通过规则${canPublish?'':'（请先逐条处理）'}</button>`}</div></details>`;
    }).join('');
}
async function loadRuleDrafts(){const container=document.getElementById('rule-draft-list');try{const data=await api('/rule-drafts');ruleDraftBatches=data.batches||[];renderRuleDrafts();}catch(err){container.innerHTML=`<div class="draft-empty">${esc(err.message)}</div>`;}}
async function loadRuleSummary(){try{const data=await api('/rule-drafts');const batches=data.batches||[],pending=batches.filter(x=>x.status!=='published'),rules=pending.reduce((sum,b)=>sum+(b.rules||[]).length,0);document.getElementById('rule-summary').innerHTML=`<div class="panel-head"><div><span class="panel-kicker">CURRENT STATUS</span><h3>规则草稿概览</h3></div></div><div class="kpi-grid" style="margin-top:16px"><div class="kpi-card accent"><span>待处理批次</span><strong>${pending.length}</strong><small>等待负责人确认</small></div><div class="kpi-card"><span>候选规则</span><strong>${rules}</strong><small>当前未发布草稿</small></div><div class="kpi-card"><span>已发布批次</span><strong>${batches.filter(x=>x.status==='published').length}</strong><small>已有审核记录</small></div></div>`;}catch(err){document.getElementById('rule-summary').innerHTML=`<div class="empty-state">${esc(err.message)}</div>`;}}
function collectDraftRule(batchId,draftId){const card=document.getElementById('draft-'+draftId),batch=ruleDraftBatches.find(x=>x.batch_id===batchId),draft=batch&&(batch.rules||[]).find(x=>x.draft_id===draftId);if(!card||!draft)throw new Error('找不到规则草稿');let extra={};try{extra=JSON.parse(card.querySelector('[data-key="extra"]').value||'{}');}catch(_){throw new Error('判定参数不是合法 JSON');}const value=key=>card.querySelector(`[data-key="${key}"]`).value.trim();return{...extra,id:draft.rule.id,name:value('name'),field:value('field'),type:value('type'),severity:value('severity'),scene:value('scene').split(/[，,]/).map(x=>x.trim()).filter(Boolean),condition:value('condition'),message:value('message'),source_excerpt:value('source_excerpt')};}
async function decideDraft(batchId,draftId,action){const card=document.getElementById('draft-'+draftId);try{const payload={action,comment:card.querySelector('[data-key="comment"]').value.trim()};if(action==='approve')payload.rule=collectDraftRule(batchId,draftId);const data=await api(`/rule-drafts/${encodeURIComponent(batchId)}/rules/${encodeURIComponent(draftId)}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});await loadRuleDrafts();toast(data.message||'规则状态已更新',data.ok===false?'error':'success');}catch(err){toast(err.message,'error');}}
async function publishDraftBatch(batchId){try{const data=await api(`/rule-drafts/${encodeURIComponent(batchId)}/publish`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await loadRuleDrafts();toast(data.message,'success');}catch(err){await loadRuleDrafts();toast(err.message,'error');}}

window.addEventListener('DOMContentLoaded',initializeAuth);
