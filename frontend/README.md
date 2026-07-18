# YaoBi-CaseGuide Frontend

这是一个零依赖静态前端原型，用于演示沈钦荣腰痹经验智能体的患者导引、实时医案侧栏、规则线索页、医生端 CDSS 草案和最终医案导出。

## 运行

**真·Tao 在环（推荐）**——让语言模型真正自主选择并调用 skill：

```bash
pip install -e .
TAO_BACKEND=mock python -m backend.server --port 8000   # 或 transformers / http
# 打开 http://localhost:8000 —— 右上角显示「Tao 在线」徽章
```

**仅静态预览**（离线规则模式，前端自动回退并如实标注）：

```bash
cd frontend
python -m http.server 4173
# 打开 http://localhost:4173
```

可用 `window.YAOBI_API_BASE` 指向独立部署的后端（默认同源）。

## 页面能力

- 开始页：知情提示、用途边界、患者不可自用声明。
- 红旗筛查页：外伤、马尾综合征、进行性无力、感染、肿瘤/体重下降、骨折风险。
- 渐进式问诊页：主诉病程、疼痛特征、神经骨科、中医四诊、合并病用药。
- 实时侧栏：医案完整度、主诉、现病史、标签、缺失字段。
- 规则线索页：患者模式显示关键线索；医生模式显示候选诊断、候选证型、方剂路线和药物模块 CDSS 草案。
- 最终医案页：标准医案 Markdown、复制、下载、JSON 导出。

## 安全边界

前端与后端保持一致：患者端不显示最终诊断、签名处方或患者可执行剂量；CDSS 草案固定为医生端 `draft_for_clinician_review`，最终诊断和处方需要医师审核签名。
