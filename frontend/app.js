/* ═══════════════════════════════════════════════════════════════════════
   YaoBi-CaseGuide Frontend v2
   沈钦荣腰痹经验 CDSS — 基于209例医案数据驱动规则
   ═══════════════════════════════════════════════════════════════════════ */

// ── Step definitions ──────────────────────────────────────────────────────────
const STEPS = [
  { id: 'start',       label: '开始与知情', title: '自动导引患者生成标准腰痹医案' },
  { id: 'redflag',     label: '红旗筛查',   title: '排除需要立即线下评估的危险信号' },
  { id: 'basic',       label: '主诉病程',   title: '采集基础信息、主诉与病程' },
  { id: 'pain',        label: '疼痛特征',   title: '采集疼痛部位、性质、诱因与缓解因素' },
  { id: 'neuro',       label: '神经骨科',   title: '采集麻木、无力、影像与既往诊断' },
  { id: 'tcm',         label: '中医四诊',   title: '采集寒热、湿、舌象、脉象等中医信息' },
  { id: 'comorbidity', label: '合并病用药', title: '采集合并症、用药史与过敏史' },
  { id: 'signals',     label: '规则线索',   title: '沈老经验规则信号与 CDSS 草案' },
  { id: 'final',       label: '最终医案',   title: '标准医案 · 医生复核清单 · 导出' },
];

// ── Question banks (based on 209-case data) ───────────────────────────────────
const QS = {
  redflag: [
    { id:'RF001', q:'腰痛是否由跌倒、车祸、重物砸伤等明显外伤后出现？', opts:['是','否','不确定'], urgent:['是'] },
    { id:'RF002', q:'是否出现大小便控制困难、会阴区麻木，或突然排尿困难？', opts:['是','否','不确定'], urgent:['是'] },
    { id:'RF003', q:'是否出现一侧或双侧下肢明显无力、走路拖脚、进行性加重？', opts:['是','否','不确定'], urgent:['是'] },
    { id:'RF004', q:'是否伴有发热、寒战，或近期有感染史？', opts:['是','否','不确定'], caution:['是','不确定'] },
    { id:'RF005', q:'是否有肿瘤病史、原因不明体重下降、夜间痛明显加重？', opts:['是','否','不确定'], caution:['是','不确定'] },
    { id:'RF006', q:'是否长期使用激素，或已知严重骨质疏松，并突然出现剧烈腰背痛？', opts:['是','否','不确定'], caution:['是','不确定'] },
  ],
  basic: [
    { id:'age',            q:'你的年龄是？', type:'number', placeholder:'例如：68' },
    { id:'sex',            q:'性别？', opts:['女','男','其他/不便说明'] },
    { id:'occupation',     q:'职业或日常劳动情况？', opts:['体力劳动','久坐办公','退休/无业','其他'], note:true },
    { id:'main_symptom',   q:'你最主要的不舒服是什么？', opts:['腰痛','腰腿痛','腰酸','腰痛伴腿麻','腰背部疼痛'], note:true },
    { id:'duration',       q:'这种情况持续多久了？', opts:['1周内','2-4周','1-6个月','半年至1年','1-5年','5年以上'], note:true },
    { id:'acute_worsening',q:'这次加重多久了？', opts:['无明显加重','3天内','1-2周','1月'], note:true },
  ],
  pain: [
    { id:'location',     q:'疼痛主要在哪个部位？', multi:true, opts:['腰正中','一侧腰部','双侧腰部','腰骶部','臀部','大腿后侧','小腿','足部','说不清'] },
    { id:'radiation',    q:'疼痛会不会从腰部放射到臀部或下肢？', opts:['不会','到臀部','到大腿','到小腿','到足部','不确定'] },
    { id:'pain_nature',  q:'疼痛性质更像哪一种？', multi:true, opts:['酸痛','胀痛','刺痛','冷痛','灼痛','隐痛','掣痛/牵拉痛','麻痛','说不清'] },
    { id:'severity',     q:'0 到 10 分，你觉得疼痛大约几分？', type:'range' },
    { id:'aggravating',  q:'什么情况下会加重？', multi:true, opts:['久坐','久站','弯腰','劳累','受凉','阴雨天','走路','咳嗽打喷嚏','夜间','没有明显规律'] },
    { id:'relieving',    q:'什么情况下会缓解？', multi:true, opts:['休息','热敷','活动后','按摩','卧床','服止痛药','没有明显缓解'] },
  ],
  neuro: [
    { id:'numbness',          q:'是否有下肢麻木？', opts:['没有','偶尔有','经常有','持续存在','说不清'] },
    { id:'numbness_location', q:'麻木主要在哪个部位？', multi:true, opts:['臀部','大腿外侧','大腿后侧','小腿外侧','小腿后侧','足背','足底','脚趾','双下肢','说不清'] },
    { id:'weakness',          q:'是否感觉腿无力、走路不稳、脚抬不起来？', opts:['没有','轻微','明显','越来越重'] },
    { id:'walking_limit',     q:'走一段路后腰腿痛或麻木是否加重，休息或弯腰后缓解？', opts:['是','否','不确定'] },
    { id:'imaging',           q:'是否做过腰椎 X 线、CT、MRI 或骨密度检查？', multi:true, opts:['做过MRI','做过CT','做过X线','做过骨密度','没做过','不记得'] },
    { id:'western_diag',      q:'医生曾经告诉你有什么诊断？', multi:true, opts:['腰椎间盘突出','腰椎管狭窄','腰椎滑脱','骨质疏松','骨折/压缩性骨折','腰肌劳损','坐骨神经痛','其他','不清楚'] },
  ],
  tcm: [
    { id:'cold_heat',     q:'你平时怕冷还是怕热？', opts:['怕冷','怕热','都不明显','有时怕冷有时怕热'] },
    { id:'cold_relation', q:'腰腿痛遇冷会不会加重？热敷会不会舒服？', opts:['遇冷加重，热敷舒服','热敷不舒服','没影响','不确定'] },
    { id:'dampness',      q:'身体是否容易困重、沉重，尤其阴雨天更明显？', opts:['明显','轻微','没有','不确定'] },
    { id:'sleep',         q:'睡眠怎么样？', multi:true, opts:['正常','入睡困难','容易醒','多梦','早醒','疼痛影响睡眠','睡不踏实'] },
    { id:'appetite',      q:'胃口怎么样？', opts:['正常','胃口差','容易腹胀','吃药容易胃不舒服','恶心反酸'] },
    { id:'mouth_taste',   q:'是否经常口苦、口干，或咽喉不清爽？', opts:['明显','轻微','没有'] },
    { id:'tongue_color',  q:'舌头颜色更像偏淡、偏暗紫，还是偏红？', opts:['偏淡','偏暗紫','偏红','说不清'] },
    { id:'tongue_coat',   q:'舌苔是否偏厚腻、白腻或黄腻？', opts:['薄白','白腻','黄腻','厚腻','说不清'] },
  ],
  comorbidity: [
    { id:'diseases',      q:'是否有以下疾病？', multi:true, opts:['高血压','糖尿病','骨质疏松','肾功能异常','肝功能异常','胃溃疡/胃炎','心脏病','肿瘤病史','无','不清楚'] },
    { id:'medications',   q:'最近是否服用过止痛药或消炎药？', multi:true, opts:['塞来昔布','布洛芬','双氯芬酸','艾瑞昔布','依托考昔','乙哌立松','甲钴胺','其他','没有','不清楚'] },
    { id:'anticoag',      q:'是否正在服用抗凝药、阿司匹林、激素或降糖药？', opts:['是','否','不确定'] },
    { id:'allergy',       q:'是否有药物或中药过敏史？', opts:['有','没有','不确定'] },
  ],
};

// ── Shen rule signal definitions (data-driven from 209 cases) ─────────────────
const SHEN_SIGNALS = [
  {
    id: 'danggui_sini',
    chip: '当归四逆',
    label: '当归四逆汤路线',
    formula: '当归四逆汤（47例）',
    herbs: ['当归10g', '桂枝10g', '细辛3g', '通草10g', '白芍15g', '大枣10g', '炙甘草15g'],
    reason: '下肢麻木+放射痛：当归58%，桂枝51%使用率。血虚寒凝，温经通络。',
    check: (a, tags) => tags.has('lower_limb_numbness') || tags.has('radiating_leg_pain'),
  },
  {
    id: 'cold_damp',
    chip: '寒湿痹阻',
    label: '桂枝芍药知母汤路线',
    formula: '桂枝芍药知母汤（30例）',
    herbs: ['桂枝10g', '白芍20g', '知母20g', '附片10g先煎', '麻黄10g', '防风10g', '白术30g'],
    reason: '受凉加重+热敷舒服：寒湿痹阻证，温经散寒为主。附片先煎必须。',
    check: (a, tags) => tags.has('cold_aggravation') && tags.has('warmth_relieves'),
  },
  {
    id: 'ganshen',
    chip: '肝肾不足',
    label: '独活寄生汤路线',
    formula: '独活寄生汤（34例）',
    herbs: ['独活15g', '桑寄生15-30g', '杜仲12g', '川牛膝15g', '熟地黄10g', '当归10g', '白术30g', '茯苓30g'],
    reason: '高龄+骨质疏松：杜仲59%，熟地48%使用率。补肝肾强筋骨为本。',
    check: (a, tags) => tags.has('osteoporosis') || tags.has('elderly') || tags.has('very_elderly'),
  },
  {
    id: 'chaihu',
    chip: '少阳柴胡',
    label: '柴胡类方路线',
    formula: '柴胡类方（30例）',
    herbs: ['北柴胡5-10g', '黄芩6g', '党参15g', '姜半夏6g', '炒芥子10g', '当归10g', '桂枝10g'],
    reason: '口苦+失眠：年轻患者柴胡47%，炒芥子41%使用率。和解少阳，化痰散结。',
    check: (a, tags) => tags.has('bitter_taste') && tags.has('insomnia'),
  },
  {
    id: 'wendan',
    chip: '温胆痰热',
    label: '温胆汤路线',
    formula: '温胆汤加减',
    herbs: ['竹茹10g', '栀子10g', '炒芥子10g', '茯苓30g', '陈皮6g', '姜半夏6g'],
    reason: '竹茹/栀子/炒芥子在口苦+失眠+苔腻时使用率均达100%，规律极强。',
    check: (a, tags) => tags.has('bitter_taste') && tags.has('insomnia') && (tags.has('white_greasy_coating') || tags.has('yellow_greasy_coating') || tags.has('dampness')),
  },
  {
    id: 'weiguo',
    chip: '顾护中焦',
    label: '顾护中焦模块',
    formula: '健脾和胃叠加',
    herbs: ['陈皮6g', '炒麦芽15g', '砂仁后下', '姜半夏6g', '炒芥子10g'],
    reason: '胃脘不适：独活79%，党参50%，姜半夏50%使用率。防药伤胃，顾中焦。',
    check: (a, tags) => tags.has('poor_appetite') || a.appetite === '恶心反酸' || a.appetite === '吃药容易胃不舒服',
  },
  {
    id: 'qixue',
    chip: '气血痹阻',
    label: '独活寄生+当归四逆合方',
    formula: '气血痹阻证（66.5%）',
    herbs: ['当归10g', '白芍20g', '白术30g', '茯苓30g', '独活15g', '细辛3g', '丹参30g'],
    reason: '舌暗紫+久病+麻木：最常见证型（139/209=66.5%）。活血化瘀、通络止痛。',
    check: (a, tags) => tags.has('dark_tongue') || (tags.has('chronic_yabi') && tags.has('lower_limb_numbness')),
  },
  {
    id: 'gaonian',
    chip: '高龄扶正',
    label: '扶正顾本路线',
    formula: '补肾类方',
    herbs: ['熟地黄10g', '蒸萸肉10g', '茯苓30g', '杜仲12g', '陈皮6g', '当归10g', '黄芪30g'],
    reason: '≥73岁高龄：峻药慎用，扶正为先。当归74%，杜仲59%，陈皮52%使用率。',
    check: (a, tags) => tags.has('very_elderly'),
  },
];

// ── Feature matrix (for start screen) ────────────────────────────────────────
const FEATURES = [
  { icon: '🔬', name: '209例数据驱动',  desc: '沈钦荣医师209例实际医案统计，规则精度来自真实临床数据' },
  { icon: '🤖', name: 'CaseGuide FSM',   desc: '10状态有限状态机问诊，追问次数可配置（全局/各状态独立）' },
  { icon: '📐', name: '规则引擎',        desc: '10证型+8方剂路线+12药物模块，标签驱动，证据完全可追溯' },
  { icon: '⚡', name: 'Tao Direct',      desc: 'TAO_BACKEND=transformers 本地推理，不需要 FastAPI 包装层' },
  { icon: '🛡️', name: 'Output Guard',   desc: '拦截最终诊断、完整处方、患者可执行剂量，多层安全防护' },
  { icon: '🩺', name: 'CDSS 草案',       desc: '医生端候选诊断与处方策略草案，非患者可见，非最终医嘱' },
  { icon: '✍️', name: '医师审核签名',   desc: '最终诊断、处方、剂量只能由 licensed physician 手工签名锁定' },
  { icon: '📄', name: '多格式导出',      desc: '标准医案 Markdown、规则 JSON、医生复核摘要一键导出' },
];

// ── Application State ─────────────────────────────────────────────────────────
const state = {
  step: 0,
  doctorMode: true,
  modelMode: false,
  answers: JSON.parse(localStorage.getItem('yb-answers') || '{}'),
  fsm: JSON.parse(localStorage.getItem('yb-fsm') || '{"rounds":{}}'),
  maxFollowups: Number(localStorage.getItem('yb-max') || 3),
  stageMax: JSON.parse(localStorage.getItem('yb-stage-max') || '{}'),
};

// ── Persistence ───────────────────────────────────────────────────────────────
function save() {
  localStorage.setItem('yb-answers', JSON.stringify(state.answers));
  localStorage.setItem('yb-fsm', JSON.stringify(state.fsm));
  localStorage.setItem('yb-max', String(state.maxFollowups));
  localStorage.setItem('yb-stage-max', JSON.stringify(state.stageMax));
  updateSidebar();
}

// ── Answer helpers ────────────────────────────────────────────────────────────
function ans(id) { return state.answers[id]; }
function setAns(id, val, multi = false) {
  if (multi) {
    const cur = new Set(Array.isArray(state.answers[id]) ? state.answers[id] : []);
    cur.has(val) ? cur.delete(val) : cur.add(val);
    state.answers[id] = [...cur];
  } else {
    state.answers[id] = val;
  }
  save();
}
function isAnswered(q) {
  const v = state.answers[q.id];
  return Array.isArray(v) ? v.length > 0 : v !== undefined && v !== '' && v !== null;
}

// ── FSM round helpers ─────────────────────────────────────────────────────────
function stageMax(stage) { return state.stageMax[stage] ?? state.maxFollowups; }
function round(stage) { return state.fsm.rounds[stage] || 0; }
function setRound(stage, v) { state.fsm.rounds[stage] = Math.max(0, Math.min(stageMax(stage) - 1, v)); save(); }
function poolDone(stage) { return (QS[stage] || []).every(q => isAnswered(q)); }
function atLimit(stage)  { return round(stage) >= stageMax(stage) - 1; }

// ── Tag computation ───────────────────────────────────────────────────────────
function getTags() {
  const a = state.answers;
  const t = new Set();
  const age = Number(a.age);
  if (age >= 60) t.add('elderly');
  if (age >= 73) t.add('very_elderly');
  if (age > 0 && age <= 40) t.add('young_patient');
  const dur = String(a.duration || '');
  if (dur.includes('年') || dur.includes('5年') || dur.includes('1-5年')) { t.add('chronic_yabi'); t.add('long_duration'); }
  if (['到小腿','到足部'].includes(a.radiation)) t.add('radiating_leg_pain');
  if (['偶尔有','经常有','持续存在'].includes(a.numbness) || (a.pain_nature||[]).includes('麻痛')) t.add('lower_limb_numbness');
  if ((a.aggravating||[]).includes('受凉') || a.cold_relation === '遇冷加重，热敷舒服') t.add('cold_aggravation');
  if ((a.relieving||[]).includes('热敷') || a.cold_relation === '遇冷加重，热敷舒服') t.add('warmth_relieves');
  if (a.cold_heat === '怕冷') t.add('cold_constitution');
  if ((a.diseases||[]).includes('骨质疏松') || (a.western_diag||[]).includes('骨质疏松')) t.add('osteoporosis');
  if (a.tongue_color === '偏暗紫') t.add('dark_tongue');
  if (a.tongue_color === '偏红')   t.add('red_tongue');
  if (a.tongue_coat === '白腻')    t.add('white_greasy_coating');
  if (a.tongue_coat === '黄腻')    t.add('yellow_greasy_coating');
  if (a.tongue_coat === '厚腻')    { t.add('white_greasy_coating'); t.add('dampness'); }
  if (a.dampness === '明显' || a.dampness === '轻微') t.add('dampness');
  const slp = a.sleep;
  if (slp && !(Array.isArray(slp) ? slp.includes('正常') : slp === '正常')) t.add('insomnia');
  if (['胃口差','容易腹胀','吃药容易胃不舒服','恶心反酸'].includes(a.appetite)) t.add('poor_appetite');
  if (a.mouth_taste && a.mouth_taste !== '没有') t.add('bitter_taste');
  if ((a.diseases||[]).includes('糖尿病')) t.add('diabetes');
  if ((a.diseases||[]).includes('高血压')) t.add('hypertension');
  if (['到小腿','到足部','到大腿'].some(v => a.radiation === v)) t.add('lumbar_leg_pain');
  if (['经常有','持续存在'].includes(a.numbness)) t.add('severe_numbness');
  return t;
}

// ── Shen signals ──────────────────────────────────────────────────────────────
function computeSignals() {
  const a = state.answers;
  const tags = getTags();
  return SHEN_SIGNALS.map(s => ({ ...s, active: s.check(a, tags) }));
}

// ── Red flag status ───────────────────────────────────────────────────────────
function redFlagStatus() {
  const rf = QS.redflag;
  const positives = rf.filter(q => (q.urgent  || []).includes(state.answers[q.id])).map(q => q.q);
  const cautions  = rf.filter(q => (q.caution || []).includes(state.answers[q.id])).map(q => q.q);
  const allDone   = rf.every(q => state.answers[q.id]);
  return {
    status: positives.length ? 'urgent' : cautions.length ? 'caution' : allDone ? 'safe' : 'unknown',
    positives, cautions,
  };
}

// ── Case builder ──────────────────────────────────────────────────────────────
function buildCase() {
  const a = state.answers;
  const tags = getTags();
  const signals = computeSignals().filter(s => s.active);
  const red = redFlagStatus();
  const chief = [
    a.duration && (a.duration.includes('年') || a.duration.includes('5年')) ? '反复' : '',
    a.main_symptom || '腰痛',
    a.duration || '',
    a.acute_worsening && a.acute_worsening !== '无明显加重' ? `，加重${a.acute_worsening}` : '',
    tags.has('lower_limb_numbness') ? '，伴下肢麻木' : '',
  ].join('');
  const modules = [];
  if (tags.has('white_greasy_coating') || tags.has('chronic_yabi')) modules.push({ m:'健脾化湿底盘', herbs:['白术30g','茯苓30g','党参15g','甘草6g'] });
  if (tags.has('osteoporosis') || tags.has('elderly')) modules.push({ m:'补肝肾强筋骨', herbs:['杜仲12g','桑寄生15-30g','川牛膝15g','熟地黄10g'] });
  if (tags.has('lower_limb_numbness')) modules.push({ m:'当归四逆通络', herbs:['当归10g','桂枝10g','细辛3g','通草10g','炙甘草15g'] });
  if (tags.has('cold_aggravation')) modules.push({ m:'温经散寒', herbs:['附片10g先煎','桂枝10g','细辛3g'] });
  if (tags.has('insomnia') || tags.has('bitter_taste')) modules.push({ m:'少阳/温胆安神', herbs:['竹茹10g','栀子10g','炒芥子10g','茯苓30g'] });
  if (tags.has('poor_appetite')) modules.push({ m:'顾护中焦', herbs:['陈皮6g','炒麦芽15g','砂仁后下'] });
  if (tags.has('dark_tongue') && tags.has('chronic_yabi')) modules.push({ m:'活血化瘀通络', herbs:['丹参30g','赤芍20g','地龙10g'] });
  return { chief, tags: [...tags], signals, red, modules };
}

// ── Question reason (context-aware) ──────────────────────────────────────────
function questionReason(q, stage) {
  const tags = getTags();
  const sigs = computeSignals().filter(s => s.active).map(s => s.chip);
  if (stage === 'redflag') return '🚨 红旗筛查优先——任何危险信号将立即停止后续问诊并建议急诊评估。';
  if (['cold_relation','cold_heat'].includes(q.id) && (tags.has('elderly') || tags.has('lower_limb_numbness')))
    return '结合高龄/麻木/久病线索，深化寒湿、温经散寒与当归四逆路线辨别。';
  if (['numbness','numbness_location','radiation'].includes(q.id))
    return '深化放射痛、麻木和神经根受压线索（下肢麻木+放射痛→当归四逆汤，使用率58%）。';
  if (['imaging','western_diag','diseases'].includes(q.id))
    return '补足影像、骨质疏松和医学背景，便于医生复核独活寄生汤路线适用性。';
  if (['sleep','appetite','mouth_taste'].includes(q.id))
    return `深化沈老规则高价值变量：口苦+失眠时竹茹/栀子/炒芥子使用率100%（温胆汤路线）。${sigs.length ? ` 当前激活信号：${sigs.join('、')}` : ''}`;
  if (['tongue_color','tongue_coat'].includes(q.id))
    return '舌象为辨证高价值变量：舌暗紫→气血痹阻（66.5%），苔白腻→健脾化湿底盘，苔黄腻→清热利湿。';
  return `补齐关键字段，提升本阶段规则分流准确性。${state.modelMode && sigs.length ? ` 【模型增强】激活信号：${sigs.join('、')}` : ''}`;
}

// ── Question pool for current stage ──────────────────────────────────────────
function stageQuestions(stage) {
  const list = QS[stage] || [];
  const r = round(stage);
  const unanswered = list.filter(q => !isAnswered(q));
  const pool = unanswered.length ? unanswered : list;
  const slice = pool.slice(r * 3, r * 3 + 3);
  return (slice.length ? slice : pool.slice(-3)).map(q => ({ ...q, reason: questionReason(q, stage) }));
}

// ── Render orchestrator ───────────────────────────────────────────────────────
const $screen    = document.querySelector('#screen');
const $pageTitle = document.querySelector('#pageTitle');

function render() {
  renderStepper();
  const step = STEPS[state.step];
  $pageTitle.textContent = step.title;
  if (step.id === 'start')   return renderStart();
  if (step.id === 'signals') return renderSignals();
  if (step.id === 'final')   return renderFinal();
  renderStage(step.id);
}

// ── Stepper ───────────────────────────────────────────────────────────────────
function renderStepper() {
  const el = document.querySelector('#stepper');
  el.innerHTML = STEPS.map((s, i) => `
    <button class="step ${i===state.step?'active':''} ${i<state.step?'done':''}" data-i="${i}" type="button">
      <span class="step-num">${i<state.step ? '✓' : i+1}</span>
      <span class="step-label">${s.label}</span>
    </button>`).join('');
  el.querySelectorAll('button').forEach(b => b.addEventListener('click', () => { state.step = Number(b.dataset.i); render(); }));
}

// ── Start Screen ──────────────────────────────────────────────────────────────
function renderStart() {
  $screen.innerHTML = `
  <div class="hero">
    <div class="hero-grid">
      <div class="hero-left">
        <p class="eyebrow">名老中医腰痹诊疗经验研究助手 · 209例数据驱动</p>
        <h2>把零散腰痛描述整理成<br/>可复核、可教学的标准医案</h2>
        <p>以红旗筛查为安全底线，中西医结合问诊为骨架，沈钦荣腰痹209例数据提炼规则为导引逻辑，输出标准医案、规则线索、医生复核清单和 CDSS 草案。</p>
        <div class="hero-badges">
          <span class="hero-badge">🔴 红旗优先</span>
          <span class="hero-badge">📊 209例数据</span>
          <span class="hero-badge">⚙ 追问次数可配置</span>
          <span class="hero-badge">🔒 Output Guard</span>
          <span class="hero-badge">✍️ 医师签名闭环</span>
        </div>
        <button class="btn btn-primary" id="startBtn" style="font-size:15px;padding:14px 32px">开始整理医案</button>
      </div>
      <div class="hero-notice">
        <strong>⚠️ 重要边界声明</strong>
        <p>本工具仅供名老中医经验研究、临床教学与医生复核使用。患者端不生成最终诊断、完整处方或可执行剂量。CDSS 草案需 licensed physician 审核签名后方可生效。</p>
      </div>
    </div>
    <div class="feature-grid">${FEATURES.map(f => `
      <div class="feature-card">
        <div class="feature-icon">${f.icon}</div>
        <strong>${f.name}</strong>
        <p>${f.desc}</p>
      </div>`).join('')}
    </div>
    <div class="runtime-block">
<span class="comment"># 模型增强模式（可选）</span>
<span class="cmd">TAO_BACKEND</span>=<span class="str">transformers</span> python -m backend.main --tao-chat <span class="str">"请解释本案沈老规则线索"</span> --stream
<span class="comment"># 规则模式（默认，纯确定性）</span>
python -m backend.main --text <span class="str">"腰痛伴下肢麻木5年，舌暗，苔白腻"</span>
    </div>
  </div>`;
  document.querySelector('#startBtn').addEventListener('click', () => { state.step = 1; render(); });
}

// ── Question Stage (FSM) ──────────────────────────────────────────────────────
function renderStage(stage) {
  const urgent = redFlagStatus().status === 'urgent';
  if (stage !== 'redflag' && urgent) {
    $screen.innerHTML = `
      <div class="emergency-panel">
        <div class="emergency-icon">🚨</div>
        <h3>已命中危险信号，请立即线下就医</h3>
        <p>本工具已停止后续中医问诊。请携带本信息前往急诊或专科评估，由医生进一步判断是否需要立即处理。</p>
        <button class="btn btn-ghost" id="backRed">← 返回红旗筛查</button>
      </div>`;
    document.querySelector('#backRed').addEventListener('click', () => { state.step = 1; render(); });
    return;
  }

  const r     = round(stage);
  const maxR  = stageMax(stage);
  const done  = poolDone(stage);
  const limit = atLimit(stage);
  const isDone = done || limit;
  const qs    = stageQuestions(stage);

  $screen.innerHTML = `
    <div class="fsm-strip ${isDone ? 'done' : ''}">
      <div class="fsm-strip-left">
        <strong>有限状态机追问</strong>
        <span class="pill pill-round">第 ${r+1}/${maxR} 轮</span>
        <span class="pill ${state.modelMode ? 'pill-model' : 'pill-rule'}">${state.modelMode ? '模型增强' : '规则模式'}</span>
        ${isDone ? '<span class="pill pill-done">✓ 本状态完成</span>' : ''}
      </div>
      <span class="fsm-meta">${
        done  ? '所有问题已采集完毕，可进入下一阶段。' :
        limit ? `已达本状态追问上限（${maxR} 轮），点击"终止追问"或继续进入下一阶段。` :
        `每轮最多 3 问，结合当前规则标签与上一轮答案深化追问。还剩 ${maxR - r - 1} 轮。`
      }</span>
      <div class="fsm-actions">
        ${isDone
          ? `<button class="btn btn-danger btn-sm" id="endBtn">终止追问 →</button>`
          : `<button class="btn btn-ghost btn-sm" id="endBtn">手动终止本状态</button>
             <button class="btn btn-ghost btn-sm" id="deepenBtn">深化追问 +1 轮</button>`}
      </div>
    </div>
    ${isDone ? `<div class="auto-banner"><span class="auto-banner-icon">✅</span><p>${done ? '本状态问题全部采集完毕' : `已完成 ${maxR} 轮追问`}，点击"进入下一阶段"继续。</p></div>` : ''}
    <div class="card-grid" id="cardGrid"></div>
    <div class="footer-bar">
      <div class="footer-left">
        <button class="btn btn-ghost btn-sm" id="prevBtn">← 上一步</button>
      </div>
      <div class="footer-right">
        <button class="btn ${isDone ? 'btn-jade' : 'btn-primary'}" id="nextBtn">
          ${isDone ? '进入下一阶段 →' : '下一步 →'}
        </button>
      </div>
    </div>`;

  // Render question cards
  const grid = $screen.querySelector('#cardGrid');
  qs.forEach((q, idx) => { grid.appendChild(renderQCard(q, stage, idx)); });

  // Events
  $screen.querySelector('#prevBtn').addEventListener('click', () => { state.step = Math.max(0, state.step-1); render(); });
  $screen.querySelector('#nextBtn').addEventListener('click', () => advance(stage));
  const endBtn = $screen.querySelector('#endBtn');
  if (endBtn) endBtn.addEventListener('click', () => advance(stage));
  const deepBtn = $screen.querySelector('#deepenBtn');
  if (deepBtn) deepBtn.addEventListener('click', () => { setRound(stage, r+1); render(); });
}

function advance(stage) {
  state.fsm.rounds[stage] = state.fsm.rounds[stage] || 0;
  state.step = Math.min(STEPS.length-1, state.step+1);
  render();
}

// ── Question Card ─────────────────────────────────────────────────────────────
function renderQCard(q, stage, idx) {
  const tpl  = document.querySelector('#qTpl').content.cloneNode(true);
  const card = tpl.querySelector('.question-card');
  card.style.animationDelay = `${idx * 0.06}s`;
  if (stage === 'redflag') card.classList.add('redflag');
  if (isAnswered(q)) card.classList.add('answered');

  tpl.querySelector('[data-stage]').textContent = stage.toUpperCase();
  tpl.querySelector('[data-id]').textContent = q.id;
  if (isAnswered(q)) {
    tpl.querySelector('.q-answered-badge').style.display = 'inline-flex';
    tpl.querySelector('.q-tag-state').classList.add('q-tag-done');
  } else if (stage === 'redflag') {
    tpl.querySelector('.q-tag-state').classList.add('q-tag-rf');
  }
  tpl.querySelector('.q-reason').textContent = q.reason;
  tpl.querySelector('.q-text').textContent   = q.q;

  const opts = tpl.querySelector('.options');
  const cur  = ans(q.id);

  if (q.type === 'number') {
    opts.innerHTML = `<input type="number" placeholder="${q.placeholder||''}" value="${cur||''}"
      style="border:1px solid var(--line);border-radius:var(--r-md);padding:10px 14px;width:160px;font-size:16px;font-weight:700"/>`;
    opts.querySelector('input').addEventListener('input', e => { setAns(q.id, Number(e.target.value)); renderStage(stage); });
    tpl.querySelector('.q-note').remove();
  } else if (q.type === 'range') {
    opts.innerHTML = `
      <div class="range-wrap">
        <input type="range" min="0" max="10" value="${cur ?? 5}"/>
        <div class="range-labels"><span>0 无痛</span><span>10 剧痛</span></div>
        <div class="range-val">${cur ?? 5} <small style="font-size:14px;font-weight:500">/ 10 分</small></div>
      </div>`;
    const range = opts.querySelector('input');
    const label = opts.querySelector('.range-val');
    range.addEventListener('input', e => { label.innerHTML = `${e.target.value} <small style="font-size:14px;font-weight:500">/ 10 分</small>`; setAns(q.id, Number(e.target.value)); });
    tpl.querySelector('.q-note').remove();
  } else {
    (q.opts || []).forEach(opt => {
      const isMultiSel = q.multi ? (Array.isArray(cur) && cur.includes(opt)) : cur === opt;
      const isUrgent   = (q.urgent || []).includes(opt) && isMultiSel;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `opt-pill ${isMultiSel ? 'selected' : ''} ${isUrgent ? 'urgent-sel' : ''}`;
      btn.textContent = opt;
      btn.addEventListener('click', () => { setAns(q.id, opt, q.multi || false); renderStage(STEPS[state.step].id); });
      opts.appendChild(btn);
    });
    if (!q.note) tpl.querySelector('.q-note').remove();
    else {
      const note = tpl.querySelector('.q-note');
      note.value = ans(`${q.id}_note`) || '';
      note.addEventListener('input', e => setAns(`${q.id}_note`, e.target.value));
    }
  }
  return card;
}

// ── Signals Screen ────────────────────────────────────────────────────────────
function renderSignals() {
  const c   = buildCase();
  const sigs = c.signals;
  const tags = c.tags;

  $screen.innerHTML = `
    <div class="result-panel success">
      <p class="eyebrow">沈老经验规则信号 · 实时命中</p>
      <h3>命中 ${sigs.length} 个规则信号</h3>
      ${sigs.length ? `<div class="signal-grid">${sigs.map(s => `
        <div class="signal-card">
          <div class="signal-card-head">
            <span class="signal-name">${s.label}</span>
            <span class="signal-formula">${s.formula}</span>
          </div>
          <p class="signal-desc">${s.reason}</p>
          <div class="herb-chips" style="margin-top:8px">${s.herbs.map(h => `
            <span class="herb-chip ${h.includes('先煎')?'xianjian':''}">${h}</span>`).join('')}
          </div>
        </div>`).join('')}</div>`
      : '<p>暂无信号命中，请先完成前面各阶段采集。</p>'}
    </div>

    <div class="result-panel">
      <p class="eyebrow">结构化标签 · 规则引擎输入</p>
      <h3>当前标签（${tags.length}个）</h3>
      <div class="tag-cloud">${tags.length
        ? tags.map(t => `<span class="tag-pill" title="${tagDesc(t)}">${t}</span>`).join('')
        : '<span style="color:var(--muted-2)">暂无标签，请先完成问诊</span>'
      }</div>
    </div>

    <div class="result-panel">
      <p class="eyebrow">方剂路线信号 · 医生复核用</p>
      <h3>候选药物模块（${c.modules.length}个）</h3>
      <div class="formula-grid">${c.modules.length
        ? c.modules.map((m, i) => `
          <div class="formula-card">
            <div class="formula-card-head">
              <span class="formula-rank">${i+1}</span>
              <span class="formula-name">${m.m}</span>
            </div>
            <div class="herb-chips">${m.herbs.map(h => `
              <span class="herb-chip ${h.includes('先煎')?'xianjian':h.includes('高风险')||h.includes('审核')?'danger':''}">${h}</span>`).join('')}
            </div>
          </div>`).join('')
        : '<p style="color:var(--muted-2)">信息不足，待补充采集</p>'
      }</div>
    </div>

    <div class="result-panel">
      <p class="eyebrow">CDSS 草案 · draft_for_clinician_review</p>
      <h3>候选诊断方向</h3>
      <ul>
        <li><strong>西医候选：</strong>${tags.includes('radiating_leg_pain')||tags.includes('lower_limb_numbness') ? '腰椎间盘突出/神经根受压相关腰腿痛（待查体影像复核）' : '非特异性腰痛等待鉴别'}</li>
        <li><strong>中医候选证型：</strong>${tags.includes('osteoporosis') ? '肝肾不足证（杜仲、桑寄生、续断路线）' : tags.includes('dark_tongue') ? '气血痹阻证（66.5%最常见，独活寄生+当归四逆）' : '待完善信息后匹配'}</li>
        <li><strong>寒热判断：</strong>${tags.includes('cold_aggravation') ? '寒证线索明显，附片先煎+桂枝+细辛路线' : tags.includes('yellow_greasy_coating') ? '湿热线索，薏苡仁+黄柏+苍术路线' : '寒热信息待补充'}</li>
        <li><strong>安全复核重点：</strong>高风险药物（附片先煎、细辛≤3g、全蝎3g）、NSAIDs、抗凝药、肝肾功能、胃肠风险</li>
      </ul>
      <p style="margin-top:12px;font-size:12px;color:var(--danger)">⚠️ 以上为规则引擎草案，必须由 licensed physician 审核后方可作为临床参考。</p>
    </div>

    <div class="footer-bar">
      <div class="footer-left"><button class="btn btn-ghost btn-sm" id="prevBtn">← 上一步</button></div>
      <div class="footer-right"><button class="btn btn-jade" id="nextBtn">生成最终医案 →</button></div>
    </div>`;
  $screen.querySelector('#prevBtn').addEventListener('click', () => { state.step--; render(); });
  $screen.querySelector('#nextBtn').addEventListener('click', () => { state.step++; render(); });
}

// ── Final Report ──────────────────────────────────────────────────────────────
function renderFinal() {
  const rpt = buildReport();
  const tabs = { case: rpt.md, handoff: rpt.handoff, tao: rpt.tao, cdss: rpt.cdss, physician: rpt.physician, json: JSON.stringify(rpt.json, null, 2) };

  $screen.innerHTML = `
    <div class="result-panel">
      <div class="report-tabs">
        <button class="tab-btn active" data-t="case">📋 标准医案</button>
        <button class="tab-btn" data-t="handoff">🩺 医生复核</button>
        <button class="tab-btn" data-t="tao">🤖 Tao解释</button>
        <button class="tab-btn" data-t="cdss">📊 CDSS草案</button>
        <button class="tab-btn" data-t="physician">✍️ 医师签名</button>
        <button class="tab-btn" data-t="json">{ } 规则JSON</button>
      </div>
      <pre class="report-box" id="reportBox">${escHtml(rpt.md)}</pre>
      <div class="footer-bar" style="margin-top:16px">
        <div class="footer-left">
          <button class="btn btn-ghost btn-sm" id="prevBtn">← 上一步</button>
          <button class="btn btn-ghost btn-sm" id="copyBtn">📋 复制</button>
        </div>
        <div class="footer-right">
          <button class="btn btn-primary btn-sm" id="dlMdBtn">⬇ 下载 Markdown</button>
          <button class="btn btn-ghost btn-sm" id="dlJsonBtn">⬇ 下载 JSON</button>
        </div>
      </div>
    </div>`;

  const box = $screen.querySelector('#reportBox');
  $screen.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      $screen.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b === btn));
      box.textContent = tabs[btn.dataset.t];
    });
  });
  $screen.querySelector('#prevBtn').addEventListener('click', () => { state.step--; render(); });
  $screen.querySelector('#copyBtn').addEventListener('click', () => navigator.clipboard?.writeText(box.textContent));
  $screen.querySelector('#dlMdBtn').addEventListener('click', () => dl('yaobi-case.md', rpt.md, 'text/markdown'));
  $screen.querySelector('#dlJsonBtn').addEventListener('click', () => dl('yaobi-case.json', JSON.stringify(rpt.json, null, 2)));
}

// ── Report builder ────────────────────────────────────────────────────────────
function buildReport() {
  const a = state.answers;
  const c = buildCase();
  const sigs = c.signals;

  const md = `# 腰痹医案草稿
> 生成时间：${new Date().toLocaleDateString('zh-CN')}
> 工具：YaoBi-CaseGuide v2 · 沈钦荣腰痹经验 CDSS · 209例数据驱动

## 一、基本信息
患者：${a.sex || '未详'}，${a.age || '未详'}岁，职业：${a.occupation || '未详'}

## 二、主诉
${c.chief || '（未采集）'}

## 三、现病史
疼痛部位：${fmt(a.location)}；放射情况：${fmt(a.radiation)}；疼痛性质：${fmt(a.pain_nature)}；疼痛评分：${a.severity ?? '未详'}/10。
加重因素：${fmt(a.aggravating)}；缓解因素：${fmt(a.relieving)}。
下肢麻木：${fmt(a.numbness)}，麻木部位：${fmt(a.numbness_location)}；下肢无力：${fmt(a.weakness)}。
间歇性跛行：${fmt(a.walking_limit)}。

## 四、伴随症状
寒热：${fmt(a.cold_heat)}；寒热与疼痛关系：${fmt(a.cold_relation)}；湿困感：${fmt(a.dampness)}。
睡眠：${fmt(a.sleep)}；胃纳：${fmt(a.appetite)}；口苦口干：${fmt(a.mouth_taste)}。

## 五、既往史与检查
既往疾病：${fmt(a.diseases)}；影像/检查：${fmt(a.imaging)}；既往诊断：${fmt(a.western_diag)}。
近期用药：${fmt(a.medications)}；抗凝/激素/降糖药：${fmt(a.anticoag)}；过敏史：${fmt(a.allergy)}。

## 六、中医四诊信息
舌色：${fmt(a.tongue_color)}；舌苔：${fmt(a.tongue_coat)}；脉象：待医生面诊补充。

## 七、结构化标签
${c.tags.map(t => `- \`${t}\` — ${tagDesc(t)}`).join('\n') || '- 暂无（待补充采集）'}

## 八、沈老经验规则信号（基于209例数据）
${sigs.length ? sigs.map((s,i) => `${i+1}. **${s.label}**（${s.formula}）\n   ${s.reason}`).join('\n') : '暂无命中信号，待补充信息后重新匹配。'}

## 九、候选药物模块
${c.modules.length ? c.modules.map((m,i) => `${i+1}. **${m.m}**：${m.herbs.join('、')}`).join('\n') : '信息不足，待补充。'}

## 十、医生复核清单
- 红旗筛查状态：${c.red.status}${c.red.positives.length ? `（阳性：${c.red.positives.join('；')}）` : ''}
- 下肢肌力、感觉、反射查体（需面诊）
- 脉象（需面诊）
- 影像/骨密度报告原文核实
- NSAIDs 胃肠肝肾风险 · 抗凝药相互作用
- 附片先煎 · 细辛≤3g · 虫类药过敏史 · 高风险药物审核

---
> **⚠️ 声明：** 本报告为医案整理和医生端CDSS草案，不构成最终诊断、签名处方或患者可执行剂量。
> 最终诊断与处方须由 licensed physician 手工录入并签名锁定。`;

  const handoff = `# 医生复核摘要

**主诉：** ${c.chief}

**规则标签：** ${c.tags.join('、') || '待补充'}

**沈老信号：** ${sigs.map(s => s.label).join('、') || '待采集'}

**候选方剂路线：** ${c.modules.map(m => m.m).join('、') || '待补充'}

**信息缺口：**
- 脉象（需面诊）
- 影像原文报告
- 下肢肌力/感觉查体
- 骨密度数值

**安全复核重点：**
- 附片：先煎30-60分钟
- 细辛：最大剂量3g
- 全蝎3g / 蜈蚣2g：确认无过敏史
- NSAIDs + 抗凝药相互作用
- 肝肾功能（附片/全蝎前）`;

  const tao = `# Tao 教学解释叠加层

**当前模式：** ${state.modelMode ? '模型增强（Tao overlay 已配置）' : '规则模式（Tao overlay 未启用）'}

**激活命令：**
\`\`\`bash
TAO_BACKEND=transformers python -m backend.main --tao-chat "请解释本案规则线索" --stream
\`\`\`

**Tao 工作边界：**
- ✅ 允许：问诊问题改写（患者友好）、规则解释叠加、教学分析
- ❌ 禁止：最终诊断、完整处方、患者可执行剂量

**Output Guard 保护层：**
所有 Tao 输出经过 JSON Repair + Output Guard 过滤，任何违规内容回退规则模板。

**激活信号：**
${sigs.map(s => `- ${s.label}：${s.reason}`).join('\n') || '暂无激活信号'}`;

  const cdss = `# CDSS 草案
**状态：** draft_for_clinician_review
**patient_visible:** false
**complete_prescription_generated:** false
**patient_executable_dose_generated:** false

**候选方向（非最终医嘱）：**
- 西医：${c.tags.includes('lower_limb_numbness') ? '腰椎间盘突出/神经根受压（待影像复核）' : '非特异性腰痛待鉴别'}
- 中医：${c.tags.includes('osteoporosis') ? '肝肾不足证（独活寄生汤路线）' : '气血痹阻证（66.5%，独活寄生+当归四逆）'}
- 寒热：${c.tags.includes('cold_aggravation') ? '寒证，温经散寒路线' : '寒热信息待补'}

**激活规则信号：**
${sigs.map(s => `- **${s.chip}** → ${s.formula}`).join('\n') || '待补充信息'}

**候选药物模块（待医师审核选用）：**
${c.modules.map(m => `- ${m.m}：${m.herbs.join('、')}`).join('\n') || '待补充'}`;

  const physician = `# 医师审核签名区

**重要声明：**
最终诊断、完整处方、剂量、煎服法、疗程只能由 licensed physician 手工录入。

系统输出（CDSS草案）仅为规则证据和辅助参考，医生确认并签名后方可作为正式医嘱。

**系统不生成、不输出、不允许：**
- 完整处方（含完整剂量）
- 患者可执行的煎服指导
- "可以自行用药"类建议

**医师签名后需锁定字段：**
- [ ] 西医诊断（最终）
- [ ] 中医诊断 + 证型（最终）
- [ ] 处方（含药名、剂量、煎服法）
- [ ] 疗程与复诊计划
- [ ] 医师签名 + 时间戳`;

  return {
    md, handoff, tao, cdss, physician,
    json: {
      answers: state.answers,
      tags: c.tags,
      shen_signals: sigs.map(s => ({ id:s.id, label:s.label, formula:s.formula })),
      modules: c.modules.map(m => ({ name:m.m, herbs:m.herbs })),
      red_flags: c.red,
      fsm_config: { maxFollowups: state.maxFollowups, stageMax: state.stageMax },
      mode: state.modelMode ? 'model_enhanced' : 'rule_only',
      cdss_status: 'draft_for_clinician_review',
      data_source: '209 real cases from Dr. Shen Qinrong (沈钦荣医师)',
    },
  };
}

// ── Settings Panel ────────────────────────────────────────────────────────────
function renderSettings() {
  const stageLabels = { redflag:'红旗筛查', basic:'主诉病程', pain:'疼痛特征', neuro:'神经骨科', tcm:'中医四诊', comorbidity:'合并病用药' };
  const overlay = document.createElement('div');
  overlay.className = 'settings-overlay';
  overlay.innerHTML = `
    <div class="settings-drawer">
      <div class="drawer-header">
        <h3>⚙ 问诊配置</h3>
        <button class="btn btn-ghost btn-sm" id="closeSettings">关闭</button>
      </div>
      <div class="drawer-body">
        <div class="setting-section">
          <h4>全局追问轮数（1–5 轮）</h4>
          <div class="setting-row">
            <input type="range" id="globalMax" min="1" max="5" value="${state.maxFollowups}"/>
            <span class="setting-val" id="globalMaxVal">${state.maxFollowups} 轮</span>
          </div>
          <p class="setting-hint">每个状态默认最多追问此轮次，每轮最多 3 问。可被各状态单独覆盖。</p>
        </div>

        <div class="setting-section">
          <h4>各状态单独追问上限（留空=使用全局）</h4>
          ${Object.entries(stageLabels).map(([k,l]) => `
            <div class="setting-row">
              <span class="setting-stage-label">${l}</span>
              <input type="number" class="stage-inp" data-s="${k}" min="1" max="5"
                value="${state.stageMax[k]||''}" placeholder="${state.maxFollowups}"/>
              <span style="font-size:12px;color:var(--muted-2)">轮</span>
            </div>`).join('')}
        </div>

        <div class="setting-section">
          <h4>问诊模式</h4>
          <div class="mode-toggle">
            <div class="mode-option ${!state.modelMode?'selected':''}" id="ruleMode">
              <strong>📐 规则模式</strong>
              <p>纯确定性规则，证据完全可追溯，零 LLM 调用</p>
            </div>
            <div class="mode-option ${state.modelMode?'selected':''}" id="modelMode">
              <strong>🤖 模型增强</strong>
              <p>Tao Direct Runtime 叠加问题改写与教学解释</p>
            </div>
          </div>
        </div>

        <div class="setting-section">
          <h4>数据统计说明（209例）</h4>
          <ul style="font-size:12px;color:var(--muted)">
            <li>气血痹阻证：66.5%（139/209），最常见证型</li>
            <li>当归四逆汤类：47例，麻木+放射痛核心方</li>
            <li>白术30g/茯苓30g：沈老惯用底盘剂量</li>
            <li>竹茹/栀子/炒芥子：温胆痰热证100%使用率</li>
            <li>细辛：固定3g，不可超量</li>
          </ul>
        </div>

        <div class="setting-section">
          <button class="btn btn-danger" id="resetBtn" style="width:100%">🗑 清除所有已采集答案（重新开始）</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  // Global max
  const gi = overlay.querySelector('#globalMax');
  const gv = overlay.querySelector('#globalMaxVal');
  gi.addEventListener('input', () => { state.maxFollowups = Number(gi.value); gv.textContent = `${state.maxFollowups} 轮`; save(); });

  // Per-stage
  overlay.querySelectorAll('.stage-inp').forEach(inp => {
    inp.addEventListener('input', () => {
      const v = inp.value.trim();
      if (!v || isNaN(v)) delete state.stageMax[inp.dataset.s];
      else state.stageMax[inp.dataset.s] = Math.max(1, Math.min(5, Number(v)));
      save();
    });
  });

  // Mode
  overlay.querySelector('#ruleMode').addEventListener('click', () => {
    state.modelMode = false;
    overlay.querySelector('#ruleMode').classList.add('selected');
    overlay.querySelector('#modelMode').classList.remove('selected');
    save();
  });
  overlay.querySelector('#modelMode').addEventListener('click', () => {
    state.modelMode = true;
    overlay.querySelector('#modelMode').classList.add('selected');
    overlay.querySelector('#ruleMode').classList.remove('selected');
    save();
  });

  // Reset
  overlay.querySelector('#resetBtn').addEventListener('click', () => {
    if (!confirm('确认清除所有已采集答案？此操作不可恢复。')) return;
    state.answers = {}; state.fsm = { rounds: {} }; save();
    overlay.remove(); state.step = 0; render();
  });

  overlay.querySelector('#closeSettings').addEventListener('click', () => { overlay.remove(); render(); });
}

// ── Sidebar updater ───────────────────────────────────────────────────────────
function updateSidebar() {
  const c    = buildCase();
  const sigs = computeSignals();

  // Quality ring
  const missing = ['age','sex','duration','location','radiation','numbness','cold_relation','sleep','appetite','imaging','diseases','tongue_color']
    .filter(k => !state.answers[k]);
  const score = Math.max(0, Math.round((12 - missing.length) / 12 * 100));
  const ring  = document.querySelector('#qualityRing');
  if (ring) {
    ring.textContent = `${score}%`;
    ring.style.background = `conic-gradient(var(--jade) ${score * 3.6}deg, var(--line) 0deg)`;
  }
  const grade = score >= 80 ? 'good / 可复核' : score >= 60 ? 'fair / 需补问' : 'needs_more_info';
  const hint  = score >= 80 ? '可生成医生复核摘要。' : '建议继续补充关键字段。';
  const qg = document.querySelector('#qualityGrade'); if (qg) qg.textContent = grade;
  const qh = document.querySelector('#qualityHint');  if (qh) qh.textContent = hint;

  // Chief
  const pc = document.querySelector('#previewChief'); if (pc) pc.textContent = c.chief || '未采集';
  const ph = document.querySelector('#previewHistory');
  if (ph) ph.textContent = `疼痛：${fmt(state.answers.location)}；放射：${fmt(state.answers.radiation)}；麻木：${fmt(state.answers.numbness)}`;
  const pt = document.querySelector('#previewTags'); if (pt) pt.textContent = c.tags.join('、') || '暂无';
  const pm = document.querySelector('#previewMissing'); if (pm) pm.textContent = missing.join('、') || '关键字段已较完整';

  // Signal chips
  const chips = document.querySelectorAll('#signalChips .signal-chip');
  sigs.forEach((s, i) => {
    if (chips[i]) chips[i].className = `signal-chip ${s.active ? 'active' : 'inactive'}`;
  });

  // FSM config display
  const fcd = document.querySelector('#fsmConfigDisplay');
  if (fcd) fcd.textContent = `${state.modelMode ? '模型增强' : '规则'}模式 · 全局追问 ${state.maxFollowups} 轮`;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmt(v) { return Array.isArray(v) ? (v.join('、') || '未详') : (v || '未详'); }
function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function dl(name, text, type = 'text/plain') {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name; a.click(); URL.revokeObjectURL(a.href);
}
function tagDesc(t) {
  const m = {
    elderly:'≥60岁，退变/肝肾不足背景', very_elderly:'≥73岁，峻药慎用，扶正为先',
    young_patient:'≤40岁，多实证，柴胡路线47%',
    chronic_yabi:'腰痹时间较久（含"年"）', long_duration:'病程较长',
    lower_limb_numbness:'下肢麻木，当归四逆汤信号', radiating_leg_pain:'下肢放射痛',
    cold_aggravation:'受凉加重，寒湿/温阳路线', warmth_relieves:'热敷舒服，寒湿路线',
    cold_constitution:'怕冷体质', osteoporosis:'骨质疏松，独活寄生汤核心适应证',
    dark_tongue:'舌暗紫，气血痹阻证66.5%', red_tongue:'舌偏红，热证线索',
    white_greasy_coating:'白腻苔，健脾化湿底盘', yellow_greasy_coating:'黄腻苔，湿热证线索',
    dampness:'湿困，薏苡仁+白术+茯苓', insomnia:'睡眠欠佳，温胆汤信号',
    poor_appetite:'胃纳差，顾护中焦信号', bitter_taste:'口苦，柴胡/温胆路线',
    lumbar_leg_pain:'腰腿痛', diabetes:'糖尿病，用药禁忌需关注',
    hypertension:'高血压，麻黄慎用',
  };
  return m[t] || t;
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.querySelector('#settingsBtn').addEventListener('click', renderSettings);
document.querySelector('#doctorModeBtn').addEventListener('click', () => {
  state.doctorMode = !state.doctorMode;
  document.querySelector('#doctorModeBtn').textContent = state.doctorMode ? '医生模式' : '患者模式';
});
document.querySelector('#exportJsonBtn').addEventListener('click', () => {
  dl('yaobi-case.json', JSON.stringify(buildReport().json, null, 2));
});

render();
updateSidebar();
