"""Single patient-report renderer for validated generic clinical analysis."""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mechanism_categories import CATEGORY_ORDER, classify_mechanism


def classify_treatment_mechanism(trial: dict, verdict: str = "", analysis: dict | None = None) -> tuple[str, str, str]:
    result = classify_mechanism(trial, analysis=analysis)
    return result["label_zh"], result["label_zh"], result["category"]
def esc(v: Any) -> str:
 return html.escape("" if v is None else str(v), quote=True)


def trial_anchor(value: Any) -> str:
 return "trial-" + re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip())


def safe_external_url(value: Any, *, fallback: str = "#") -> str:
 try:
  parsed = urlsplit(str(value or "").strip())
 except ValueError:
  return fallback
 return str(value).strip() if parsed.scheme.casefold() in {"http", "https"} and parsed.hostname else fallback


def _legacy_development_evidence_html(trial: dict[str, Any], zh: bool) -> str:
 evidence=trial.get("development_evidence") or []; search=trial.get("evidence_search") or {}
 if not evidence and not search:return ""
 heading="研发与既往证据" if zh else "Development and prior evidence"
 if evidence:
  rows=[]
  for item in evidence:
   stage=item.get("evidence_stage") or ""
   citation=item.get("citation") or ""
   url=safe_external_url(item.get("url"))
   if url == "#":
    url = ""
   link=f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(citation)}</a>' if url else esc(citation)
   findings=("主要结果：" if zh else "Findings: ")+str(item.get("findings") or "")
   applicability=("与患者的关系：" if zh else "Patient applicability: ")+str(item.get("applicability") or "")
   limitations=("限制：" if zh else "Limitations: ")+str(item.get("limitations") or "")
   rows.append(f'<li><b>{esc(stage)}</b> · {link}<div>{esc(findings)}</div><div>{esc(applicability)}</div><div>{esc(limitations)}</div></li>')
  body='<ul class="evidence-list">'+"".join(rows)+"</ul>"
 else:
  note=search.get("summary") or ("已检索但未找到与该药物及患者癌种直接相关的可核实公开论文。" if zh else "The search found no verifiable publication directly relevant to this agent and patient cancer.")
  body=f'<p>{esc(note)}</p>'
 return f"<h4>{heading}</h4>{body}"


def development_evidence_html(trial: dict[str, Any], zh: bool) -> str:
 evidence = trial.get("development_evidence") or []
 search = trial.get("evidence_search") or {}
 if not evidence and not search:
  return ""
 heading = "研发与既往证据" if zh else "Development and prior evidence"
 if not evidence:
  note = search.get("summary") or (
   "已检索，但未找到与该药物及患者癌种直接相关且可核实的公开论文。"
   if zh else
   "The search found no verifiable publication directly relevant to this agent and patient cancer."
  )
  return f"<h4>{heading}</h4><p>{esc(note)}</p>"
 rows = []
 for item in evidence:
  stage = item.get("evidence_stage") or ""
  citation = item.get("citation") or ""
  url = safe_external_url(item.get("url"))
  link = (
   f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(citation)}</a>'
   if url != "#" else esc(citation)
  )
  findings = ("主要结果：" if zh else "Findings: ") + str(item.get("findings") or "")
  applicability = (
   ("与患者的关系：" if zh else "Patient applicability: ")
   + str(item.get("applicability") or "")
  )
  limitations = ("限制：" if zh else "Limitations: ") + str(item.get("limitations") or "")
  rows.append(
   f'<li><b>{esc(stage)}</b> · {link}<div>{esc(findings)}</div>'
   f'<div>{esc(applicability)}</div><div>{esc(limitations)}</div></li>'
  )
 return f'<h4>{heading}</h4><ul class="evidence-list">{"".join(rows)}</ul>'


def decision_report_html(payload: dict[str, Any], zh: bool) -> str:
 decision = payload.get("decision_report") or {}
 paths = decision.get("decision_paths") or []
 goals = decision.get("goals_of_care") or {}
 if not paths and not goals:
  return ""
 title = "决策核实路径" if zh else "Decision verification paths"
 note = (
  "以下为需要与医生及研究中心核实的优先路径，不是治疗建议或入组结论。"
  if zh else
  "These are prioritized verification paths for discussion with clinicians and study centres, not treatment recommendations."
 )
 rows = []
 for item in paths:
  if not isinstance(item, dict):
   continue
  rank = item.get("rank")
  trial_id = item.get("trial_id")
  rationale = item.get("rationale")
  if trial_id and rationale:
   trial_title = item.get("trial_title") or trial_id
   bucket_labels = {
    "patient_country_best": "患者所在国最适合" if zh else "Best in patient country",
    "overseas_best": "境外最适合" if zh else "Best overseas",
    "confirmation_best": "待确认中最适合" if zh else "Best pending confirmation",
   }
   bucket = bucket_labels.get(item.get("recommendation_bucket"), "")
   pending = item.get("blockers_pending") or []
   efficacy = (item.get("efficacy_snapshot") or {}).get("applies_because") or ""
   details = []
   if efficacy:
    label = "疗效证据关系" if zh else "Evidence relevance"
    details.append(f"<div><b>{esc(label)}:</b> {esc(efficacy)}</div>")
   if pending:
    label = "待核实" if zh else "To verify"
    details.append(f'<div><b>{esc(label)}:</b> {esc("; ".join(map(str, pending[:5])))}</div>')
   rows.append(
    f"<li><b>{esc(rank)}. {esc(bucket + ' · ' if bucket else '')}{esc(trial_title)} "
    f"(<a href=\"#{esc(trial_anchor(trial_id))}\">{esc(trial_id)}</a>)</b>"
    f"<div>{esc(rationale)}</div>{''.join(details)}</li>"
   )
 goals_html = ""
 if goals.get("triggered") is True:
  reasons = "; ".join(str(value) for value in goals.get("reasons") or [])
  label = "建议同步进行治疗目标讨论" if zh else "A goals-of-care discussion is also recommended"
  recommendation = str(goals.get("discussion_recommendation") or "").strip()
  recommendation_html = "".join(
   f"<p>{esc(paragraph)}</p>"
   for paragraph in recommendation.split("\n\n") if paragraph.strip()
  )
  goals_html = (
   f'<div class="goals-discussion"><p><b>{esc(label)}:</b> {esc(reasons)}</p>'
   f"{recommendation_html}</div>"
  )
 return (
  f'<section class="overview"><h3>{esc(title)}</h3><p>{esc(note)}</p>'
  f'<ol class="evidence-list">{"".join(rows)}</ol>{goals_html}</section>'
 )


def patient_summary_html(payload: dict[str, Any], zh: bool) -> str:
 decision = payload.get("decision_report") or {}
 summary = decision.get("patient_summary") or {}
 summary_text = str(summary.get("summary_text") or "").strip()
 flags = decision.get("consistency_flags") or []
 if not summary_text and not flags:
  return ""
 title = "患者摘要" if zh else "Patient summary"
 flag_title = "关键信息待核实" if zh else "Key information to verify"
 flag_rows = "".join(
  f"<li>{esc(item.get('flag'))}</li>"
  for item in flags if isinstance(item, dict) and item.get("flag")
 )
 flag_html = f"<h4>{esc(flag_title)}</h4><ul>{flag_rows}</ul>" if flag_rows else ""
 return (
  f'<section class="overview"><h3>{esc(title)}</h3>'
  f"<p>{esc(summary_text)}</p>{flag_html}</section>"
 )


def render_html(p: dict[str, Any], path: Path) -> None:
 if p.get("language") not in {"zh-CN", "en"}:
  raise ValueError("Report language must be 'zh-CN' or 'en'")
 zh=p["language"]=="zh-CN"; patient=p["patient"]; trials=p["trials"]; loc=patient.get("country","")
 # Recall and mechanism matching can be multi-label, but presentation is not.
 trials=list({str(t.get("id") or t.get("trial_uid") or index):t for index,t in enumerate(trials)}.values())
 T=lambda z,e:z if zh else e
 access_defs=[
  ("domestic",T("\u56fd\u5185\u53ef\u53ca","In-country access")),

  ("country_unverified",T("\u56fd\u5bb6\u8bb0\u5f55\u5f85\u6838\u5b9e","Country record unverified")),
  ("overseas",T("\u5883\u5916","Overseas"))]
 grouped={category:[] for category in CATEGORY_ORDER}
 for t in trials:grouped[t["mechanism_category"]["category"]].append(t)
 category_desc={
  "targeted_therapy":T("\u7cbe\u51c6\u9776\u70b9\u836f\u7269\u53ca\u9776\u5411\u8054\u5408","Precision targets and targeted combinations"),
  "ras_mapk_pathway":T("RAS / MAPK \u53ca\u76f8\u5173\u901a\u8def\u7b56\u7565","RAS / MAPK and related pathway strategies"),
  "immune_combination":T("\u514d\u75ab\u68c0\u67e5\u70b9\u3001\u53cc\u7279\u5f02\u6027\u6297\u4f53\u53ca\u514d\u75ab\u8054\u5408","Checkpoint, bispecific and immune combinations"),
  "cell_and_biologic":T("CAR-T / TCR-T / TIL / \u75ab\u82d7\u53ca\u6eb6\u7624\u6cbb\u7597","CAR-T / TCR-T / TIL / vaccines and oncolytic therapy"),
  "solid_tumor_basket":T("\u6cdb\u5b9e\u4f53\u7624\u53ca\u5206\u5b50\u7bee\u5b50\u7814\u7a76","Solid-tumor and molecular basket studies"),
  "core_marker_screening":T("\u9700\u786e\u8ba4 HLA\u3001\u6297\u539f\u8868\u8fbe\u6216\u5206\u5b50\u5206\u578b","HLA, antigen or molecular confirmation required"),
  "other":T("\u5176\u4ed6\u9700\u4eba\u5de5\u590d\u6838\u7684\u5e72\u9884\u65b9\u5411","Other interventions requiring manual review")}
 banner_style={"targeted_therapy":"b-target","ras_mapk_pathway":"b-pathway","immune_combination":"b-immune","cell_and_biologic":"b-cell","solid_tumor_basket":"b-basket","core_marker_screening":"b-marker","other":"b-other"}
 sections=[]
 section_index=0
 for category in CATEGORY_ORDER:
  items=grouped[category]
  if not items:continue
  section_index+=1; m=items[0]["mechanism_category"]; cards=[]
  for t in items:
   ga=t["gating"]; ca=t["country_assessment"]; access_key=ca["class"]
   display_access_key="domestic" if access_key in {"domestic_named", "domestic_registry"} else access_key
   if access_key=="domestic_named":access=T(f"{loc} \u5177\u540d\u4e2d\u5fc3 {t.get('patient_country_site_count',0)} \u4e2a",f"{t.get('patient_country_site_count',0)} named centre(s) in {loc}")
   elif access_key=="domestic_registry":access=T(f"{loc} \u672c\u571f\u6ce8\u518c\uff0c\u5177\u4f53\u4e2d\u5fc3\u5f85\u6838\u5b9e",f"Native {loc} registry; centre unverified")
   elif access_key=="country_unverified":access=T(f"\u4ec5\u6709 {loc} \u56fd\u5bb6\u8bb0\u5f55\uff0c\u4e0d\u80fd\u786e\u8ba4\u4e2d\u5fc3",f"Only a {loc} country record is available; centre not established")
   else:access=T("\u60a3\u8005\u6240\u5728\u56fd\u5bb6\u6682\u65e0\u5730\u70b9\u8bc1\u636e","No location evidence in patient country")
   inactive_status=str(t.get("overall_status") or "").strip().upper().replace(" ","_") in {"ACTIVE_NOT_RECRUITING","COMPLETED","NO_LONGER_RECRUITING","NOT_RECRUITING","SUSPENDED","TERMINATED","WITHDRAWN"} or (t.get("registry_status_rule") or {}).get("rule_id")=="REGISTRY-INACTIVE"
   verdict=(T("\u5df2\u5173\u95ed\u62db\u52df","Recruitment closed") if inactive_status else {"match":T("\u9884\u5339\u914d","Potential match"),"conditional":T("\u9700\u786e\u8ba4","Conditional"),"exclude":T("\u4e0d\u7b26\u5408","Excluded")}[ga["verdict"]])
   evidence=ga["satisfied"]+ga["pending"]+ga["exclusion_reasons"]
   status_labels={"met":T("满足","Met"),"likely_met":T("初步满足","Likely met"),"unknown":T("需确认","Needs confirmation"),"needs_confirmation":T("需确认","Needs confirmation"),"potential_conflict":T("潜在冲突","Potential conflict"),"missing":T("登记数据缺失","Registry data missing")}
   status_labels = {
    "met": T("满足", "Met"),
    "likely_met": T("初步满足", "Likely met"),
    "unknown": T("需确认", "Needs confirmation"),
    "needs_confirmation": T("需确认", "Needs confirmation"),
    "potential_conflict": T("潜在冲突", "Potential conflict"),
    "missing": T("信息缺失", "Information missing"),
   }
   evaluations=(ga.get("inclusion_evaluation") or [])+(ga.get("exclusion_evaluation") or [])
   for item in evaluations:
    if isinstance(item,dict):
     evidence.append(f"[{status_labels.get(item.get('status'),item.get('status') or T('需确认','Needs confirmation'))}] {item.get('criterion','')} — {item.get('reason','')}")
   lis="".join(f"<li>{esc(x)}</li>" for x in evidence) or f"<li>{T(chr(38656)+chr(20013)+chr(24515)+chr(22797)+chr(26680)+chr(27491)+chr(24335)+chr(26041)+chr(26696),'Formal protocol review required')}</li>"
   risk_html="".join(f"<li>{esc(x)}</li>" for x in t["risk_context"])
   development_html=development_evidence_html(t,zh)
   trial_url=safe_external_url(t.get("resolved_source_url"))
   reported_phases = [
    str(value).strip() for value in (t.get('phases') or [])
    if str(value).strip().casefold() not in {'', 'n/a', 'na', 'none', 'null', 'unknown'}
   ]
   phase_label = '/'.join(reported_phases) or T('阶段未注明', 'Phase not reported')
   cards.append(f'<span id="{esc(trial_anchor(t.get("id")))}"></span>')
   cards.append(f"""<details class="trial {ga['verdict']}" data-access="{display_access_key}" data-verdict="{esc(ga['verdict'])}"><summary><span class="tier-dot"></span><div class="s-main"><div class="s-title">{esc(t['display_title'])}</div><div class="s-meta"><a class="nct" href="{esc(trial_url)}" target="_blank" rel="noopener">{esc(t.get('id'))}</a><span class="chip chip-ph">{esc(phase_label)}</span><span class="chip chip-mech">{esc(m['label_zh' if zh else 'label_en'])}</span><span class="chip chip-access">{esc(access)}</span><span class="chip chip-verdict">{esc(verdict)}</span></div></div><span class="caret">\u25be</span></summary><div class="body"><p>{esc(t['efficacy_context'])}</p>{development_html}<h4>{T(chr(20837)+chr(25490)+chr(26680)+chr(23545),'Eligibility review')}</h4><ul>{lis}</ul><h4>{T(chr(39118)+chr(38505)+chr(32972)+chr(26223),'Risk context')}</h4><ul>{risk_html}</ul><div class="next"><b>{T(chr(19979)+chr(19968)+chr(27493),'Next step')} \u00b7 </b>{esc(access)}</div></div></details>""")
  item_word=T(chr(39033),"items")
  sections.append(f"""<section class="mechanism-group"><div class="banner {banner_style[category]}"><div class="num">{section_index:02d}</div><div class="bmeta"><div class="beyebrow">Treatment mechanism</div><div class="btitle">{esc(m['label_zh' if zh else 'label_en'])}</div></div><div class="cnt" data-item-word="{esc(item_word)}">{len(items)} {item_word}</div></div><div class="tier-desc">{esc(category_desc[category])}</div>{''.join(cards)}</section>""")
 c=p["counts"]; delta=p.get("portal_delta") or {}; geo=p.get("geography_audit") or {}
 title=T("\u4e3a\u60a8\u7b5b\u9009\u7684\u5168\u7403\u4e34\u5e8a\u8bd5\u9a8c","Global clinical trials selected for you")
 validation_notice="" if p.get("formal_report_ready") else T("\u9a8c\u8bc1\u62a5\u544a\uff1a\u672a\u901a\u8fc7\u5168\u91cf\u5206\u6790\u3001\u5b8c\u6574\u68c0\u7d22\u6216\u95e8\u6237\u589e\u91cf\u8d28\u91cf\u95e8\uff0c\u4e0d\u5f97\u4f5c\u4e3a\u5b8c\u6574\u60a3\u8005\u62a5\u544a\u3002","Validation report: full-analysis, complete-retrieval, or current portal-delta quality gates are not all satisfied; this is not a complete patient report.")
 disclaimer=T("\u672c\u62a5\u544a\u7528\u4e8e\u4fe1\u606f\u5339\u914d\u548c\u9884\u7b5b\uff0c\u4e0d\u6784\u6210\u533b\u5b66\u5efa\u8bae\u6216\u5165\u7ec4\u7ed3\u8bba\u3002\u6240\u6709\u4e2d\u5fc3\u3001\u540d\u989d\u548c\u5165\u6392\u6761\u4ef6\u987b\u7531\u7814\u7a76\u4e2d\u5fc3\u786e\u8ba4\u3002","This report supports matching and pre-screening only. It is not medical advice or an enrollment decision. Sites, slots and eligibility require study-centre confirmation.")
 if not p.get("formal_report_ready"):
  validation_notice=T("验证报告：分析覆盖或结构完整性未通过，不生成患者试验卡片。","Validation report: analysis coverage or structural integrity failed; patient trial cards were not generated.")
 report_warning_messages=[]
 for warning in p.get("report_warnings") or []:
  if isinstance(warning,dict):
   report_warning_messages.append(str(warning.get("message_zh" if zh else "message_en") or warning.get("code") or ""))
 if report_warning_messages:
  disclaimer += " " + " ".join(message for message in report_warning_messages if message)
 css="""
:root{--bg:#fafaf7;--surface:#fff;--alt:#f3f3ef;--border:#d9d8d1;--text:#20211f;--muted:#666963;--brand:#173d5a;--wine:#7b263e;--green:#245c43;--amber:#8e5d0b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 Arial,'Microsoft YaHei',sans-serif}.wrap{max-width:900px;margin:auto;padding:24px 18px 80px}.hero{background:#173d5a;color:white;padding:27px;border-radius:8px}.hero .eyebrow{font-size:11px;text-transform:uppercase;opacity:.8}.hero h1{margin:6px 0 12px;font-size:27px}.snap{display:flex;flex-wrap:wrap;gap:7px}.snap span{border:1px solid #ffffff44;background:#ffffff16;padding:3px 9px;font-size:12px}.disc{margin:13px 0;padding:11px 14px;background:#fff2d9;border-left:4px solid var(--amber)}.overview{background:white;border:1px solid var(--border);padding:16px;margin:12px 0}.ov-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.ov-cell{background:var(--alt);padding:10px;text-align:center}.ov-cell b{display:block;color:var(--brand);font-size:23px}.filters{display:flex;gap:7px;flex-wrap:wrap;margin:14px 0}.filters button{border:1px solid var(--border);background:white;padding:6px 12px;cursor:pointer}.filters button.on{background:var(--brand);color:white}.banner{display:flex;align-items:center;gap:13px;color:white;padding:14px 16px;border-radius:7px;margin:25px 0 8px}.num{border:1px solid #ffffff88;border-radius:50%;width:40px;height:40px;display:grid;place-items:center}.bmeta{flex:1}.beyebrow{font-size:10px;text-transform:uppercase;opacity:.8}.btitle{font-size:18px;font-weight:700}.cnt{font-size:12px}.b-target{background:#74243d}.b-pathway{background:#183f5d}.b-immune{background:#315e50}.b-cell{background:#5c4072}.b-basket{background:#73552d}.b-marker{background:#526472}.b-other{background:#62625e}.tier-desc{font-size:12px;color:var(--muted);margin:0 2px 9px}details{background:white;border:1px solid var(--border);border-left:4px solid var(--amber);margin:7px 0}details.match{border-left-color:var(--green)}details.exclude{border-left-color:#888}summary{display:flex;gap:10px;padding:12px;cursor:pointer;list-style:none}.tier-dot{width:8px;height:8px;border-radius:50%;background:var(--amber);margin-top:7px}.match .tier-dot{background:var(--green)}.exclude .tier-dot{background:#888}.s-main{flex:1;min-width:0}.s-title{font-weight:700;overflow-wrap:anywhere}.s-meta{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}.chip,.nct{font-size:10.5px;padding:2px 7px;background:var(--alt);border-radius:5px}.nct{color:var(--brand);padding-left:0;background:none}.body{padding:0 16px 14px 31px}.body h4{font-size:11px;text-transform:uppercase;margin:10px 0 3px}.body ul{margin:0 0 5px;padding-left:19px}.evidence-list li{margin-bottom:9px}.evidence-list div{color:var(--muted);font-size:12px}.next{background:var(--alt);border-left:3px solid var(--wine);padding:8px 10px;margin-top:9px}.is-hidden{display:none!important}footer{margin-top:32px;border-top:1px solid var(--border);padding-top:12px;font-size:11px;color:var(--muted)}@media(max-width:650px){.wrap{padding:12px}.ov-grid{grid-template-columns:1fr 1fr}}
"""
 buttons=[("all",T("\u5168\u90e8","All"))]+access_defs
 filters="".join(f'<button class="{"on" if key=="all" else ""}" data-filter="{key}">{esc(label)}</button>' for key,label in buttons)
 overview=[(len(trials),T("\u5019\u9009\u8bd5\u9a8c","Candidates")),(geo.get("domestic_named",0)+geo.get("domestic_registry",0),T("\u56fd\u5185\u53ef\u53ca","In-country access")),(geo.get("country_unverified",0),T("\u56fd\u5bb6\u8bb0\u5f55\u5f85\u6838\u5b9e","Country unverified")),(geo.get("overseas",0),T("\u5883\u5916","Overseas"))]
 ov="".join(f'<div class="ov-cell"><b>{n}</b><span>{esc(label)}</span></div>' for n,label in overview)
 manifest=p.get("run_manifest") or {}; mc=manifest.get("counts") or {}
 manifest_title=T("\u5168\u6d41\u7a0b\u8fd0\u884c\u6e05\u5355","Full-run manifest")
 manifest_rows=[
  (T("\u53ec\u56de","Recall"),mc.get("recall",0)),
  (T("\u786c\u89c4\u5219\u6392\u9664","Hard exclusions"),mc.get("hard_excluded",0)),
  ("Gater",mc.get("gater_completed",0)),
  (T("\u6df1\u5ea6\u5206\u6790","Deep analysis"),mc.get("deep_completed",0)),
  (T("\u9057\u6f0f","Omitted"),mc.get("omitted",0))]
 prepared_hash_label=T("检索数据 SHA-256","Prepared data SHA-256")
 analysis_hash_label=T("分析结果 SHA-256","Analysis result SHA-256")
 manifest_html=f'<section class="overview"><h3>{esc(manifest_title)}</h3><div class="ov-grid">{"".join(f"<div class=ov-cell><b>{esc(value)}</b><span>{esc(label)}</span></div>" for label,value in manifest_rows)}</div><p class="manifest-hash">{esc(prepared_hash_label)}: {esc(manifest.get("prepared_sha256"))}<br>{esc(analysis_hash_label)}: {esc(manifest.get("analysis_sha256"))}</p></section>'
 patient_summary=patient_summary_html(p,zh)
 decision_html=decision_report_html(p,zh)
 script="""function applyFilter(f){document.querySelectorAll('.mechanism-group').forEach(g=>{g.querySelectorAll('details.trial').forEach(x=>{const hide=f!=='all'&&x.dataset.access!==f;x.classList.toggle('is-hidden',hide);});const n=g.querySelectorAll('details.trial:not(.is-hidden)').length;g.classList.toggle('is-hidden',n===0);const c=g.querySelector('.cnt');c.textContent=n+' '+c.dataset.itemWord;});}document.querySelectorAll('.filters button').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('.filters button').forEach(x=>x.classList.remove('on'));b.classList.add('on');applyFilter(b.dataset.filter);}));applyFilter('all');"""
 eyebrow=T(chr(20020)+chr(24202)+chr(35797)+chr(39564)+chr(21305)+chr(37197)+chr(25253)+chr(21578)+" · "+chr(24739)+chr(32773)+chr(29256),"Clinical trial matching report · Patient edition")
 database_label=T(chr(25968)+chr(25454)+chr(24211)+chr(26356)+chr(26032)+chr(26102)+chr(38388),"Database as of")
 schema_label=T("MCP 数据结构版本","MCP schema")
 delta_label=T("WHO 门户增量","WHO portal delta")
 trial_word=T("项试验","trial(s)")
 doc=f"""<!doctype html><html lang="{p['language']}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} \u00b7 {esc(patient.get('patient_id'))}</title><style>{css}</style></head><body><main class="wrap"><header class="hero"><div class="eyebrow">{eyebrow}</div><h1>{esc(title)}</h1><div class="snap"><span>{esc(patient.get('patient_id'))}</span><span>{esc(patient.get('cancer_type'))} \u00b7 {esc(patient.get('stage'))}</span><span>{esc(' / '.join(patient.get('mutations') or []))}</span><span>{esc(loc)}</span></div></header><div class="disc">{esc(disclaimer)}</div>{f'<div class="disc">{esc(validation_notice)}</div>' if validation_notice else ''}<section class="overview"><div class="ov-grid">{ov}</div></section>{manifest_html}{patient_summary}{decision_html}<div class="filters">{filters}</div>{''.join(sections)}<footer>{database_label}: {esc(p['database_as_of'])} \u00b7 {esc(schema_label)} {esc(p['database_metadata'].get('schema_version'))} \u00b7 {esc(delta_label)}: {esc(delta.get('status'))}, {esc(delta.get('returned') if delta.get('returned') is not None else 0)} {esc(trial_word)}</footer></main><script>{script}</script></body></html>"""
 path.write_text(doc,encoding="utf-8")


def render_validation_html(payload: dict[str, Any], path: Path) -> None:
 """Render a conspicuous audit artifact without patient-facing trial cards."""
 zh = payload.get("language") == "zh-CN"
 manifest = payload.get("run_manifest") or {}
 counts = manifest.get("counts") or {}
 gates = manifest.get("quality_gates") or {}
 coverage = manifest.get("coverage_audit") or {}
 title = "流程验证未通过" if zh else "Formal workflow validation failed"
 message = (
  "该产物不是正式患者报告。质量门未全部通过，禁止将其改名或作为完整匹配结果交付。"
  if zh else
  "This artifact is not a formal patient report. Quality gates failed; do not rename or deliver it as a complete matching result."
 )
 labels = {
  "recall": "召回数" if zh else "Recall",
  "hard_excluded": "硬排除数" if zh else "Hard exclusions",
  "gater_completed": "Gater完成数" if zh else "Gater completed",
  "deep_completed": "深度分析数" if zh else "Deep analysis completed",
  "risk_completed": "风险分析数" if zh else "Risk completed",
  "efficacy_completed": "疗效分析数" if zh else "Efficacy completed",
  "evidence_completed": "论文证据数" if zh else "Evidence completed",
  "omitted": "遗漏数" if zh else "Omitted",
 }
 count_rows = "".join(
  f"<tr><th>{esc(labels[key])}</th><td>{esc(counts.get(key, 0))}</td></tr>"
  for key in labels
 )
 gate_rows = "".join(
  f"<tr><th>{esc(key)}</th><td class=\"{'ok' if value else 'bad'}\">"
  f"{'PASS' if value else 'FAIL'}</td></tr>"
  for key, value in gates.items()
 )
 missing = {
  "missing_disposition_ids": coverage.get("missing_disposition_ids") or [],
  "missing_risk_ids": coverage.get("missing_risk_ids") or [],
  "missing_efficacy_ids": coverage.get("missing_efficacy_ids") or [],
  "missing_evidence_ids": coverage.get("missing_evidence_ids") or [],
 }
 missing_rows = "".join(
  f"<tr><th>{esc(key)}</th><td>{esc(', '.join(values[:50]))}</td></tr>"
  for key, values in missing.items() if values
 ) or f"<tr><td colspan=\"2\">{esc('无集合级遗漏' if zh else 'No set-level omissions')}</td></tr>"
 document = f"""<!doctype html>
<html lang="{'zh-CN' if zh else 'en'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
body{{margin:0;background:#fff7ed;color:#28170c;font:15px/1.55 Arial,sans-serif}}
main{{max-width:860px;margin:40px auto;padding:0 20px}}
header{{background:#9a3412;color:#fff;padding:24px;border:6px solid #431407}}
h1{{margin:0 0 8px;font-size:28px}}section{{background:#fff;border:2px solid #fdba74;margin-top:18px;padding:18px}}
table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;border-bottom:1px solid #fed7aa;padding:8px}}
.ok{{color:#166534;font-weight:700}}.bad{{color:#b91c1c;font-weight:700}}
code{{overflow-wrap:anywhere}}footer{{margin-top:20px;font-size:12px;color:#7c2d12}}
</style></head><body><main><header><h1>{esc(title)}</h1><p>{esc(message)}</p></header>
<section><h2>{esc('运行清单' if zh else 'Run manifest')}</h2><table>{count_rows}</table></section>
<section><h2>{esc('质量门' if zh else 'Quality gates')}</h2><table>{gate_rows}</table></section>
<section><h2>{esc('缺失集合' if zh else 'Missing sets')}</h2><table>{missing_rows}</table></section>
<footer>prepared SHA-256: <code>{esc(manifest.get('prepared_sha256'))}</code><br>
analysis SHA-256: <code>{esc(manifest.get('analysis_sha256'))}</code></footer>
</main></body></html>"""
 path.write_text(document, encoding="utf-8")
