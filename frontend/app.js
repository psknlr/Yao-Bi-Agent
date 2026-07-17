const modules = [
  { id: 'dashboard', label: '总览看板', icon: '◧' },
  { id: 'intake', label: '智能问诊', icon: '✚' },
  { id: 'chat', label: '智能问答', icon: '✦' },
  { id: 'agents', label: '智能体协作', icon: '⇄' },
  { id: 'reasoning', label: '经验推理', icon: '❖' },
  { id: 'summary', label: '经验总结', icon: '✎' },
  { id: 'mining', label: '规则挖掘', icon: '⛏' },
  { id: 'evidence', label: '证据回溯', icon: '⌖' },
  { id: 'review', label: '医师审核', icon: '✒' },
  { id: 'safety', label: '评估与安全', icon: '⛨' },
  { id: 'settings', label: '设置', icon: '⚙' },
];

const MINED = typeof window !== 'undefined' && window.MINED_RULES ? window.MINED_RULES : null;

// ---------------------------------------------------------------------------
// Backend API client — when the YaoBi server is reachable, the language model
// genuinely drives skill selection / planning / follow-up probes (server-side).
// When offline, the UI falls back to the client-side rule logic and labels it
// honestly as 关键词/规则 (no fake "Tao" claims).
// ---------------------------------------------------------------------------
const API_BASE = (typeof window !== 'undefined' && window.YAOBI_API_BASE) || '';
const api = {
  state: { checked: false, online: false, tao: null, provenance: null },
  // `ngrok-skip-browser-warning` stops ngrok's free interstitial HTML from breaking fetch JSON.
  async health() {
    try {
      const r = await fetch(`${API_BASE}/api/health`, { cache: 'no-store', headers: { 'ngrok-skip-browser-warning': 'true' } });
      if (!r.ok) throw new Error(String(r.status));
      const j = await r.json();
      this.state = { checked: true, online: true, tao: j.tao || null, provenance: j.provenance || null };
    } catch (e) {
      this.state = { checked: true, online: false, tao: null, provenance: null };
    }
    return this.state;
  },
  async post(path, body) {
    const r = await fetch(`${API_BASE}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'ngrok-skip-browser-warning': 'true' }, body: JSON.stringify(body || {}) });
    if (!r.ok) throw new Error(`${path} -> ${r.status}`);
    return r.json();
  },
};
function taoOnline() { return !!(api.state.online && api.state.tao && api.state.tao.enabled); }
function taoRuntimeTag() {
  const t = api.state.tao;
  if (!t) return '';
  return `${t.backend} · ${t.model_id}${t.quantization && t.quantization !== 'none' ? ' · ' + t.quantization : ''}`;
}
function casePayload(extra = {}) {
  const c = buildCase();
  const asList = v => Array.isArray(v) ? v : (v ? [v] : []);
  return {
    tags: c.tags,
    red_flags: { status: c.red.status, positive_items: c.red.positives },
    // Intake comorbidity answers feed the server-side herb-drug / contraindication checker.
    comorbidity: { diseases: asList(state.answers.diseases), medications: asList(state.answers.medications) },
    doctor_mode: state.doctorMode,
    ...extra,
  };
}
function renderTaoBadge() {
  let el = document.querySelector('#taoBadge');
  if (!el) {
    const actions = document.querySelector('.topbar-actions');
    if (!actions) return;
    el = document.createElement('span');
    el.id = 'taoBadge';
    el.className = 'tao-badge';
    actions.insertBefore(el, actions.firstChild);
  }
  const online = taoOnline();
  el.classList.toggle('on', online);
  el.classList.toggle('off', !online);
  const prov = api.state.provenance;
  const provTag = prov ? `规则库版本 ${prov.rules_version} · 应用 v${prov.app_version}` : '';
  el.title = online
    ? `语言模型在线：${taoRuntimeTag()}${provTag ? '\n' + provTag : ''}`
    : '未连接 Tao 后端：离线规则模式（标签如实显示为关键词）';
  el.innerHTML = online
    ? `<span class="dot"></span>Tao 在线 · ${api.state.tao.backend}`
    : `<span class="dot"></span>离线 · 规则模式`;
}

// ---- CDSS governance: physician feedback loop (确认/需修订/不采纳 → POST /api/feedback) ----
const FEEDBACK_ACTIONS = [
  { id: 'confirmed', label: '👍 确认' },
  { id: 'revised', label: '✏️ 需修订' },
  { id: 'rejected', label: '👎 不采纳' },
];

// Feedback state lives on JS state objects (not the DOM) because every render is a full
// innerHTML replacement. `holder.feedback = {action, reason, pendingReason, sent}`.
function feedbackWidget(holder, key) {
  if (!taoOnline()) return '';
  const fb = holder.feedback;
  if (fb && fb.sent) {
    const act = FEEDBACK_ACTIONS.find(a => a.id === fb.action);
    return `<div class="feedback-row done">医师反馈已记录：${act ? act.label : escapeHtml(fb.action)} ✓</div>`;
  }
  const buttons = FEEDBACK_ACTIONS.map(a =>
    `<button class="chip-btn feedback-btn${fb && fb.action === a.id ? ' selected' : ''}" data-fbkey="${key}" data-action="${a.id}">${a.label}</button>`).join('');
  const reasonBox = fb && fb.pendingReason
    ? `<input class="feedback-reason" data-fbkey="${key}" type="text" maxlength="200" placeholder="原因（可选，请勿包含患者身份信息），回车提交" value="${escapeHtml(fb.reason || '')}" />
       <button class="chip-btn feedback-submit" data-fbkey="${key}">提交</button>`
    : '';
  return `<div class="feedback-row"><span class="feedback-label">医师反馈</span>${buttons}${reasonBox}</div>`;
}

function submitFeedback(holder, target, extra, rerender) {
  const fb = holder.feedback || {};
  fb.sent = true;
  fb.pendingReason = false;
  holder.feedback = fb;
  // Fire-and-forget: feedback must never block or break the clinical UI.
  api.post('/api/feedback', {
    action: fb.action,
    target,
    reason: fb.reason || '',
    doctor_mode: state.doctorMode,
    ...extra,
  }).catch(() => {});
  rerender();
}

// resolver(key) -> {holder, target, extra} for every widget rendered on the current screen.
function wireFeedback(resolver, rerender) {
  screen.querySelectorAll('.feedback-btn').forEach(b => b.addEventListener('click', () => {
    const ctx = resolver(b.dataset.fbkey);
    if (!ctx) return;
    const action = b.dataset.action;
    ctx.holder.feedback = { ...(ctx.holder.feedback || {}), action };
    if (action === 'confirmed') {
      submitFeedback(ctx.holder, ctx.target, ctx.extra, rerender);
    } else {
      ctx.holder.feedback.pendingReason = true;
      rerender();
    }
  }));
  const send = key => {
    const ctx = resolver(key);
    if (ctx) submitFeedback(ctx.holder, ctx.target, ctx.extra, rerender);
  };
  screen.querySelectorAll('.feedback-reason').forEach(inp => {
    inp.addEventListener('input', () => {
      const ctx = resolver(inp.dataset.fbkey);
      if (ctx) ctx.holder.feedback = { ...(ctx.holder.feedback || {}), reason: inp.value, pendingReason: true };
    });
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') send(inp.dataset.fbkey); });
  });
  screen.querySelectorAll('.feedback-submit').forEach(b => b.addEventListener('click', () => send(b.dataset.fbkey)));
}

const steps = [
  { id: 'start', label: '开始与知情', title: '自动导引患者生成标准腰痹医案' },
  { id: 'redflag', label: '红旗筛查', title: '先排除需要立即线下评估的危险信号' },
  { id: 'basic', label: '主诉病程', title: '采集基础信息、主诉与病程' },
  { id: 'pain', label: '疼痛特征', title: '采集疼痛部位、放射、性质和诱因' },
  { id: 'neuro', label: '神经骨科', title: '采集麻木、无力、影像与既往诊断' },
  { id: 'tcm', label: '中医四诊', title: '用患者能理解的问题采集中医信息' },
  { id: 'comorbidity', label: '合并病用药', title: '采集合并病、NSAIDs、肌松药与过敏史' },
  { id: 'signals', label: '规则线索', title: '沈老经验规则线索与 CDSS 草案' },
  { id: 'final', label: '最终医案', title: '标准医案、医生复核清单与导出' },
];

const featureMatrix = [
  ['多智能体协作', '11 个智能体在共享黑板上自主接力：规则智能体为主、语言模型智能体受守卫，红旗智能体可自主中止下游。'],
  ['多轮智能问答', '语言模型在受限技能集内自主选择并调用对应 skill，按用户提问挖掘脱敏数据，附示例问题引导提问。'],
  ['CaseGuide FSM', '有限状态机分阶段问诊，默认每个状态最多 3 轮追问（可设置 1–5 轮）、每轮最多 3 问，答完可自动进入下一状态。'],
  ['Tao 自动追问', '在规则约束下由 Tao 生成本状态主题内的澄清式追问，仅作补充线索、不驱动状态跳转，违规即回退。'],
  ['经验辨证推理', '规则派生辨证推理链（症状→证候→治法→方剂→安全），Tao 语言化叠加并经 Output Guard 校验。'],
  ['案例经验总结', '自动生成单案医案按语与脱敏经验规律总结，研究教学用，非诊断非处方。'],
  ['Rule Engine', '结构化标签、候选证型、方剂路线、模块命中和冲突检查。'],
  ['Tao Direct Runtime', '本地 Transformers 直接推理：TAO_BACKEND=transformers，无需 FastAPI 包装。'],
  ['JSON Repair', '修复模型 JSON 围栏、尾逗号、单引号等常见格式错误。'],
  ['Output Guard', '拦截最终诊断、完整处方、患者可执行剂量和替代医生建议。'],
  ['CDSS Draft', '医生端候选诊断/候选证型/处方策略草案，非患者可见。'],
  ['Physician Review', '最终诊断、处方、剂量只能由 licensed physician 手工签名。'],
  ['Export', '标准医案 Markdown、规则 JSON、医生复核摘要导出。'],
];

const questions = {
  redflag: [
    { id: 'RF001', q: '腰痛是否由跌倒、车祸、重物砸伤等明显外伤后出现？', options: ['是', '否', '不确定'], urgent: ['是'] },
    { id: 'RF002', q: '是否出现大小便控制困难、会阴区麻木，或突然尿不出来？', options: ['是', '否', '不确定'], urgent: ['是'] },
    { id: 'RF003', q: '是否出现一侧或双侧下肢明显无力、走路拖脚、进行性加重？', options: ['是', '否', '不确定'], urgent: ['是'] },
    { id: 'RF004', q: '是否伴有发热、寒战，或近期有感染？', options: ['是', '否', '不确定'], caution: ['是', '不确定'] },
    { id: 'RF005', q: '是否有肿瘤病史、原因不明体重下降、夜间痛明显加重？', options: ['是', '否', '不确定'], caution: ['是', '不确定'] },
    { id: 'RF006', q: '是否长期使用激素，或已知严重骨质疏松，并突然出现剧烈腰背痛？', options: ['是', '否', '不确定'], caution: ['是', '不确定'] },
  ],
  basic: [
    { id: 'age', q: '你的年龄是？', type: 'number', placeholder: '例如：68' },
    { id: 'sex', q: '性别？', options: ['女', '男', '其他/不便说明'] },
    { id: 'main_symptom', q: '你最主要的不舒服是什么？', options: ['腰痛', '腰腿痛', '腰酸', '腰痛伴腿麻'], note: true },
    { id: 'duration', q: '这种情况持续多久了？', options: ['3天', '2周', '半年', '5年'], note: true },
    { id: 'acute_worsening', q: '这次加重多久了？', options: ['无明显加重', '3天', '2周', '1月'], note: true },
  ],
  pain: [
    { id: 'location', q: '疼痛主要在哪个部位？', multi: true, options: ['腰正中', '一侧腰部', '双侧腰部', '腰骶部', '臀部', '大腿后侧', '小腿', '足部', '说不清'] },
    { id: 'radiation', q: '疼痛会不会从腰部放射到臀部或下肢？', options: ['不会', '到臀部', '到大腿', '到小腿', '到足部', '不确定'] },
    { id: 'pain_nature', q: '疼痛性质更像哪一种？', multi: true, options: ['酸痛', '胀痛', '刺痛', '冷痛', '灼痛', '隐痛', '掣痛/牵拉痛', '麻痛', '说不清'] },
    { id: 'severity', q: '0 到 10 分，你觉得疼痛大约几分？', type: 'range' },
    { id: 'aggravating', q: '什么情况下会加重？', multi: true, options: ['久坐', '久站', '弯腰', '劳累', '受凉', '阴雨天', '走路', '咳嗽打喷嚏', '夜间', '没有明显规律'] },
    { id: 'relieving', q: '什么情况下会缓解？', multi: true, options: ['休息', '热敷', '活动后', '按摩', '卧床', '服止痛药', '没有明显缓解'] },
  ],
  neuro: [
    { id: 'numbness', q: '是否有下肢麻木？', options: ['没有', '偶尔有', '经常有', '持续存在', '说不清'] },
    { id: 'numbness_location', q: '麻木主要在哪个部位？', multi: true, options: ['臀部', '大腿外侧', '大腿后侧', '小腿外侧', '小腿后侧', '足背', '足底', '脚趾', '双下肢', '说不清'] },
    { id: 'weakness', q: '是否感觉腿无力、走路不稳、脚抬不起来？', options: ['没有', '轻微', '明显', '越来越重'] },
    { id: 'walking_limitation', q: '走一段路后是否腰腿痛或麻木加重，休息或弯腰后缓解？', options: ['是', '否', '不确定'] },
    { id: 'imaging', q: '是否做过腰椎 X 线、CT、MRI 或骨密度检查？', multi: true, options: ['做过MRI', '做过CT', '做过X线', '做过骨密度', '没做过', '不记得'] },
    { id: 'western_diagnosis', q: '医生曾经告诉你有什么诊断？', multi: true, options: ['腰椎间盘突出', '腰椎管狭窄', '腰椎滑脱', '骨质疏松', '骨折/压缩性骨折', '腰肌劳损', '坐骨神经痛', '其他', '不清楚'] },
  ],
  tcm: [
    { id: 'cold_heat', q: '你平时怕冷还是怕热？', options: ['怕冷', '怕热', '都不明显', '有时怕冷有时怕热'] },
    { id: 'cold_relation', q: '腰腿痛遇冷会不会加重？热敷会不会舒服？', options: ['遇冷加重，热敷舒服', '热敷不舒服', '没影响', '不确定'] },
    { id: 'dampness', q: '身体是否容易困重、沉重，尤其阴雨天更明显？', options: ['明显', '轻微', '没有', '不确定'] },
    { id: 'sleep', q: '睡眠怎么样？', multi: true, options: ['正常', '入睡困难', '容易醒', '多梦', '早醒', '疼痛影响睡眠', '睡不踏实'] },
    { id: 'appetite', q: '胃口怎么样？', options: ['正常', '胃口差', '容易腹胀', '吃药容易胃不舒服', '恶心反酸'] },
    { id: 'mouth_taste', q: '是否经常口苦、口干，或咽喉不清爽？', options: ['明显', '轻微', '没有'] },
    { id: 'tongue_color', q: '舌头颜色更像偏淡、偏暗紫，还是偏红？', options: ['偏淡', '偏暗紫', '偏红', '说不清'] },
    { id: 'tongue_coating', q: '舌苔是否偏厚腻、白腻或黄腻？', options: ['薄白', '白腻', '黄腻', '厚腻', '说不清'] },
  ],
  comorbidity: [
    { id: 'diseases', q: '是否有以下疾病？', multi: true, options: ['高血压', '糖尿病', '骨质疏松', '肾功能异常', '肝功能异常', '胃溃疡/胃炎', '心脏病', '肿瘤病史', '过敏史', '无', '不清楚'] },
    { id: 'medications', q: '最近是否服用过止痛药或消炎药？', multi: true, options: ['塞来昔布', '布洛芬', '双氯芬酸', '艾瑞昔布', '依托考昔', '乙哌立松', '甲钴胺', '其他', '没有', '不清楚'] },
    { id: 'anticoagulant', q: '是否正在服用抗凝药、阿司匹林、激素或降糖药？', options: ['是', '否', '不确定'] },
    { id: 'allergy', q: '是否有药物或中药过敏史？', options: ['有', '没有', '不确定'] },
  ],
};

const state = {
  module: localStorage.getItem('yaobi-module') || 'dashboard',
  step: 0,
  // Least privilege by default: the UI starts in patient mode; clinician mode is an
  // explicit opt-in, and the backend independently re-verifies the role server-side
  // (YAOBI_CLINICIAN_TOKEN) — the client can only request it, never grant it.
  doctorMode: false,
  chat: { history: [] },
  intakeMode: localStorage.getItem('yaobi-intake-mode') || 'chat',
  interview: null,
  // Case narrative and FSM state are health data: keep them in sessionStorage only
  // (cleared when the tab closes), never in long-lived localStorage. Migrate and
  // scrub any residue written by older versions.
  answers: JSON.parse(sessionStorage.getItem('yaobi-case') || localStorage.getItem('yaobi-case') || '{}'),
  fsm: JSON.parse(sessionStorage.getItem('yaobi-fsm') || localStorage.getItem('yaobi-fsm') || '{\"rounds\":{},\"lastAnswers\":{}}'),
  // Physician feedback holder for the form-mode final report (session-scoped).
  reportFeedback: {},
};
state.fsm.rounds = state.fsm.rounds || {};
state.fsm.lastAnswers = state.fsm.lastAnswers || {};
state.fsm.maxRounds = Number(state.fsm.maxRounds) >= 1 ? Number(state.fsm.maxRounds) : 3;
state.fsm.autoAdvance = state.fsm.autoAdvance !== false;

const screen = document.querySelector('#screen');
const pageTitle = document.querySelector('#pageTitle');

function save() {
  sessionStorage.setItem('yaobi-case', JSON.stringify(state.answers));
  sessionStorage.setItem('yaobi-fsm', JSON.stringify(state.fsm));
  // Scrub legacy persistent copies (health data must not survive the session).
  localStorage.removeItem('yaobi-case');
  localStorage.removeItem('yaobi-fsm');
  updatePreview();
}

function answerValue(id) { return state.answers[id]; }
function setAnswer(id, value, multi = false) {
  if (multi) {
    const current = new Set(Array.isArray(state.answers[id]) ? state.answers[id] : []);
    current.has(value) ? current.delete(value) : current.add(value);
    state.answers[id] = [...current];
  } else {
    state.answers[id] = value;
  }
  save();
}


function maxRounds() {
  return Math.max(1, Number(state.fsm.maxRounds) || 3);
}

function stageRound(stage) {
  return Math.min(state.fsm.rounds[stage] || 0, maxRounds() - 1);
}

function setStageRound(stage, value) {
  state.fsm.rounds[stage] = Math.max(0, Math.min(maxRounds() - 1, value));
  save();
}

function setMaxRounds(value) {
  state.fsm.maxRounds = Math.max(1, Math.min(5, Number(value) || 3));
  save();
}

function questionAnswered(q) {
  const value = state.answers[q.id];
  return Array.isArray(value) ? value.length > 0 : value !== undefined && value !== '' && value !== null;
}

function questionReason(q, stage) {
  const tags = getTags();
  if (stage === 'redflag') return '红旗筛查优先；若命中危险信号，将立即停止后续普通问诊。';
  if (['cold_relation', 'cold_heat'].includes(q.id) && (tags.includes('elderly') || tags.includes('lower_limb_numbness'))) return '结合上一轮高龄/麻木/久病线索，深化寒湿、温经散寒与通络规则变量。';
  if (['numbness', 'numbness_location', 'radiation'].includes(q.id)) return '结合疼痛部位和上一轮回答，深化放射痛、麻木和神经根风险线索。';
  if (['imaging', 'western_diagnosis', 'diseases'].includes(q.id)) return '结合当下规则命中，补足影像、骨质疏松和医生复核背景。';
  if (['sleep', 'appetite', 'mouth_taste'].includes(q.id)) return '结合沈老规则中的口苦、睡眠、胃纳变量，深化少阳/顾护中焦线索。';
  return '根据当前规则标签与上一轮答案，补齐本状态最有信息增益的字段。';
}

function currentStageQuestions(stage) {
  const list = questions[stage] || [];
  const round = stageRound(stage);
  const key = `${stage}:${round}`;
  state.fsm.shownIds = state.fsm.shownIds || {};
  let ids = state.fsm.shownIds[key];
  if (!ids || !ids.length) {
    const unanswered = list.filter(q => !questionAnswered(q));
    const selected = unanswered.length ? unanswered.slice(0, 3) : list.slice(round * 3, round * 3 + 3);
    ids = (selected.length ? selected : list.slice(-3)).map(q => q.id);
    state.fsm.shownIds[key] = ids;
    save();
  }
  return list.filter(q => ids.includes(q.id)).map(q => ({ ...q, reason: questionReason(q, stage) }));
}

function stageFollowupsExhausted(stage) {
  return stageRound(stage) + 1 >= maxRounds();
}

function maybeAutoAdvance(stage) {
  if (!state.fsm.autoAdvance) return false;
  const list = questions[stage] || [];
  const shown = currentStageQuestions(stage);
  const allAnswered = list.every(q => questionAnswered(q));
  const shownAnswered = shown.every(q => questionAnswered(q));
  if (stage === 'redflag' && !allAnswered) return false; // 红旗筛查是硬门控，必须答完才能离开。
  if (allAnswered || (shownAnswered && stageFollowupsExhausted(stage))) {
    state.fsm.lastAnswers[stage] = { ...state.answers };
    save();
    state.step = Math.min(steps.length - 1, state.step + 1);
    render();
    return true;
  }
  if (shownAnswered) {
    state.fsm.lastAnswers[stage] = { ...state.answers };
    setStageRound(stage, stageRound(stage) + 1);
    render();
    return true;
  }
  return false;
}

function renderStepper() {
  const el = document.querySelector('#stepper');
  el.innerHTML = steps.map((s, i) => `
    <button class="step ${i === state.step ? 'active' : ''} ${i < state.step ? 'done' : ''}" data-step="${i}" type="button">
      <span class="step-index">${i + 1}</span><span>${s.label}</span>
    </button>`).join('');
  el.querySelectorAll('button').forEach(btn => btn.addEventListener('click', () => { state.step = Number(btn.dataset.step); render(); }));
}

function renderModuleNav() {
  const nav = document.querySelector('#moduleNav');
  nav.innerHTML = modules.map(m => `
    <button class="module-link ${m.id === state.module ? 'active' : ''}" data-module="${m.id}" type="button">
      <span class="module-icon">${m.icon}</span><span>${m.label}</span>
    </button>`).join('');
  nav.querySelectorAll('button').forEach(btn => btn.addEventListener('click', () => {
    state.module = btn.dataset.module;
    localStorage.setItem('yaobi-module', state.module);
    render();
  }));
}

function intakeMode() { return state.intakeMode === 'form' ? 'form' : 'chat'; }
function setIntakeMode(mode) { state.intakeMode = mode; localStorage.setItem('yaobi-intake-mode', mode); render(); }

function render() {
  renderModuleNav();
  renderTaoBadge();
  const intake = state.module === 'intake';
  const chatIntake = intake && intakeMode() === 'chat';
  document.querySelector('#stepper').style.display = (intake && !chatIntake) ? '' : 'none';
  document.querySelector('.case-sidebar').style.display = (intake && !chatIntake) ? '' : 'none';
  document.querySelector('.app-shell').classList.toggle('wide', !intake || chatIntake);
  if (chatIntake) return renderConversationalInterview();
  if (!intake) {
    if (state.module === 'dashboard') return renderDashboard();
    if (state.module === 'chat') return renderChatModule();
    if (state.module === 'agents') return renderAgentsModule();
    if (state.module === 'reasoning') return renderReasoningModule();
    if (state.module === 'summary') return renderSummaryModule();
    if (state.module === 'mining') return renderMiningModule();
    if (state.module === 'evidence') return renderEvidenceModule();
    if (state.module === 'review') return renderReviewModule();
    if (state.module === 'safety') return renderSafetyModule();
    if (state.module === 'settings') return renderSettingsModule();
  }
  renderStepper();
  pageTitle.textContent = steps[state.step].title;
  const id = steps[state.step].id;
  if (id === 'start') return renderStart();
  if (id === 'signals') return renderSignals();
  if (id === 'final') return renderFinal();
  renderQuestionStage(id);
}

// ---------------------------------------------------------------------------
// Conversational interview (Tao-driven FSM: extract → ask → report)
// ---------------------------------------------------------------------------

function ensureInterview() {
  if (!state.interview) {
    state.interview = { sessionId: 'iv-' + Math.random().toString(36).slice(2, 10), history: [], info: null, report: null, pending: false, kicked: false };
  }
  return state.interview;
}

async function interviewSend(message) {
  const iv = ensureInterview();
  if (iv.pending) return;
  if (message) iv.history.push({ role: 'user', content: message });
  iv.pending = true;
  renderConversationalInterview();
  try {
    const res = await api.post('/api/interview', { session_id: iv.sessionId, message: message || '' });
    iv.pending = false;
    iv.info = res;
    iv.report = res.report || null;
    iv.history.push({ role: 'assistant', content: res.report || res.message, state: res.state, done: res.done });
  } catch (e) {
    iv.pending = false;
    await api.health(); renderTaoBadge();
    iv.history.push({ role: 'assistant', content: '（连接后端失败，无法继续对话式问诊。请确认服务在线，或切换到表单式问诊。）', error: true });
  }
  renderConversationalInterview();
}

function interviewReset() {
  const iv = ensureInterview();
  api.post('/api/interview', { session_id: iv.sessionId, reset: true }).catch(() => {});
  state.interview = null;
  ensureInterview().kicked = true;
  interviewSend('');   // fresh opening question
}

async function interviewReview(action, notes) {
  const iv = ensureInterview();
  if (iv.pending) return;
  // Override is a two-phase, attributable approval: reviewer ID is mandatory (audit),
  // and the server executes nothing until the same reviewer re-confirms the approval id.
  if (action === 'override') {
    iv.reviewerId = (window.prompt('高风险操作：请填写医师工号/ID（审计归责必填）', iv.reviewerId || '') || '').trim();
    if (!iv.reviewerId) { alert('未填写医师工号/ID，已取消覆盖操作。'); return; }
  }
  const actionLabel = {confirm: '✓ 医师确认急诊转诊建议', revise: '✎ 医师修订转诊建议', override: '↺ 医师申请覆盖红旗评估（需二次确认）'}[action] || action;
  iv.history.push({ role: 'physician', content: `${actionLabel}${notes ? '\n备注：' + notes : ''}` });
  iv.pending = true;
  renderConversationalInterview();
  try {
    const body = { session_id: iv.sessionId, review_action: action, doctor_mode: state.doctorMode, reviewer_id: iv.reviewerId || '' };
    if (action === 'override') {
      body.override_reason = notes;
    } else {
      body.physician_notes = notes;
    }
    let res = await api.post('/api/interview', body);
    if (res.error) {
      iv.pending = false;
      iv.history.push({ role: 'assistant', content: `（医师审核被拒绝：${res.message || res.error}）`, error: true });
      renderConversationalInterview();
      return;
    }
    // Phase 2: server created a pending approval → explicit second confirmation, then
    // resend with the approval id. Declining leaves the red flags fully in force.
    if (action === 'override' && res.pending_approval) {
      const approval = res.pending_approval;
      const ok = window.confirm(`二次确认：确定覆盖红旗评估并恢复问诊？\n审批号：${approval.approval_id}\n覆盖理由：${notes}`);
      if (ok) {
        res = await api.post('/api/interview', { ...body, confirm_override: true, approval_id: approval.approval_id });
      } else {
        iv.pending = false;
        iv.history.push({ role: 'assistant', content: '（已取消覆盖：审批未确认，红旗评估维持原状。）' });
        renderConversationalInterview();
        return;
      }
    }
    iv.pending = false;
    iv.info = res;
    iv.report = res.report || null;
    // Override resumes the FSM → response message is the next Tao question (assistant).
    // Confirm / revise stop the interview → response message is a physician confirmation echo (skip, already shown above).
    if (action === 'override') {
      iv.history.push({ role: 'assistant', content: res.message, state: res.state, done: res.done });
    }
  } catch (e) {
    iv.pending = false;
    await api.health(); renderTaoBadge();
    iv.history.push({ role: 'assistant', content: '（医师审核提交失败，请重试。）', error: true });
  }
  renderConversationalInterview();
}

function renderConversationalInterview() {
  pageTitle.textContent = '智能问诊 · Tao 驱动的对话式腰痹问诊';
  const iv = ensureInterview();
  if (!taoOnline()) {
    screen.innerHTML = `
      <section class="result-panel">
        <div class="intake-switch"><strong>问诊方式</strong>
          <button class="option-pill selected" data-mode="chat">对话式（Tao）</button>
          <button class="option-pill" data-mode="form">表单式</button>
        </div>
        <h3>对话式问诊需要连接 Tao 后端</h3>
        <p class="muted">对话式问诊由语言模型实时抽取信息、自主追问并生成报告。请启动后端服务（Colab 用 ngrok 暴露），或切换到「表单式」问诊。</p>
      </section>`;
    screen.querySelectorAll('[data-mode]').forEach(b => b.addEventListener('click', () => setIntakeMode(b.dataset.mode)));
    return;
  }
  if (!iv.kicked && iv.history.length === 0) { iv.kicked = true; interviewSend(''); }

  const info = iv.info || {};
  const patterns = info.candidate_patterns || [];
  const topP = Math.max(...patterns.map(p => p.prob || 0), 0.001);
  const bubbles = iv.history.map(m => {
    if (m.role === 'user') return `<div class="chat-turn"><div class="bubble user">${escapeHtml(m.content)}</div></div>`;
    if (m.role === 'physician') {
      return `<div class="chat-turn"><div class="bubble physician"><div class="bot-meta"><span class="kind-badge physician-badge">医师审核</span></div><div class="bot-body">${mdLite(m.content)}</div></div></div>`;
    }
    const tag = m.done ? '<span class="kind-badge llm-on">问诊小结</span>' : '<span class="kind-badge llm">Tao 追问</span>';
    const fbHtml = m.done && state.doctorMode ? feedbackWidget(iv, 'iv-report') : '';
    return `<div class="chat-turn"><div class="bubble bot"><div class="bot-meta">${tag}${m.state ? `<span class="route-tag">${escapeHtml(m.state)}</span>` : ''}</div><div class="bot-body">${mdLite(m.content)}</div>${fbHtml}</div></div>`;
  }).join('');
  const pendingHtml = iv.pending ? `<div class="chat-turn"><div class="bubble bot"><div class="bot-body muted">⏳ Tao 正在分析并组织下一步追问…（${escapeHtml(taoRuntimeTag())}）</div></div></div>` : '';

  const redFlags = (info.red_flags || []);
  const pr = info.physician_review || {};
  const sidePanel = `
    <section class="result-panel interview-side">
      <h3>问诊状态</h3>
      <p class="eyebrow">${escapeHtml(info.state || 'SAFETY_TRIAGE')} · 第 ${info.turn_count || 0} 轮</p>
      <p class="muted">${escapeHtml(info.state_goal || '安全筛查与信息采集')}</p>
      ${redFlags.length ? `<div class="redflag-box"><strong>⚠ 风险信号${info.safety_level === 'emergency' ? ' · 急诊' : ''}</strong><ul>${redFlags.map(f => `<li>${escapeHtml(f)}</li>`).join('')}</ul></div>` : ''}
      ${pr.status ? `<div class="review-status-badge review-${pr.status}">医师审核：${escapeHtml({'confirmed':'已确认转诊','revised':'已修订','overridden':'已覆盖（恢复问诊）'}[pr.status] || pr.status)}</div>` : ''}
      <h4>候选证候（规则评分，模型据此追问）</h4>
      ${patterns.length ? patterns.slice(0, 5).map(p => `
        <div class="bar-row"><span class="bar-label">${escapeHtml(p.pattern)}</span>
          <span class="bar-track"><span class="bar-fill" style="width:${Math.round((p.prob || 0) / topP * 100)}%"></span></span>
          <span class="bar-value">${Math.round((p.prob || 0) * 100)}%</span></div>`).join('') : '<p class="muted">待补充信息</p>'}
      <p class="muted">不确定度：${info.uncertainty != null ? info.uncertainty : '—'}</p>
      ${(info.target_slots || []).length ? `<h4>本轮关注</h4><div class="chip-cloud">${info.target_slots.map(s => `<span class="chip">${escapeHtml(s)}</span>`).join('')}</div>` : ''}
    </section>`;

  // Physician review panel — only in doctor_mode when the FSM has halted on a safety referral
  // and the physician hasn't reviewed yet.
  const needsReview = state.doctorMode && info.physician_review_required && info.state === 'SAFETY_REFERRAL';
  const taoguidance = info.referral_tao_guidance || '';
  const reviewPanel = needsReview ? `
    <section class="result-panel physician-review-panel">
      <p class="eyebrow">PHYSICIAN_REVIEW · doctor_mode · 仅执业医师可见</p>
      <h3>医师审核转诊建议</h3>
      <p>系统已检测到危险信号并暂停问诊。请选择处置方式：</p>
      ${taoguidance ? `<details class="tao-guidance-details"><summary><strong>Tao 急诊转诊参考（供医师参考）</strong></summary><div class="bot-body">${mdLite(taoguidance)}</div></details>` : ''}
      <div class="review-actions">
        <button class="primary-btn" id="ivConfirm">✓ 确认转诊建议</button>
        <button class="ghost-btn" id="ivRevise">✎ 修订并添加医师备注</button>
        <button class="danger-btn" id="ivOverride">↺ 覆盖红旗判断（慎用）</button>
      </div>
      <div id="reviewNotesArea" class="review-notes-area" style="display:none">
        <p class="muted" id="reviewNoteHint"></p>
        <textarea id="ivNotes" class="free-note" placeholder="请输入医师备注（必填）…" rows="3"></textarea>
        <button class="primary-btn" id="ivSubmitNotes">提交</button>
      </div>
    </section>` : '';

  screen.innerHTML = `
    <div class="intake-switch">
      <strong>问诊方式</strong>
      <button class="option-pill selected" data-mode="chat">对话式（Tao）</button>
      <button class="option-pill" data-mode="form">表单式</button>
      <span class="muted">由 ${escapeHtml(taoRuntimeTag())} 实时抽取信息、自主追问、生成报告</span>
      <button class="ghost-btn" id="ivReset" type="button">重新开始</button>
    </div>
    <div class="panel-grid interview-grid">
      <section class="result-panel">
        <p class="eyebrow">draft_for_clinician_review · 语言模型抽取→规则红旗/证候→Tao 自主追问→会诊报告</p>
        <div class="chat-window">${bubbles || '<p class="muted">请描述您目前最主要的腰部不适（部位、多久、是否放射到腿、有无麻木无力）。</p>'}${pendingHtml}</div>
        ${!info.done ? `<div class="chat-input"><input id="ivInput" type="text" placeholder="像跟医生说话一样描述症状，例如：腰痛半年，弯腰加重，左腿发麻……" /><button class="primary-btn" id="ivSend">发送</button></div>` : ''}
      </section>
      ${sidePanel}
    </div>
    ${reviewPanel}`;

  screen.querySelectorAll('[data-mode]').forEach(b => b.addEventListener('click', () => setIntakeMode(b.dataset.mode)));
  document.querySelector('#ivReset').addEventListener('click', interviewReset);
  if (!info.done) {
    const input = document.querySelector('#ivInput');
    const send = () => { const v = input.value.trim(); if (v) { input.value = ''; interviewSend(v); } };
    document.querySelector('#ivSend').addEventListener('click', send);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
  }
  if (needsReview) {
    let reviewMode = '';
    const showNotes = (hint) => {
      document.querySelector('#reviewNotesArea').style.display = '';
      document.querySelector('#reviewNoteHint').textContent = hint;
    };
    document.querySelector('#ivConfirm').addEventListener('click', () => interviewReview('confirm', ''));
    document.querySelector('#ivRevise').addEventListener('click', () => { reviewMode = 'revise'; showNotes('请填写医师修订备注（将附加到转诊记录）：'); });
    document.querySelector('#ivOverride').addEventListener('click', () => { reviewMode = 'override'; showNotes('请填写覆盖理由（将记录在案，审计用）：'); });
    document.querySelector('#ivSubmitNotes').addEventListener('click', () => {
      const notes = (document.querySelector('#ivNotes').value || '').trim();
      if (!notes) { alert('备注内容不能为空。'); return; }
      interviewReview(reviewMode, notes);
    });
  }
  wireFeedback(() => ({
    holder: iv,
    target: 'interview_report',
    extra: { session_id: iv.sessionId, used_llm: true, intent: 'interview_report' },
  }), renderConversationalInterview);
  const win = screen.querySelector('.chat-window');
  if (win) win.scrollTop = win.scrollHeight;
}

function renderStart() {
  screen.innerHTML = `
    <div class="intake-switch">
      <strong>问诊方式</strong>
      <button class="option-pill" data-mode="chat">对话式（Tao）</button>
      <button class="option-pill selected" data-mode="form">表单式</button>
      <span class="muted">对话式由语言模型实时抽取、自主追问；表单式为固定问卷流程</span>
    </div>
    <section class="hero">
      <div class="hero-grid">
        <div>
          <p class="eyebrow">名老中医腰痹诊疗经验研究助手 / CDSS 草案</p>
          <h2>把零散腰痛描述整理成可复核、可标注、可教学的标准医案</h2>
          <p>本工具以红旗筛查为安全底线，以中西医问诊为骨架，以沈钦荣腰痹经验规则为导引逻辑，输出标准化医案、规则线索、医生复核清单和医生端 CDSS 草案。</p>
          <div class="badges"><span class="badge">红旗优先</span><span class="badge">每屏 1–3 问</span><span class="badge">规则证据可追溯</span><span class="badge">医师签名闭环</span></div>
          <button class="primary-btn" id="startBtn">开始整理医案</button>
        </div>
        <div class="notice">
          <strong>重要边界：</strong>患者端不会生成最终诊断、完整处方或患者可执行剂量。医生端 CDSS 仅生成草案，最终诊断和处方需 licensed physician 手工录入并签名。
        </div>
      </div>
      <div class="feature-grid">${featureMatrix.map(([name, desc]) => `<article><strong>${name}</strong><p>${desc}</p></article>`).join('')}</div>
      <pre class="runtime-code">TAO_BACKEND=transformers python -m backend.main --tao-chat "请解释本案规则线索" --stream</pre>
    </section>`;
  document.querySelector('#startBtn').addEventListener('click', () => { state.step = 1; render(); });
  screen.querySelectorAll('[data-mode]').forEach(b => b.addEventListener('click', () => setIntakeMode(b.dataset.mode)));
}

function renderQuestionStage(stage) {
  const urgent = getRedFlagStatus().status === 'urgent';
  if (stage !== 'redflag' && urgent) {
    screen.innerHTML = `<section class="result-panel redflag"><h3>已命中红旗信号</h3><p>请先线下就医或急诊评估。本工具已停止后续中医问诊。</p><button class="ghost-btn" id="backRed">返回红旗筛查</button></section>`;
    document.querySelector('#backRed').addEventListener('click', () => { state.step = 1; render(); });
    return;
  }
  const round = stageRound(stage);
  const list = currentStageQuestions(stage);
  const remaining = Math.max(0, maxRounds() - round - 1);
  const exhausted = stageFollowupsExhausted(stage);
  screen.innerHTML = `
    <section class="fsm-strip">
      <strong>有限状态机追问</strong>
      <span>本状态第 ${round + 1}/${maxRounds()} 轮（剩余追问 ${remaining} 轮），每轮最多 3 问；问题会叠加当前规则标签、上一轮答案与 Tao 大模型深化理由。</span>
      <label class="fsm-setting">追问轮数上限
        <select id="maxRoundsSelect">${[1, 2, 3, 4, 5].map(n => `<option value="${n}" ${n === maxRounds() ? 'selected' : ''}>${n}</option>`).join('')}</select>
      </label>
      <label class="fsm-setting"><input type="checkbox" id="autoAdvanceToggle" ${state.fsm.autoAdvance ? 'checked' : ''} />答完自动进入下一状态</label>
      <label class="fsm-setting" title="${taoOnline() ? 'Tao 在线：' + taoRuntimeTag() : '需连接后端服务'}"><input type="checkbox" id="taoProbeToggle" ${taoEnabled() ? 'checked' : ''} />Tao 自动追问${taoOnline() ? ' ·在线' : ' ·需后端'}</label>
      <button class="ghost-btn" id="endStateBtn" type="button">手动结束本状态</button>
    </section>
    <div class="card-grid"></div>
    <div class="footer-actions"><button class="ghost-btn" id="prevBtn">上一步</button><button class="ghost-btn" id="deepenBtn" ${exhausted ? 'disabled title="已达到本状态追问轮数上限"' : ''}>本状态深化追问</button><button class="primary-btn" id="nextBtn">进入下一个状态</button></div>`;
  const grid = screen.querySelector('.card-grid');
  list.forEach(q => grid.appendChild(renderQuestion(q, stage)));
  loadTaoProbes(stage, grid);   // genuine Tao follow-up probes when the backend is online
  document.querySelector('#maxRoundsSelect').addEventListener('change', e => { setMaxRounds(e.target.value); render(); });
  document.querySelector('#autoAdvanceToggle').addEventListener('change', e => { state.fsm.autoAdvance = e.target.checked; save(); });
  document.querySelector('#taoProbeToggle').addEventListener('change', e => { state.fsm.taoProbes = e.target.checked; save(); render(); });
  document.querySelector('#prevBtn').addEventListener('click', () => { state.step = Math.max(0, state.step - 1); render(); });
  document.querySelector('#deepenBtn').addEventListener('click', () => { if (exhausted) return; state.fsm.lastAnswers[stage] = { ...state.answers }; setStageRound(stage, round + 1); render(); });
  document.querySelector('#endStateBtn').addEventListener('click', () => {
    if (stage === 'redflag' && !questions.redflag.every(q => questionAnswered(q))) { alert('红旗筛查是安全硬门控，请先回答全部红旗问题。'); return; }
    state.fsm.lastAnswers[stage] = { ...state.answers }; state.step = Math.min(steps.length - 1, state.step + 1); render();
  });
  document.querySelector('#nextBtn').addEventListener('click', () => {
    if (stage === 'redflag' && !questions.redflag.every(q => questionAnswered(q))) { alert('红旗筛查是安全硬门控，请先回答全部红旗问题。'); return; }
    state.fsm.lastAnswers[stage] = { ...state.answers }; state.step = Math.min(steps.length - 1, state.step + 1); render();
  });
}

function renderQuestion(q, stage) {
  const tpl = document.querySelector('#questionTemplate').content.cloneNode(true);
  const card = tpl.querySelector('.question-card');
  card.classList.toggle('redflag', stage === 'redflag');
  card.classList.toggle('tao-probe', !!q.probe);
  const metaPrefix = q.probe ? 'TAO 自动追问' : `${stage.toUpperCase()} · ${q.id}`;
  tpl.querySelector('.question-meta').textContent = `${metaPrefix} · ${q.reason || '规则深化追问'}`;
  tpl.querySelector('h3').textContent = q.q;
  const options = tpl.querySelector('.options');
  const current = answerValue(q.id);
  if (q.probe || q.type === 'free') {
    options.innerHTML = `<input class="free-note" type="text" placeholder="可简要回答，作为补充线索（非必填）" />`;
    options.querySelector('input').value = current || '';   // DOM property, not attribute — no injection surface
    options.querySelector('input').addEventListener('input', e => setAnswer(q.id, e.target.value));
    tpl.querySelector('.free-note:last-child')?.remove();
    return card;
  }
  if (q.type === 'number') {
    options.innerHTML = `<input class="free-note" type="number" placeholder="${escapeHtml(q.placeholder || '')}" />`;
    options.querySelector('input').value = current || '';
    options.querySelector('input').addEventListener('input', e => setAnswer(q.id, Number(e.target.value)));
  } else if (q.type === 'range') {
    options.innerHTML = `<input type="range" min="0" max="10" value="${current ?? 5}" /><strong>${current ?? 5} 分</strong>`;
    const range = options.querySelector('input'); const label = options.querySelector('strong');
    range.addEventListener('input', e => { label.textContent = `${e.target.value} 分`; setAnswer(q.id, Number(e.target.value)); });
  } else {
    q.options.forEach(opt => {
      const selected = q.multi ? Array.isArray(current) && current.includes(opt) : current === opt;
      const btn = document.createElement('button');
      btn.type = 'button'; btn.className = `option-pill ${selected ? 'selected' : ''}`; btn.textContent = opt;
      btn.addEventListener('click', () => {
        setAnswer(q.id, opt, q.multi);
        // 单选答完后自动深化追问或自动进入下一状态；多选保留手动控制，避免选到一半被跳转。
        if (!q.multi && maybeAutoAdvance(stage)) return;
        renderQuestionStage(steps[state.step].id);
      });
      options.appendChild(btn);
    });
  }
  const note = tpl.querySelector('.free-note');
  if (q.type === 'number') note.remove();
  else {
    note.value = answerValue(`${q.id}_note`) || '';
    note.addEventListener('input', e => setAnswer(`${q.id}_note`, e.target.value));
  }
  return card;
}

function getTags() {
  const a = state.answers;
  const tags = [];
  if ((a.age || 0) >= 60) tags.push('elderly');
  if ((a.duration || '').includes('年')) tags.push('chronic_yabi', 'long_duration');
  if (['到小腿', '到足部'].includes(a.radiation)) tags.push('radiating_leg_pain');
  if (['偶尔有', '经常有', '持续存在'].includes(a.numbness) || (a.pain_nature || []).includes('麻痛')) tags.push('lower_limb_numbness');
  if ((a.aggravating || []).includes('受凉') || a.cold_relation === '遇冷加重，热敷舒服') tags.push('cold_aggravation');
  if ((a.relieving || []).includes('热敷') || a.cold_relation === '遇冷加重，热敷舒服') tags.push('warmth_relieves');
  if ((a.diseases || []).includes('骨质疏松') || (a.western_diagnosis || []).includes('骨质疏松')) tags.push('osteoporosis');
  if (a.tongue_color === '偏暗紫') tags.push('dark_tongue');
  if (a.tongue_coating === '白腻') tags.push('white_greasy_coating');
  if (a.sleep && a.sleep !== '正常') tags.push('insomnia');
  if (a.appetite === '胃口差') tags.push('poor_appetite');
  if (a.mouth_taste && a.mouth_taste !== '没有') tags.push('bitter_taste');
  return [...new Set(tags)];
}

function getRedFlagStatus() {
  const rf = questions.redflag;
  const positives = rf.filter(q => (q.urgent || []).includes(state.answers[q.id])).map(q => q.q);
  const cautions = rf.filter(q => (q.caution || []).includes(state.answers[q.id])).map(q => q.q);
  return { status: positives.length ? 'urgent' : cautions.length ? 'caution' : rf.every(q => state.answers[q.id]) ? 'safe' : 'unknown', positives, cautions };
}

function buildCase() {
  const a = state.answers; const tags = getTags(); const red = getRedFlagStatus();
  const chief = `${a.duration && (a.duration.includes('年') ? '反复' : '')}${a.main_symptom || '腰痛'}${a.duration || ''}${a.acute_worsening && a.acute_worsening !== '无明显加重' ? `，加重${a.acute_worsening}` : ''}${a.numbness && a.numbness !== '没有' ? '，伴下肢麻木' : ''}`;
  const modules = [];
  if (tags.includes('white_greasy_coating') || tags.includes('chronic_yabi')) modules.push('健脾化湿底盘');
  if (tags.includes('osteoporosis') || tags.includes('elderly')) modules.push('补肝肾强筋骨模块');
  if (tags.includes('lower_limb_numbness')) modules.push('当归四逆/通草细辛通络路线信号', '虫类搜络模块（需医师审核）');
  if (tags.includes('cold_aggravation')) modules.push('温经散寒模块');
  if (tags.includes('insomnia') || tags.includes('bitter_taste')) modules.push('少阳/安神除烦模块');
  return { chief, tags, red, modules };
}

function renderSignals() {
  const c = buildCase();
  screen.innerHTML = `
    <section class="result-panel success"><h3>患者模式：对医生有帮助的信息</h3><ul>${c.tags.map(t => `<li>${tagLabel(t)}</li>`).join('')}</ul></section>
    <section class="result-panel"><h3>规则引擎命中瀑布流</h3><p class="eyebrow">Rule Engine · Evidence Traceable</p><ul>${c.tags.map(t => `<li>${t} → ${tagLabel(t)}</li>`).join('') || '<li>待补充标签</li>'}</ul></section>
    <section class="result-panel"><h3>Tao Direct Runtime 叠加层</h3><p class="eyebrow">Transformers local inference · JSON Repair · Output Guard</p><p>后端可直接使用 <code>TAO_BACKEND=transformers</code> 加载 <code>CMLM/Dao1-30b-a3b</code>，不需要 FastAPI 包装；模型只叠加教学解释和问诊改写，若输出诊断/处方/剂量则回退规则模板。</p></section>
    <section class="result-panel"><h3>医生/CDSS 模式：候选诊断与处方策略草案</h3><p class="eyebrow">draft_for_clinician_review · 非最终医嘱 · 非患者可见 · 无患者可执行剂量</p><ul>
      <li>西医候选：${c.tags.includes('radiating_leg_pain') || c.tags.includes('lower_limb_numbness') ? '腰椎间盘突出/神经根受压相关腰腿痛（待查体影像复核）' : '非特异性腰痛等待鉴别'}</li>
      <li>中医候选：${c.tags.includes('osteoporosis') ? '肝肾不足背景' : '气血痹阻夹湿候选'}；${c.tags.includes('cold_aggravation') ? '寒湿/寒凝经脉线索' : '寒热信息待补'}</li>
      <li>方剂路线信号：${c.modules.join('、') || '待补充信息'}</li>
      <li>安全复核：高风险药物、NSAIDs、抗凝/激素/降糖药、肝肾功能、胃肠风险。</li>
    </ul></section>
    <section class="result-panel"><h3>医师审核签名闭环</h3><p>最终诊断、完整处方、剂量、煎服法和疗程只能在医生端由 licensed physician 手工录入并签名；患者端只显示医案整理线索。</p></section>
    <div class="footer-actions"><button class="ghost-btn" id="prevBtn">上一步</button><button class="primary-btn" id="nextBtn">生成最终医案</button></div>`;
  document.querySelector('#prevBtn').addEventListener('click', () => { state.step--; render(); });
  document.querySelector('#nextBtn').addEventListener('click', () => { state.step++; render(); });
}

function renderFinal() {
  const report = buildReport();
  const tabs = {
    case: report.markdown,
    reasoning: report.reasoning,
    summary: report.summary,
    handoff: report.handoff,
    tao: report.tao,
    cdss: report.cdss,
    physician: report.physician,
    json: JSON.stringify(report.json, null, 2),
  };
  screen.innerHTML = `<section class="result-panel"><div class="report-tabs">
    <button class="tab-btn active" data-tab="case">标准医案</button>
    <button class="tab-btn" data-tab="reasoning">经验推理</button>
    <button class="tab-btn" data-tab="summary">经验按语</button>
    <button class="tab-btn" data-tab="handoff">医生复核</button>
    <button class="tab-btn" data-tab="tao">Tao 教学解释</button>
    <button class="tab-btn" data-tab="cdss">CDSS 草案</button>
    <button class="tab-btn" data-tab="physician">医师签名</button>
    <button class="tab-btn" data-tab="json">规则 JSON</button>
  </div><pre class="report-box" id="reportBox"></pre><div class="footer-actions"><button class="ghost-btn" id="copyBtn">复制当前内容</button><button class="primary-btn" id="downloadBtn">下载 Markdown</button></div>${state.doctorMode ? feedbackWidget(state.reportFeedback, 'form-report') : ''}</section>`;
  const box = document.querySelector('#reportBox');
  const setTab = key => { box.textContent = tabs[key]; document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === key)); };
  setTab('case');
  document.querySelectorAll('.tab-btn').forEach(btn => btn.addEventListener('click', () => setTab(btn.dataset.tab)));
  document.querySelector('#copyBtn').addEventListener('click', () => navigator.clipboard?.writeText(box.textContent));
  document.querySelector('#downloadBtn').addEventListener('click', () => download('yaobi-case.md', box.textContent));
  wireFeedback(() => ({
    holder: state.reportFeedback,
    target: 'form_report',
    extra: { intent: 'form_final_report', used_llm: false },
  }), renderFinal);
}

function buildReport() {
  const a = state.answers; const c = buildCase();
  const md = `# 腰痹医案草稿\n\n## 一、基本信息\n患者：${a.sex || '未详'}，${a.age || '未详'}岁\n\n## 二、主诉\n${c.chief}\n\n## 三、现病史\n疼痛部位：${fmt(a.location)}；放射情况：${fmt(a.radiation)}；疼痛性质：${fmt(a.pain_nature)}；疼痛评分：${a.severity ?? '未详'}/10。加重因素：${fmt(a.aggravating)}；缓解因素：${fmt(a.relieving)}。下肢麻木：${fmt(a.numbness)}，部位：${fmt(a.numbness_location)}；下肢无力：${fmt(a.weakness)}。\n\n## 四、伴随症状\n寒热：${fmt(a.cold_heat)}；寒热与疼痛关系：${fmt(a.cold_relation)}；睡眠：${fmt(a.sleep)}；胃纳：${fmt(a.appetite)}；口苦口干：${fmt(a.mouth_taste)}。\n\n## 五、既往史与检查\n既往疾病：${fmt(a.diseases)}；影像/检查：${fmt(a.imaging)}；既往诊断：${fmt(a.western_diagnosis)}；近期用药：${fmt(a.medications)}；过敏史：${fmt(a.allergy)}。\n\n## 六、中医四诊信息\n舌色：${fmt(a.tongue_color)}；舌苔：${fmt(a.tongue_coating)}；脉象：待医生面诊补充。\n\n## 七、结构化标签\n${c.tags.map(t => `- ${t}`).join('\n') || '- 暂无'}\n\n## 八、沈老经验规则线索\n${c.modules.map((m, i) => `${i + 1}. ${m}`).join('\n') || '1. 信息不足，待补充。'}\n\n## 九、医生复核清单\n- 红旗筛查：${c.red.status}\n- 下肢肌力、感觉、反射查体\n- 影像/骨密度报告\n- NSAIDs、抗凝药、肝肾功能和胃肠风险\n- 高风险药物需医师审核\n\n> 本报告为医案整理和医生端 CDSS 草案，不构成最终诊断、签名处方或患者可执行剂量。`;
  const handoff = `# 医生复核摘要\n\n- 主要问题：${c.chief}。\n- 规则标签：${c.tags.join('、') || '待补充'}。\n- 方剂路线信号：${c.modules.join('、') || '待补充'}。\n- 信息缺口：脉象、影像原文、下肢肌力/感觉查体、用药剂量与不良反应。`;
  const tao = `# Tao 教学解释叠加\n\n运行方式：TAO_BACKEND=transformers python -m backend.main --tao-chat "请解释本案规则线索" --stream\n\nTao Direct Runtime 负责把规则证据转写为教学解释；输出需通过 JSON Repair 和 Output Guard，不允许最终诊断、完整处方或患者可执行剂量。`;
  const cdss = `# CDSS 草案\n\n状态：draft_for_clinician_review；patient_visible=false；complete_prescription_generated=false；patient_executable_dose_generated=false。\n\n候选方向：${c.tags.includes('lower_limb_numbness') ? '腰腿痛/神经根相关风险待复核' : '腰痛待鉴别'}；候选证型：${c.tags.includes('osteoporosis') ? '肝肾不足背景' : '气血痹阻夹湿候选'}。`;
  const physician = `# 医师审核签名\n\n最终诊断、完整处方、剂量、煎服法、疗程只能由 licensed physician 手工录入。系统输出仅为规则证据和草案，医生确认后方可锁定。`;
  const r = buildReasoningChain();
  const reasoning = `# 医师经验辨证推理（规则派生）\n\n` + r.chain.map((s, i) => `## ${i + 1}. ${s.t}\n${s.c}`).join('\n\n') + `\n\n> 倾向性表述，非最终诊断/处方；后端开启 Tao 后由 physician_reasoning_skill 语言化并经 Output Guard 校验。`;
  const summary = `# 医案按语（教学复盘）\n\n## 辨证要点\n证候倾向「${r.top.name}」，证据：${r.top.ev.join('、')}。\n\n## 治法治则\n${r.therapy}（待医师审定）。\n\n## 选方用药思路\n${c.modules.join('、') || '待补充'}。具体方药、加减与用量由医师审定。\n\n## 沈老经验体现\n温通经络、益气养血、顾护肝肾脾胃与少阳枢机。\n\n> 科研教学复盘，非诊断非处方；后端由 case_experience_summary_skill 生成并经安全校验。`;
  return { markdown: md, reasoning, summary, handoff, tao, cdss, physician, json: { answers: state.answers, tags: c.tags, modules: c.modules, red_flags: c.red, syndrome_tendency: r.top.name, therapy: r.therapy, tao_probe_answers: Object.fromEntries(Object.entries(state.answers).filter(([k]) => k.startsWith('TAO_PROBE_'))), runtime: 'Tao Direct Transformers Runtime', guards: ['JSON Repair', 'Output Guard'], cdss_status: 'draft_for_clinician_review' } };
}

function fmt(v) { return Array.isArray(v) ? (v.join('、') || '未详') : (v || '未详'); }
function tagLabel(t) {
  const map = { elderly: '年龄偏大，对退变/肝肾不足背景有价值', chronic_yabi: '腰痛时间较久', lower_limb_numbness: '有下肢麻木', radiating_leg_pain: '有下肢放射痛', cold_aggravation: '受凉加重', warmth_relieves: '热敷缓解', osteoporosis: '骨质疏松线索', dark_tongue: '舌色偏暗紫', white_greasy_coating: '白腻苔', insomnia: '睡眠欠佳', poor_appetite: '胃纳较差', bitter_taste: '口苦/少阳线索' };
  return map[t] || t;
}

function updatePreview() {
  const c = buildCase();
  document.querySelector('#previewChief').textContent = c.chief || '未采集';
  document.querySelector('#previewHistory').textContent = `疼痛：${fmt(state.answers.location)}；放射：${fmt(state.answers.radiation)}；麻木：${fmt(state.answers.numbness)}。`;
  document.querySelector('#previewTags').textContent = c.tags.join('、') || '暂无';
  const missing = ['age','sex','duration','location','radiation','numbness','cold_relation','sleep','appetite','imaging','diseases'].filter(k => !state.answers[k]);
  document.querySelector('#previewMissing').textContent = missing.join('、') || '关键字段已较完整';
  const score = Math.max(0, Math.round((11 - missing.length) / 11 * 100));
  document.querySelector('#qualityRing').textContent = `${score}%`;
  document.querySelector('#qualityRing').style.background = `conic-gradient(var(--green) ${score * 3.6}deg, #eadfce 0deg)`;
  document.querySelector('#qualityGrade').textContent = score >= 80 ? 'good / 可复核' : score >= 60 ? 'fair / 需补问' : 'needs_more_info';
  document.querySelector('#qualityHint').textContent = score >= 80 ? '已可生成医生复核摘要。' : '建议继续补充关键字段。';
}
function download(name, text) { const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([text], { type: 'text/markdown' })); a.download = name; a.click(); URL.revokeObjectURL(a.href); }

// ---------------------------------------------------------------------------
// Tao auto follow-up probes (rule-constrained, advisory)
// ---------------------------------------------------------------------------

function taoEnabled() { return state.fsm.taoProbes !== false; }
const PROBE_BUDGET = 2;
const PROBE_THEME = { pain: '疼痛特征', neuro: '神经骨科线索', tcm: '中医四诊', comorbidity: '合并病与用药' };

// Genuine Tao follow-up probes: the model generates rule-bounded clarifying questions
// server-side (tao_followup_probe_skill) and they pass JSON-repair + Output Guard. When the
// backend is offline we show an honest note instead of fabricating "Tao" probes.
async function loadTaoProbes(stage, grid) {
  if (!taoEnabled() || !PROBE_THEME[stage]) return;
  if (!taoOnline()) {
    const note = document.createElement('article');
    note.className = 'question-card tao-probe';
    note.innerHTML = `<div class="question-meta">TAO 自动追问 · 离线</div><h3>Tao 自动追问需连接后端</h3><p class="muted">启动后端服务（Colab 用 ngrok 暴露）后，本状态会由 Tao 在「${PROBE_THEME[stage]}」主题内生成澄清式追问——规则约束、不驱动状态跳转、经 Output Guard 校验。</p>`;
    grid.appendChild(note);
    return;
  }
  try {
    const res = await api.post('/api/followup_probe', casePayload({ stage, last_answers: state.fsm.lastAnswers[stage] || {}, budget: PROBE_BUDGET }));
    const rt = res.tao_probe_runtime || {};
    const probes = res.probes || [];
    if (!probes.length) {
      const note = document.createElement('article');
      note.className = 'question-card tao-probe';
      note.innerHTML = `<div class="question-meta">TAO 自动追问 · ${escapeHtml(rt.status || 'no_probe')}（${escapeHtml((res.tao || {}).backend || '')}）</div><h3>本轮无新增追问</h3><p class="muted">Tao 在规则约束内未生成新的追问，或被 Output Guard 回退到规则。</p>`;
      grid.appendChild(note);
      return;
    }
    probes.forEach(p => {
      const q = { id: p.id, q: p.question, probe: true, reason: `${p.reason || '本状态主题内的澄清式追问'} · Tao ${rt.status}（${(res.tao || {}).backend || ''}）` };
      grid.appendChild(renderQuestion(q, stage));
    });
  } catch (e) {
    await api.health(); renderTaoBadge();
  }
}

// ---------------------------------------------------------------------------
// Client-side reasoning + experience mirror (backend remains source of truth)
// ---------------------------------------------------------------------------

const SYNDROME_THERAPY_CN = {
  '气血痹阻证': '益气养血、通络止痛', '气滞血瘀证': '行气活血、化瘀通络', '寒湿痹阻证': '温经散寒、祛湿通络',
  '湿热痹阻证': '清热利湿、通络止痛', '肝肾不足证': '补益肝肾、强筋壮骨', '肾阳不足证': '温补肾阳、散寒止痛',
  '脾虚不运证': '健脾益气、化湿和中', '少阳证类': '和解少阳、疏利枢机', '气血不足证': '补益气血、荣筋止痛',
};

function inferSyndromes() {
  const t = getTags();
  const cands = [];
  if (t.includes('lower_limb_numbness') || t.includes('radiating_leg_pain')) cands.push({ name: '气血痹阻证', score: 6, ev: ['下肢麻木/放射痛'] });
  if (t.includes('dark_tongue')) cands.push({ name: '气滞血瘀证', score: 5, ev: ['舌质暗紫'] });
  if (t.includes('cold_aggravation') && t.includes('warmth_relieves')) cands.push({ name: '寒湿痹阻证', score: 4, ev: ['遇冷加重、得温则减'] });
  if (t.includes('osteoporosis') || t.includes('elderly')) cands.push({ name: '肝肾不足证', score: 4, ev: ['高龄/骨质疏松'] });
  if (t.includes('bitter_taste') && t.includes('insomnia')) cands.push({ name: '少阳证类', score: 3, ev: ['口苦+失眠'] });
  if (t.includes('poor_appetite')) cands.push({ name: '脾虚不运证', score: 3, ev: ['纳差'] });
  if (!cands.length) cands.push({ name: '气血痹阻证', score: 2, ev: ['默认主导证候，待补充四诊'] });
  return cands.sort((a, b) => b.score - a.score);
}

function buildReasoningChain() {
  const c = buildCase();
  const syn = inferSyndromes();
  const top = syn[0];
  const therapy = SYNDROME_THERAPY_CN[top.name] || '待辨证确立';
  const chain = [
    { t: '四诊与症状采集要点', c: `主诉「${c.chief || '腰痛'}」，关键线索：${c.tags.map(tagLabel).join('、') || '待补充'}。` },
    { t: '辨证倾向（供医师审定）', c: `证候倾向「${top.name}」，证据：${top.ev.join('、')}${syn[1] ? `；次选：${syn.slice(1, 3).map(s => s.name).join('、')}` : ''}。` },
    { t: '治法（倾向）', c: `可考虑：${therapy}（待医师审定）。` },
    { t: '方剂路线信号（路线倾向）', c: `${c.modules.join('、') || '信息待补充'}。仅为路线倾向，方药、加减与用量由医师审定。` },
    { t: '安全与禁忌复核', c: `红旗状态：${c.red.status}；附片/细辛/全蝎/蜈蚣等高风险药需医师重点复核。` },
  ];
  return { chain, top, therapy, syn, c };
}

// ---------------------------------------------------------------------------
// Conversational Q&A (mirrors backend ConversationSession + skill_router)
// ---------------------------------------------------------------------------

const CHAT_INTENTS = [
  { id: 'syndrome_inquiry', label: '证候辨析', group: '辨证论治', kw: ['证型', '证候', '辨证', '什么证', '寒湿', '血瘀', '肝肾', '少阳', '气血'], examples: ['这个病人偏向什么证型？', '为什么考虑气血痹阻证？'] },
  { id: 'reasoning_inquiry', label: '辨证推理', group: '辨证论治', kw: ['思路', '推理', '为什么这样', '辨证论治', '怎么分析', '治法'], examples: ['从症状到治法是怎么推的？', '讲讲这个案子的推理过程'] },
  { id: 'formula_inquiry', label: '方剂路线', group: '辨证论治', kw: ['方剂', '方子', '用什么方', '主方', '路线', '独活寄生', '当归四逆', '桂枝芍药知母'], examples: ['可以考虑哪些方剂路线？', '下肢麻木倾向哪条方剂路线？'] },
  { id: 'herb_inquiry', label: '用药模块', group: '辨证论治', kw: ['药物', '用药', '中药', '模块', '功效', '通络', '祛风', '补肝肾', '虫类'], examples: ['对应哪些用药功效模块？', '有没有通络相关的药物模块？'] },
  { id: 'safety_inquiry', label: '安全审查', group: '安全与风险', kw: ['安全', '风险', '冲突', '禁忌', '高风险', '毒性', '配伍'], examples: ['这个方案有什么用药安全风险？', '有没有配伍冲突？'] },
  { id: 'red_flag_inquiry', label: '红旗排查', group: '安全与风险', kw: ['红旗', '危险信号', '急诊', '马尾', '肿瘤', '感染', '骨折', '无力'], examples: ['有哪些危险信号需要排查？', '什么情况要立刻去医院？'] },
  { id: 'dose_inquiry', label: '剂量经验', group: '安全与风险', kw: ['剂量', '用量', '多少克', '几克', '克数'], examples: ['细辛常用多少量？', '附片的剂量分布是怎样的？'] },
  { id: 'mining_inquiry', label: '数据挖掘', group: '数据挖掘', kw: ['数据', '多少例', '最多', '统计', '规律', '占比', '分布', '关联', '几个'], examples: ['数据里哪个证型最多？', '气血痹阻证最常用什么方？', '下肢麻木对应什么方剂？'] },
  { id: 'evidence_inquiry', label: '证据回溯', group: '数据挖掘', kw: ['证据', '依据', 'support', 'confidence', 'lift', '回溯', '规则来源', '出处'], examples: ['这些建议的挖掘证据是什么？', '匹配到哪些候选规则？'] },
  { id: 'experience_inquiry', label: '经验总结', group: '经验与系统', kw: ['总结', '按语', '经验', '复盘', '医案小结', '规律总结'], examples: ['总结一下这个医案', '概括沈老腰痹用药经验规律'] },
  { id: 'agent_inquiry', label: '协作机制', group: '经验与系统', kw: ['智能体', '协作', 'agent', '编排', '黑板', '怎么工作', '流程'], examples: ['这些智能体是怎么协作的？', '红旗命中后流程怎么走？'] },
  { id: 'capabilities', label: '功能引导', group: '经验与系统', kw: ['能问什么', '帮助', '怎么用', '功能', '可以做什么', 'help'], examples: ['我可以问你哪些问题？', '你能做什么？'] },
];
const BLOCK_KW = ['完整处方', '开方', '开个方', '给我方子', '最终诊断', '确诊'];

function chatRoute(q) {
  const text = (q || '').toLowerCase();
  if (!state.doctorMode && BLOCK_KW.some(k => q.includes(k))) return { id: 'safety_block', label: '安全拦截', method: 'guard' };
  let best = CHAT_INTENTS.find(i => i.id === 'capabilities'), score = 0;
  CHAT_INTENTS.forEach(i => { const n = i.kw.filter(k => text.includes(k.toLowerCase())).length; if (n > score) { score = n; best = i; } });
  return { ...best, method: taoEnabled() ? 'llm' : 'keyword', score };
}

function chatQueryMined(q) {
  if (!MINED) return '尚未加载脱敏挖掘数据，请先运行挖掘管道。';
  const zheng = (MINED.dataset_stats || {}).zheng_distribution || {};
  for (const [name, count] of Object.entries(zheng)) {
    if (name && q.includes(name)) {
      const routes = (MINED.formula_signature_hits || []).filter(h => Object.keys(h.by_zheng || {})[0] === name).slice(0, 4).map(h => `${h.formula}(${h.n_cases}例)`);
      return `「${name}」在脱敏样本中出现 **${count}** 例。${routes.length ? '关联主方路线：' + routes.join('、') : ''}`;
    }
  }
  for (const h of (MINED.formula_signature_hits || [])) { if (q.includes(h.formula)) return `「${h.formula}」命中 **${h.n_cases}** 张处方，主对应证型「${Object.keys(h.by_zheng || {})[0] || '—'}」。`; }
  for (const [herb, d] of Object.entries(MINED.dose_table || {})) { if (q.includes(herb)) return `「${herb}」经验剂量：常用 ${d.mode_g} 克（${d.min_g}–${d.max_g} 克，n=${d.n}）。仅为医师端研究分布，非可执行医嘱。`; }
  for (const [kw, tag] of Object.entries({ 麻木: 'lower_limb_numbness', 放射: 'radiating_leg_pain', 遇冷: 'cold_aggravation', 口苦: 'bitter_taste', 失眠: 'insomnia', 高龄: 'elderly', 骨质疏松: 'osteoporosis' })) {
    if (q.includes(kw)) {
      const assoc = (MINED.rule_candidates || []).filter(r => String(r.rule_type).includes('association') && r.if && r.if.tag === tag).slice(0, 5);
      if (assoc.length) return `「${kw}」相关挖掘关联规律：\n` + assoc.map(r => `- ${kw} → ${Object.values(r.then)[0]}（support ${r.statistics.support}，confidence ${r.statistics.confidence}，lift ${r.statistics.lift}）`).join('\n');
    }
  }
  const s = MINED.dataset_stats || {};
  const topZ = Object.entries(zheng).slice(0, 4).map(([k, v]) => `${k}(${v})`).join('、');
  const topF = (MINED.formula_signature_hits || []).slice(0, 4).map(h => `${h.formula}(${h.n_cases})`).join('、');
  return `脱敏样本 ${s.n_cases || '—'} 例（含处方 ${s.n_with_prescription || '—'} 例）。高频证型：${topZ || '—'}。核心方剂路线：${topF || '—'}。`;
}

function chatAnswer(intent, q) {
  const c = buildCase();
  const syn = inferSyndromes();
  const llm = taoEnabled();
  switch (intent) {
    case 'safety_block': return { md: '不能生成最终诊断、完整处方或患者可执行剂量；可以提供候选证型、方剂路线信号、用药模块解释（无剂量）和医生复核清单。', skills: ['patient_request_guard_skill'] };
    case 'syndrome_inquiry': return { md: '候选证型（倾向，非最终诊断）：\n' + syn.slice(0, 4).map(s => `- ${s.name}：${s.ev.join('、')}`).join('\n'), skills: ['syndrome_router_skill'] };
    case 'reasoning_inquiry': { const r = buildReasoningChain(); return { md: '辨证推理链（倾向性）：\n' + r.chain.map((s, i) => `${i + 1}. ${s.t}：${s.c}`).join('\n'), skills: ['physician_reasoning_skill'], usedLlm: llm }; }
    case 'formula_inquiry': return { md: '候选方剂路线信号（非处方）：\n' + (c.modules.length ? c.modules.map(m => `- ${m}`).join('\n') : '- 信息待补充'), skills: ['formula_base_selector_skill'] };
    case 'herb_inquiry': return { md: '用药功效模块草案（需医师审核，无剂量）：\n' + (c.modules.length ? c.modules.map(m => `- ${m}`).join('\n') : '- 待补充'), skills: ['herb_module_composer_skill'] };
    case 'safety_inquiry': return { md: `安全状态：**${c.red.status}**。附片/细辛/全蝎/蜈蚣等高风险药需医师重点复核；注意孕期、抗凝、肝肾功能与胃肠风险。`, skills: ['safety_guard_skill', 'conflict_checker_skill'] };
    case 'red_flag_inquiry': return { md: `红旗筛查状态：**${c.red.status}**。四类需立即排查：\n- 马尾综合征（大小便障碍、会阴麻木）\n- 肿瘤风险（肿瘤史、消瘦、夜间痛进行性加重）\n- 感染风险（发热寒战、近期感染）\n- 骨折风险（外伤、长期激素、重度骨质疏松骤发剧痛）`, skills: ['red_flag_screen_skill'] };
    case 'dose_inquiry': return { md: chatQueryMined(q), skills: ['xlsx_dose_mining'] };
    case 'mining_inquiry': return { md: chatQueryMined(q), skills: ['xlsx_case_miner'] };
    case 'evidence_inquiry': { const ev = MINED ? (MINED.rule_candidates || []).filter(r => { const t = r.if && r.if.tag; return t && (c.tags.includes(t) || (t.startsWith('zheng::') && t.split('::')[1] === syn[0].name)); }).slice(0, 6) : []; return { md: ev.length ? '匹配到的挖掘候选规则（待专家审核）：\n' + ev.map(r => `- ${r.rule_id}：${Object.values(r.then)[0]}（lift ${r.statistics.lift || '—'}）`).join('\n') : '当前病例标签未匹配到挖掘候选规则，或挖掘数据未加载。', skills: ['mined_evidence_skill'] }; }
    case 'experience_inquiry': { const r = buildReasoningChain(); return { md: `医案按语（教学复盘）：\n- 辨证倾向「${r.top.name}」\n- 治法：${r.therapy}\n- 选方用药：${c.modules.join('、') || '待补充'}\n- 沈老经验：温通经络、益气养血、顾护肝肾脾胃与少阳枢机`, skills: ['case_experience_summary_skill'], usedLlm: llm }; }
    case 'agent_inquiry': return { md: '多智能体在共享黑板上自主协作：CaseStructuring → RedFlag → OrthoRisk → TcmSyndrome → FormulaReasoning → HerbModule → ConflictSafety → EvidenceTrace → Reasoning(语言模型) → Experience(语言模型) → PhysicianReview。红旗智能体命中急诊信号时自主中止下游临床智能体，仅急诊提示续跑。', skills: ['AgentOrchestrator'] };
    default: return { md: '你可以这样问我（点下方示例也行）：\n' + groupedStarters().map(g => `**${g.group}**：` + g.items.map(i => `「${i.examples[0]}」`).join('；')).join('\n'), skills: ['skill_router'] };
  }
}

function groupedStarters() {
  const groups = {};
  CHAT_INTENTS.forEach(i => { (groups[i.group] = groups[i.group] || { group: i.group, items: [] }).items.push(i); });
  return Object.values(groups);
}

function chatPlan(q) {
  const text = q.toLowerCase();
  const scored = CHAT_INTENTS.filter(i => i.id !== 'capabilities')
    .map(i => ({ i, hits: i.kw.filter(k => text.includes(k.toLowerCase())) }))
    .filter(x => x.hits.length).sort((a, b) => b.hits.length - a.hits.length);
  let plan = scored.slice(0, 3).map(x => ({ intent: x.i.id, label: x.i.label, reason: `问题含「${x.hits.slice(0, 2).join('、')}」线索，需要${x.i.label}` }));
  if (!plan.length) { const r = chatRoute(q); plan = [{ intent: r.id, label: r.label, reason: '按默认路由调用' }]; }
  return plan;
}

async function chatAsk(q) {
  if (!q || !q.trim()) return;
  q = q.trim();
  const others = CHAT_INTENTS.filter(i => i.id !== 'capabilities').slice(0, 4).map(i => i.examples[0]);

  // Backend-first: the language model genuinely routes / plans and invokes skills server-side.
  if (taoOnline()) {
    state.chat.history.push({ q, pending: true });
    renderChatModule();
    try {
      const endpoint = state.chat.autonomous ? '/api/autonomous' : '/api/chat';
      const res = await api.post(endpoint, casePayload({ question: q }));
      state.chat.history.pop();
      state.chat.history.push(state.chat.autonomous ? adaptAuto(q, res) : adaptChat(q, res));
      return renderChatModule();
    } catch (e) {
      state.chat.history.pop();
      await api.health();           // backend may have dropped; refresh status then fall back
      renderTaoBadge();
    }
  }

  // Offline fallback: client-side rules, labelled honestly (no fake Tao claims).
  if (!state.doctorMode && BLOCK_KW.some(k => q.includes(k))) {
    const a = chatAnswer('safety_block', q);
    state.chat.history.push({ q, intent: 'safety_block', label: '安全拦截', method: 'guard', md: a.md, skills: a.skills, real: false, followups: ['有哪些危险信号需要排查？', '可以考虑哪些方剂路线？'] });
    return renderChatModule();
  }
  if (state.chat.autonomous) {
    const plan = chatPlan(q);
    const steps = plan.map((p, i) => { const a = chatAnswer(p.intent, q); return { step: i + 1, intent: p.intent, label: p.label, reason: p.reason, md: a.md, skills: a.skills, usedLlm: false }; });
    const md = steps.length > 1
      ? `为回答此问题，自主规划了 ${steps.length} 步并委派子智能体：${steps.map(s => s.label).join(' → ')}。`
      : steps[0].md;
    state.chat.history.push({ q, autonomous: true, plan, steps, label: '自主多步', method: 'keyword', md, multiStep: steps.length > 1, usedLlm: false, real: false, followups: others });
    return renderChatModule();
  }
  const route = chatRoute(q);
  const ans = chatAnswer(route.id, q);
  state.chat.history.push({ q, intent: route.id, label: route.label, method: 'keyword', md: ans.md, skills: ans.skills, usedLlm: false, real: false, followups: others });
  renderChatModule();
}

// Adapt backend turn payloads into the chat-history shape the renderer expects.
function adaptChat(q, res) {
  const t = res.turn || {}; const tao = res.tao || {}; const rt = t.llm_routing || {};
  return {
    q, intent: t.intent, label: t.intent_label || t.label || t.intent, method: t.method,
    md: t.answer, skills: t.skills || [], usedLlm: !!t.used_llm, real: true,
    routingStatus: rt.status, backend: tao.backend, model: tao.model_id,
    followups: t.suggested_followups || [],
  };
}
function adaptAuto(q, res) {
  const t = res.turn || {}; const tao = res.tao || {}; const rt = t.plan_runtime || {};
  return {
    q, autonomous: true, multiStep: !!t.multi_step, label: '自主多步', method: t.plan_method,
    plan: (t.plan || []).map(p => ({ label: p.label })),
    steps: (t.steps || []).map(s => ({ step: s.step, intent: s.intent, label: s.label, reason: s.reason, md: s.answer, usedLlm: !!s.used_llm })),
    md: t.answer, usedLlm: !!t.used_llm, real: true, routingStatus: rt.status, backend: tao.backend, model: tao.model_id,
    followups: CHAT_INTENTS.filter(i => i.id !== 'capabilities').slice(0, 4).map(i => i.examples[0]),
  };
}

function renderChatModule() {
  pageTitle.textContent = '智能问答 · 语言模型自主选择技能并按提问挖掘';
  const h = state.chat.history;
  const bubbles = h.map((t, ti) => {
    if (t.pending) {
      return `<div class="chat-turn"><div class="bubble user">${escapeHtml(t.q)}</div><div class="bubble bot"><div class="bot-body muted">⏳ 正在调用语言模型选择技能并作答…（${escapeHtml(taoRuntimeTag())}）</div></div></div>`;
    }
    const isGuard = t.method === 'guard' || String(t.method || '').includes('guard') || t.intent === 'safety_block';
    const realLlm = t.real && t.method === 'llm';
    const routeWord = t.autonomous ? '规划' : '路由';
    const routeTag = isGuard ? '安全护栏'
      : t.real ? (realLlm ? `${routeWord}：Tao 选择 ✓${t.routingStatus ? '（' + t.routingStatus + '）' : ''}` : `${routeWord}：关键词回退`)
      : `${routeWord}：关键词（离线）`;
    const backendTag = t.real && t.backend ? `<span class="route-tag" title="语言模型运行时">${escapeHtml(t.backend)}${t.model ? ' · ' + escapeHtml(t.model) : ''}</span>` : '';
    const planHtml = t.autonomous && t.multiStep ? `
        <div class="plan-strip"><span class="plan-label">自主计划</span>${t.plan.map((p, i) => `<span class="plan-step">${i + 1}. ${p.label}</span>${i < t.plan.length - 1 ? '<span class="plan-arrow">→</span>' : ''}`).join('')}</div>` : '';
    const stepsHtml = t.autonomous && t.multiStep ? t.steps.map(s => `
        <div class="substep"><div class="substep-head"><span class="substep-no">${s.step}</span><strong>${s.label}</strong><span class="route-tag">委派 → ${s.intent}</span>${s.usedLlm ? '<span class="kind-badge llm-on">Tao</span>' : ''}</div><div class="substep-reason">${escapeHtml(s.reason)}</div><div class="bot-body">${mdLite(s.md)}</div></div>`).join('') : `<div class="bot-body">${mdLite(t.md)}</div>`;
    return `
    <div class="chat-turn">
      <div class="bubble user">${escapeHtml(t.q)}</div>
      <div class="bubble bot">
        <div class="bot-meta"><span class="kind-badge ${isGuard ? 'rule' : (realLlm ? 'llm' : 'rule')}">${t.label}</span>
          <span class="route-tag">${routeTag}</span>
          ${backendTag}
          ${t.autonomous && t.multiStep ? `<span class="route-tag">子智能体 ${t.steps.length} 个</span>` : ''}
          ${t.usedLlm ? '<span class="kind-badge llm-on">Tao 在环</span>' : ''}
          ${!t.autonomous && t.skills ? `<span class="route-tag">技能：${(t.skills || []).join(' / ')}</span>` : ''}
        </div>
        ${planHtml}
        ${t.autonomous && t.multiStep ? `<div class="bot-body synth">${mdLite(t.md)}</div>` : ''}
        ${stepsHtml}
        ${t.followups && t.followups.length ? `<div class="chip-row followups">${t.followups.map(f => `<button class="chip-btn" data-q="${escapeHtml(f)}">${f}</button>`).join('')}</div>` : ''}
        ${t.real ? feedbackWidget(t, `chat-${ti}`) : ''}
      </div>
    </div>`;
  }).join('');
  const starters = groupedStarters().map(g => `
    <div class="starter-group"><span class="starter-title">${g.group}</span>
      <div class="chip-row">${g.items.map(i => `<button class="chip-btn" data-q="${escapeHtml(i.examples[0])}" title="${i.label}">${i.examples[0]}</button>`).join('')}</div>
    </div>`).join('');
  const statusLine = taoOnline()
    ? `Tao 在线（${taoRuntimeTag()}）· 语言模型自主选择技能，并结合沈氏经验规则与脱敏数据进行辨证论治分析（供执业医师审核）`
    : '离线·规则模式（未连接 Tao 后端）· 仅显示确定性规则要点 · 启动后端服务后由语言模型给出深度分析';
  screen.innerHTML = `
    <section class="result-panel">
      <p class="eyebrow">draft_for_clinician_review · ${statusLine}</p>
      <div class="chat-window">${bubbles || '<p class="muted">向我提问腰痹辨证、方药、安全、数据挖掘或经验总结。连接 Tao 后端后，语言模型会自主选择对应技能并用规则与脱敏数据作答；开启「自主多步」可让智能体把复杂问题拆解、委派给多个子智能体并综合作答。</p>'}</div>
      <div class="chat-toolbar"><label class="fsm-setting"><input type="checkbox" id="autoModeToggle" ${state.chat.autonomous ? 'checked' : ''} />自主多步（规划+子智能体委派）</label><span class="muted">复合问题（如“是什么证型、用什么方、有何风险”）会被拆成多步</span></div>
      <div class="chat-input"><input id="chatInput" type="text" placeholder="输入问题，如：这个病人是什么证型、用什么方、有什么风险？" /><button class="primary-btn" id="chatSend">发送</button>${h.length ? '<button class="ghost-btn" id="chatClear">清空</button>' : ''}</div>
    </section>
    <section class="result-panel"><h3>你可以这样问（点击直接提问）</h3>${starters}
      <p class="muted">语言模型只能从上述受限技能集中选择，越界或解析失败回退关键词路由；患者端请求最终诊断/处方/剂量会被安全护栏拦截。</p>
    </section>`;
  const input = document.querySelector('#chatInput');
  const send = () => { chatAsk(input.value); };
  document.querySelector('#autoModeToggle').addEventListener('change', e => { state.chat.autonomous = e.target.checked; });
  document.querySelector('#chatSend').addEventListener('click', send);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
  const clr = document.querySelector('#chatClear');
  if (clr) clr.addEventListener('click', () => { state.chat.history = []; renderChatModule(); });
  screen.querySelectorAll('.chip-btn:not(.feedback-btn):not(.feedback-submit)').forEach(b => b.addEventListener('click', () => chatAsk(b.dataset.q)));
  wireFeedback(key => {
    const t = state.chat.history[Number(String(key).replace('chat-', ''))];
    if (!t) return null;
    return {
      holder: t,
      target: t.autonomous ? 'autonomous_turn' : 'chat_turn',
      extra: { intent: t.intent || null, used_llm: !!t.usedLlm, answer_source: t.method || null },
    };
  }, renderChatModule);
  const win = screen.querySelector('.chat-window');
  if (win) win.scrollTop = win.scrollHeight;
}

function escapeHtml(s) { return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
// Small Markdown renderer: headings / bullet lists / blockquotes / bold — for the long,
// professional Tao consultation answers (not just **bold** + line breaks).
function mdLite(s) {
  const lines = escapeHtml(String(s == null ? '' : s)).split('\n');
  const out = [];
  let inList = false;
  const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
  for (const raw of lines) {
    const line = raw.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    const bullet = line.match(/^\s*[-•]\s+(.*)$/);
    if (h) { closeList(); const lvl = Math.min(6, h[1].length + 3); out.push(`<h${lvl} class="md-h">${h[2]}</h${lvl}>`); }
    else if (bullet) { if (!inList) { out.push('<ul class="md-ul">'); inList = true; } out.push(`<li>${bullet[1]}</li>`); }
    else if (/^&gt;\s*/.test(line)) { closeList(); out.push(`<blockquote class="md-quote">${line.replace(/^&gt;\s*/, '')}</blockquote>`); }
    else if (line.trim() === '') { closeList(); out.push('<div class="md-gap"></div>'); }
    else { closeList(); out.push(`<p class="md-p">${line}</p>`); }
  }
  closeList();
  return out.join('');
}

// ---------------------------------------------------------------------------
// Multi-agent collaboration (mirrors backend AgentOrchestrator trace)
// ---------------------------------------------------------------------------

const AGENTS = [
  { name: 'CaseStructuringAgent', role: '病例结构化与质量', kind: 'rule', handoff: 'RedFlagAgent' },
  { name: 'RedFlagAgent', role: '红旗硬门控', kind: 'rule', handoff: 'OrthoRiskAgent' },
  { name: 'OrthoRiskAgent', role: '骨伤科风险分层', kind: 'rule', handoff: 'TcmSyndromeAgent' },
  { name: 'TcmSyndromeAgent', role: '中医证候路由', kind: 'rule', handoff: 'FormulaReasoningAgent' },
  { name: 'FormulaReasoningAgent', role: '方剂路径推理', kind: 'rule', handoff: 'HerbModuleAgent' },
  { name: 'HerbModuleAgent', role: '药物功效模块', kind: 'rule', handoff: 'ConflictSafetyAgent' },
  { name: 'ConflictSafetyAgent', role: '冲突与安全审查', kind: 'rule', handoff: 'EvidenceTraceAgent' },
  { name: 'EvidenceTraceAgent', role: '证据回溯', kind: 'rule', handoff: 'ReasoningAgent' },
  { name: 'ReasoningAgent', role: '医师经验辨证推理', kind: 'llm', handoff: 'ExperienceAgent' },
  { name: 'ExperienceAgent', role: '案例经验总结', kind: 'llm', handoff: 'PhysicianReviewAgent' },
  { name: 'PhysicianReviewAgent', role: '医师审核装配', kind: 'rule', handoff: 'licensed_physician(human)' },
];

function buildAgentTrace() {
  const c = buildCase();
  const syn = inferSyndromes();
  const urgent = getRedFlagStatus().status === 'urgent';
  const llm = false;   // offline client mirror: Tao is only ever in the loop via the backend
  const summary = {
    CaseStructuringAgent: `结构化 ${c.tags.length} 个标签，刷新沈老经验信号。`,
    RedFlagAgent: urgent ? '命中红旗危险信号，自主中止下游临床协作。' : '未见急诊级红旗，放行下游协作。',
    OrthoRiskAgent: c.tags.includes('osteoporosis') ? '骨折风险背景升级，需医师重点复核。' : '四类骨伤科风险均为低风险背景。',
    TcmSyndromeAgent: `证候倾向「${syn[0].name}」，共 ${syn.length} 个候选。`,
    FormulaReasoningAgent: `主方路线信号：${c.modules[0] || '待补充'}。`,
    HerbModuleAgent: `组合 ${c.modules.length} 个功效模块草案，待安全审查。`,
    ConflictSafetyAgent: `安全状态：${c.red.status}；高风险药需医师复核。`,
    EvidenceTraceAgent: MINED ? '匹配 xlsx 脱敏挖掘证据（待专家审核）。' : '挖掘数据未加载。',
    ReasoningAgent: llm ? 'Tao 语言化辨证推理链（经 Output Guard 校验）。' : '规则派生辨证推理链（Tao 未启用）。',
    ExperienceAgent: llm ? 'Tao 润色医案按语（经 Output Guard 校验）。' : '确定性医案按语（Tao 未启用）。',
    PhysicianReviewAgent: '装配医生复核包与 CDSS 草案，移交医师签名（人类终审）。',
  };
  const conf = {
    CaseStructuringAgent: 0.8, RedFlagAgent: 1.0, OrthoRiskAgent: 1.0,
    TcmSyndromeAgent: Math.min(1, syn[0].score / 8), FormulaReasoningAgent: 0.6,
    HerbModuleAgent: 1.0, ConflictSafetyAgent: 1.0, EvidenceTraceAgent: 1.0,
    ReasoningAgent: 0.7, ExperienceAgent: 0.7, PhysicianReviewAgent: 1.0,
  };
  const trace = AGENTS.map((a, i) => {
    let status = 'ok';
    if (a.name === 'RedFlagAgent' && urgent) status = 'halt';
    else if (urgent && i > 1) status = 'skipped';
    else if (a.name === 'OrthoRiskAgent' && c.tags.includes('osteoporosis')) status = 'escalate';
    else if (a.name === 'ConflictSafetyAgent' && c.red.status !== 'safe') status = 'escalate';
    return { ...a, status, used_llm: a.kind === 'llm' && llm && !urgent, summary: summary[a.name], confidence: conf[a.name] };
  });
  if (urgent) trace.push({ name: 'EmergencyNoticeAgent', role: '急诊转诊提示', kind: 'rule', handoff: 'licensed_physician(human)', status: 'blocked', used_llm: false, summary: '已生成急诊/线下评估提示，停止常规辨证与方药协作。', confidence: 1.0 });
  return { trace, llm, urgent };
}

function renderAgentsModule() {
  pageTitle.textContent = '智能体协作 · 多智能体在共享黑板上的自主协作';
  paintAgents(buildAgentTrace(), false);   // immediate paint (offline client mirror)
  if (taoOnline()) {                        // then replace with the real backend orchestrator run
    api.post('/api/collaboration', casePayload()).then(res => {
      const trace = (res.collaboration_trace || []).map(t => ({
        name: t.agent, role: t.role, kind: t.kind, status: t.status, summary: t.summary,
        confidence: t.confidence, used_llm: !!t.used_llm, handoff: (t.handoff_to || []).join('、'),
      }));
      paintAgents({ trace, llm: !!res.llm_in_loop, urgent: !!res.halted, usedLlm: res.used_llm_agents || [], real: true, tao: res.tao }, true);
    }).catch(async () => { await api.health(); renderTaoBadge(); });
  }
}

function paintAgents(model, real) {
  if (state.module !== 'agents') return;   // user navigated away before the fetch resolved
  const trace = model.trace || [];
  const urgent = model.urgent;
  const usedLlm = (model.usedLlm && model.usedLlm.length) ? model.usedLlm : trace.filter(t => t.used_llm).map(t => t.name);
  const llmInLoop = real ? model.llm : false;   // offline mirror can never claim Tao is in the loop
  const statusCn = { ok: '完成', halt: '自主中止', skipped: '已跳过', escalate: '升级复核', blocked: '阻断' };
  const source = real
    ? `后端 AgentOrchestrator 实时返回 · ${escapeHtml((model.tao || {}).backend || '')} · ${escapeHtml((model.tao || {}).model_id || '')}`
    : (taoOnline() ? '正在向后端请求实时协作…' : '离线·客户端镜像（未连接后端，Tao 不在环）');
  screen.innerHTML = `
    <div class="stat-grid">
      <article class="stat-card"><p class="eyebrow">参与智能体</p><strong>${trace.length}</strong><span>规则 + 语言模型协同</span></article>
      <article class="stat-card"><p class="eyebrow">语言模型在环</p><strong>${llmInLoop ? '是' : '否'}</strong><span>${usedLlm.join('、') || (real ? 'Tao 被守卫回退/中止' : '离线未调用')}</span></article>
      <article class="stat-card"><p class="eyebrow">自主控制</p><strong>${urgent ? '红旗中止' : '正常放行'}</strong><span>红旗智能体可中止下游</span></article>
      <article class="stat-card"><p class="eyebrow">数据来源</p><strong>${real ? '实时后端' : (taoOnline() ? '加载中' : '离线')}</strong><span>${real ? '确定性为准·语言模型受守卫' : '客户端镜像'}</span></article>
    </div>
    <section class="result-panel"><p class="eyebrow">draft_for_clinician_review · ${source}</p>
      <h3>协作时间轴（后端 AgentOrchestrator）</h3>
      <ol class="agent-timeline">${trace.map((t, i) => `
        <li class="agent-step ${t.status}">
          <div class="agent-head">
            <span class="agent-order">${i + 1}</span>
            <strong>${escapeHtml(t.name)}</strong>
            <span class="kind-badge ${t.kind}">${t.kind === 'llm' ? '语言模型' : '规则'}</span>
            ${t.used_llm ? '<span class="kind-badge llm-on">Tao 在环</span>' : ''}
            <span class="agent-status ${t.status}">${statusCn[t.status] || t.status}</span>
            ${t.confidence != null ? `<span class="agent-conf">置信度 ${Number(t.confidence).toFixed(2)}</span>` : ''}
          </div>
          <p class="agent-role">${escapeHtml(t.role)}</p>
          <p class="agent-summary">${escapeHtml(t.summary)}</p>
          <p class="agent-handoff">→ 接力：${escapeHtml(t.handoff)}</p>
        </li>`).join('')}</ol>
    </section>
    <section class="result-panel"><h3>自主协作机制说明</h3>
      <ul>
        <li><strong>共享黑板</strong>：上游智能体把结论写入黑板，下游读取并续接，形成自主接力（后端 <code>Blackboard</code>）。</li>
        <li><strong>自主控制流</strong>：<code>RedFlagAgent</code> 命中急诊红旗时自主中止下游临床智能体，仅 <code>EmergencyNoticeAgent</code> 续跑。</li>
        <li><strong>语言模型在环</strong>：仅 <code>ReasoningAgent</code>、<code>ExperienceAgent</code> 调用 Tao，且必经 JSON Repair + Output Guard，违规回退规则结论。</li>
        <li><strong>人类终审</strong>：<code>PhysicianReviewAgent</code> 仅装配草案，最终诊断/处方/剂量交执业医师签名。</li>
      </ul>
    </section>`;
}

function renderReasoningModule() {
  pageTitle.textContent = '经验推理 · 基于规则 + Tao 的医师辨证推理';
  const { chain } = buildReasoningChain();
  const narrative = chain.map(s => `## ${s.t}\n${s.c}`).join('\n\n') + '\n\n> 规则派生推理链，全部为倾向性、非最终口吻；最终诊断、处方与用量须由执业医师审核签名。';
  screen.innerHTML = `
    <div class="panel-grid">
      <section class="result-panel"><p class="eyebrow">draft_for_clinician_review · 非患者可见 · 非最终诊断</p>
        <h3>辨证推理链（规则派生，可回溯）</h3>
        <ol class="reason-chain">${chain.map(s => `<li><strong>${s.t}</strong><p>${s.c}</p></li>`).join('')}</ol>
      </section>
      <section class="result-panel"><h3>Tao 推理叠加层</h3>
        <p class="muted" id="taoReasoningNote">配置后端 <code>TAO_BACKEND=transformers</code>（或 mock/http）后，<code>physician_reasoning_skill</code> 会把推理链语言化为辨证教学解释，经 JSON Repair + Output Guard 校验；出现最终诊断/处方/用量即回退规则链。${taoOnline() ? '正在请求后端实时推理…' : '当前离线，以下为规则派生示例叙述：'}</p>
        <pre class="report-box" id="taoReasoningBox">${narrative}</pre>
      </section>
    </div>`;
  if (taoOnline()) enhanceWithBackend('/api/reasoning', 'taoReasoningBox', 'taoReasoningNote');
}

// Replace a client-side mirror panel with the genuine backend (Tao-in-the-loop) output.
async function enhanceWithBackend(path, boxId, noteId) {
  try {
    const res = await api.post(path, casePayload());
    const r = res.result || {};
    const box = document.querySelector('#' + boxId);
    if (box && r.answer) box.textContent = r.answer;
    const note = noteId && document.querySelector('#' + noteId);
    if (note) note.innerHTML = `后端实时返回：<code>${escapeHtml((res.tao || {}).backend || '')} · ${escapeHtml((res.tao || {}).model_id || '')}</code>${r.used_llm ? ' · <strong>Tao 在环（已采纳）</strong>' : ' · Tao 未采纳/已回退规则'}${r.skills && r.skills.length ? ' · 技能：' + escapeHtml(r.skills.join(' / ')) : ''}`;
  } catch (e) {
    await api.health(); renderTaoBadge();
  }
}

function renderSummaryModule() {
  pageTitle.textContent = '经验总结 · 单案医案按语 + 脱敏经验规律';
  const { top, therapy, c } = buildReasoningChain();
  const caseMd = `# 医案按语（教学复盘 · 非诊断非处方）\n\n## 一、辨证要点\n关键线索：${c.tags.map(tagLabel).join('、') || '待补充'}；证候倾向「${top.name}」。\n\n## 二、治法治则\n${therapy}（待医师审定）。\n\n## 三、选方用药思路\n${c.modules.join('、') || '待补充'}。具体方药、加减与用量由医师审定。\n\n## 四、沈老经验体现\n温通经络、益气养血、顾护肝肾脾胃与少阳枢机的整体思路。\n\n## 五、随访复诊要点\n关注疼痛/麻木变化、睡眠与胃纳，有无新发无力或二便异常。`;
  let expHtml = '<p class="muted">未加载挖掘数据，运行挖掘管道后展示脱敏经验规律。</p>';
  if (MINED) {
    const s = MINED.dataset_stats || {};
    const routes = (MINED.formula_signature_hits || []).slice(0, 5).map(h => h.formula).join('、');
    const assoc = (MINED.rule_candidates || []).filter(r => String(r.rule_type).includes('association')).slice(0, 6);
    expHtml = `
      <p>脱敏病例 <strong>${s.n_cases || '—'}</strong> 例，含处方 <strong>${s.n_with_prescription || '—'}</strong> 例。</p>
      <p><strong>高频证候</strong>：${Object.keys(s.zheng_distribution || {}).slice(0, 4).join('、') || '—'}</p>
      <p><strong>核心方剂路线</strong>：${routes || '—'}</p>
      <p><strong>症状—方药关联规律（部分）</strong></p>
      <ul>${assoc.map(r => `<li>${cnTag(r.if.tag)} → ${Object.values(r.then)[0]}（lift ${r.statistics.lift}，n=${r.statistics.n_both}）</li>`).join('') || '<li>—</li>'}</ul>
      <p class="muted">全部为脱敏聚合统计与待专家审核的研究信号，剂量分布见证据回溯页，非可执行医嘱。</p>`;
  }
  screen.innerHTML = `
    <div class="panel-grid">
      <section class="result-panel"><p class="eyebrow">draft_for_clinician_review · 单案模式</p><h3>医案按语（当前病例）</h3><pre class="report-box" id="summaryCaseBox">${caseMd}</pre></section>
      <section class="result-panel"><p class="eyebrow">experience 模式 · 脱敏统计</p><h3>沈钦荣腰痹经验规律总结</h3>${expHtml}</section>
    </div>
    <section class="result-panel"><h3>Tao 自动生成说明</h3><p class="muted" id="summaryNote"><code>case_experience_summary_skill</code> 在后端把上述结构化要点交给 Tao 润色为按语/经验总结，经 Output Guard 校验，不得新增数据外结论、不得产出最终诊断或可执行处方；失败回退确定性模板。${taoOnline() ? '正在请求后端实时生成…' : '（当前离线，显示规则派生模板）'}</p></section>`;
  if (taoOnline()) enhanceWithBackend('/api/summary', 'summaryCaseBox', 'summaryNote');
}

// ---------------------------------------------------------------------------
// Module views (xlsx mining / evidence / review / safety / settings)
// ---------------------------------------------------------------------------

const TAG_CN = {
  lower_limb_numbness: '下肢麻木', radiating_leg_pain: '下肢放射痛', bilateral_leg_involvement: '双下肢受累',
  cold_aggravation: '遇冷/受凉加重', cold_pain: '冷痛', bitter_taste: '口苦', insomnia: '失眠/寐差',
  fatigue: '乏力', poor_appetite: '纳差', distending_pain: '胀痛', soreness: '酸痛', activity_limitation: '活动受限',
  night_pain: '夜间痛', sedentary_aggravation: '久坐加重', acute_on_chronic: '慢病急性加重', elderly: '高龄(≥60)',
  osteoporosis: '骨质疏松',
};
const cnTag = t => (t || '').startsWith('zheng::') ? `证型·${t.split('::')[1]}` : (TAG_CN[t] || t);

function noDataPanel() {
  return `<section class="result-panel"><h3>暂无挖掘数据</h3><p>请先在本地运行脱敏挖掘管道生成 <code>frontend/mined_rules.js</code>：</p>
    <pre class="runtime-code">python -m backend.mining.xlsx_case_miner --xlsx 门诊导出.xlsx \\
    --yaml rules/11_mined_rule_candidates.yaml --frontend frontend/mined_rules.js</pre>
    <p>原始 xlsx 含患者身份信息，仅保留在本地 <code>data/private/</code>，不会进入仓库；产物只包含聚合统计与行号引用。</p></section>`;
}

function barRows(dist, total, labelFn = x => x, max = 10) {
  const entries = Object.entries(dist || {}).slice(0, max);
  const top = Math.max(...entries.map(([, v]) => v), 1);
  return entries.map(([k, v]) => `
    <div class="bar-row"><span class="bar-label">${labelFn(k)}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${Math.round(v / top * 100)}%"></span></span>
      <span class="bar-value">${v}${total ? ` · ${Math.round(v / total * 100)}%` : ''}</span></div>`).join('');
}

function renderDashboard() {
  pageTitle.textContent = '总览看板 · 沈钦荣腰痹门诊经验数据';
  if (!MINED) { screen.innerHTML = noDataPanel(); return; }
  const s = MINED.dataset_stats;
  screen.innerHTML = `
    <div class="stat-grid">
      <article class="stat-card"><p class="eyebrow">脱敏病例</p><strong>${s.n_cases}</strong><span>门诊就诊记录</span></article>
      <article class="stat-card"><p class="eyebrow">含中药处方</p><strong>${s.n_with_prescription}</strong><span>可供方药挖掘</span></article>
      <article class="stat-card"><p class="eyebrow">候选规则</p><strong>${(MINED.rule_candidates || []).length}</strong><span>全部待专家审核</span></article>
      <article class="stat-card"><p class="eyebrow">签名方剂路线</p><strong>${(MINED.formula_signature_hits || []).length}</strong><span>≥70% 签名药物命中</span></article>
    </div>
    <div class="panel-grid">
      <section class="result-panel"><h3>证型分布</h3>${barRows(s.zheng_distribution, s.n_cases)}</section>
      <section class="result-panel"><h3>症状标签分布</h3>${barRows(s.symptom_tag_distribution, s.n_cases, cnTag, 12)}</section>
      <section class="result-panel"><h3>高频药物（按处方计）</h3><div class="chip-cloud">${Object.entries(MINED.herb_frequency_top || {}).slice(0, 28).map(([h, n]) => `<span class="chip">${h}<em>${n}</em></span>`).join('')}</div></section>
      <section class="result-panel"><h3>功效模块使用</h3>${barRows(MINED.herb_module_counts, s.n_with_prescription)}</section>
      <section class="result-panel"><h3>西医诊断 Top</h3>${barRows(s.western_dx_top, s.n_cases)}</section>
      <section class="result-panel"><h3>人群结构</h3>${barRows(s.age_band_distribution, s.n_cases, b => b === '未知' ? '未知' : b.replace('s', ' 岁段'))}<p class="muted">性别：${Object.entries(s.sex_distribution).map(([k, v]) => `${k} ${v}`).join(' / ')}</p></section>
    </div>
    <section class="result-panel warning"><h3>数据质量提示</h3><p>${(MINED.data_quality || {}).tongue_pulse_note || ''}</p><p class="muted">${MINED.meta ? MINED.meta.privacy : ''}</p></section>`;
}

function renderMiningModule() {
  pageTitle.textContent = '规则挖掘 · xlsx 医案候选规则（support / confidence / lift）';
  if (!MINED) { screen.innerHTML = noDataPanel(); return; }
  const rules = MINED.rule_candidates || [];
  const filter = state.miningFilter || 'all';
  const types = ['all', ...new Set(rules.map(r => r.rule_type))];
  const shown = rules.filter(r => filter === 'all' || r.rule_type === filter);
  screen.innerHTML = `
    <section class="result-panel">
      <div class="filter-row">${types.map(t => `<button class="option-pill ${t === filter ? 'selected' : ''}" data-filter="${t}">${t === 'all' ? '全部' : t}</button>`).join('')}</div>
      <table class="rule-table"><thead><tr><th>规则 ID</th><th>IF</th><th>THEN</th><th>support</th><th>confidence</th><th>lift</th><th>状态</th></tr></thead><tbody>
      ${shown.map(r => {
        const st = r.statistics || {};
        const cell = v => v === undefined ? '—' : v;
        return `<tr data-rule="${r.rule_id}"><td class="mono">${r.rule_id}</td>
          <td>${Object.entries(r.if || {}).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join('、') : cnTag(String(v))}`).join('<br/>')}</td>
          <td>${Object.entries(r.then || {}).map(([, v]) => Array.isArray(v) ? v.join('–') + ' 克' : v).join('<br/>')}</td>
          <td>${cell(st.support)}</td><td>${cell(st.confidence)}</td>
          <td>${st.lift !== undefined ? `<span class="lift-pill ${st.lift >= 1.5 ? 'hi' : ''}">${st.lift}</span>` : '—'}</td>
          <td><span class="badge-pending">待专家审核</span></td></tr>`;
      }).join('')}
      </tbody></table>
      <p class="muted">共 ${shown.length} 条；所有候选规则 clinician_only=true，仅供医师复核与科研教学，不构成诊断或处方依据，不向患者展示。</p>
    </section>`;
  screen.querySelectorAll('[data-filter]').forEach(btn => btn.addEventListener('click', () => { state.miningFilter = btn.dataset.filter; renderMiningModule(); }));
  screen.querySelectorAll('tr[data-rule]').forEach(tr => tr.addEventListener('click', () => { state.evidenceRule = tr.dataset.rule; state.module = 'evidence'; localStorage.setItem('yaobi-module', 'evidence'); render(); }));
}

function renderEvidenceModule() {
  pageTitle.textContent = '证据回溯 · 签名方剂命中与剂量分布';
  if (!MINED) { screen.innerHTML = noDataPanel(); return; }
  const rules = MINED.rule_candidates || [];
  const selected = rules.find(r => r.rule_id === state.evidenceRule) || rules[0];
  const hits = MINED.formula_signature_hits || [];
  screen.innerHTML = `
    <div class="panel-grid">
      <section class="result-panel"><h3>候选规则详情</h3>
        ${selected ? `<p class="mono">${selected.rule_id}</p>
          <p><strong>IF</strong> ${Object.entries(selected.if || {}).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join('、') : cnTag(String(v))}`).join('；')}</p>
          <p><strong>THEN</strong> ${JSON.stringify(selected.then, null, 0).replace(/[{}"]/g, '')}</p>
          <p><strong>统计</strong> ${Object.entries(selected.statistics || {}).map(([k, v]) => `${k}=${v}`).join('，')}</p>
          <p><strong>证据</strong> ${selected.evidence}</p>
          <p><span class="badge-pending">pending_expert_review</span> <span class="badge-pending">clinician_only</span> 强度：${selected.strength || '—'}</p>` : '<p>暂无规则。</p>'}
        <p class="muted">在「规则挖掘」页点击任意一行即可在此回溯证据。</p>
      </section>
      <section class="result-panel"><h3>签名方剂命中（证据行号已脱敏）</h3>
        ${hits.map(h => `<details><summary><strong>${h.formula}</strong> · ${h.n_cases} 例 · 主证型 ${Object.keys(h.by_zheng)[0] || '—'}</summary>
          <p>证型分布：${Object.entries(h.by_zheng).map(([z, n]) => `${z} ${n}`).join('；')}</p>
          <p class="mono">xlsx 行号：${(h.evidence_rows || []).join(', ')}</p></details>`).join('')}
      </section>
      <section class="result-panel"><h3>重点药物剂量分布（医师端研究用）</h3>
        <table class="rule-table"><thead><tr><th>药物</th><th>n</th><th>最小(g)</th><th>最大(g)</th><th>常用(g)</th><th>复核</th></tr></thead><tbody>
        ${Object.entries(MINED.dose_table || {}).map(([h, d]) => `<tr><td>${h}</td><td>${d.n}</td><td>${d.min_g}</td><td>${d.max_g}</td><td>${d.mode_g}</td><td><span class="badge-pending">医师必核</span></td></tr>`).join('')}
        </tbody></table>
        <p class="muted">剂量分布仅为医师端经验研究信号，不向患者输出，不构成可执行剂量。</p>
      </section>
    </div>`;
}

function renderReviewModule() {
  pageTitle.textContent = '医师审核 · Physician Review 签名闭环';
  const c = buildCase();
  screen.innerHTML = `
    <div class="panel-grid">
      <section class="result-panel"><h3>待审核队列</h3>
        <ul class="review-list">
          <li><strong>当前问诊医案草稿</strong><span>${c.chief || '未开始'}</span><span class="badge-pending">draft_for_clinician_review</span></li>
          <li><strong>挖掘候选规则</strong><span>${MINED ? (MINED.rule_candidates || []).length : 0} 条</span><span class="badge-pending">pending_expert_review</span></li>
        </ul></section>
      <section class="result-panel"><h3>签名边界（不可由模型代签）</h3>
        <ul>
          <li>最终诊断：仅 licensed physician 手工录入（source=physician_entered）。</li>
          <li>完整处方、剂量、煎服法、疗程：仅医师手工录入并签名。</li>
          <li>模型生成内容（source=model_generated）一律 rejected_model_generated_diagnosis。</li>
          <li>附片、细辛、全蝎、蜈蚣、麻黄等剂量必须医师逐项复核。</li>
        </ul>
        <div class="sign-form">
          <label>医师工号 <input class="free-note" placeholder="DOC-001" /></label>
          <label>执业证号 <input class="free-note" placeholder="LICENSE-001" /></label>
          <button class="primary-btn" type="button" id="signBtn">签名确认（演示）</button>
        </div></section>
    </div>`;
  document.querySelector('#signBtn').addEventListener('click', () => alert('演示环境：实际签名需在 physician_review_skill 后端流程中完成审计记录。'));
}

function renderSafetyModule() {
  pageTitle.textContent = '评估与安全 · 红旗门控 / Output Guard / 数据质量';
  const q = MINED ? MINED.data_quality : null;
  screen.innerHTML = `
    <div class="panel-grid">
      <section class="result-panel redflag"><h3>红旗硬门控（四类）</h3>
        <ul><li>马尾综合征：大小便障碍、会阴麻木 → 立即急诊。</li><li>肿瘤风险：肿瘤史、消瘦、夜间痛进行性加重。</li><li>感染风险：发热寒战、近期感染。</li><li>骨折风险：外伤、激素、重度骨质疏松骤发剧痛。</li></ul>
        <p>命中 urgent 立即终止问诊；红旗问题未答完时任何方式不能跳过（FSM 与 UI 双重拦截）。</p></section>
      <section class="result-panel"><h3>Output Guard 禁用输出</h3>
        <ul><li>最终诊断、完整处方、患者可执行剂量、煎服法 → 拦截并回退规则模板。</li><li>Tao 只能重排/改写/解释既有问题，新增问题 id 一律丢弃。</li><li>患者端请求诊断处方 → patient_request_guard 阻断。</li></ul></section>
      <section class="result-panel"><h3>测试与验收指标</h3>
        <ul><li>pytest 全量：41 项（含红旗拦截、禁用输出、挖掘脱敏、PII 泄漏检查）。</li><li>红旗召回测试：urgent 信号 100% 进入 S_EMERGENCY_NOTICE。</li><li>禁用输出拦截：模型代签诊断/处方用例全部 rejected。</li></ul></section>
      <section class="result-panel warning"><h3>数据质量与局限</h3>
        ${q ? `<ul><li>病例数 ${q.n_cases}，含处方 ${q.n_with_prescription}。</li><li>舌脉可用性：${q.tongue_pulse_usable ? '可用' : '不可用'}。${q.tongue_pulse_note}</li><li>缺证型 ${q.n_missing_zheng} 例。</li></ul>` : '<p>运行挖掘管道后展示。</p>'}
        <p>单中心、单医师、横断面数据；候选规则只是统计信号，须经专家审核才能升级为正式规则。</p></section>
    </div>`;
}

function renderSettingsModule() {
  pageTitle.textContent = '设置 · FSM 追问与 Tao 运行时';
  screen.innerHTML = `
    <div class="panel-grid">
      <section class="result-panel"><h3>有限状态机追问设置</h3>
        <label class="fsm-setting">每状态追问轮数上限（1–5）
          <select id="settingMaxRounds">${[1, 2, 3, 4, 5].map(n => `<option value="${n}" ${n === maxRounds() ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
        <label class="fsm-setting"><input type="checkbox" id="settingAutoAdvance" ${state.fsm.autoAdvance ? 'checked' : ''} />答完自动进入下一状态（自动终止追问）</label>
        <p class="muted">对应后端 CaseGuideSession(max_followups_per_state, questions_per_turn) 与 set_max_followups()。</p></section>
      <section class="result-panel"><h3>Tao Direct Runtime</h3>
        <pre class="runtime-code">TAO_BACKEND=transformers python -m backend.main --tao-chat "请解释本案规则线索" --stream</pre>
        <p>模型仅叠加问诊改写与教学解释；JSON Repair + Output Guard 失败即回退规则模板。</p></section>
      <section class="result-panel"><h3>本地数据</h3>
        <button class="ghost-btn" id="clearBtn" type="button">清空本地问诊数据</button>
        <p class="muted">病情数据仅保存在本次会话（sessionStorage），关闭标签页即清除；此按钮立即清空。</p></section>
    </div>`;
  document.querySelector('#settingMaxRounds').addEventListener('change', e => setMaxRounds(e.target.value));
  document.querySelector('#settingAutoAdvance').addEventListener('change', e => { state.fsm.autoAdvance = e.target.checked; save(); });
  document.querySelector('#clearBtn').addEventListener('click', () => {
    if (!confirm('确定清空本地问诊数据？')) return;
    sessionStorage.removeItem('yaobi-case'); sessionStorage.removeItem('yaobi-fsm');
    localStorage.removeItem('yaobi-case'); localStorage.removeItem('yaobi-fsm');
    location.reload();
  });
}

document.querySelector('#doctorModeBtn').addEventListener('click', () => { state.doctorMode = !state.doctorMode; alert(state.doctorMode ? '已进入医生/研究者模式' : '已进入患者简洁模式'); });
document.querySelector('#exportJsonBtn').addEventListener('click', () => download('yaobi-case.json', JSON.stringify(buildReport().json, null, 2)));
render(); updatePreview(); renderTaoBadge();

// Detect the backend on load; if the Tao server is reachable, the UI switches from the
// offline rule mirror to genuine language-model-driven skill calls and re-renders honestly.
(async function initBackend() {
  await api.health();
  renderTaoBadge();
  render();   // re-render the current module now that online/offline status is known
})();
