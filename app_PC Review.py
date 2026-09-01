import csv
import io
import json
import os
import re
import time
import html as html_lib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

# ─────────────────────────────────────────────────────────────
# 의약품 허가정보·약가 통합 조회 — Streamlit version
# 기존 Colab 노트북의 API 엔드포인트/필드명/매칭 규칙을 유지합니다.
# ─────────────────────────────────────────────────────────────
MFDS_LIST_URL = "http://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnInq07"
MFDS_DETAIL_URL = "https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnDtlInq06"
HIRA_PRICE_URL = "https://apis.data.go.kr/B551182/dgamtCrtrInfoService1.2/getDgamtList"
FIELD_BAR_CODE = "BAR_CODE"
LIST_NUM_OF_ROWS = 500
HIRA_CALL_LIMIT = 600
MFDS_CALL_LIMIT = 1000
LIST_CSV_FIELDS = ["ITEM_SEQ", "ITEM_NAME", "ENTP_NAME", "CANCEL_NAME", "ITEM_PERMIT_DATE"]  # 바코드는 상세 API에서만 확인 가능
BASE_COLUMNS = ["허가제품명", "제약사한글명", "약가", "약효분류", "주성분영문명", "성분명", "효능효과", "용법용량"]
HIRA_MEFT_FIELD = "meftDivNo"
# 식약처 상세 응답에서 실제 확인된 직접 필드와 NB_DOC_DATA 문서 섹션입니다.
# 사용자가 체크한 항목만 API 응답/캐시에서 결과로 펼칩니다.
EXTRA_FIELD_SPECS = {
    "영문제품명": {"label": "영문 제품명", "source": "ITEM_ENG_NAME", "transform": "direct"},
    "주성분영문명": {"label": "주성분 영문명", "source": "MAIN_INGR_ENG", "transform": "direct"},
    "전문일반구분": {"label": "전문·일반의약품 구분", "source": "ETC_OTC_CODE", "transform": "direct"},
    "ATC코드": {"label": "ATC 코드", "source": "ATC_CODE", "transform": "direct"},
    "원료약품및분량": {"label": "원료약품 및 분량", "source": "MATERIAL_NAME", "transform": "direct"},
    "소아_고령자투여": {"label": "소아·고령자 투여", "source": "NB_DOC_DATA", "keywords": ["소아에 대한 투여", "소아투여", "고령자에 대한 투여", "고령자투여"], "transform": "section"},
    "임부_수유부투여": {"label": "임부 및 수유부 투여", "source": "NB_DOC_DATA", "keywords": ["임부 및 수유부에 대한 투여", "임부, 수유부, 가임여성 및 남성에 대한 투여", "가임여성 및 남성에 대한 투여", "임부에 대한 투여", "수유부에 대한 투여", "임부투여", "수유부투여"], "transform": "section"},
    "금기사항": {"label": "금기사항", "source": "NB_DOC_DATA", "keywords": ["다음 환자에게는 투여하지 말 것", "투여하지 말 것", "금기"], "transform": "section"},
    "신중투여": {"label": "신중히 투여할 환자", "source": "NB_DOC_DATA", "keywords": ["다음 환자에는 신중히 투여할 것", "신중히 투여"], "transform": "section"},
    "일반적주의": {"label": "일반적 주의", "source": "NB_DOC_DATA", "keywords": ["일반적 주의"], "transform": "section"},
    "상호작용": {"label": "상호작용", "source": "NB_DOC_DATA", "keywords": ["상호작용"], "transform": "section"},
    "이상반응": {"label": "이상반응", "source": "NB_DOC_DATA", "keywords": ["이상반응", "이상 반응"], "transform": "section"},
    "과량투여처치": {"label": "과량투여시의 처치", "source": "NB_DOC_DATA", "keywords": ["과량투여시의 처치", "과량투여", "과량 투여"], "transform": "section"},
    "적용상의주의사항": {"label": "적용상의 주의사항", "source": "NB_DOC_DATA", "keywords": ["적용상의 주의", "적용상 주의"], "transform": "section"},
    "기타주의사항": {"label": "기타 사용상 주의사항", "source": "NB_DOC_DATA", "keywords": ["기타"], "transform": "section"},
    "포장단위": {"label": "포장단위", "source": "PACK_UNIT", "transform": "direct"},
    "유효기간": {"label": "유효기간", "source": "VALID_TERM", "transform": "direct"},
    "보관정보": {"label": "보관정보", "source": "STORAGE_METHOD", "transform": "direct"},
    "보관_취급주의사항": {"label": "보관 및 취급상의 주의사항", "source": "NB_DOC_DATA", "keywords": ["보관 및 취급상의 주의사항", "보관 및 취급상의 주의", "보관취급상의주의사항"], "transform": "section"},
    "성상": {"label": "성상", "source": "CHART", "transform": "direct"},
    "변경내용": {"label": "변경내용", "source": "GBN_NAME", "transform": "direct"},
    "변경일자": {"label": "변경일자", "source": "CHANGE_DATE", "transform": "direct"},
}
EXTRA_FIELD_ORDER = [
    "영문제품명", "주성분영문명", "전문일반구분", "ATC코드", "원료약품및분량",
    "소아_고령자투여", "임부_수유부투여",
    "금기사항", "신중투여", "일반적주의",
    "상호작용",
    "이상반응", "과량투여처치",
    "적용상의주의사항", "기타주의사항",
    "포장단위", "유효기간", "보관정보", "보관_취급주의사항", "성상", "변경내용", "변경일자",
]
EXTRA_FIELD_LABELS = {key: EXTRA_FIELD_SPECS[key]["label"] for key in EXTRA_FIELD_ORDER}
EXTRA_FIELD_KEYWORDS = {key: EXTRA_FIELD_SPECS[key]["keywords"] for key in EXTRA_FIELD_ORDER if "keywords" in EXTRA_FIELD_SPECS[key]}
EXTRA_DIRECT_FIELDS = {key: EXTRA_FIELD_SPECS[key]["source"] for key in EXTRA_FIELD_ORDER if EXTRA_FIELD_SPECS[key]["transform"] == "direct"}
NEW_DRUG_PRESET_KEYS = [
    "영문제품명", "ATC코드", "포장단위", "주성분영문명", "원료약품및분량",
    "유효기간", "성상", "전문일반구분", "보관정보", "보관_취급주의사항",
]
ALWAYS_FETCH_DETAIL_KEYS = ["주성분영문명"]
RESULT_COLUMN_ORDER = ["허가제품명", "제약사한글명", "약가", "약효분류", "영문제품명", "주성분영문명", "전문일반구분", "ATC코드", "원료약품및분량", "포장단위", "유효기간", "성상", "보관정보", "성분명", "효능효과", "용법용량"]
HEADING_PATTERN = re.compile(r"^\s*\d+\s*[.\-]")
MAX_RETRY = 5

DUR_CATEGORIES = [
    "병용금기(급여)", "병용금기(비급여)", "임부금기", "연령금기", "효능군중복",
    "수유부주의", "비대면진료처방금지", "비용효과적인함량의약품",
]
DUR_EXTRA_COLUMNS = {
    "임부금기": ["금기등급", "상세정보"],
    "연령금기": ["특정연령", "특정연령단위코드", "연령처리조건", "상세정보"],
    "효능군중복": ["효능군", "Group"],
}
DUR_CODE_COLUMN_PATTERN = re.compile(r"(제품코드|약품코드|품목코드|EDI)", re.IGNORECASE)

DATA_DIR = Path(os.environ.get("DRUG_APP_DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
LIST_FILE = DATA_DIR / "허가목록_원본.csv"
TEMP_FILE = DATA_DIR / "허가목록_임시.json"
CACHE_CODE_FILE = DATA_DIR / "cache_약가_코드별.json"
CACHE_NAME_FILE = DATA_DIR / "cache_약가_이름별.json"
CACHE_DETAIL_FILE = DATA_DIR / "cache_상세정보.json"
CACHE_MEFT_FILE = DATA_DIR / "cache_약효분류.json"
LIST_META_FILE = DATA_DIR / "허가목록_메타.json"
KST = timezone(timedelta(hours=9))


def clean_whitespace(text):
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def clean_ingredient(text):
    if not text:
        return ""
    return clean_whitespace(re.sub(r"\[[A-Za-z0-9]+\]", "", text))


def unescape_html_repeated(text, rounds=3):
    if text is None:
        return ""
    value = str(text)
    for _ in range(max(1, rounds)):
        new_value = html_lib.unescape(value)
        if new_value == value:
            break
        value = new_value
    return value.replace("\xa0", " ").replace("&nbsp;", " ")


def clean_markup(text):
    if not text:
        return ""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = unescape_html_repeated(text)
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
    return clean_whitespace(text)


def parse_nested_doc_xml(xml_str):
    if not xml_str or not xml_str.strip():
        return ""
    try:
        root = ET.fromstring(xml_str.strip())
    except ET.ParseError:
        return clean_markup(xml_str)
    parts = []
    for el in root.iter():
        title = el.get("title")
        if title:
            parts.append(title.strip())
        if el.text and el.text.strip():
            parts.append(el.text.strip())
    return clean_markup(" ".join(parts))


def normalize_section_title(text):
    text = unescape_html_repeated(text)
    text = clean_whitespace(text)
    text = re.sub(r"^\s*\d+\s*[.\-)]\s*", "", text)
    text = re.sub(r"[\s,·ㆍ/()\[\]{}:;]", "", text)
    return text


def parse_doc_sections(xml_str, keywords):
    """중첩 XML에서 title에 특정 키워드가 포함된 상위 항목과 하위 내용 추출."""
    if not xml_str or not xml_str.strip():
        return ""
    try:
        root = ET.fromstring(xml_str.strip())
    except ET.ParseError:
        return ""
    normalized_keywords = [normalize_section_title(keyword) for keyword in keywords if keyword]
    chunks = []
    for el in root.iter():
        title = el.get("title")
        if title and title.strip():
            chunks.append((bool(HEADING_PATTERN.match(title.strip())), title.strip()))
        if el.text and el.text.strip():
            chunks.append((False, el.text.strip()))
    heading_idx = [i for i, (heading, _) in enumerate(chunks) if heading]
    picked = []
    if heading_idx:
        for n, idx in enumerate(heading_idx):
            heading_text = chunks[idx][1]
            normalized_heading = normalize_section_title(heading_text)
            if any(keyword in normalized_heading or normalized_heading in keyword for keyword in normalized_keywords):
                end = heading_idx[n + 1] if n + 1 < len(heading_idx) else len(chunks)
                picked.extend(text for _, text in chunks[idx:end])
    else:
        picked.extend(
            text for _, text in chunks
            if any(keyword in normalize_section_title(text) for keyword in normalized_keywords)
        )
    return clean_markup(" ".join(picked))


def summarize_text(text, max_chars=420, max_sentences=3):
    """의학적 판단을 새로 생성하지 않고, 원문 앞부분과 핵심 문장만 발췌합니다."""
    text = clean_whitespace(text)
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    sentences = [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+|(?=\d+\.)", text) if part.strip()]
    selected = []
    for sentence in sentences:
        if sentence not in selected:
            selected.append(sentence)
        if len(selected) >= max_sentences:
            break
    summary = " ".join(selected)
    if len(summary) < 80:
        summary = text[:max_chars]
    return summary[:max_chars].rstrip() + ("…" if len(summary) > max_chars else "")


def summarize_usage(text, max_chars=520):
    """용법·용량에서 성인/소아/고령자 등 투여군별 문장을 우선 발췌합니다."""
    text = clean_whitespace(text)
    if not text:
        return ""
    headings = list(re.finditer(r"<[^>]{1,20}>", text))
    if not headings:
        return summarize_text(text, max_chars=max_chars, max_sentences=4)
    parts = []
    for index, match in enumerate(headings):
        start = match.start()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[start:end].strip()
        if section:
            parts.append(summarize_text(section, max_chars=180, max_sentences=2))
    summary = " ".join(parts)
    return summary[:max_chars].rstrip() + ("…" if len(summary) > max_chars else "")


def make_summary_df(result_df):
    """용법·용량과 효능·효과를 짧게 표시하는 규칙 기반 요약본."""
    summary_df = result_df.copy()
    if "효능효과" in summary_df.columns:
        summary_df["효능효과"] = summary_df["효능효과"].map(lambda value: summarize_text(value))
    if "용법용량" in summary_df.columns:
        summary_df["용법용량"] = summary_df["용법용량"].map(lambda value: summarize_usage(value))
    for column in summary_df.columns:
        if column not in {"효능효과", "용법용량", "허가제품명", "제약사한글명", "약가", "약효분류", "주성분영문명", "영문제품명", "전문일반구분", "ATC코드", "원료약품및분량", "포장단위", "유효기간", "성상", "보관정보"}:
            summary_df[column] = summary_df[column].map(lambda value: summarize_text(value, max_chars=260, max_sentences=2))
    return summary_df


def format_price(value):
    text = clean_whitespace(value)
    if not text:
        return ""
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return text
    try:
        number = int(digits)
    except ValueError:
        return text
    return f"{number:,}원"


def load_json_cache(path):
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as file:
                value = json.load(file)
                return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def save_json_cache(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    tmp.replace(path)


def barcode_key8(bar_code):
    if not bar_code:
        return None
    digits = re.sub(r"\D", "", str(bar_code))
    return digits[3:11] if len(digits) >= 11 else None


def mdscd_key8(mds_cd):
    """엑셀에서 숫자로 저장된 코드는 645200020.0 처럼 float로 들어오는데, 그대로
    문자열화하면 소수점의 '0'이 붙어 코드가 틀어지므로 정수로 먼저 변환합니다."""
    if mds_cd is None:
        return None
    if isinstance(mds_cd, float):
        if pd.isna(mds_cd):
            return None
        mds_cd = str(int(mds_cd))
    elif not mds_cd:
        return None
    digits = re.sub(r"\D", "", str(mds_cd))
    return digits[:8] if len(digits) >= 8 else None


def normalize_code_digits(value):
    if value is None:
        return ""
    if isinstance(value, float):
        if pd.isna(value):
            return ""
        value = str(int(value)) if value.is_integer() else str(value)
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return re.sub(r"\D", "", text)


def code_lookup_keys(value):
    digits = normalize_code_digits(value)
    if not digits:
        return []
    keys = [digits]
    key8 = digits[:8] if len(digits) >= 8 else ""
    if key8 and key8 not in keys:
        keys.append(key8)
    return keys


def split_barcode_values(bar_code_text):
    raw = str(bar_code_text or "")
    parts = re.split(r"[,/;\n]+", raw)
    values = []
    for part in parts:
        digits = normalize_code_digits(part)
        if digits:
            values.append(digits)
    return values


def collect_dur_candidate_codes(item_seq, detail):
    candidates = []

    def add_codes(source, raw_value):
        digits = normalize_code_digits(raw_value)
        if not digits:
            return
        for key in code_lookup_keys(digits):
            candidates.append((key, source, digits))

    for barcode in split_barcode_values(detail.get("_bar_code", "")):
        add_codes("바코드", barcode)
    add_codes("EDI코드", detail.get("_edi_code", ""))
    add_codes("품목코드", item_seq)

    deduped = []
    seen = set()
    for item in candidates:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def _excel_engine_for(filename):
    return "pyxlsb" if filename.lower().endswith(".xlsb") else None


def _find_dur_header_row(file_bytes, filename, max_scan=15):
    """제목 행이 위에 몇 줄 더 있는 파일 대응: '코드'가 들어간 열이 나오는 행을 헤더로 판단합니다."""
    engine = _excel_engine_for(filename)
    raw = pd.read_excel(io.BytesIO(file_bytes), header=None, engine=engine, nrows=max_scan)
    for i in range(len(raw)):
        row_vals = [str(v) for v in raw.iloc[i].tolist()]
        if any(DUR_CODE_COLUMN_PATTERN.search(v) for v in row_vals):
            return i
    return 0


def read_dur_excel(file_bytes, filename):
    engine = _excel_engine_for(filename)
    header_row = _find_dur_header_row(file_bytes, filename)
    df = pd.read_excel(io.BytesIO(file_bytes), header=header_row, engine=engine)
    df.columns = [str(c).strip() for c in df.columns]
    return df, header_row


def build_dur_index(df):
    """코드 열이 여러 개인 DUR 파일을 폭넓게 인덱싱합니다.
    - exact digits (예: 073400390)
    - 앞 8자리 key (예: 07340039)
    모두 저장해 바코드/EDI/품목코드 어느 쪽으로도 매칭되기 쉽게 만듭니다.
    """
    code_cols = [c for c in df.columns if DUR_CODE_COLUMN_PATTERN.search(str(c))]
    if not code_cols:
        return {}, code_cols
    idx = {}
    for row_number, (_, row) in enumerate(df.iterrows(), start=1):
        row_dict = row.to_dict()
        for col in code_cols:
            digits = normalize_code_digits(row.get(col))
            if not digits:
                continue
            payload = {
                "row": row_dict,
                "_dur_code_column": str(col),
                "_dur_code_value": digits,
                "_dur_row_number": row_number,
            }
            for key in code_lookup_keys(digits):
                idx.setdefault(key, []).append(payload)
    return idx, code_cols


@st.cache_data(show_spinner="DUR 파일을 읽는 중입니다…")
def parse_dur_excel_cached(file_bytes, filename):
    """파일 내용(바이트)+이름이 그대로면 캐시된 결과를 재사용해, 다른 위젯을 조작할 때마다
    무거운 엑셀을 다시 파싱하지 않도록 합니다."""
    df, header_row = read_dur_excel(file_bytes, filename)
    index, code_columns = build_dur_index(df)
    return df, header_row, index, code_columns


def _items_from_body(body):
    items = body.get("items", []) if isinstance(body, dict) else []
    if isinstance(items, dict):
        items = items.get("item", [])
    if isinstance(items, dict):
        items = [items]
    return items or []


def _parse_response_items(resp, api_name):
    resp.raise_for_status()
    text = resp.text.strip()
    if text.startswith("<"):
        root = ET.fromstring(text)
        result_code = root.findtext(".//resultCode")
        if result_code and result_code != "00":
            raise ValueError(f"{api_name} API 오류: {root.findtext('.//resultMsg')}")
        return [{child.tag: (child.text or "") for child in item_el} for item_el in root.findall(".//items/item")]
    data = resp.json()
    body = data.get("body", data.get("response", {}).get("body", {}))
    return _items_from_body(body)


def hira_get(service_key, params):
    query = {"serviceKey": service_key, "type": "json"}
    query.update(params)
    return _parse_response_items(requests.get(HIRA_PRICE_URL, params=query, timeout=30), "HIRA")


def match_price(item_name, bar_code, service_key, call_counter, cache_code, cache_name, errors):
    key8 = barcode_key8(bar_code)
    if key8:
        if key8 in cache_code:
            cached = cache_code[key8]
            return (cached, "bar_code_8자리(캐시)") if cached else ("", "매칭없음(캐시)")
        if call_counter["hira"] < HIRA_CALL_LIMIT:
            call_counter["hira"] += 1
            try:
                items = hira_get(service_key, {"mdsCd": key8})
            except (requests.RequestException, ValueError) as exc:
                errors.append(f"HIRA mdsCd={key8}: {exc}")
                items = []
            valid = [it for it in items if it.get("payTpNm") != "삭제"]
            exact = [it for it in valid if mdscd_key8(it.get("mdsCd")) == key8]
            if exact:
                price = exact[0].get("mxCprc", "")
                cache_code[key8] = price
                return price, "bar_code_8자리(직접조회)"
    name_key = re.sub(r"_\(.*?\)\s*$", "", item_name or "").strip()
    if name_key in cache_name:
        cached = cache_name[name_key]
    else:
        if call_counter["hira"] >= HIRA_CALL_LIMIT:
            return "", "호출한도초과"
        call_counter["hira"] += 1
        call_ok = True
        try:
            items = hira_get(service_key, {"itmNm": name_key})
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"HIRA itmNm={name_key}: {exc}")
            items, call_ok = [], False
        valid = [it for it in items if it.get("payTpNm") != "삭제"]
        cached = ""
        if key8:
            exact = [it for it in valid if mdscd_key8(it.get("mdsCd")) == key8]
            if exact:
                cached = exact[0].get("mxCprc", "")
                cache_code[key8] = cached
        if not cached:
            def norm(name):
                return re.sub(r"_\(.*?\)\s*$", "", name or "").strip()
            exact_name = [it for it in valid if norm(it.get("itmNm")) == name_key]
            if exact_name:
                cached = exact_name[0].get("mxCprc", "")
        if call_ok:
            cache_name[name_key] = cached
    if cached:
        return cached, "품목명검색(8자리검증 또는 완전일치)"
    return "", "매칭없음"


def get_effect_classification(item_name, bar_code, service_key, call_counter, cache_meft, errors):
    """심평원 응답의 meftDivNo를 항상 조회해 약효분류로 반환합니다."""
    key8 = barcode_key8(bar_code)
    cache_key = f"mdsCd:{key8}" if key8 else f"itmNm:{re.sub(r'_\(.*?\)\s*$', '', item_name or '').strip()}"
    if cache_key in cache_meft:
        return cache_meft[cache_key]
    if call_counter["hira"] >= HIRA_CALL_LIMIT:
        return ""
    call_counter["hira"] += 1
    try:
        params = {"mdsCd": key8} if key8 else {"itmNm": re.sub(r"_\(.*?\)\s*$", "", item_name or "").strip()}
        items = hira_get(service_key, params)
    except (requests.RequestException, ValueError) as exc:
        errors.append(f"HIRA 약효분류 {cache_key}: {exc}")
        return ""
    valid = [item for item in items if item.get("payTpNm") != "삭제"]
    if key8:
        valid = [item for item in valid if mdscd_key8(item.get("mdsCd")) == key8] or valid
    value = clean_whitespace(valid[0].get(HIRA_MEFT_FIELD, "")) if valid else ""
    cache_meft[cache_key] = value
    return value


def fetch_detail(item_seq, service_key, call_counter, cache_detail, wanted_extras, errors):
    cached = cache_detail.get(item_seq, {})
    have_base = "성분명" in cached
    missing_extras = [key for key in wanted_extras if key not in cached or not clean_whitespace(cached.get(key, ""))]
    if have_base and not missing_extras:
        return cached
    cached_direct_ready = all(
        key not in EXTRA_DIRECT_FIELDS or ("_raw_" + key) in cached
        for key in missing_extras
    )
    if have_base and "_raw_nb_xml" in cached and cached_direct_ready:
        for key in missing_extras:
            if key in EXTRA_DIRECT_FIELDS:
                cached[key] = cached.get("_raw_" + key, "")
            else:
                cached[key] = parse_doc_sections(cached["_raw_nb_xml"], EXTRA_FIELD_KEYWORDS[key])
        cache_detail[item_seq] = cached
        return cached
    if call_counter["mfds"] >= MFDS_CALL_LIMIT:
        return cached if cached else {"성분명": "", "효능효과": "", "용법용량": ""}
    call_counter["mfds"] += 1
    params = {"serviceKey": service_key, "item_seq": item_seq, "type": "json"}
    try:
        items = _parse_response_items(requests.get(MFDS_DETAIL_URL, params=params, timeout=30), "MFDS 상세")
        if not items:
            errors.append(f"MFDS 상세 item_seq={item_seq}: items 없음")
            return cached if cached else {"성분명": "", "효능효과": "", "용법용량": ""}
        item = items[0]
    except (requests.RequestException, ValueError, ET.ParseError) as exc:
        errors.append(f"MFDS 상세 item_seq={item_seq}: {exc}")
        return cached if cached else {"성분명": "", "효능효과": "", "용법용량": ""}
    nb_xml = item.get("NB_DOC_DATA", "")
    cached["성분명"] = clean_ingredient(item.get("MAIN_ITEM_INGR", ""))
    cached["효능효과"] = parse_nested_doc_xml(item.get("EE_DOC_DATA", ""))
    cached["용법용량"] = parse_nested_doc_xml(item.get("UD_DOC_DATA", ""))
    # 목록 API(getDrugPrdtPrmsnInq07)에는 바코드 필드가 없으므로, 상세 API 응답에서 받아 캐시합니다.
    cached["_bar_code"] = item.get("BAR_CODE", "")
    cached["_edi_code"] = item.get("EDI_CODE", "")
    cached["_raw_nb_xml"] = nb_xml
    for key, field in EXTRA_DIRECT_FIELDS.items():
        cached["_raw_" + key] = clean_whitespace(item.get(field, ""))
    for key in wanted_extras:
        cached[key] = cached.get("_raw_" + key, "") if key in EXTRA_DIRECT_FIELDS else parse_doc_sections(nb_xml, EXTRA_FIELD_KEYWORDS[key])
    cache_detail[item_seq] = cached
    return cached


def current_kst_date():
    return datetime.now(KST).date().isoformat()


def list_cache_is_fresh():
    if not LIST_FILE.exists() or not LIST_META_FILE.exists():
        return False
    try:
        with LIST_META_FILE.open("r", encoding="utf-8") as file:
            meta = json.load(file)
        return meta.get("collected_date_kst") == current_kst_date()
    except (OSError, json.JSONDecodeError):
        return False


def fetch_list_page(page, mfds_key):
    params = {"serviceKey": mfds_key, "pageNo": page, "numOfRows": LIST_NUM_OF_ROWS, "type": "json"}
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            resp = requests.get(MFDS_LIST_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            body = data.get("body", data.get("response", {}).get("body", {}))
            return body, _items_from_body(body)
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            time.sleep(min(2 ** attempt, 30))
    raise last_err


@st.cache_data(show_spinner=False)
def load_permitted_drugs(mfds_key, data_dir_string, cache_day, force_refresh_token=0):
    list_file = Path(data_dir_string) / "허가목록_원본.csv"
    temp_file = Path(data_dir_string) / "허가목록_임시.json"
    meta_file = Path(data_dir_string) / "허가목록_메타.json"
    cache_is_fresh = list_file.exists() and meta_file.exists()
    if cache_is_fresh:
        try:
            with meta_file.open("r", encoding="utf-8") as file:
                cache_is_fresh = json.load(file).get("collected_date_kst") == cache_day
        except (OSError, json.JSONDecodeError):
            cache_is_fresh = False
    if cache_is_fresh:
        with list_file.open("r", encoding="utf-8-sig", newline="") as file:
            return list(csv.DictReader(file))
    if not mfds_key:
        raise ValueError("오늘 날짜의 허가목록 캐시가 없습니다. 식약처 인증키를 입력해야 허가목록을 갱신할 수 있습니다.")
    if temp_file.exists():
        with temp_file.open("r", encoding="utf-8") as file:
            resume = json.load(file)
        all_rows, page, total_count = resume["rows"], resume["next_page"], resume.get("total_count")
    else:
        all_rows, page, total_count = [], 1, None
    progress = st.progress(0, text="식약처 허가목록을 수집하는 중입니다…")
    while True:
        body, items = fetch_list_page(page, mfds_key)
        if total_count is None:
            total_count = int(body.get("totalCount", 0))
        if not items:
            break
        all_rows.extend(items)
        page += 1
        if total_count:
            progress.progress(min(len(all_rows) / total_count, 1.0), text=f"허가목록 수집 중: {len(all_rows):,}/{total_count:,}")
        if page % 10 == 0 or (total_count and len(all_rows) >= total_count):
            with temp_file.open("w", encoding="utf-8") as file:
                json.dump({"rows": all_rows, "next_page": page, "total_count": total_count}, file, ensure_ascii=False)
        if total_count and len(all_rows) >= total_count:
            break
        time.sleep(0.1)
    progress.empty()
    with list_file.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=LIST_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in LIST_CSV_FIELDS} for row in all_rows)
    if temp_file.exists():
        temp_file.unlink()
    with meta_file.open("w", encoding="utf-8") as file:
        json.dump({"collected_date_kst": cache_day, "updated_at": datetime.now(KST).isoformat()}, file, ensure_ascii=False, indent=2)
    return all_rows


def order_result_columns(df):
    ordered = [column for column in RESULT_COLUMN_ORDER if column in df.columns]
    ordered += [column for column in EXTRA_FIELD_ORDER if column in df.columns and column not in ordered]
    ordered += [column for column in df.columns if column not in ordered]
    return df[ordered]


def make_display_df(result_df, summary_view=False, transpose_view=False):
    display_df = make_summary_df(result_df) if summary_view else result_df.copy()
    display_df = order_result_columns(display_df)
    if transpose_view:
        display_df = display_df.T
        display_df.index.name = "항목"
    return display_df


def result_column_config(df):
    """긴 텍스트는 넓게 시작하고, Streamlit 표에서 드래그로 폭을 조절합니다."""
    config = {}
    long_columns = {"효능효과", "용법용량", "성분명", "이상반응", "상호작용", "금기사항", "원료약품및분량"}
    for column in df.columns:
        config[column] = st.column_config.TextColumn(
            label=str(column),
            width="large" if column in long_columns else "medium",
            help="헤더 경계를 드래그해 컬럼 너비를 조절할 수 있습니다.",
        )
    return config


def filter_result_dataframe(df, widget_prefix="result_filter"):
    """컬럼별 필터를 적용해 결과 DataFrame을 반환합니다."""
    filtered = df.copy()
    with st.expander("컬럼별 필터", expanded=False):
        st.caption("문자열은 포함 검색, 범주형은 여러 값 선택, 숫자형은 범위 필터를 사용합니다.")
        filter_columns = st.columns(2)
        for index, column in enumerate(df.columns):
            series = df[column].fillna("")
            numeric = pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce")
            numeric_ratio = numeric.notna().mean() if len(series) else 0
            with filter_columns[index % 2]:
                if numeric_ratio >= 0.8 and numeric.notna().any():
                    minimum, maximum = float(numeric.min()), float(numeric.max())
                    if minimum < maximum:
                        selected_range = st.slider(
                            str(column), minimum, maximum, (minimum, maximum), key=f"{widget_prefix}_num_{index}"
                        )
                        filtered = filtered[numeric.loc[filtered.index].between(*selected_range)]
                    else:
                        st.caption(f"{column}: {minimum:g}")
                else:
                    values = sorted({str(value) for value in series if str(value).strip()})
                    if 0 < len(values) <= 30:
                        selected_values = st.multiselect(
                            str(column), values, key=f"{widget_prefix}_cat_{index}"
                        )
                        if selected_values:
                            filtered = filtered[filtered[column].astype(str).isin(selected_values)]
                    else:
                        text = st.text_input(f"{column} 포함 검색", key=f"{widget_prefix}_text_{index}")
                        if text.strip():
                            filtered = filtered[filtered[column].astype(str).str.contains(text.strip(), case=False, na=False)]
    return filtered


def render_resizable_wrapped_table(display_df, show_index=False, height=720, table_key="result"):
    """외부 라이브러리 없이 컬럼 드래그 리사이즈와 셀 줄바꿈을 함께 제공합니다."""
    table_df = display_df.reset_index() if show_index else display_df.reset_index(drop=True)
    if show_index:
        table_df = table_df.rename(columns={table_df.columns[0]: str(display_df.index.name or "항목")})
    headers = [str(column) for column in table_df.columns]
    long_columns = {"효능효과", "용법용량", "성분명", "이상반응", "상호작용", "금기사항", "원료약품및분량"}
    narrow_columns = {"약가", "제약사한글명", "약효분류"}

    def cell(value):
        if value is None or (isinstance(value, float) and pd.isna(value)) or pd.isna(value):
            value = ""
        return html_lib.escape(str(value)).replace("\\n", "<br>")

    if show_index:
        # 행/열 전환 보기: 약 하나가 세로줄 하나가 됩니다. 맨 앞(항목명) 열만 고정 폭으로 두고,
        # 나머지 약 컬럼들은 화면 폭에서 그 만큼을 뺀 나머지를 균등하게 나눠 갖도록 %로 지정합니다.
        # 그래서 화면을 늘리면 약별 폭이 넓어지고, 줄이면 좁아지며 전체 약이 한 화면에 들어옵니다.
        index_width_px = 160
        drug_column_count = max(len(headers) - 1, 1)
        col_widths = [f"{index_width_px}px"] + [
            f"calc((100% - {index_width_px}px) / {drug_column_count})"
        ] * drug_column_count
    else:
        def width_for(header):
            if header in narrow_columns:
                return 200
            if header == "허가제품명":
                return 1000
            if header in long_columns:
                return 720
            return 360

        col_widths = [f"{width_for(header)}px" for header in headers]

    colgroup = "".join(
        f'<col data-column="{index}" style="width:{width}">' for index, width in enumerate(col_widths)
    )
    header_html = "".join(f'<th data-column="{index}">{cell(header)}<span class="resize-handle" data-column="{index}"></span></th>' for index, header in enumerate(headers))
    body_html = []
    for row in table_df.itertuples(index=False, name=None):
        body_html.append("<tr>" + "".join(f"<td>{cell(value)}</td>" for value in row) + "</tr>")
    table_width_css = "width:100%;" if show_index else "width:max-content; min-width:100%;"
    markup = f"""
    <style>
      html, body {{ margin:0; padding:0; background:#fff; font-family:Arial, sans-serif; }}
      .table-wrap {{ width:100%; height:calc(100vh - 24px); overflow:auto; border:1px solid #d9dee7; }}
      table {{ border-collapse:collapse; table-layout:fixed; {table_width_css} font-size:13px; }}
      col {{ width:360px; }}
      th, td {{ border:1px solid #d9dee7; padding:8px; vertical-align:top; white-space:normal; overflow-wrap:anywhere; word-break:break-word; line-height:1.45; }}
      th {{ position:sticky; top:0; z-index:2; background:#f3f6fa; font-weight:700; text-align:left; user-select:none; }}
      .resize-handle {{ position:absolute; top:0; right:-4px; width:8px; height:100%; cursor:col-resize; z-index:3; }}
      .resize-handle:hover, .resizing {{ background:#5b8def; opacity:.55; }}
      body.resizing {{ cursor:col-resize; user-select:none; }}
    </style>
    <div class="table-wrap" id="wrap-{table_key}">
      <table id="table-{table_key}"><colgroup>{colgroup}</colgroup><thead><tr>{header_html}</tr></thead><tbody>{''.join(body_html)}</tbody></table>
    </div>
    <script>
      (() => {{
        const table = document.getElementById('table-{table_key}');
        const cols = table.querySelectorAll('col');
        table.querySelectorAll('.resize-handle').forEach(handle => {{
          handle.addEventListener('mousedown', event => {{
            event.preventDefault();
            const index = Number(handle.dataset.column);
            const startX = event.clientX;
            const startWidth = cols[index].getBoundingClientRect().width;
            document.body.classList.add('resizing');
            handle.classList.add('resizing');
            const move = moveEvent => {{
              const nextWidth = Math.max(120, startWidth + moveEvent.clientX - startX);
              cols[index].style.width = nextWidth + 'px';
            }};
            const stop = () => {{
              document.body.classList.remove('resizing');
              handle.classList.remove('resizing');
              document.removeEventListener('mousemove', move);
              document.removeEventListener('mouseup', stop);
            }};
            document.addEventListener('mousemove', move);
            document.addEventListener('mouseup', stop);
          }});
        }});
      }})();
    </script>
    """
    components.html(markup, height=height, scrolling=False)


def make_comparison_df(result_df):
    """행에는 조회 항목, 열에는 의약품을 배치한 비교표를 생성합니다."""
    comparison = result_df.copy()
    comparison_names = []
    seen = {}
    for _, row in comparison.iterrows():
        name = str(row.get("허가제품명", "품목"))
        seen[name] = seen.get(name, 0) + 1
        comparison_names.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    comparison["_비교용약품명"] = comparison_names
    comparison = comparison.set_index("_비교용약품명").T
    comparison.index.name = "조회 항목"
    return comparison


def lookup_selected(rows, mfds_key, hira_key, wanted_extras):
    cache_code = load_json_cache(CACHE_CODE_FILE)
    cache_name = load_json_cache(CACHE_NAME_FILE)
    cache_detail = load_json_cache(CACHE_DETAIL_FILE)
    cache_meft = load_json_cache(CACHE_MEFT_FILE)
    call_counter = {"hira": 0, "mfds": 0}
    errors = []
    columns = BASE_COLUMNS + wanted_extras
    output = []
    progress = st.progress(0, text="조회 중입니다…")
    for index, row in enumerate(rows, start=1):
        item_seq = row.get("ITEM_SEQ", "")
        item_name = clean_whitespace(row.get("ITEM_NAME", ""))
        entp_name = clean_whitespace(row.get("ENTP_NAME", ""))
        # 목록 API에는 바코드가 없으므로, 상세 API(fetch_detail)를 먼저 불러 실제 바코드를 얻습니다.
        fetch_keys = list(dict.fromkeys(ALWAYS_FETCH_DETAIL_KEYS + wanted_extras))
        detail = fetch_detail(item_seq, mfds_key, call_counter, cache_detail, fetch_keys, errors)
        bar_code = detail.get("_bar_code", "")
        price, method = match_price(item_name, bar_code, hira_key, call_counter, cache_code, cache_name, errors)
        effect_classification = get_effect_classification(item_name, bar_code, hira_key, call_counter, cache_meft, errors)
        out_row = {"허가제품명": item_name, "제약사한글명": entp_name, "약가": format_price(price), "약효분류": effect_classification, "주성분영문명": detail.get("주성분영문명", ""), "성분명": detail.get("성분명", ""), "효능효과": detail.get("효능효과", ""), "용법용량": detail.get("용법용량", "")}
        for key in wanted_extras:
            out_row[key] = detail.get(key, "")
        output.append(out_row)
        progress.progress(index / len(rows), text=f"조회 중: {index}/{len(rows)}")
    progress.empty()
    save_json_cache(CACHE_CODE_FILE, cache_code)
    save_json_cache(CACHE_NAME_FILE, cache_name)
    save_json_cache(CACHE_DETAIL_FILE, cache_detail)
    save_json_cache(CACHE_MEFT_FILE, cache_meft)
    return order_result_columns(pd.DataFrame(output)), errors


def check_dur(rows, mfds_key, dur_indices, cache_detail, call_counter, errors):
    """선택한 품목들이 업로드된 DUR(병용금기 등) 리스트에 포함되는지 확인합니다.
    바코드, EDI코드, 품목코드를 모두 후보로 사용하고, 어떤 코드열에 매칭되었는지 함께 보여줍니다.
    """
    result_rows = []
    seen_extra_cols = []
    seen_matches = set()
    for row in rows:
        item_seq = row.get("ITEM_SEQ", "")
        item_name = clean_whitespace(row.get("ITEM_NAME", ""))
        entp_name = clean_whitespace(row.get("ENTP_NAME", ""))
        detail = fetch_detail(item_seq, mfds_key, call_counter, cache_detail, [], errors)
        candidates = collect_dur_candidate_codes(item_seq, detail)
        if not candidates:
            continue
        for category in DUR_CATEGORIES:
            index = dur_indices.get(category, {})
            for lookup_key, match_source, raw_code in candidates:
                if lookup_key not in index:
                    continue
                for payload in index[lookup_key]:
                    row_dict = payload["row"]
                    dedupe_key = (
                        item_seq,
                        category,
                        payload.get("_dur_code_column", ""),
                        payload.get("_dur_code_value", ""),
                        payload.get("_dur_row_number", 0),
                    )
                    if dedupe_key in seen_matches:
                        continue
                    seen_matches.add(dedupe_key)
                    result_row = {
                        "허가제품명": item_name,
                        "제약사한글명": entp_name,
                        "DUR종류": category,
                        "매칭근거": match_source,
                        "매칭코드": raw_code,
                        "DUR코드열": payload.get("_dur_code_column", ""),
                    }
                    for column in DUR_EXTRA_COLUMNS.get(category, []):
                        value = row_dict.get(column, "")
                        result_row[column] = "" if pd.isna(value) else str(value)
                        if column not in seen_extra_cols:
                            seen_extra_cols.append(column)
                    result_rows.append(result_row)
    columns = ["허가제품명", "제약사한글명", "DUR종류", "매칭근거", "매칭코드", "DUR코드열"] + seen_extra_cols
    return pd.DataFrame(result_rows, columns=columns)


# ─────────────────────────────────────────────────────────────
# 비교표 검증 (원본: 의약품 심의자료 검증기 Streamlit v6.2 / app(1).py)
# 검색·후보 선택·항목 추출은 위쪽 app.py 로직을 그대로 쓰고,
# 이 블록은 "3번에서 조회한 result_df"를 원문으로 삼아 사용자가 붙여넣은
# 비교표(심의자료)를 검증하는 기능만 담당한다. 함수명은 위쪽 app.py의
# 동명 함수(fetch_detail 등)와 절대 겹치지 않도록 cmp_ 접두사를 붙였다.
# ─────────────────────────────────────────────────────────────
CMP_SEMANTIC_FIELDS = ["적응증", "용법용량", "소아", "금기"]

# 비교표에서 쓰는 자연스러운 필드명 → 위 app.py가 만드는 result_df의 실제 컬럼명.
# 값이 None인 항목(제형)은 result_df에 직접 컬럼이 없어 허가제품명에서 규칙 기반으로 추출한다.
CMP_FIELD_TO_RESULT_COLUMN = {
    "의약품명": "허가제품명",
    "성분명": "성분명",
    "제조판매사": "제약사한글명",
    "함량": "원료약품및분량",   # "3번 추가 조회 항목"에서 [원료약품 및 분량]을 선택해야 값이 채워진다
    "제형": None,               # 허가제품명에서 규칙 기반 추출 (아래 cmp_form)
    "적응증": "효능효과",
    "용법용량": "용법용량",
    "소아": "소아_고령자투여",   # "3번"에서 [소아·고령자 투여]를 선택해야 값이 채워진다
    "보관": "보관정보",         # "3번"에서 [보관정보]를 선택해야 값이 채워진다
    "금기": "금기사항",         # "3번"에서 [금기사항]을 선택해야 값이 채워진다
    "약가": "약가",
}
# 위 매핑 중 "3번 추가 조회 항목" 체크박스가 있어야만 값이 채워지는 컬럼들.
# 사용자가 해당 체크박스를 켜지 않았으면 "미조회"임을 명확히 구분해서 안내한다.
CMP_OPTIONAL_EXTRA_COLUMNS = {"원료약품및분량", "소아_고령자투여", "보관정보", "금기사항"}


def cmp_normalize(text):
    """공백·괄호·중점 등 제거 후 lower() — 제품명 매칭/단순 비교용 (app(1).py의 _normalize)."""
    if not text:
        return ""
    s = re.sub(r"\s+", "", str(text))
    s = re.sub(r"[\(\)\[\]\{\}_:·,\.\-]", "", s)
    return s.lower()


def cmp_amount(name):
    """제품명 문자열에서 함량(숫자+단위) 추출 — 함량을 직접 조회하지 않았을 때의 대체 수단."""
    m = re.search(
        r"\d+(?:\.\d+)?\s*(?:mg|g|mcg|μg|㎍|IU|mEq|mL|%)\s*(?:/\s*\d+(?:\.\d+)?\s*(?:mg|mL))?",
        name or "",
    )
    return m.group(0).strip() if m else ""


_CMP_FORM_MAP = [
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


def cmp_form(name):
    """제품명 문자열에서 제형 추정 — app.py의 result_df에는 제형 컬럼이 따로 없어 항상 이 방식으로 구한다."""
    for ko, syns in _CMP_FORM_MAP:
        for s in syns:
            if s.lower() in (name or "").lower():
                return ko
    return ""


def parse_compare_table(text):
    """사용자가 붙여넣은 비교표 텍스트를 파싱한다.
    한 줄 = '필드명|값', 빈 줄 = 제품 구분. (app(1).py의 parse_compare)"""
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


def compare_one_field(field, table_val, src_val, extra_not_fetched=False):
    """비교표 기재값(table_val)과 원문값(src_val) 하나를 판정한다. (app(1).py의 compare_one 기반)
    extra_not_fetched=True면 '3번 추가 조회 항목'을 아예 선택하지 않아 원문 자체를 조회하지
    않은 상태이므로, 단순 빈 값과 구분해 안내한다."""
    tv = (table_val or "").strip()
    sv = (src_val or "").strip()
    if extra_not_fetched and not sv:
        return "⚪ 확인 불가", "이 항목은 '3번 추가 조회 항목'에서 선택하지 않아 원문을 조회하지 않았습니다."
    if not tv and not sv:
        return "⚪ 확인 불가", "양쪽 모두 비어있음"
    if not tv:
        return "🔴 수정 필요", "비교표에 기재 누락"
    if not sv:
        return "⚪ 확인 불가", "원문(API)에 해당 항목 값이 비어있음"
    if field in ("약가", "함량"):
        tn = re.sub(r"[^\d.]", "", tv)
        sn = re.sub(r"[^\d.]", "", sv)
        if not tn or not sn:
            return "⚪ 확인 불가", "수치 파싱 실패"
        return ("🟢 일치" if tn == sn else "🔴 수정 필요"), f"비교표={tn}, 원문={sn}"
    if field in CMP_SEMANTIC_FIELDS:
        return "🟠 의미 단위 — LLM 확인 필요", "지침에 따른 의미 비교가 필요합니다 (아래 LLM 프롬프트 참고)"
    tn, sn = cmp_normalize(tv), cmp_normalize(sv)
    if tn == sn:
        return "🟢 일치", "정규화 일치"
    if tn in sn or sn in tn:
        return "🟡 확인 필요", "정규화 부분 일치 (표현 차이 가능)"
    return "🔴 수정 필요", f"정규화 불일치: [{tv}] vs [{sv}]"


def match_compare_item_to_result_row(cmp_item, result_df):
    """비교표 항목의 '의약품명'을 result_df(3번에서 조회한 원문)의 허가제품명과 매칭한다."""
    name = cmp_normalize(cmp_item.get("의약품명", ""))
    if not name or result_df.empty:
        return None
    # 1) 완전 일치
    for _, row in result_df.iterrows():
        if cmp_normalize(row.get("허가제품명", "")) == name:
            return row
    # 2) 부분 포함(양방향)
    for _, row in result_df.iterrows():
        rn = cmp_normalize(row.get("허가제품명", ""))
        if rn and (name in rn or rn in name):
            return row
    return None


def build_cmp_source_value(field, result_row):
    """result_df 한 행에서 비교표 필드에 대응하는 '원문값'을 구한다.
    반환: (원문값, 이 항목을 3번에서 선택하지 않아 애초에 조회되지 않은 것인지 여부)"""
    column = CMP_FIELD_TO_RESULT_COLUMN.get(field)
    product_name = result_row.get("허가제품명", "")
    if field == "제형":
        return cmp_form(product_name), False
    if column is None:
        return "", False
    value = clean_whitespace(result_row.get(column, "")) if column in result_row.index else ""
    if not value and field == "함량":
        # 원료약품및분량을 조회하지 않았거나 비어있으면 제품명에서 규칙 기반으로 대체 추출
        derived = cmp_amount(product_name)
        return derived, (column in CMP_OPTIONAL_EXTRA_COLUMNS and column not in result_row.index)
    extra_not_fetched = (column in CMP_OPTIONAL_EXTRA_COLUMNS) and (column not in result_row.index)
    return value, extra_not_fetched


st.set_page_config(page_title="의약품 통합 조회", page_icon="💊", layout="wide")
st.title("💊 의약품 허가정보·약가 통합 조회")
st.caption("식약처 허가·상세정보와 심평원 약가를 결합해 조회합니다. 데이터의 기준일과 API 응답을 함께 확인하세요.")

with st.sidebar:
    st.header("설정")
    st.markdown("API 키는 코드에 저장하지 말고 아래 입력란 또는 Streamlit secrets를 사용하세요.")
    try:
        secret_mfds = st.secrets.get("MFDS_KEY", "")
        secret_hira = st.secrets.get("HIRA_KEY", "")
    except Exception:
        secret_mfds, secret_hira = "", ""
    mfds_key = st.text_input("식약처 인증키 (디코딩된 키)", value=secret_mfds, type="password")
    hira_key = st.text_input("심평원 인증키 (디코딩된 키)", value=secret_hira, type="password")
    if st.button("허가목록 캐시 새로고침"):
        st.cache_data.clear()
        for cache_path in (LIST_FILE, LIST_META_FILE, TEMP_FILE):
            if cache_path.exists():
                cache_path.unlink()
        st.rerun()
    st.caption("허가목록은 KST 기준 하루 1회만 자동 갱신합니다. API 키는 파일에 저장하지 않고 Streamlit Secrets/입력값으로만 재사용합니다.")
    st.divider()
    st.markdown("**저장 위치**")
    st.code(str(DATA_DIR), language="text")
    st.divider()
    st.markdown("**DUR 품목리스트 업로드**")
    st.caption("엑셀에 '제품코드'(또는 제품코드A/B, 약품코드) 열이 있어야 합니다. 매달 새 파일로 다시 올리면 그 종류만 갱신됩니다.")
    if "dur_indices" not in st.session_state:
        st.session_state.dur_indices = {}
    for category in DUR_CATEGORIES:
        uploaded = st.file_uploader(category, type=["xlsx", "xls", "xlsb"], key=f"dur_upload_{category}")
        if uploaded is not None:
            try:
                # 파일 내용이 바뀌지 않았으면 다시 읽지 않습니다 (그래서 검색창 등 다른 위젯을
                # 조작해도 매번 재파싱으로 느려지지 않습니다).
                dur_df, header_row, dur_index, code_columns = parse_dur_excel_cached(uploaded.getvalue(), uploaded.name)
            except Exception as exc:
                st.error(f"[{category}] '{uploaded.name}' 읽기 실패: {exc}")
            else:
                if not code_columns:
                    st.warning(
                        f"[{category}] '{uploaded.name}'에서 제품코드/약품코드 열을 찾지 못했습니다. "
                        f"(헤더로 인식한 행: {header_row}, 전체 열: {list(dur_df.columns)})"
                    )
                else:
                    st.session_state.dur_indices[category] = dur_index
                    st.success(f"[{category}] {len(dur_df):,}행, 코드열 {code_columns}, 매칭 제품코드 {len(dur_index):,}종 적용됨")

cache_day = current_kst_date()
cache_fresh = list_cache_is_fresh()
if not cache_fresh and not mfds_key:
    st.warning("오늘 날짜의 허가목록 캐시가 없어 식약처 인증키가 필요합니다. 심평원 인증키는 실제 조회 시 필요합니다.")
    st.stop()
if not hira_key:
    st.info("검색목록은 열 수 있지만, 약가·상세정보 조회에는 심평원 인증키가 필요합니다.")

try:
    all_rows = load_permitted_drugs(mfds_key, str(DATA_DIR), cache_day)
except Exception as exc:
    st.error(f"허가목록 수집에 실패했습니다: {exc}")
    st.exception(exc)
    st.stop()

normal_rows = [row for row in all_rows if row.get("CANCEL_NAME") == "정상"]
by_seq = {row.get("ITEM_SEQ", ""): row for row in normal_rows}
st.success(f"정상 품목 {len(normal_rows):,}건 준비 완료")

if "selection" not in st.session_state:
    st.session_state.selection = []
if "last_result" not in st.session_state:
    st.session_state.last_result = None
if "last_errors" not in st.session_state:
    st.session_state.last_errors = []

st.subheader("1. 의약품 검색")
query = st.text_input("의약품명 또는 제약사명", placeholder="예: 타이레놀, 한미약품")
matches = []
if query.strip():
    q = query.strip().casefold()
    matches = [row for row in normal_rows if q in row.get("ITEM_NAME", "").casefold() or q in row.get("ENTP_NAME", "").casefold()][:200]
    st.caption(f"검색결과 {len(matches)}건 표시 (최대 200건)")

# 검색 결과를 드롭다운이 아닌 전체 행이 보이는 선택 가능한 표로 표시합니다.
if matches:
    search_df = pd.DataFrame([
        {"의약품명": row.get("ITEM_NAME", ""), "제약사": row.get("ENTP_NAME", ""), "품목코드": row.get("ITEM_SEQ", "")}
        for row in matches
    ])
    search_event = st.dataframe(
        search_df,
        use_container_width=True,
        hide_index=True,
        height=min(520, 36 + len(search_df) * 35),
        selection_mode="multi-row",
        on_select="rerun",
        # 검색어별로 위젯 상태를 분리해 첫 행이 자동 선택되지 않도록 합니다.
        key=f"search_results_table_{query.strip().casefold()}",
    )
    # selection.rows는 현재 검색표의 위치 인덱스이므로 반드시 iloc로 매핑합니다.
    search_selected_seqs = [
        str(search_df.iloc[index]["품목코드"])
        for index in search_event.selection.rows
        if 0 <= index < len(search_df)
    ]
    st.caption(f"검색 결과에서 선택한 품목: **{len(search_selected_seqs)}건**")
    if st.button("선택한 검색 결과를 조회 목록에 추가", disabled=not search_selected_seqs, key="add_search_selection"):
        for seq in search_selected_seqs:
            if seq and seq not in st.session_state.selection:
                st.session_state.selection.append(seq)
        st.rerun()

st.subheader("2. 조회할 품목")
selected_rows = [by_seq[seq] for seq in st.session_state.selection if seq in by_seq]
if selected_rows:
    selected_df = pd.DataFrame([
        {"의약품명": row.get("ITEM_NAME", ""), "제약사": row.get("ENTP_NAME", ""), "품목코드": row.get("ITEM_SEQ", "")}
        for row in selected_rows
    ])
    st.caption("아래 표에서 제거할 행을 선택한 뒤 버튼을 누르세요.")
    selected_event = st.dataframe(
        selected_df,
        use_container_width=True,
        hide_index=True,
        height=min(360, 36 + len(selected_df) * 35),
        selection_mode="multi-row",
        on_select="rerun",
        key="selected_items_table",
    )
    if st.button("선택한 품목 제거", disabled=not selected_event.selection.rows):
        remove_seq = {str(selected_df.iloc[index]["품목코드"]) for index in selected_event.selection.rows}
        st.session_state.selection = [seq for seq in st.session_state.selection if seq not in remove_seq]
        st.rerun()
else:
    st.info("1번 검색 결과 표에서 조회할 행을 클릭하세요.")
st.write(f"현재 선택된 품목: **{len(st.session_state.selection)}건**")

st.subheader("3. 추가 조회 항목")
if "preset_new_drug_intro" not in st.session_state:
    st.session_state.preset_new_drug_intro = all(st.session_state.get(f"extra_{key}", False) for key in NEW_DRUG_PRESET_KEYS)
if "preset_new_drug_intro_prev" not in st.session_state:
    st.session_state.preset_new_drug_intro_prev = st.session_state.preset_new_drug_intro
st.checkbox(
    "[신약 도입 준비]",
    key="preset_new_drug_intro",
    help="영문 제품명, ATC 코드, 포장단위, 주성분 영문명, 원료약품 및 분량, 유효기간, 성상, 전문·일반의약품 구분, 보관정보, 보관 및 취급상의 주의사항을 한 번에 선택/해제합니다.",
)
if st.session_state.preset_new_drug_intro != st.session_state.preset_new_drug_intro_prev:
    for preset_key in NEW_DRUG_PRESET_KEYS:
        st.session_state[f"extra_{preset_key}"] = st.session_state.preset_new_drug_intro
    st.session_state.preset_new_drug_intro_prev = st.session_state.preset_new_drug_intro
    st.rerun()
selected_extras = []
extra_columns = st.columns(3)
for index, key in enumerate(EXTRA_FIELD_ORDER):
    with extra_columns[index % 3]:
        if st.checkbox(EXTRA_FIELD_LABELS[key], key=f"extra_{key}"):
            selected_extras.append(key)

summary_view = st.checkbox(
    "요약 버전으로 보기",
    help="AI가 새로운 의학적 판단을 생성하지 않고, 원문에서 앞부분과 문장 단위 내용을 발췌해 짧게 표시합니다.",
)

if st.button("선택한 품목 조회", type="primary", disabled=not st.session_state.selection or not (mfds_key and hira_key)):
    selected_rows = [by_seq[seq] for seq in st.session_state.selection if seq in by_seq]
    with st.spinner("식약처 상세정보와 심평원 약가를 조회하는 중입니다…"):
        result_df, errors = lookup_selected(selected_rows, mfds_key, hira_key, selected_extras)
    st.session_state.last_result = result_df
    st.session_state.last_errors = errors
    st.rerun()

if st.session_state.last_result is not None:
    st.subheader("조회 결과")
    result_df = st.session_state.last_result
    filtered_result_df = result_df
    result_tab, comparison_tab, dur_tab, cmp_tab = st.tabs(["상세 결과", "여러 약품 비교표", "DUR 확인", "🧾 비교표 검증"])
    with result_tab:
        transpose_view = st.checkbox(
            "행/열 전환", key="result_transpose", value=True,
            help="표시와 CSV 다운로드 모두 행/열을 전환합니다. 켜면 약 하나가 세로줄 하나가 되고, 화면 폭에 맞춰 각 약의 너비가 자동으로 좁아지거나 넓어집니다.",
        )
        displayed_df = make_display_df(filtered_result_df, summary_view=summary_view, transpose_view=transpose_view)
        if summary_view:
            st.caption("요약 버전: 원문에서 문장 단위로 발췌한 표시용 요약입니다. 임상적 판단을 대신하지 않습니다.")
        render_resizable_wrapped_table(displayed_df, show_index=transpose_view, height=720, table_key="detail")
        # 화면에 표시한 동일한 DataFrame을 사용하므로 행/열 전환 상태가 CSV에도 반영됩니다.
        csv_bytes = displayed_df.to_csv(index=transpose_view, encoding="utf-8-sig").encode("utf-8-sig")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_suffix = "_행열전환" if transpose_view else ""
        st.download_button("결과 CSV 다운로드", data=csv_bytes, file_name=f"의약품조회결과_{timestamp}{csv_suffix}.csv", mime="text/csv")
    with comparison_tab:
        if len(filtered_result_df) < 2:
            st.info("두 품목 이상 조회하면 비교표가 표시됩니다.")
        else:
            comparison_df = make_comparison_df(make_summary_df(filtered_result_df) if summary_view else filtered_result_df)
            st.caption("행은 조회 항목, 열은 의약품입니다. 헤더 경계를 드래그해 약품별 컬럼 너비를 조절할 수 있습니다.")
            render_resizable_wrapped_table(comparison_df, show_index=True, height=760, table_key="comparison")
            comparison_csv = comparison_df.to_csv(index=True, encoding="utf-8-sig").encode("utf-8-sig")
            st.download_button("비교표 CSV 다운로드", data=comparison_csv, file_name=f"의약품비교표_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", mime="text/csv")
    with dur_tab:
        if not st.session_state.dur_indices:
            st.info("사이드바의 'DUR 품목리스트 업로드'에서 먼저 엑셀을 올려주세요.")
        else:
            if st.button("선택한 품목 DUR 확인", key="run_dur_check"):
                dur_call_counter = {"hira": 0, "mfds": 0}
                dur_errors = []
                dur_cache_detail = load_json_cache(CACHE_DETAIL_FILE)
                with st.spinner("DUR(병용금기 등) 확인 중입니다…"):
                    dur_result_df = check_dur(
                        selected_rows, mfds_key, st.session_state.dur_indices,
                        dur_cache_detail, dur_call_counter, dur_errors,
                    )
                save_json_cache(CACHE_DETAIL_FILE, dur_cache_detail)
                st.session_state.dur_result = dur_result_df
                st.session_state.dur_errors = dur_errors
            if "dur_result" in st.session_state:
                dur_result_df = st.session_state.dur_result
                if dur_result_df.empty:
                    st.success("선택한 품목 중 업로드된 DUR 리스트에 해당하는 품목이 없습니다.")
                else:
                    st.dataframe(dur_result_df, use_container_width=True, hide_index=True)
                    dur_csv = dur_result_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                    st.download_button(
                        "DUR 확인 결과 CSV 다운로드", data=dur_csv,
                        file_name=f"DUR확인결과_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", mime="text/csv",
                    )
                if st.session_state.get("dur_errors"):
                    with st.expander(f"DUR 확인 중 경고/오류 {len(st.session_state.dur_errors)}건"):
                        for error in st.session_state.dur_errors:
                            st.warning(error)

    with cmp_tab:
        st.caption(
            "3번에서 조회한 위 원문(result_df)을 기준으로, 사용자가 붙여넣은 심의자료 비교표를 검증합니다. "
            "**비교표에 실제로 적은 항목만** 판정합니다 — 적지 않은 항목은 원문에 있어도 검토하지 않습니다."
        )
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
        cmp_text = st.text_area(
            "비교표 입력 (한 줄 = '필드명|값', 빈 줄 = 제품 구분)", value=cmp_default, height=200, key="cmp_text",
        )
        cmp_items = parse_compare_table(cmp_text)

        if not cmp_items:
            st.info("비교표를 입력하면 여기에 검증 결과가 표시됩니다.")
        else:
            rows_for_table = []
            for cmp_item in cmp_items:
                product_label = cmp_item.get("의약품명", "(의약품명 미기재)")
                result_row = match_compare_item_to_result_row(cmp_item, filtered_result_df)
                if result_row is None:
                    rows_for_table.append({
                        "품목": product_label, "필드": "(제품 매칭)",
                        "비교표": product_label, "원문": "",
                        "판정": "⚪ 확인 불가",
                        "근거": "위 원문 조회 목록에서 이 제품명과 일치/유사한 항목을 찾지 못했습니다. 3번에서 해당 품목을 함께 조회했는지 확인하세요.",
                    })
                    continue
                # 비교표에 실제로 적힌 필드만 검토한다 ('의약품명'·'비고'는 식별용이라 제외)
                for field, tbl_val in cmp_item.items():
                    if field in ("의약품명", "비고"):
                        continue
                    src_val, extra_not_fetched = build_cmp_source_value(field, result_row)
                    status, reason = compare_one_field(field, tbl_val, src_val, extra_not_fetched)
                    rows_for_table.append({
                        "품목": result_row.get("허가제품명", product_label), "필드": field,
                        "비교표": tbl_val or "(빈칸)", "원문": src_val or "(없음)",
                        "판정": status, "근거": reason,
                    })

            color_map = {
                "🟢 일치": "#16a34a", "🔴 수정 필요": "#dc2626",
                "🟡 확인 필요": "#ca8a04", "🟠 의미 단위 — LLM 확인 필요": "#ea580c",
                "⚪ 확인 불가": "#9ca3af",
            }
            for row in rows_for_table:
                color = color_map.get(row["판정"], "#9ca3af")
                st.markdown(
                    f"<div style='border-left:5px solid {color};padding:8px 12px;"
                    f"background:#fafafa;margin-bottom:6px;border-radius:4px'>"
                    f"<b>{row['품목']}</b> · <b>{row['필드']}</b> · "
                    f"<span style='color:{color};font-weight:700'>{row['판정']}</span>"
                    f"<br/><small>비교표: {row['비교표'][:160]}</small><br/>"
                    f"<small>원문: {row['원문'][:160]}</small><br/>"
                    f"<small style='color:#666'>근거: {row['근거']}</small></div>",
                    unsafe_allow_html=True,
                )

            llm_prompts = [r for r in rows_for_table if "🟠" in r["판정"]]
            if llm_prompts:
                with st.expander(f"🤖 의미 단위 LLM 프롬프트 ({len(llm_prompts)}건) — 적응증·용법용량·소아·금기"):
                    st.caption(
                        "숫자·단순일치는 이미 규칙으로 판정했습니다. 이 항목들은 의미(요약/범위/조건삭제 여부) 비교가 "
                        "필요해 LLM 확인용 프롬프트로 제공합니다. 그대로 복사해 Claude 등에 붙여넣으세요."
                    )
                    for i, row in enumerate(llm_prompts, 1):
                        prompt = (
                            f"[비교표 기재 내용]\n\"{row['비교표']}\"\n\n"
                            f"[공식 원문 — 식약처/심평원]\n\"{row['원문']}\"\n\n"
                            f"품목: {row['품목']}\n필드: {row['필드']}\n\n"
                            "지침: 비교표 문장이 원문에 비추어 사실인지만 판단하라. 표현이 달라도 핵심 의미가 "
                            "유지되면 '일치'다. 대상환자/연령/선행치료/병용요법 등 핵심 조건이 삭제되어 범위가 "
                            "넓어지거나 원문에 없는 내용이 있으면 '수정필요'다. 원문에 없는 정보는 추측하지 마라.\n"
                            "판정 후보: 🟢 일치 / 🟡 표현차이 / 🔴 수정필요 / ⚪ 확인불가"
                        )
                        st.text_area(f"프롬프트 #{i} · {row['품목']} · {row['필드']}", value=prompt, height=160, key=f"cmp_llm_p_{i}")

            cmp_buf = ["품목,필드,비교표,원문,판정,근거"]
            for row in rows_for_table:
                cmp_buf.append(
                    f"{row['품목']},{row['필드']},\"{row['비교표']}\",\"{row['원문']}\",{row['판정']},{row['근거']}"
                )
            cmp_csv_bytes = ("\uFEFF" + "\n".join(cmp_buf)).encode("utf-8")
            st.download_button(
                "⬇ 비교표 검증결과.csv 다운로드", data=cmp_csv_bytes,
                file_name=f"비교표검증결과_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", mime="text/csv",
                key="cmp_csv_download",
            )

    if st.session_state.last_errors:
        with st.expander(f"API 경고/오류 {len(st.session_state.last_errors)}건"):
            for error in st.session_state.last_errors:
                st.warning(error)
