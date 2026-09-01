#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""의약품 심의자료 검증기 (Streamlit v6.2)
- 사용자 원본(user_template.py)의 MFDS_LIST_URL = getDrugPrdtPrmsnInq07 흐름 그대로 port
- itemName 파라미터로 1차 검색 → 응답 items 전체를 클라이언트에서 normalize substring 필터
- 검색 결과는 화면에 후보 전체를 st.multiselect로 노출 (잘못된 매칭을 사용자가 직접 걸러낼 수 있게)
- 허가 상세 + 약가 + 비교표 검증까지 한 화면에서 처리 (CSV는 옵션 다운로드)
- 모든 키 처리는 with st.sidebar: 안에서만 수행
- 모듈 레벨 차단 호출 (argparse / sys.argv / getpass / input) 0건
"""
from __future__ import annotations
import csv
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

import streamlit as st

# ───────── 1. 상수 ─────────
MFDS_LIST = os.getenv(
    "MFDS_LIST_URL",
    "http://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnInq07",
)
MFDS_DETAIL = os.getenv(
    "MFDS_DETAIL_URL",
    "https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnDtlInq06",
)
HIRA_URL = os.getenv(
    "HIRA_PRICE_URL",
    "https://apis.data.go.kr/B551182/dgamtCrtrInfoService1.2/getDgamtList",
)

PERMIT_FIELDS = [
    "의약품명", "성분명", "제조판매사", "함량", "제형",
    "적응증", "용법용량", "소아", "보관", "금기",
    "BAR", "허가일자", "ITEM_SEQ",
]
PRICE_FIELDS = ["의약품명", "약가", "적용시작일", "적용종료일", "mdsCd", "제조판매사"]

# ───────── 2. 정규화 (querystring과 ITEM_NAME 양쪽에 동일 적용) ─────────
def _normalize(t):
    """공백·괄호·중점·특수기호 제거 후 lower() — substring 매칭용"""
    if not t:
        return ""
    s = re.sub(r"\s+", "", str(t))
    s = re.sub(r"[\(\)\[\]\{\}_:·,\.·\-]", "", s)
    return s.lower()


def _amount(name):
    m = re.search(
        r"\d+(?:\.\d+)?\s*(?:mg|g|mcg|μg|㎍|IU|mEq|mL|%)\s*(?:/\s*\d+(?:\.\d+)?\s*(?:mg|mL))?",
        name or "",
    )
    return m.group(0).strip() if m else ""


_FORM_MAP = [
    ("서방정", ["SR", "CR", "XR", "ER", "서방"]),
    ("캡슐", ["Cap", "Capsule", "캡슐"]),
    ("주사", ["Inj", "Injection", "주사"]),
    ("바이알", ["vial", "Vial"]),
    ("펜", ["pen", "Pen"]),
    ("현탁액", ["Susp", "susp"]),
    ("점안액", ["Ophth", "eye"]),
    ("정", ["Tab", "Tablet", "정"]),
    ("설하정", ["설하"]),
]


def _form(name):
    for ko, syns in _FORM_MAP:
        for s in syns:
            if s.lower() in (name or "").lower():
                return ko
    return ""


def _section(nbdoc, anchor_keys):
    if not nbdoc:
        return ""
    txt = re.sub(r"<[^>]+>", " ", nbdoc)
    txt = re.sub(r"\s+", " ", txt)
    for k in anchor_keys:
        m = re.search(re.escape(k) + r"([\s\S]{0,500}?)(?=\d+\.\s|※|\Z)", txt)
        if m:
            return m.group(1).strip()
    return ""


# ───────── 3. 캐시 ─────────
_CACHE_DIR = Path(__file__).resolve().parent / "cache"


def _cache_path(key):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def cache_get(key, ttl_days=7):
    p = _cache_path(key)
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > ttl_days * 86400:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def cache_put(key, data):
    _cache_path(key).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ───────── 4. HTTP 클라이언트 (JSON → failed XML fallback) ─────────
def http_json(url, params, timeout=30):
    key = params.get("serviceKey")
    body_params = {k: v for k, v in params.items() if k != "serviceKey" and v is not None}
    qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in body_params.items())
    full = url + ("?" + qs if qs else "")
    if key:
        sep = "&" if qs else "?"
        full += f"{sep}serviceKey={urllib.parse.quote(str(key), safe='')}"
    try:
        with urllib.request.urlopen(full, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="ignore")
        try:
            return json.loads(raw)
        except Exception:
            return {"_xml": raw}
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode("utf-8", errors="ignore")}
    except Exception as e:
        return {"_net_error": str(e)}


# ───────── 5. MFDS LIST 검색 — 원본 흐름 그대로 + 정규화 substring 후필터 ─────────
def search_mfds(query, key):
    """원본과 동일하게 itemName 파라미터로 호출 → 응답 items 전체를 정규화 substring로 필터"""
    q_norm = _normalize(query)
    cached = cache_get(f"mfds_search:{query}")
    data = cached
    if data is None and key:
        params = {"itemName": query, "numOfRows": 100, "pageNo": 1, "type": "json"}
        data = http_json(MFDS_LIST, {**params, "serviceKey": key}) or {}
        cache_put(f"mfds_search:{query}", data)
    items = (data.get("body") or {}).get("items") if isinstance(data, dict) else None
    if isinstance(items, dict):
        items = [items]
    items = items or []
    if not items or not q_norm:
        return items
    # ★ 사용자 원본엔 없는 강제 후필터: normalize(query) ⊆ normalize(ITEM_NAME)
    filtered = [
        r for r in items if q_norm in _normalize(r.get("ITEM_NAME", ""))
    ]
    return filtered


def fetch_detail(item_seq, key):
    cached = cache_get(f"mfds_detail:{item_seq}")
    if cached is not None:
        return cached
    if not key:
        return {}
    try:
        params = {"itemSeq": item_seq, "type": "json"}
        data = http_json(MFDS_DETAIL, {**params, "serviceKey": key}) or {}
        cache_put(f"mfds_detail:{item_seq}", data)
        return data
    except Exception:
        return {}


def fetch_price(item_name, key):
    cached = cache_get(f"hira:{item_name}")
    if cached is not None:
        return cached
    if not key:
        return {}
    try:
        params = {"itemName": item_name, "numOfRows": 30, "pageNo": 1, "type": "json"}
        data = http_json(HIRA_URL, {**params, "serviceKey": key}) or {}
        cache_put(f"hira:{item_name}", data)
        return data
    except Exception:
        return {}


# ───────── 6. 데모 DB (원본에 있던 것과 같은 흐름, 검색 후필터까지 적용) ─────────
# 등록 의약품 7종 — 전체 식약처 DB 일반화 매칭 검증용
_MOCK_LIST = [
    {"ITEM_SEQ": "MOCK_LYR",     "ITEM_NAME": "리리카캡슐75밀리그램(프레가발린)",
     "ENTP_NAME": "비아트리스코리아(주)", "ITEM_PERMIT_DATE": "2010-03-15",
     "CANCEL_NAME": "정상"},
    {"ITEM_SEQ": "MOCK_LYRSR",   "ITEM_NAME": "리리카CR서방정330밀리그램(프레가발린)",
     "ENTP_NAME": "비아트리스코리아(주)", "ITEM_PERMIT_DATE": "2014-09-20",
     "CANCEL_NAME": "정상"},
    {"ITEM_SEQ": "MOCK_NARC",    "ITEM_NAME": "나르코정(나프록센나트륨)",
     "ENTP_NAME": "한독소비(주)", "ITEM_PERMIT_DATE": "2008-05-10",
     "CANCEL_NAME": "정상"},
    {"ITEM_SEQ": "MOCK_NARC_SUB","ITEM_NAME": "나르코설하정5밀리그램(나파모스틴)",
     "ENTP_NAME": "한독소비(주)", "ITEM_PERMIT_DATE": "2015-11-01",
     "CANCEL_NAME": "정상"},
    {"ITEM_SEQ": "MOCK_DIC",     "ITEM_NAME": "디카맥스D정(Calcium carbonate + Cholecalciferol)",
     "ENTP_NAME": "동아제약(주)", "ITEM_PERMIT_DATE": "2018-07-10",
     "CANCEL_NAME": "정상"},
    {"ITEM_SEQ": "MOCK_TYL",     "ITEM_NAME": "타이레놀정500밀리그램",
     "ENTP_NAME": "한국존슨앤드존슨(주)", "ITEM_PERMIT_DATE": "2000-01-01",
     "CANCEL_NAME": "정상"},
    {"ITEM_SEQ": "MOCK_LUT",     "ITEM_NAME": "러츠날캡슐100밀리그램(루피나미드)",
     "ENTP_NAME": "한국에자이(주)", "ITEM_PERMIT_DATE": "2012-04-12",
     "CANCEL_NAME": "정상"},
]

_MOCK_PRICE_BY_NAME = {
    "리리카캡슐75밀리그램":      {"itmNm": "리리카캡슐75밀리그램", "mnfEntpNm": "비아트리스코리아(주)",
                                 "mxCprc": "523",  "adtStaDd": "2024-01-01", "sellEptDd": "", "mdsCd": "MOCK_LYR_C75"},
    "리리카CR서방정330밀리그램":  {"itmNm": "리리카CR서방정330밀리그램", "mnfEntpNm": "비아트리스코리아(주)",
                                 "mxCprc": "1399", "adtStaDd": "2024-04-01", "sellEptDd": "", "mdsCd": "MOCK_LYR_C330"},
    "나르코정":                  {"itmNm": "나르코정", "mnfEntpNm": "한독소비(주)",
                                 "mxCprc": "350",  "adtStaDd": "2024-01-01", "sellEptDd": "", "mdsCd": "MOCK_NARC_T"},
    "나르코설하정5밀리그램":      {"itmNm": "나르코설하정5밀리그램", "mnfEntpNm": "한독소비(주)",
                                 "mxCprc": "680",  "adtStaDd": "2024-06-01", "sellEptDd": "", "mdsCd": "MOCK_NARC_S"},
    "디카맥스D정":               {"itmNm": "디카맥스D정", "mnfEntpNm": "동아제약(주)",
                                 "mxCprc": "70",   "adtStaDd": "2024-07-01", "sellEptDd": "", "mdsCd": "MOCK_DIC_D"},
    "타이레놀정500밀리그램":      {"itmNm": "타이레놀정500밀리그램", "mnfEntpNm": "한국존슨앤드존슨(주)",
                                 "mxCprc": "120",  "adtStaDd": "2024-01-01", "sellEptDd": "", "mdsCd": "MOCK_TYL_500"},
    "러츠날캡슐100밀리그램":      {"itmNm": "러츠날캡슐100밀리그램", "mnfEntpNm": "한국에자이(주)",
                                 "mxCprc": "2100", "adtStaDd": "2024-01-01", "sellEptDd": "", "mdsCd": "MOCK_LUT_100"},
}


def mock_search_mfds(query):
    q = _normalize(query)
    return [r for r in _MOCK_LIST if q in _normalize(r["ITEM_NAME"])]


def mock_fetch_detail(item_seq):
    row = next((r for r in _MOCK_LIST if r["ITEM_SEQ"] == item_seq), None)
    if not row:
        return {}
    nb_base = (
        "1. 다음 환자에게는 투여하지 말 것: 이 약의 성분에 과민증 환자에 대한 금기. "
        "2. 소아에 대한 투여: 소아(만 12세 미만)에 대한 안전성·유효성은 확립되어 있지 않다. "
        "3. 임부에 대한 투여: 임부 또는 임신하고 있을 가능성이 있는 여성에는 투여하지 말 것."
    )
    spec = {
        "MOCK_LYR":  ("프레가발린", "말초성 신경병증 통증, 섬유근육통, 부분발작 보조요법 (성인)",
                      "초기 1일 150mg, 3-7일에 걸쳐 최대 600mg까지 증량. 1일 2회 분할.", "9011"),
        "MOCK_LYRSR":("프레가발린", "말초성 신경병증 통증, 섬유근육통",
                      "초기 1일 165mg, 최대 660mg. 1일 2회.", "9012"),
        "MOCK_NARC": ("나프록센나트륨", "골관절염, 류마티스관절염, 통증, 발열",
                      "성인 1회 250~500mg, 1일 2회 (식후).", "9013"),
        "MOCK_NARC_SUB": ("나파모스틴", "구내염 및 구인두 통증 완화",
                         "성인 1회 5mg, 1일 4회까지 (혀 아래 두고 녹임).", "9014"),
        "MOCK_DIC":  ("칼슘카보네이트+콜레칼시페롤", "칼슘 및 비타민 D3 보급",
                      "성인 1일 1회, 1정 (식후).", "9015"),
        "MOCK_TYL":  ("아세트아미노펜", "발열, 두통, 관절통 등 경증 통증",
                      "성인 1회 300~500mg, 1일 3~4회.", "9016"),
        "MOCK_LUT":  ("루피나미드", "부분발작 보조요법 (성인)",
                      "초기 1일 200mg, 2주에 걸쳐 400mg까지 증량. 1일 2회 분할.", "9017"),
    }.get(item_seq)
    if not spec:
        return {}
    ingr, ee, ud, bar = spec
    return {
        "MAIN_ITEM_INGR": ingr,
        "EE_DOC_DATA": f"<p>{ee}</p>",
        "UD_DOC_DATA": f"<p>{ud}</p>",
        "NB_DOC_DATA": f"<p>{nb_base}</p>",
        "STORAGE_METHOD": "기밀용기, 실온(1~30℃) 보관",
        "BAR_CODE": f"880654300{bar}",
        "ITEM_PERMIT_DATE": row.get("ITEM_PERMIT_DATE", ""),
        "ITEM_NAME": row["ITEM_NAME"],
        "ENTP_NAME": row["ENTP_NAME"],
    }


def mock_fetch_price(item_name):
    pr = _MOCK_PRICE_BY_NAME.get(item_name)
    if not pr:
        return {"response": {"body": {"items": []}}}
    return {"response": {"body": {"items": [pr]}}}


# ───────── 7. 1품목 통합 조회 (허가 상세 + 약가) ─────────
def fetch_one_product(item_seq, item_name, entp_name, key, demo):
    """return dict with all PERMIT_FIELDS + PRICE_FIELDS values"""
    if demo:
        det = mock_fetch_detail(item_seq)
        pr = mock_fetch_price(item_name)
    else:
        det = fetch_detail(item_seq, key) if key else {}
        pr = fetch_price(item_name, key) if key else {}

    items = (((pr.get("response") or {}).get("body") or {}).get("items")) or []
    if isinstance(items, dict):
        items = [items]
    current = None
    ref = time.strftime("%Y%m%d")
    for it in items:
        ad = str(it.get("adtStaDd", "")).replace("-", "")
        se = str(it.get("sellEptDd", "")).replace("-", "")
        if ad and ad > ref:
            continue
        if se and se < ref:
            continue
        if not current or ad > str(current.get("adtStaDd", "")):
            current = it

    ee = det.get("EE_DOC_DATA", "") or ""
    ud = det.get("UD_DOC_DATA", "") or ""
    nb = det.get("NB_DOC_DATA", "") or ""
    ee_clean = re.sub(r"<[^>]+>", " ", ee)
    ud_clean = re.sub(r"<[^>]+>", " ", ud)

    return {
        "의약품명": det.get("ITEM_NAME") or item_name,
        "성분명": det.get("MAIN_ITEM_INGR", ""),
        "제조판매사": det.get("ENTP_NAME") or entp_name,
        "함량": _amount(det.get("ITEM_NAME", "")),
        "제형": _form(det.get("ITEM_NAME", "")),
        "적응증": re.sub(r"\s+", " ", ee_clean).strip(),
        "용법용량": re.sub(r"\s+", " ", ud_clean).strip(),
        "소아": _section(nb, ["소아에 대한 투여"]),
        "보관": det.get("STORAGE_METHOD", ""),
        "금기": _section(nb, ["투여하지 말 것", "금기"]),
        "BAR": det.get("BAR_CODE", ""),
        "허가일자": det.get("ITEM_PERMIT_DATE", ""),
        "ITEM_SEQ": item_seq,
        "약가": current.get("mxCprc", "") if current else "",
        "약가적용시작일": current.get("adtStaDd", "") if current else "",
        "약가적용종료일": current.get("sellEptDd", "") if current else "",
        "mdsCd": current.get("mdsCd", "") if current else "",
    }


# ───────── 8. 비교표 파서 (사용자 붙여넣은 텍스트) ─────────
def parse_compare(text):
    """빈 줄 = 제품 구분, 한 줄 = '필드명|값'
    return: list[dict]"""
    if not text.strip():
        return []
    items, current = [], {}
    for raw in text.split("\n"):
        ln = raw.strip()
        if not ln:
            if current:
                items.append(current)
                current = {}
            continue
        if "|" in ln and re.match(r"^[가-힣A-Za-z]", ln):
            k, v = ln.split("|", 1)
            k, v = k.strip(), v.strip()
            if k == "의약품명" and current:
                items.append(current)
                current = {}
            current[k] = v
        else:
            current["비고"] = (current.get("비고", "") + " " + ln).strip()
    if current:
        items.append(current)
    return items


# ───────── 9. 비교 규칙 (공백·괄호 제거 후 단순 일치 / 일부 substring) ─────────
SEMANTIC_FIELDS = ["적응증", "용법용량", "소아", "금기"]


def compare_one(field, table_val, src_val):
    tv = (table_val or "").strip()
    sv = (src_val or "").strip()
    if not tv and not sv:
        return "⚪ 확인 불가", "양쪽 모두 비어있음"
    if not tv:
        return "🔴 수정 필요", "비교표에 기재 누락"
    if not sv:
        return "⚪ 확인 불가", "원문(API)에 해당 항목 비어있음"
    if field in ("약가", "함량"):
        tn = re.sub(r"[^\d.]", "", tv)
        sn = re.sub(r"[^\d.]", "", sv)
        if not tn or not sn:
            return "⚪ 확인 불가", "수치 파싱 실패"
        return ("🟢 일치" if tn == sn else "🔴 수정 필요"), f"비교표={tn}, 원문={sn}"
    if field in SEMANTIC_FIELDS:
        return "🟠 의미 단위 — LLM 확인 필요", "사용자가 🤖 복사 후 LLM에 위임"
    tn, sn = _normalize(tv), _normalize(sv)
    if tn == sn:
        return "🟢 일치", "정규화 일치"
    if tn in sn or sn in tn:
        return "🟡 확인 필요", "정규화 부분 일치 (표현 차이 가능)"
    return "🔴 수정 필요", f"정규화 불일치: [{tv}] vs [{sv}]"


# ───────── 10. Streamlit UI (사이드바 단일 진입점) ─────────
st.set_page_config(page_title="의약품 심의자료 검증기", page_icon="💊", layout="wide")
st.title("💊 의약품 심의자료 검증기")
st.caption(
    "식약처·심평원 OpenAPI로 허가/약가를 조회하고, 사용자가 붙여준 비교표와 한 화면에서 검증. "
    "키는 사이드바에서 한 번 입력. 비워두면 데모 DB(7종)로 동작."
)


def _resolve_initial_key():
    try:
        v = st.secrets.get("DATA_GO_KR_KEY") or st.secrets.get("MFDS_SERVICE_KEY")
        if v:
            return str(v).strip()
    except Exception:
        pass
    return ""


with st.sidebar:
    st.header("🔑 인증키")
    init_key = _resolve_initial_key()
    mfds_key = st.text_input("식약처 인증키 (선택)", value=init_key, type="password",
                             help="data.go.kr 에서 발급받은 본인 키. 비워두면 데모(7종) 동작.",
                             key="mfds_key_input")
    st.caption("키는 세션 메모리에만 보관. 외부 서버로 전송되지 않습니다.")

    st.divider()
    st.header("📅 옵션")
    ref_date_in = st.date_input("약가 기준일", value=time.strftime("%Y-%m-%d"),
                                key="ref_date_input")

key = (mfds_key or "").strip() or None
demo_mode = not bool(key)


# ───── 본문 좌측: 검색 + 다중선택 ─────
st.subheader("1️⃣ 의약품 검색")
query_input = st.text_input(
    "의약품명 일부 입력 (예: 나르코, 나르코설하정, 리리카)",
    placeholder="예: 나르코",
    key="query_input",
)

if st.button("🔍 검색", type="secondary", use_container_width=False) or query_input:
    if not query_input.strip():
        st.info("💡 의약품명 일부를 입력하면 검색됩니다.")
        st.stop()
    if demo_mode:
        candidates = mock_search_mfds(query_input)
    else:
        candidates = search_mfds(query_input, key)
    if not candidates:
        st.warning(f"'{query_input}' 검색 결과 0건입니다. 다른 키워드로 시도해 보세요.")
        st.stop()
    # 중복 ITEM_SEQ 제거 후 multiselect 표시
    seen = set()
    deduped = []
    for r in candidates:
        seq = r.get("ITEM_SEQ", "")
        if seq and seq not in seen:
            seen.add(seq)
            deduped.append(r)
    st.success(f"🔎 '{query_input}' 검색 결과 {len(deduped)}건 (전체 후보 표시)")
    by_seq = {r["ITEM_SEQ"]: r for r in deduped}
    labels = []
    for seq, r in by_seq.items():
        labels.append(f"{r.get('ITEM_NAME','')} | {r.get('ENTP_NAME','')} | ITEM_SEQ={seq}")
    chosen = st.multiselect(
        "비교할 품목을 모두 선택하세요 (복수 선택 가능)",
        options=list(by_seq.keys()),
        default=list(by_seq.keys())[:1],
        format_func=lambda s: next(
            (f"{by_seq[s].get('ITEM_NAME','')} | {by_seq[s].get('ENTP_NAME','')}")
            for _ in [0] if s in by_seq
        ),
        key="chosen_multiselect",
    )

    # ───── 본문 우측: 비교표 붙여넣기 ─────
    colL, colR = st.columns([1, 1])
    with colR:
        st.subheader("2️⃣ 비교표 (선택 — 빈 줄로 제품 구분)")
        cmp_default = (
            "의약품명|리리카CR서방정330밀리그램\n"
            "함량|330mg\n"
            "제형|서방정\n"
            "적응증|말초성 신경병증 통증\n"
            "용법용량|초기 1일 165mg, 최대 660mg\n"
            "약가|1,399원\n"
            "\n"
            "의약품명|리리카캡슐75밀리그램\n"
            "함량|75mg\n"
            "약가|523원\n"
        )
        cmp_text = st.text_area("비교표 입력", value=cmp_default, height=200,
                                key="cmp_text")
        cmp_items = parse_compare(cmp_text)

    if not chosen:
        st.info("위에서 비교할 품목을 1개 이상 선택하세요.")
        st.stop()

    # ───── 통합 조회 + 비교 ─────
    if st.button("✅ 선택 품목 조회 + 검증", type="primary", use_container_width=False):
        with st.spinner("⏳ 식약처·심평원 호출 중…" if not demo_mode
                        else "⏳ 데모 DB 조회 중…"):
            results = []
            for seq in chosen:
                r = by_seq[seq]
                data = fetch_one_product(seq, r.get("ITEM_NAME", ""),
                                          r.get("ENTP_NAME", ""), key, demo_mode)
                results.append((r, data))

        # ───── 3) 화면 내 직접 표시 ─────
        st.subheader("3️⃣ 허가·약가 원본 (화면 표시)")
        for r, data in results:
            label = f"💊 {data['의약품명']}  |  {data['제조판매사']}  |  약가 {data['약가']}원"
            with st.expander(label, expanded=True):
                col_a, col_b = st.columns(2)
                with col_a:
                    st.markdown("**📑 식약처 허가 (13필드)**")
                    for f in PERMIT_FIELDS:
                        st.write(f"- **{f}**: {data.get(f, '') or '(없음)'}")
                with col_b:
                    st.markdown("**💰 심평원 약가**")
                    for f in PRICE_FIELDS:
                        v = data.get(f if f in PRICE_FIELDS else
                                     ("약가" if f == "의약품명" and "약가" in data else
                                      "약가적용시작일" if f == "의약품명" else f), "") or ""
                        st.write(f"- **{f}**: {v or '(없음)'}")
                    st.write(f"- **적용시작일**: {data.get('약가적용시작일','')}")
                    st.write(f"- **적용종료일**: {data.get('약가적용종료일','')}")
                    st.write(f"- **mdsCd**: {data.get('mdsCd','')}")

        # ───── 4) 비교 검증 결과 ─────
        st.subheader("4️⃣ 비교표 검증 결과")
        if not cmp_items:
            st.info("비교표를 입력하면 여기에 검증 결과 표시.")
        else:
            cmp_by_name = {it.get("의약품명", ""): it for it in cmp_items}
            rows_for_table = []
            for r, data in results:
                cmp_item = cmp_by_name.get(data["의약품명"]) or {}
                for field in PERMIT_FIELDS + ["약가"]:
                    src_val = data.get(field, "")
                    tbl_val = cmp_item.get(field, "")
                    status, reason = compare_one(field, tbl_val, src_val)
                    rows_for_table.append({
                        "품목": data["의약품명"],
                        "필드": field,
                        "비교표": tbl_val or "(빈칸)",
                        "원문": src_val or "(없음)",
                        "판정": status,
                        "근거": reason,
                    })
            # 색상 마커 표
            for row in rows_for_table:
                color = {
                    "🟢 일치": "#16a34a", "🔴 수정 필요": "#dc2626",
                    "🟡 확인 필요": "#ca8a04", "🟠 의미 단위 — LLM 확인 필요": "#ea580c",
                    "⚪ 확인 불가": "#9ca3af"
                }.get(row["판정"], "#9ca3af")
                st.markdown(
                    f"<div style='border-left:5px solid {color};padding:8px 12px;"
                    f"background:#fafafa;margin-bottom:6px;border-radius:4px'>"
                    f"<b>{row['품목']}</b> · <b>{row['필드']}</b> · "
                    f"<span style='color:{color};font-weight:700'>{row['판정']}</span>"
                    f"<br/><small>비교표: {row['비교표'][:120]}</small><br/>"
                    f"<small>원문: {row['원문'][:120]}</small><br/>"
                    f"<small style='color:#666'>근거: {row['근거']}</small></div>",
                    unsafe_allow_html=True,
                )
            # 🟠 의미 단위 LLM 프롬프트 (이전 🤖 복사)
            llm_prompts = [r for r in rows_for_table if "🟠" in r["판정"]]
            if llm_prompts:
                with st.expander(f"🤖 LLM 프롬프트 ({len(llm_prompts)}건)"):
                    for i, row in enumerate(llm_prompts, 1):
                        prompt = (
                            f"[비교 1] 심의자료 원문 (저장값 그대로):\n\"{row['비교표']}\"\n\n"
                            f"[비교 2] 공식 허가 원문 (사용자가 nedrug.mfds.go.kr / hira.or.kr 에서 복사):\n"
                            f"\"{row['원문']}\"\n\n품목: {row['품목']}\n필드: {row['필드']}\n"
                            f"판정 후보: ✅ 의미 일치 / ⚠ 일부 차이 / ❌ 의미 불일치·범위 확대\n"
                            f"원문 인용 없이 답하거나 기억에 의존한 답변은 부정확하므로 폐기.\n"
                        )
                        st.text_area(f"프롬프트 #{i} ({row['필드']})", value=prompt,
                                     height=140, key=f"llm_p_{i}")

            # ───── 5) 옵션 CSV 다운로드 ─────
            buf = ["품목,필드,비교표,원문,판정,근거"]
            for row in rows_for_table:
                buf.append(f"{row['품목']},{row['필드']},\"{row['비교표']}\",\"{row['원문']}\","
                           f"{row['판정']},{row['근거']}")
            csv_bytes = ("\uFEFF" + "\n".join(buf)).encode("utf-8")
            st.download_button(
                "⬇ 검증결과.csv (옵션 다운로드)",
                csv_bytes, file_name="검증결과.csv", mime="text/csv",
            )

        st.success(f"✅ 완료 · 모드={'데모' if demo_mode else '실제 API'} · 비교 {'있음' if cmp_items else '없음'}")
