"""Deterministic flat treatment-mechanism grouping for patient reports."""
from __future__ import annotations
import re
from typing import Any

TAXONOMY_VERSION="flat-mechanism-v1"
CATEGORY_ORDER=["targeted_therapy","ras_mapk_pathway","immune_combination","cell_and_biologic","solid_tumor_basket","core_marker_screening","other"]
CATEGORY_LABELS={
 "targeted_therapy":"\u9776\u70b9\u6cbb\u7597",
 "ras_mapk_pathway":"\u6cdb\u901a\u8def\u6cbb\u7597",
 "immune_combination":"\u514d\u75ab\u6cbb\u7597",
 "cell_and_biologic":"\u7ec6\u80de\u4e0e\u751f\u7269\u6cbb\u7597",
 "solid_tumor_basket":"\u6cdb\u5b9e\u4f53\u7624\u4e0e\u7bee\u5b50\u7814\u7a76",
 "core_marker_screening":"\u751f\u7269\u6807\u5fd7\u7269\u5f85\u786e\u8ba4",
 "other":"\u5176\u4ed6\u6cbb\u7597\u4e0e\u7814\u7a76\u65b9\u5411"}
CATEGORY_LABELS_EN={
 "targeted_therapy":"Targeted therapy","ras_mapk_pathway":"Pathway therapy",
 "immune_combination":"Immunotherapy","cell_and_biologic":"Cell and biologic therapy",
 "solid_tumor_basket":"Solid-tumor and basket studies",
 "core_marker_screening":"Biomarker confirmation required","other":"Other treatment and research approaches"}

def _flatten(value:Any)->list[str]:
 if value is None:return []
 if isinstance(value,dict):return [x for item in value.values() for x in _flatten(item)]
 if isinstance(value,(list,tuple,set)):return [x for item in value for x in _flatten(item)]
 return [str(value)]

def trial_text(trial:dict[str,Any],analysis:dict[str,Any]|None=None)->str:
 # Retrieval-branch labels are excluded: they describe recall, not the administered treatment.
 # Gating/eligibility narratives are excluded: a treatment mentioned only in an exclusion criterion must not determine the mechanism.
 # Only an explicit administered-mechanism list may supplement registry fields.
 # Full efficacy/risk prose can mention comparator therapies and must not classify the trial.
 risk=(analysis or {}).get("risk_annotation") if isinstance(analysis,dict) else None
 mechanisms=(risk or {}).get("trial_mechanisms_identified") if isinstance(risk,dict) else None
 efficacy=(analysis or {}).get("efficacy_context") if isinstance(analysis,dict) else None
 efficacy_summary=(efficacy or {}).get("summary") if isinstance(efficacy,dict) else ""
 explicit_summary=(
  efficacy_summary if "administered intervention" in str(efficacy_summary).casefold() else ""
 )
 values=[trial.get("title"),trial.get("scientific_title"),trial.get("interventions"),trial.get("intervention_summary"),mechanisms or [],explicit_summary]
 return re.sub(r"\s+"," "," ".join(_flatten(values))).casefold()

def classify_mechanism(trial:dict[str,Any],analysis:dict[str,Any]|None=None,patient:dict[str,Any]|None=None)->dict[str,Any]:
 text=trial_text(trial,analysis); has=lambda *terms:any(term.casefold() in text for term in terms)
 core_marker=has("hla","cea-positive","cea positive","cldn","claudin","her2 expression","msi-h","dmmr","antigen expression","biomarker selected")
 cell=has("car-t","cart ","car.bite","car-nk","allogeneic γδ t cell","allogeneic gamma delta t cell","tcr-t","tcr t","tumor infiltrating lymphocyte","tumor-infiltrating t cell","til therapy","dc-cik","cell therapy","cell vaccine","cancer vaccine","tumor vaccine","dendritic cell","neoantigen vaccine","neo-antigen vaccine","oncolytic virus","oncolytic vaccine","vaccine","obi-833") or bool(re.search(r"\bcar[\s-]+t\b",text))
 exact_target=has("kras g12c","kras g12d","braf v600","egfr exon","alk rearrangement","sotorasib","adagrasib","divarasib","furmonertinib","lazertinib","calderasib","mk-1084","rmc-6291","glecirasib","fulzerasib","garsorasib","cetuximab","panitumumab","erlotinib","afatinib","osimertinib","amivantamab","trastuzumab","deruxtecan","anti-her2","anti-egfr","targeted therapy")
 pathway=has("pan-kras","pan kras","pan-ras","ras(on)","ras on","mapk","shp2","sos1","mek inhibitor","kras inhibitor","ras inhibitor","kras mutation","ras mutation","kras-mutated","ras-mutated")
 immune=has("pd-1","pd-l1","ctla-4","lag-3","checkpoint inhibitor","immunotherapy","bispecific antibody")
 basket=has("solid tumor","solid tumors","basket trial","all solid malignancies","advanced malignancies")
 if cell:category="cell_and_biologic"
 elif exact_target:category="targeted_therapy"
 elif pathway:category="ras_mapk_pathway"
 elif immune:category="immune_combination"
 elif basket:category="solid_tumor_basket"
 elif core_marker:category="core_marker_screening"
 else:category="other"
 return {"category":category,"label":CATEGORY_LABELS[category],"label_zh":CATEGORY_LABELS[category],"label_en":CATEGORY_LABELS_EN[category],"taxonomy_version":TAXONOMY_VERSION,"requires_marker_confirmation":core_marker,"classification_basis":"deterministic_flat_keyword_rules"}

def annotate_trials(trials:list[dict[str,Any]],patient:dict[str,Any]|None=None)->list[dict[str,Any]]:
 for trial in trials:trial["mechanism_category"]=classify_mechanism(trial,patient=patient)
 return trials
