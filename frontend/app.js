// ─── Steps ──────────────────────────────────────────────────────────────────
const steps = [
  { id: 'start',       label: '开始与知情', title: '自动导引患者生成标准腰痹医案' },
  { id: 'redflag',     label: '红旗筛查',   title: '先排除需要立即线下评估的危险信号' },
  { id: 'basic',       label: '主诉病程',   title: '采集基础信息、主诉与病程' },
  { id: 'pain',        label: '疼痛特征',   title: '采集疼痛部位、放射、性质和诱因' },
  { id: 'neuro',       label: '神经骨科',   title: '采集麻木、无力、影像与既往诊断' },
  { id: 'tcm',         label: '中医四诊',   title: '用患者能理解的问题采集中医信息' },
  { id: 'comorbidity', label: '合并病用药', title: '采集合并病、NSAIDs、肌松药与过敏史' },
  { id: 'signals',     label: '规则线索',   title: '沈老经验规则线索与 CDSS 草案' },
  { id: 'final',       label: '最终医案',   title: '标准医案、医生复核清单与导出' },
];

// ─── Stage question max followups config (per stage override or global) ──────
const DEFAULT_MAX_FOLLOWUPS = 3;

const featureMatrix = [
  ['CaseGuide FSM', '有限状态机分阶段问诊，每状态追问次数可配置（默认 3 轮），每轮最多 3 问。'],
  ['Rule Engine', '结构化标签、候选证型、方剂路线、模块命中和冲突检查。'],
  ['Tao Model Mode', '模型增强模式叠加大模型问诊改写与教学解释；规则模式为纯确定性规则匹配。'],
  ['JSON Repair', '修复模型 JSON 围栏、尾逗号、单引号等常见格式错误。'],
  ['Output Guard', '拦截最终诊断、完整处方、患者可执行剂量和替代医生建议。'],
  ['CDSS Draft', '医生端候选诊断/候选证型/处方策略草案，非患者可见。'],
  ['Physician Review', '最终诊断、处方、剂量只能由 licensed physician 手工签名。'],
  ['Export', '标准医案 Markdown、规则 JSON、医生复核摘要导出。'],
];

// ─── Question banks ──────────────────────────────────────────────────────────
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
    { id: 'age',            q: '你的年龄是？', type: 'number', placeholder: '例如：68' },
    { id: 'sex',            q: '性别？', options: ['女', '男', '其他/不便说明'] },
    { id: 'main_symptom',   q: '你最主要的不舒服是什么？', options: ['腰痛', '腰腿痛', '腰酸', '腰痛伴腿麻'], note: true },
    { id: 'duration',       q: '这种情况持续多久了？', options: ['3天', '2周', '半年', '5年'], note: true },
    { id: 'acute_worsening',q: '这次加重多久了？', options: ['无明显加重', '3天', '2周', '1月'], note: true },
  ],
  pain: [
    { id: 'location',     q: '疼痛主要在哪个部位？', multi: true, options: ['腰正中', '一侧腰部', '双侧腰部', '腰骶部', '臀部', '大腿后侧', '小腿', '足部', '说不清'] },
    { id: 'radiation',    q: '疼痛会不会从腰部放射到臀部或下肢？', options: ['不会', '到臀部', '到大腿', '到小腿', '到足部', '不确定'] },
    { id: 'pain_nature',  q: '疼痛性质更像哪一种？', multi: true, options: ['酸痛', '胀痛', '刺痛', '冷痛', '灼痛', '隐痛', '掣痛/牵拉痛', '麻痛', '说不清'] },
    { id: 'severity',     q: '0 到 10 分，你觉得疼痛大约几分？', type: 'range' },
    { id: 'aggravating',  q: '什么情况下会加重？', multi: true, options: ['久坐', '久站', '弯腰', '劳累', '受凉', '阴雨天', '走路', '咳嗽打喷嚏', '夜间', '没有明显规律'] },
    { id: 'relieving',    q: '什么情况下会缓解？', multi: true, options: ['休息', '热敷', '活动后', '按摩', '卧床', '服止痛药', '没有明显缓解'] },
  ],
  neuro: [
    { id: 'numbness',          q: '是否有下肢麻木？', options: ['没有', '偶尔有', '经常有', '持续存在', '说不清'] },
    { id: 'numbness_location', q: '麻木主要在哪个部位？', multi: true, options: ['臀部', '大腿外侧', '大腿后侧', '小腿外侧', '小腿后侧', '足背', '足底', '脚趾', '双下肢', '说不清'] },
    { id: 'weakness',          q: '是否感觉腿无力、走路不稳、脚抬不起来？', options: ['没有', '轻微', '明显', '越来越重'] },
    { id: 'walking_limitation',q: '走一段路后是否腰腿痛或麻木加重，休息或弯腰后缓解？', options: ['是', '否', '不确定'] },
    { id: 'imaging',           q: '是否做过腰椎 X 线、CT、MRI 或骨密度检查？', multi: true, options: ['做过MRI', '做过CT', '做过X线', '做过骨密度', '没做过', '不记得'] },
    { id: 'western_diagnosis', q: '医生曾经告诉你有什么诊断？', multi: true, options: ['腰椎间盘突出', '腰椎管狭窄', '腰椎滑脱', '骨质疏松', '骨折/压缩性骨折', '腰肌劳损', '坐骨神经痛', '其他', '不清楚'] },
  ],
  tcm: [
    { id: 'cold_heat',     q: '你平时怕冷还是怕热？', options: ['怕冷', '怕热', '都不明显', '有时怕冷有时怕热'] },
    { id: 'cold_relation', q: '腰腿痛遇冷会不会加重？热敷会不会舒服？', options: ['遇冷加重，热敷舒服', '热敷不舒服', '没影响', '不确定'] },
    { id: 'dampness',      q: '身体是否容易困重、沉重，尤其阴雨天更明显？', options: ['明显', '轻微', '没有', '不确定'] },
    { id: 'sleep',         q: '睡眠怎么样？', multi: true, options: ['正常', '入睡困难', '容易醒', '多梦', '早醒', '疼痛影响睡眠', '睡不踏实'] },
    { id: 'appetite',      q: '胃口怎么样？', options: ['正常', '胃口差', '容易腹胀', '吃药容易胃不舒服', '恶心反酸'] },
    { id: 'mouth_taste',   q: '是否经常口苦、口干，或咽喉不清爽？', options: ['明显', '轻微', '没有'] },
    { id: 'tongue_color',  q: '舌头颜色更像偏淡、偏暗紫，还是偏红？', options: ['偏淡', '偏暗紫', '偏红', '说不清'] },
    { id: 'tongue_coating',q: '舌苔是否偏厚腻、白腻或黄腻？', options: ['薄白', '白腻', '黄腻', '厚腻', '说不清'] },
  ],
  comorbidity: [
    { id: 'diseases',      q: '是否有以下疾病？', multi: true, options: ['高血压', '糖尿病', '骨质疏松', '肾功能异常', '肝功能异常', '胃溃疡/胃炎', '心脏病', '肿瘤病史', '过敏史', '无', '不清楚'] },
    { id: 'medications',   q: '最近是否服用过止痛药或消炎药？', multi: true, options: ['塞来昔布', '布洛芬', '双氯芬酸', '艾瑞昔布', '依托考昔', '乙哌立松', '甲钴胺', '其他', '没有', '不清楚'] },
    { id: 'anticoagulant', q: '是否正在服用抗凝药、阿司匹林、激素或降糖药？', options: ['是', '否', '不确定'] },
    { id: 'allergy',       q: '是否有药物或中药过敏史？', options: ['有', '没有', '不确定'] },
  ],
};

// ─── Application State ───────────────────────────────────────────────────────
const state = {
  step: 0,
  doctorMode: true,
  modelMode: false,                 // false = rule-only, true = model-enhanced
  answers: JSON.parse(localStorage.getItem('yaobi-case') || '{}'),
  fsm: JSON.parse(localStorage.getItem('yaobi-fsm') || '{"rounds":{},"lastAnswers":{}}'),
  // Global max followups (configurable, 1–5)
  maxFollowups: Number(localStorage.getItem('yaobi-max-followups') || DEFAULT_MAX_FOLLOWUPS),
  // Per-stage max followups override (e.g. { redflag: 1, tcm: 4 })
  stageMaxFollowups: JSON.parse(localStorage.getItem('yaobi-stage-max') || '{}'),
};

const screen   = document.querySelector('#screen');
const pageTitle = document.querySelector('#pageTitle');

// ─── Persistence ─────────────────────────────────────────────────────────────
function save() {
  localStorage.setItem('yaobi-case', JSON.stringify(state.answers));
  localStorage.setItem('yaobi-fsm', JSON.stringify(state.fsm));
  localStorage.setItem('yaobi-max-followups', String(state.maxFollowups));
  localStorage.setItem('yaobi-stage-max', JSON.stringify(state.stageMaxFollowups));
  updatePreview();
}

// ─── Answers ─────────────────────────────────────────────────────────────────
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

// ─── FSM Rounds ──────────────────────────────────────────────────────────────
/** Effective max followups for a given stage */
function stageMax(stage) {
  return state.stageMaxFollowups[stage] ?? state.maxFollowups;
}

function stageRound(stage) {
  return state.fsm.rounds[stage] || 0;
}

function setStageRound(stage, value) {
  const max = stageMax(stage) - 1;
  state.fsm.rounds[stage] = Math.max(0, Math.min(max, value));
  save();
}

function questionAnswered(q) {
  const value = state.answers[q.id];
  return Array.isArray(value) ? value.length > 0 : value !== undefined && value !== '' && value !== null;
}

/** Whether all questions in this stage's pool are already answered */
function stagePoolExhausted(stage) {
  const list = questions[stage] || [];
  return list.length > 0 && list.every(q => questionAnswered(q));
}

/** Whether the current round is at or beyond the limit for this stage */
function stageLimitReached(stage) {
  return stageRound(stage) >= stageMax(stage) - 1;
}

/** Whether we should auto-advance (limit reached AND all shown questions answered) */
function shouldAutoAdvance(stage) {
  return stageLimitReached(stage) && stagePoolExhausted(stage);
}

// ─── Question Selection ───────────────────────────────────────────────────────
function questionReason(q, stage) {
  const tags = getTags();
  if (stage === 'redflag') return '红旗筛查优先；若命中危险信号，将立即停止后续普通问诊。';
  if (['cold_relation', 'cold_heat'].includes(q.id) && (tags.includes('elderly') || tags.includes('lower_limb_numbness')))
    return '结合上一轮高龄/麻木/久病线索，深化寒湿、温经散寒与通络规则变量。';
  if (['numbness', 'numbness_location', 'radiation'].includes(q.id))
    return '结合疼痛部位和上一轮回答，深化放射痛、麻木和神经根风险线索。';
  if (['imaging', 'western_diagnosis', 'diseases'].includes(q.id))
    return '结合当下规则命中，补足影像、骨质疏松和医生复核背景。';
  if (['sleep', 'appetite', 'mouth_taste'].includes(q.id))
    return '结合沈老规则中的口苦、睡眠、胃纳变量，深化少阳/顾护中焦线索。';
  if (['tongue_color', 'tongue_coating'].includes(q.id))
    return '舌象为沈老辨证高价值变量，用于寒湿/气血瘀滞/肝肾不足鉴别。';
  return '根据当前规则标签与上一轮答案，补齐本状态最有信息增益的字段。';
}

function modelEnhancedReason(q, stage) {
  const base = questionReason(q, stage);
  if (!state.modelMode) return base;
  // Model mode: append a model-enhanced explanation note
  const signals = getShenSignals();
  const active = signals.filter(s => s.active).map(s => s.label);
  if (active.length) return `【模型增强】${base} 当前激活信号：${active.join('、')}。`;
  return `【模型增强】${base}`;
}

function currentStageQuestions(stage) {
  const list = questions[stage] || [];
  const round = stageRound(stage);
  const unanswered = list.filter(q => !questionAnswered(q));
  const pool = unanswered.length ? unanswered : list;
  const selected = pool.slice(round * 3, round * 3 + 3);
  const result = (selected.length ? selected : pool.slice(-3));
  return result.map(q => ({ ...q, reason: modelEnhancedReason(q, stage) }));
}

// ─── Tags & Shen Signals ─────────────────────────────────────────────────────
function getTags() {
  const a = state.answers;
  const tags = [];
  if ((a.age || 0) >= 60) tags.push('elderly');
  if ((a.age || 0) >= 73) tags.push('very_elderly');
  if ((a.duration || '').includes('年')) tags.push('chronic_yabi', 'long_duration');
  if (['到小腿', '到足部'].includes(a.radiation)) tags.push('radiating_leg_pain');
  if (['偶尔有', '经常有', '持续存在'].includes(a.numbness) || (a.pain_nature || []).includes('麻痛')) tags.push('lower_limb_numbness');
  if ((a.aggravating || []).includes('受凉') || a.cold_relation === '遇冷加重，热敷舒服') tags.push('cold_aggravation');
  if ((a.relieving || []).includes('热敷') || a.cold_relation === '遇冷加重，热敷舒服') tags.push('warmth_relieves');
  if ((a.diseases || []).includes('骨质疏松') || (a.western_diagnosis || []).includes('骨质疏松')) tags.push('osteoporosis');
  if (a.tongue_color === '偏暗紫') tags.push('dark_tongue');
  if (a.tongue_coating === '白腻') tags.push('white_greasy_coating');
  if (a.tongue_coating === '黄腻') tags.push('yellow_greasy_coating');
  if (a.sleep && !['正常'].includes(a.sleep) && !(Array.isArray(a.sleep) && a.sleep.includes('正常'))) tags.push('insomnia');
  if (['胃口差', '容易腹胀', '吃药容易胃不舒服', '恶心反酸'].includes(a.appetite)) tags.push('poor_appetite');
  if (a.mouth_taste && a.mouth_taste !== '没有') tags.push('bitter_taste');
  if (a.cold_heat === '怕冷') tags.push('cold_constitution');
  if (a.dampness === '明显' || a.dampness === '轻微') tags.push('dampness');
  if (['做过MRI','做过CT','做过X线'].some(v => (a.imaging || []).includes(v))) tags.push('has_imaging');
  return [...new Set(tags)];
}

/**
 * Compute active 沈钦荣 rule signals from current answers/tags.
 * Returns array of { id, label, active, formula, reason }
 */
function getShenSignals() {
  const tags = new Set(getTags());
  const a = state.answers;

  const danggui_sini = tags.has('lower_limb_numbness') || tags.has('radiating_leg_pain');
  const cold_damp    = tags.has('cold_aggravation') && tags.has('warmth_relieves');
  const bushen_bone  = tags.has('osteoporosis') || tags.has('elderly');
  const chaihu       = tags.has('bitter_taste') && tags.has('insomnia');
  const stomach_prot = tags.has('poor_appetite');
  const qixue_bizhu  = tags.has('dark_tongue') && (tags.has('white_greasy_coating') || tags.has('dampness')) && tags.has('chronic_yabi');
  const young_acute  = (a.age || 99) <= 40;
  const very_elderly = tags.has('very_elderly');

  return [
    {
      id: 'danggui_sini',
      label: '当归四逆/通络',
      active: danggui_sini,
      formula: '当归四逆汤路线',
      reason: '下肢麻木/放射痛 → 温经通络，配合细辛、通草。',
    },
    {
      id: 'cold_damp',
      label: '寒湿痹阻',
      active: cold_damp,
      formula: '独活寄生汤加减',
      reason: '受凉加重+热敷舒服 → 寒湿痹阻，温经散寒为主。',
    },
    {
      id: 'bushen_bone',
      label: '肝肾不足/补骨',
      active: bushen_bone,
      formula: '独活寄生汤+补骨模块',
      reason: '高龄/骨质疏松 → 补肝肾强筋骨，杜仲、续断、牛膝。',
    },
    {
      id: 'chaihu',
      label: '少阳/柴胡信号',
      active: chaihu,
      formula: '柴胡类方叠加',
      reason: '口苦+失眠 → 少阳不和，柴胡、黄芩、合欢皮等。',
    },
    {
      id: 'stomach_prot',
      label: '顾护中焦',
      active: stomach_prot,
      formula: '健脾和胃底盘',
      reason: '胃纳差/恶心 → 先顾护中焦，砂仁、白术、茯苓。',
    },
    {
      id: 'qixue_bizhu',
      label: '气血痹阻/化瘀',
      active: qixue_bizhu,
      formula: '活血化瘀通络模块',
      reason: '暗紫舌+白腻苔+久病 → 气血痹阻夹湿，莪术、三棱、泽兰。',
    },
    {
      id: 'young_acute',
      label: '年轻/急性期',
      active: young_acute,
      formula: '祛风胜湿急性方',
      reason: '≤40岁 → 多实证/急性期，以祛风胜湿、理气止痛为主。',
    },
    {
      id: 'very_elderly',
      label: '高龄慎用峻药',
      active: very_elderly,
      formula: '扶正顾本路线',
      reason: '≥73岁 → 峻猛活血/附片类需慎用，扶正为先。',
    },
  ];
}

// ─── Red Flag ────────────────────────────────────────────────────────────────
function getRedFlagStatus() {
  const rf = questions.redflag;
  const positives = rf.filter(q => (q.urgent  || []).includes(state.answers[q.id])).map(q => q.q);
  const cautions  = rf.filter(q => (q.caution || []).includes(state.answers[q.id])).map(q => q.q);
  return {
    status: positives.length ? 'urgent' : cautions.length ? 'caution' : rf.every(q => state.answers[q.id]) ? 'safe' : 'unknown',
    positives,
    cautions,
  };
}

// ─── Case Building ────────────────────────────────────────────────────────────
function buildCase() {
  const a = state.answers;
  const tags = getTags();
  const red = getRedFlagStatus();
  const chief = `${a.duration && a.duration.includes('年') ? '反复' : ''}${a.main_symptom || '腰痛'}${a.duration || ''}${a.acute_worsening && a.acute_worsening !== '无明显加重' ? `，加重${a.acute_worsening}` : ''}${a.numbness && a.numbness !== '没有' ? '，伴下肢麻木' : ''}`;
  const modules = [];
  if (tags.includes('white_greasy_coating') || tags.includes('chronic_yabi')) modules.push('健脾化湿底盘');
  if (tags.includes('osteoporosis') || tags.includes('elderly'))              modules.push('补肝肾强筋骨模块');
  if (tags.includes('lower_limb_numbness'))                                   modules.push('当归四逆/通草细辛通络路线信号', '虫类搜络模块（需医师审核）');
  if (tags.includes('cold_aggravation'))                                      modules.push('温经散寒模块');
  if (tags.includes('insomnia') || tags.includes('bitter_taste'))             modules.push('少阳/安神除烦模块');
  if (tags.includes('poor_appetite'))                                         modules.push('顾护中焦/健脾和胃');
  if (tags.includes('qixue_bizhu') || tags.includes('dark_tongue'))           modules.push('活血化瘀通络模块');
  return { chief, tags, red, modules };
}

// ─── Rendering ────────────────────────────────────────────────────────────────
function renderStepper() {
  const el = document.querySelector('#stepper');
  el.innerHTML = steps.map((s, i) => `
    <button class="step ${i === state.step ? 'active' : ''} ${i < state.step ? 'done' : ''}" data-step="${i}" type="button">
      <span class="step-index">${i + 1}</span><span>${s.label}</span>
    </button>`).join('');
  el.querySelectorAll('button').forEach(btn =>
    btn.addEventListener('click', () => { state.step = Number(btn.dataset.step); render(); })
  );
}

function render() {
  renderStepper();
  pageTitle.textContent = steps[state.step].title;
  const id = steps[state.step].id;
  if (id === 'start')   return renderStart();
  if (id === 'signals') return renderSignals();
  if (id === 'final')   return renderFinal();
  renderQuestionStage(id);
}

// ─── Start Screen ─────────────────────────────────────────────────────────────
function renderStart() {
  screen.innerHTML = `
    <section class="hero">
      <div class="hero-grid">
        <div>
          <p class="eyebrow">名老中医腰痹诊疗经验研究助手 / CDSS 草案</p>
          <h2>把零散腰痛描述整理成可复核、可标注、可教学的标准医案</h2>
          <p>本工具以红旗筛查为安全底线，以中西医问诊为骨架，以沈钦荣腰痹经验规则为导引逻辑，输出标准化医案、规则线索、医生复核清单和医生端 CDSS 草案。</p>
          <div class="badges">
            <span class="badge">红旗优先</span>
            <span class="badge">每屏 1–3 问</span>
            <span class="badge">规则证据可追溯</span>
            <span class="badge">追问次数可配置</span>
            <span class="badge">医师签名闭环</span>
          </div>
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
}

// ─── Question Stage (FSM) ─────────────────────────────────────────────────────
function renderQuestionStage(stage) {
  // Guard: red flag block
  const urgent = getRedFlagStatus().status === 'urgent';
  if (stage !== 'redflag' && urgent) {
    screen.innerHTML = `
      <section class="result-panel redflag">
        <h3>已命中红旗信号</h3>
        <p>请先线下就医或急诊评估。本工具已停止后续中医问诊。</p>
        <button class="ghost-btn" id="backRed">返回红旗筛查</button>
      </section>`;
    document.querySelector('#backRed').addEventListener('click', () => { state.step = 1; render(); });
    return;
  }

  const round      = stageRound(stage);
  const maxRounds  = stageMax(stage);
  const list       = currentStageQuestions(stage);
  const exhausted  = stagePoolExhausted(stage);
  const atLimit    = stageLimitReached(stage);
  const canDeepen  = !atLimit && !exhausted;

  // Determine FSM strip styling
  const stripClass  = atLimit || exhausted ? 'fsm-strip fsm-strip--done' : 'fsm-strip';
  const roundLabel  = `第 ${round + 1}/${maxRounds} 轮`;
  const statusNote  = exhausted
    ? '本状态所有问题已回答完毕，可进入下一阶段。'
    : atLimit
      ? `已达本状态追问上限（${maxRounds} 轮），可继续或手动终止。`
      : `每轮最多 3 问；问题会叠加当前规则标签、上一轮答案${state.modelMode ? ' 与 Tao 大模型深化理由' : ''}。`;

  screen.innerHTML = `
    <div class="${stripClass}">
      <div class="fsm-strip-left">
        <strong>有限状态机追问</strong>
        <span class="fsm-round-badge">${roundLabel}</span>
        ${state.modelMode ? '<span class="model-badge">模型增强</span>' : '<span class="rule-badge">规则模式</span>'}
      </div>
      <span>${statusNote}</span>
      <div class="fsm-strip-actions">
        ${canDeepen
          ? `<button class="ghost-btn" id="deepenBtn">深化追问 (${maxRounds - round - 1} 轮剩余)</button>`
          : exhausted || atLimit
            ? `<button class="danger-btn" id="endStateBtn">终止追问 →</button>`
            : ''}
        ${!exhausted && !atLimit
          ? `<button class="ghost-btn" id="endStateBtn">手动终止本状态</button>`
          : ''}
      </div>
    </div>

    ${(exhausted || atLimit) ? `
    <div class="auto-advance-banner">
      <span>${exhausted ? '✓ 本状态问题已全部采集完毕' : `✓ 已完成 ${maxRounds} 轮追问`}，点击右下角"进入下一阶段"或点击"终止追问 →"自动跳转。</span>
    </div>` : ''}

    <div class="card-grid"></div>
    <div class="footer-actions">
      <button class="ghost-btn" id="prevBtn">上一步</button>
      <button class="primary-btn" id="nextBtn">${exhausted || atLimit ? '进入下一阶段 →' : '进入下一个状态'}</button>
    </div>`;

  const grid = screen.querySelector('.card-grid');
  list.forEach(q => grid.appendChild(renderQuestion(q, stage)));

  document.querySelector('#prevBtn').addEventListener('click', () => { state.step = Math.max(0, state.step - 1); render(); });

  document.querySelector('#nextBtn').addEventListener('click', () => {
    state.fsm.lastAnswers[stage] = { ...state.answers };
    state.step = Math.min(steps.length - 1, state.step + 1);
    render();
  });

  const endBtn = document.querySelector('#endStateBtn');
  if (endBtn) {
    endBtn.addEventListener('click', () => {
      state.fsm.lastAnswers[stage] = { ...state.answers };
      state.step = Math.min(steps.length - 1, state.step + 1);
      render();
    });
  }

  const deepenBtn = document.querySelector('#deepenBtn');
  if (deepenBtn) {
    deepenBtn.addEventListener('click', () => {
      state.fsm.lastAnswers[stage] = { ...state.answers };
      setStageRound(stage, round + 1);
      render();
    });
  }
}

// ─── Question Card ────────────────────────────────────────────────────────────
function renderQuestion(q, stage) {
  const tpl  = document.querySelector('#questionTemplate').content.cloneNode(true);
  const card = tpl.querySelector('.question-card');
  card.classList.toggle('redflag', stage === 'redflag');
  tpl.querySelector('.question-meta').textContent = `${stage.toUpperCase()} · ${q.id} · ${q.reason || '规则深化追问'}`;
  tpl.querySelector('h3').textContent = q.q;
  const options = tpl.querySelector('.options');
  const current = answerValue(q.id);

  if (q.type === 'number') {
    options.innerHTML = `<input class="free-note" type="number" placeholder="${q.placeholder || ''}" value="${current || ''}" />`;
    options.querySelector('input').addEventListener('input', e => setAnswer(q.id, Number(e.target.value)));
  } else if (q.type === 'range') {
    options.innerHTML = `<input type="range" min="0" max="10" value="${current ?? 5}" /><strong>${current ?? 5} 分</strong>`;
    const range = options.querySelector('input');
    const label = options.querySelector('strong');
    range.addEventListener('input', e => { label.textContent = `${e.target.value} 分`; setAnswer(q.id, Number(e.target.value)); });
  } else {
    q.options.forEach(opt => {
      const selected = q.multi
        ? Array.isArray(current) && current.includes(opt)
        : current === opt;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `option-pill ${selected ? 'selected' : ''}`;
      btn.textContent = opt;
      btn.addEventListener('click', () => {
        setAnswer(q.id, opt, q.multi);
        renderQuestionStage(steps[state.step].id);
      });
      options.appendChild(btn);
    });
  }

  const note = tpl.querySelector('.free-note');
  if (q.type === 'number') {
    // already handled above; the free-note textarea is not needed
    const textarea = card.querySelector('textarea.free-note');
    if (textarea) textarea.remove();
  } else {
    note.value = answerValue(`${q.id}_note`) || '';
    note.addEventListener('input', e => setAnswer(`${q.id}_note`, e.target.value));
  }
  return card;
}

// ─── Signals Screen ───────────────────────────────────────────────────────────
function renderSignals() {
  const c = buildCase();
  const signals = getShenSignals().filter(s => s.active);
  screen.innerHTML = `
    <section class="result-panel success">
      <h3>沈老经验规则信号（实时命中）</h3>
      ${signals.length
        ? `<div class="signal-grid">${signals.map(s => `
            <div class="signal-card">
              <span class="signal-label">${s.label}</span>
              <span class="signal-formula">${s.formula}</span>
              <p class="signal-reason">${s.reason}</p>
            </div>`).join('')}
           </div>`
        : '<p>暂无信号命中，请先完成前面各阶段采集。</p>'}
    </section>
    <section class="result-panel">
      <h3>规则引擎标签</h3>
      <div class="tag-cloud">${c.tags.map(t => `<span class="tag-pill">${t} — ${tagLabel(t)}</span>`).join('') || '<span>待补充标签</span>'}</div>
    </section>
    <section class="result-panel">
      <h3>${state.modelMode ? 'Tao 模型增强模式' : '规则模式'}：候选方剂路线信号</h3>
      <p class="eyebrow">${state.modelMode ? 'Model-enhanced · JSON Repair · Output Guard' : 'Rule-only · deterministic · evidence-traceable'}</p>
      <ul>${c.modules.map(m => `<li>${m}</li>`).join('') || '<li>待补充信息</li>'}</ul>
      ${state.modelMode
        ? `<p>后端可使用 <code>TAO_BACKEND=transformers</code> 加载 <code>CMLM/Dao1-30b-a3b</code>，模型叠加教学解释和问诊改写，若输出诊断/处方/剂量则回退规则模板。</p>`
        : `<p>当前为纯规则模式（规则优先、证据可追溯），可在顶栏切换为模型增强模式。</p>`}
    </section>
    <section class="result-panel">
      <h3>医生/CDSS 模式：候选诊断与处方策略草案</h3>
      <p class="eyebrow">draft_for_clinician_review · 非最终医嘱 · 非患者可见 · 无患者可执行剂量</p>
      <ul>
        <li>西医候选：${c.tags.includes('radiating_leg_pain') || c.tags.includes('lower_limb_numbness') ? '腰椎间盘突出/神经根受压相关腰腿痛（待查体影像复核）' : '非特异性腰痛等待鉴别'}</li>
        <li>中医候选：${c.tags.includes('osteoporosis') ? '肝肾不足背景' : '气血痹阻夹湿候选'}；${c.tags.includes('cold_aggravation') ? '寒湿/寒凝经脉线索' : '寒热信息待补'}</li>
        <li>方剂路线信号：${c.modules.join('、') || '待补充信息'}</li>
        <li>安全复核：高风险药物、NSAIDs、抗凝/激素/降糖药、肝肾功能、胃肠风险。</li>
      </ul>
    </section>
    <div class="footer-actions">
      <button class="ghost-btn" id="prevBtn">上一步</button>
      <button class="primary-btn" id="nextBtn">生成最终医案</button>
    </div>`;
  document.querySelector('#prevBtn').addEventListener('click', () => { state.step--; render(); });
  document.querySelector('#nextBtn').addEventListener('click', () => { state.step++; render(); });
}

// ─── Final Report ─────────────────────────────────────────────────────────────
function renderFinal() {
  const report = buildReport();
  const tabs = {
    case:      report.markdown,
    handoff:   report.handoff,
    tao:       report.tao,
    cdss:      report.cdss,
    physician: report.physician,
    json:      JSON.stringify(report.json, null, 2),
  };
  screen.innerHTML = `
    <section class="result-panel">
      <div class="report-tabs">
        <button class="tab-btn active" data-tab="case">标准医案</button>
        <button class="tab-btn" data-tab="handoff">医生复核</button>
        <button class="tab-btn" data-tab="tao">Tao 教学解释</button>
        <button class="tab-btn" data-tab="cdss">CDSS 草案</button>
        <button class="tab-btn" data-tab="physician">医师签名</button>
        <button class="tab-btn" data-tab="json">规则 JSON</button>
      </div>
      <pre class="report-box" id="reportBox"></pre>
      <div class="footer-actions">
        <button class="ghost-btn" id="copyBtn">复制当前内容</button>
        <button class="primary-btn" id="downloadBtn">下载 Markdown</button>
      </div>
    </section>`;
  const box    = document.querySelector('#reportBox');
  const setTab = key => {
    box.textContent = tabs[key];
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === key));
  };
  setTab('case');
  document.querySelectorAll('.tab-btn').forEach(btn => btn.addEventListener('click', () => setTab(btn.dataset.tab)));
  document.querySelector('#copyBtn').addEventListener('click', () => navigator.clipboard?.writeText(box.textContent));
  document.querySelector('#downloadBtn').addEventListener('click', () => download('yaobi-case.md', box.textContent));
}

// ─── Report Builder ───────────────────────────────────────────────────────────
function buildReport() {
  const a = state.answers;
  const c = buildCase();
  const signals = getShenSignals().filter(s => s.active);

  const md = `# 腰痹医案草稿

## 一、基本信息
患者：${a.sex || '未详'}，${a.age || '未详'}岁

## 二、主诉
${c.chief}

## 三、现病史
疼痛部位：${fmt(a.location)}；放射情况：${fmt(a.radiation)}；疼痛性质：${fmt(a.pain_nature)}；疼痛评分：${a.severity ?? '未详'}/10。加重因素：${fmt(a.aggravating)}；缓解因素：${fmt(a.relieving)}。下肢麻木：${fmt(a.numbness)}，部位：${fmt(a.numbness_location)}；下肢无力：${fmt(a.weakness)}。

## 四、伴随症状
寒热：${fmt(a.cold_heat)}；寒热与疼痛关系：${fmt(a.cold_relation)}；睡眠：${fmt(a.sleep)}；胃纳：${fmt(a.appetite)}；口苦口干：${fmt(a.mouth_taste)}。

## 五、既往史与检查
既往疾病：${fmt(a.diseases)}；影像/检查：${fmt(a.imaging)}；既往诊断：${fmt(a.western_diagnosis)}；近期用药：${fmt(a.medications)}；过敏史：${fmt(a.allergy)}。

## 六、中医四诊信息
舌色：${fmt(a.tongue_color)}；舌苔：${fmt(a.tongue_coating)}；脉象：待医生面诊补充。

## 七、结构化标签
${c.tags.map(t => `- ${t}`).join('\n') || '- 暂无'}

## 八、沈老经验规则信号
${signals.length ? signals.map((s, i) => `${i + 1}. **${s.label}** → ${s.formula}：${s.reason}`).join('\n') : '1. 信息不足，待补充。'}

## 九、方剂路线信号
${c.modules.map((m, i) => `${i + 1}. ${m}`).join('\n') || '1. 信息不足，待补充。'}

## 十、医生复核清单
- 红旗筛查：${c.red.status}
- 下肢肌力、感觉、反射查体
- 影像/骨密度报告
- NSAIDs、抗凝药、肝肾功能和胃肠风险
- 高风险药物需医师审核

> 本报告为医案整理和医生端 CDSS 草案，不构成最终诊断、签名处方或患者可执行剂量。`;

  const handoff = `# 医生复核摘要

- 主要问题：${c.chief}。
- 规则标签：${c.tags.join('、') || '待补充'}。
- 沈老信号：${signals.map(s => s.label).join('、') || '待采集'}。
- 方剂路线信号：${c.modules.join('、') || '待补充'}。
- 信息缺口：脉象、影像原文、下肢肌力/感觉查体、用药剂量与不良反应。`;

  const tao = `# Tao 教学解释叠加

运行方式：TAO_BACKEND=transformers python -m backend.main --tao-chat "请解释本案规则线索" --stream

Tao Direct Runtime 负责把规则证据转写为教学解释；输出需通过 JSON Repair 和 Output Guard，不允许最终诊断、完整处方或患者可执行剂量。

当前模式：${state.modelMode ? '模型增强模式（Tao overlay 已启用）' : '规则模式（Tao overlay 未启用）'}`;

  const cdss = `# CDSS 草案

状态：draft_for_clinician_review；patient_visible=false；complete_prescription_generated=false；patient_executable_dose_generated=false。

候选方向：${c.tags.includes('lower_limb_numbness') ? '腰腿痛/神经根相关风险待复核' : '腰痛待鉴别'}；候选证型：${c.tags.includes('osteoporosis') ? '肝肾不足背景' : '气血痹阻夹湿候选'}。

沈老规则信号：${signals.map(s => `${s.label}（${s.formula}）`).join('；') || '待采集'}。`;

  const physician = `# 医师审核签名

最终诊断、完整处方、剂量、煎服法、疗程只能由 licensed physician 手工录入。系统输出仅为规则证据和草案，医生确认后方可锁定。`;

  return {
    markdown: md, handoff, tao, cdss, physician,
    json: {
      answers: state.answers,
      tags: c.tags,
      shen_signals: signals.map(s => ({ id: s.id, label: s.label, formula: s.formula })),
      modules: c.modules,
      red_flags: c.red,
      fsm_config: { maxFollowups: state.maxFollowups, stageMaxFollowups: state.stageMaxFollowups },
      mode: state.modelMode ? 'model_enhanced' : 'rule_only',
      runtime: 'Tao Direct Transformers Runtime',
      guards: ['JSON Repair', 'Output Guard'],
      cdss_status: 'draft_for_clinician_review',
    },
  };
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function fmt(v) { return Array.isArray(v) ? (v.join('、') || '未详') : (v || '未详'); }

function tagLabel(t) {
  const map = {
    elderly:              '年龄偏大，对退变/肝肾不足背景有价值',
    very_elderly:         '≥73岁高龄，峻药慎用',
    chronic_yabi:         '腰痛时间较久',
    long_duration:        '病程较长',
    lower_limb_numbness:  '有下肢麻木',
    radiating_leg_pain:   '有下肢放射痛',
    cold_aggravation:     '受凉加重',
    warmth_relieves:      '热敷缓解',
    osteoporosis:         '骨质疏松线索',
    dark_tongue:          '舌色偏暗紫',
    white_greasy_coating: '白腻苔',
    yellow_greasy_coating:'黄腻苔',
    insomnia:             '睡眠欠佳',
    poor_appetite:        '胃纳较差',
    bitter_taste:         '口苦/少阳线索',
    cold_constitution:    '怕冷体质',
    dampness:             '湿困',
    has_imaging:          '已有影像资料',
  };
  return map[t] || t;
}

function download(name, text) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }));
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

// ─── Live Preview (Right Sidebar) ─────────────────────────────────────────────
function updatePreview() {
  const c = buildCase();
  const signals = getShenSignals().filter(s => s.active);

  document.querySelector('#previewChief').textContent   = c.chief || '未采集';
  document.querySelector('#previewHistory').textContent = `疼痛：${fmt(state.answers.location)}；放射：${fmt(state.answers.radiation)}；麻木：${fmt(state.answers.numbness)}。`;
  document.querySelector('#previewTags').textContent    = c.tags.join('、') || '暂无';

  // Shen signals in sidebar
  const signalEl = document.querySelector('#previewSignals');
  if (signalEl) {
    signalEl.textContent = signals.length
      ? signals.map(s => `${s.label}→${s.formula}`).join('\n')
      : '暂无信号命中';
  }

  // Missing fields
  const missing = ['age','sex','duration','location','radiation','numbness','cold_relation','sleep','appetite','imaging','diseases','tongue_color']
    .filter(k => !state.answers[k]);
  document.querySelector('#previewMissing').textContent = missing.join('、') || '关键字段已较完整';

  // Quality ring
  const score = Math.max(0, Math.round((12 - missing.length) / 12 * 100));
  document.querySelector('#qualityRing').textContent    = `${score}%`;
  document.querySelector('#qualityRing').style.background = `conic-gradient(var(--green) ${score * 3.6}deg, #eadfce 0deg)`;
  document.querySelector('#qualityGrade').textContent   = score >= 80 ? 'good / 可复核' : score >= 60 ? 'fair / 需补问' : 'needs_more_info';
  document.querySelector('#qualityHint').textContent    = score >= 80 ? '已可生成医生复核摘要。' : '建议继续补充关键字段。';

  // Update FSM config display
  const fsmConfigEl = document.querySelector('#fsmConfigDisplay');
  if (fsmConfigEl) {
    fsmConfigEl.textContent = `全局追问上限：${state.maxFollowups} 轮 · ${state.modelMode ? '模型增强' : '规则'}模式`;
  }
}

// ─── Settings Panel ───────────────────────────────────────────────────────────
function renderSettings() {
  const stageLabels = {
    redflag: '红旗筛查', basic: '主诉病程', pain: '疼痛特征',
    neuro: '神经骨科', tcm: '中医四诊', comorbidity: '合并病用药',
  };

  const overlay = document.createElement('div');
  overlay.className = 'settings-overlay';
  overlay.innerHTML = `
    <div class="settings-panel">
      <div class="settings-header">
        <h3>问诊配置</h3>
        <button class="ghost-btn" id="closeSettings">✕ 关闭</button>
      </div>

      <div class="settings-section">
        <label class="settings-label">全局默认追问轮数（1–5 轮）</label>
        <div class="settings-row">
          <input type="range" id="globalMaxInput" min="1" max="5" value="${state.maxFollowups}" />
          <span id="globalMaxVal" class="settings-val">${state.maxFollowups} 轮</span>
        </div>
        <p class="settings-hint">每个状态在此轮数内最多追问，每轮最多 3 问；可被各状态单独覆盖。</p>
      </div>

      <div class="settings-section">
        <label class="settings-label">各状态单独追问上限（留空 = 使用全局值）</label>
        ${Object.entries(stageLabels).map(([key, label]) => `
          <div class="settings-row">
            <span class="settings-stage-label">${label}</span>
            <input type="number" class="stage-max-input" data-stage="${key}"
              min="1" max="5" value="${state.stageMaxFollowups[key] || ''}"
              placeholder="${state.maxFollowups}" />
          </div>`).join('')}
      </div>

      <div class="settings-section">
        <label class="settings-label">问诊模式</label>
        <div class="settings-row mode-toggle-row">
          <button class="mode-btn ${!state.modelMode ? 'mode-btn--active' : ''}" id="ruleBtn">规则模式（确定性）</button>
          <button class="mode-btn ${state.modelMode ? 'mode-btn--active' : ''}" id="modelBtn">模型增强模式（Tao）</button>
        </div>
        <p class="settings-hint">
          规则模式：纯确定性规则匹配，证据完全可追溯，零 LLM 调用。<br/>
          模型增强：Tao Direct Runtime 叠加问题改写与教学解释；仍受 Output Guard 保护。
        </p>
      </div>

      <div class="settings-section">
        <button class="danger-btn" id="resetAllBtn">清除所有已采集答案（重新开始）</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  // Global max slider
  const globalInput = overlay.querySelector('#globalMaxInput');
  const globalVal   = overlay.querySelector('#globalMaxVal');
  globalInput.addEventListener('input', () => {
    state.maxFollowups = Number(globalInput.value);
    globalVal.textContent = `${state.maxFollowups} 轮`;
    save();
  });

  // Per-stage inputs
  overlay.querySelectorAll('.stage-max-input').forEach(inp => {
    inp.addEventListener('input', () => {
      const val = inp.value.trim();
      if (val === '' || isNaN(val)) {
        delete state.stageMaxFollowups[inp.dataset.stage];
      } else {
        state.stageMaxFollowups[inp.dataset.stage] = Math.max(1, Math.min(5, Number(val)));
      }
      save();
    });
  });

  // Mode buttons
  overlay.querySelector('#ruleBtn').addEventListener('click', () => {
    state.modelMode = false;
    overlay.querySelector('#ruleBtn').classList.add('mode-btn--active');
    overlay.querySelector('#modelBtn').classList.remove('mode-btn--active');
    save();
  });
  overlay.querySelector('#modelBtn').addEventListener('click', () => {
    state.modelMode = true;
    overlay.querySelector('#modelBtn').classList.add('mode-btn--active');
    overlay.querySelector('#ruleBtn').classList.remove('mode-btn--active');
    save();
  });

  // Reset
  overlay.querySelector('#resetAllBtn').addEventListener('click', () => {
    if (confirm('确认清除所有已采集答案？此操作不可恢复。')) {
      state.answers = {};
      state.fsm     = { rounds: {}, lastAnswers: {} };
      save();
      overlay.remove();
      state.step = 0;
      render();
    }
  });

  overlay.querySelector('#closeSettings').addEventListener('click', () => {
    overlay.remove();
    render(); // re-render to reflect any mode changes
  });
}

// ─── Top-bar Event Listeners ──────────────────────────────────────────────────
document.querySelector('#doctorModeBtn').addEventListener('click', () => {
  state.doctorMode = !state.doctorMode;
  document.querySelector('#doctorModeBtn').textContent = state.doctorMode ? '医生/研究者模式' : '患者简洁模式';
  render();
});

document.querySelector('#exportJsonBtn').addEventListener('click', () =>
  download('yaobi-case.json', JSON.stringify(buildReport().json, null, 2))
);

// Settings button
const settingsBtn = document.querySelector('#settingsBtn');
if (settingsBtn) {
  settingsBtn.addEventListener('click', renderSettings);
}

// ─── Boot ─────────────────────────────────────────────────────────────────────
render();
updatePreview();
