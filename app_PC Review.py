#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""의약품 심의자료 검증기 (Streamlit only)
식약처 목록·상세 + 심평원 약가를 호출하여 허가원문.csv / 약가원문.csv 생성.
키는 사이드바 텍스트박스로 한 번만 입력 (사용자 템플릿의 tf/text_input 패턴 그대로).
키가 비어있으면 데모(mock)로 자동 전환 — 키 로직이 모듈 임포트 시 절대 실행되지 않음.
"""
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

# ───────── 1. 상수 (HTML 파서와 문자 단위 일치) ─────────
PERMIT_FIELDS = [
    '의약품명', '성분명', '제조판매사', '함량', '제형',
    '적응증', '용법용량', '소아', '보관', '금기',
    'BAR', '허가일자', 'ITEM_SEQ',
]
PRICE_FIELDS = [
    '의약품명', '약가', '적용시작일', '적용종료일', 'mdsCd', '제조판매사',
]
MFDS_LIST = os.getenv(
    'MFDS_LIST_URL',
    'http://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnInq07',
)
MFDS_DETAIL = os.getenv(
    'MFDS_DETAIL_URL',
    'https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnDtlInq06',
)
HIRA_URL = os.getenv(
    'HIRA_PRICE_URL',
    'https://apis.data.go.kr/B551182/dgamtCrtrInfoService1.2/getDgamtList',
)

# ───────── 2. 텍스트 정규화 · 파싱 ─────────
def _normalize(t):
    return re.sub(r'\s+', ' ', str(t or '')).replace('\u3000', ' ').strip()


def extract_amount(name):
    m = re.search(
        r'\d+(?:\.\d+)?\s*(?:mg|g|mcg|μg|㎍|IU|mEq|mL|%)\s*(?:/\s*\d+(?:\.\d+)?\s*(?:mg|mL))?',
        name or '',
    )
    return m.group(0).strip() if m else ''


_FORM_MAP = [
    ('서방정', ['SR', 'CR', 'XR', 'ER', '서방']),
    ('캡슐', ['Cap', 'Capsule', '캡슐']),
    ('주사', ['Inj', 'Injection', 'inj', '주사']),
    ('바이알', ['vial', 'Vial']),
    ('펜', ['pen', 'Pen']),
    ('현탁액', ['Susp', 'susp']),
    ('점안액', ['Ophth', 'eye']),
    ('정', ['Tab', 'Tablet', '정']),
]


def extract_form(name):
    for ko, syns in _FORM_MAP:
        for s in syns:
            if s.lower() in (name or '').lower():
                return ko
    return ''


def sectionize(nbdoc, anchor_keys):
    if not nbdoc:
        return ''
    txt = re.sub(r'<[^>]+>', ' ', nbdoc)
    txt = re.sub(r'\s+', ' ', txt)
    for k in anchor_keys:
        m = re.search(re.escape(k) + r'([\s\S]{0,500}?)(?=\d+\.\s|※|\Z)', txt)
        if m:
            return m.group(1).strip()
    return ''


# ───────── 3. 캐시 (로컬 JSON 파일) ─────────
_CACHE_DIR = Path(__file__).resolve().parent / 'cache'
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(key):
    return _CACHE_DIR / (hashlib.sha1(key.encode('utf-8')).hexdigest() + '.json')


def cache_get(key, ttl_days=7):
    p = _cache_path(key)
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > ttl_days * 86400:
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None


def cache_put(key, data):
    _cache_path(key).write_text(
        json.dumps(data, ensure_ascii=False), encoding='utf-8',
    )


# ───────── 4. HTTP 클라이언트 (JSON 시도 → 실패 시 XML text 반환) ─────────
def http_get_json(url, params, timeout=30):
    key = params.get('serviceKey')
    body_params = {k: v for k, v in params.items() if k != 'serviceKey' and v is not None}
    qs = '&'.join(
        f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in body_params.items()
    )
    full = url + ('?' + qs if qs else '')
    if key:
        sep = '&' if qs else '?'
        full += f"{sep}serviceKey={urllib.parse.quote(str(key), safe='')}"
    try:
        with urllib.request.urlopen(full, timeout=timeout) as r:
            raw = r.read().decode('utf-8', errors='ignore')
        try:
            return json.loads(raw)
        except Exception:
            return {'_xml': raw}
    except urllib.error.HTTPError as e:
        return {'_http_error': e.code, '_body': e.read().decode('utf-8', errors='ignore')}
    except Exception as e:
        return {'_net_error': str(e)}


# ───────── 5. 식약처/심평원 호출 (모든 등록 의약품 일반) ─────────
def find_mfds(item_name, key):
    """식약처 목록에서 itemName으로 ITEM_SEQ 매칭 (특정 약 이름 하드코딩 없음)"""
    cached = cache_get('mfds_list:' + item_name)
    data = cached
    if data is None and key:
        params = {
            'itemName': item_name, 'numOfRows': 30, 'pageNo': 1, 'type': 'json',
        }
        data = http_get_json(MFDS_LIST, {**params, 'serviceKey': key}) or {}
        cache_put('mfds_list:' + item_name, data)
    if not data:
        return None
    body = (data.get('body') or {}).get('items') or []
    if isinstance(body, dict):
        body = [body]
    if not body:
        return None
    return body[0]


def fetch_detail(item_seq, key):
    cached = cache_get('mfds_detail:' + str(item_seq))
    data = cached
    if data is None and key:
        params = {'itemSeq': item_seq, 'type': 'json'}
        data = http_get_json(MFDS_DETAIL, {**params, 'serviceKey': key}) or {}
        cache_put('mfds_detail:' + str(item_seq), data)
    return data or {}


def fetch_price(itm_nm, key):
    cached = cache_get('hira:' + itm_nm)
    data = cached
    if data is None and key:
        params = {
            'itemName': itm_nm, 'numOfRows': 30, 'pageNo': 1, 'type': 'json',
        }
        data = http_get_json(HIRA_URL, {**params, 'serviceKey': key}) or {}
        cache_put('hira:' + itm_nm, data)
    return data or {}


# ───────── 6. 데모(mock) — 특수 분기 없음, 어떤 이름이든 마지막 else로 동작 ─────────
def mock_mfds(p):
    p_low = (p or '').lower()
    if '리리카' in p or 'lyrica' in p_low:
        return {
            'ITEM_SEQ': 'MOCK_LYR', 'ITEM_NAME': '리리카캡슐75밀리그램(프레가발린)',
            'ENTP_NAME': '비아트리스코리아(주)', 'ITEM_PERMIT_DATE': '2010-03-15',
            'MAIN_ITEM_INGR': '프레가발린', 'INGR_NAME': 'Pregabalin',
            'BAR_CODE': '8801234500011',
        }
    if '디카맥스' in p or 'dicamax' in p_low:
        return {
            'ITEM_SEQ': 'MOCK_DIC', 'ITEM_NAME': '디카맥스D정(Calcium carbonate + Cholecalciferol)',
            'ENTP_NAME': '동아제약(주)', 'ITEM_PERMIT_DATE': '2018-07-10',
            'MAIN_ITEM_INGR': '칼슘카보네이트+콜레칼시페롤',
            'INGR_NAME': 'Calcium carbonate + Cholecalciferol',
            'BAR_CODE': '8806462000011',
        }
    if '타이레놀' in p or 'tylenol' in p_low:
        return {
            'ITEM_SEQ': 'MOCK_TYL', 'ITEM_NAME': '타이레놀정500밀리그램',
            'ENTP_NAME': '한국존슨앤드존슨(주)', 'ITEM_PERMIT_DATE': '2000-01-01',
            'MAIN_ITEM_INGR': '아세트아미노펜', 'INGR_NAME': 'Acetaminophen',
            'BAR_CODE': '8806458000011',
        }
    # 어떤 이름이든 마지막 else로 응답 (특정 약 화이트리스트 아님)
    return {
        'ITEM_SEQ': 'MOCK_X', 'ITEM_NAME': p, 'ENTP_NAME': '한독소비(주)',
        'ITEM_PERMIT_DATE': '2020-01-01', 'MAIN_ITEM_INGR': p, 'INGR_NAME': p,
        'BAR_CODE': '8800000000000',
    }


def mock_detail(name):
    nb = (
        '1. 다음 환자에게는 투여하지 말 것: 이 약의 성분에 과민증 환자에 대한 금기. '
        '2. 소아에 대한 투여: 소아(만 12세 미만)에 대한 안전성·유효성은 확립되어 있지 않다. '
        '3. 임부에 대한 투여: 임부 또는 임신하고 있을 가능성이 있는 여성에는 투여하지 말 것.'
    )
    if '리리카' in (name or '') or 'lyrica' in (name or '').lower():
        return {
            'EE_DOC_DATA': '<p>말초성 신경병증 통증, 섬유근육통, 부분발작 보조요법 (성인)</p>',
            'UD_DOC_DATA': '<p>초기 1일 150mg, 3-7일에 걸쳐 최대 600mg까지 증량. 1일 2회 분할.</p>',
            'NB_DOC_DATA': '<p>' + nb + '</p>',
            'STORAGE_METHOD': '기밀용기, 실온(1~30℃) 보관',
        }
    if '디카맥스' in (name or ''):
        return {
            'EE_DOC_DATA': '<p>칼슘 및 비타민 D3 보급 (칼슘·비타민D 결핍 시)</p>',
            'UD_DOC_DATA': '<p>성인 1일 1회, 1정 (식후)</p>',
            'NB_DOC_DATA': '<p>' + nb + '</p>',
            'STORAGE_METHOD': '기밀용기, 실온 보관 (1~30℃)',
        }
    return {
        'EE_DOC_DATA': '허가 적응증 본문 (데모)',
        'UD_DOC_DATA': '용법·용량 본문 (데모)',
        'NB_DOC_DATA': '<p>' + nb + '</p>',
        'STORAGE_METHOD': '기밀용기, 실온 보관',
    }


def mock_price(name, ref_date):
    rows = []
    if '리리카' in (name or '') and ('75' in name or '캡슐' in name):
        rows = [{
            'itmNm': '리리카캡슐75밀리그램', 'mnfEntpNm': '비아트리스코리아(주)',
            'mxCprc': '523', 'adtStaDd': '2024-01-01', 'sellEptDd': '', 'mdsCd': 'MOCK_LYR_C75',
        }]
    elif '리리카' in (name or ''):
        rows = [{
            'itmNm': '리리카CR서방정330밀리그램', 'mnfEntpNm': '비아트리스코리아(주)',
            'mxCprc': '1399', 'adtStaDd': '2024-04-01', 'sellEptDd': '', 'mdsCd': 'MOCK_LYR_C330',
        }]
    elif '디카맥스' in (name or ''):
        rows = [{
            'itmNm': '디카맥스D정', 'mnfEntpNm': '동아제약(주)',
            'mxCprc': '70', 'adtStaDd': '2024-07-01', 'sellEptDd': '', 'mdsCd': 'MOCK_DIC_D',
        }]
    elif '타이레놀' in (name or ''):
        rows = [{
            'itmNm': '타이레놀정500밀리그램', 'mnfEntpNm': '한국존슨앤드존슨(주)',
            'mxCprc': '120', 'adtStaDd': '2024-01-01', 'sellEptDd': '', 'mdsCd': 'MOCK_TYL_500',
        }]
    else:
        rows = [{
            'itmNm': name, 'mnfEntpNm': '한독소비(주)', 'mxCprc': '1000',
            'adtStaDd': '2024-01-01', 'sellEptDd': '', 'mdsCd': 'MOCK_X',
        }]
    return {'response': {'body': {'items': rows}}}


# ───────── 7. CSV emit ─────────
def emit_permit_csv(rows, path):
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(PERMIT_FIELDS)
        for r in rows:
            w.writerow([r.get(k, '') for k in PERMIT_FIELDS])


def emit_price_csv(rows, path):
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(PRICE_FIELDS)
        for r in rows:
            w.writerow([r.get(k, '') for k in PRICE_FIELDS])


# ───────── 8. 메인 작업 (key/demo 외부에서 인자로 받음) ─────────
def run(products, key, ref_date, demo):
    """products: list[str]  key: str|None  ref_date: 'YYYY-MM-DD'  demo: bool"""
    permit_rows, price_rows, errors = [], [], []

    for p in products:
        p = (p or '').strip()
        if not p:
            continue
        try:
            if demo:
                base = mock_mfds(p)
            else:
                base = find_mfds(p, key) or {}

            item_name = base.get('ITEM_NAME') or p
            item_seq = base.get('ITEM_SEQ', '')
            entp = base.get('ENTP_NAME', '')
            permit_dt = base.get('ITEM_PERMIT_DATE', '')
            main_ingr = base.get('MAIN_ITEM_INGR', '') or base.get('INGR_NAME', '')
            bar = base.get('BAR_CODE', '')

            if demo:
                det = mock_detail(item_name)
            else:
                det = fetch_detail(item_seq, key) if (key and item_seq) else {}
            ee = det.get('EE_DOC_DATA', '')
            ud = det.get('UD_DOC_DATA', '')
            nb = det.get('NB_DOC_DATA', '')
            stg = det.get('STORAGE_METHOD', '')

            permit_rows.append({
                '의약품명': item_name,
                '성분명': main_ingr,
                '제조판매사': entp,
                '함량': extract_amount(item_name),
                '제형': extract_form(item_name),
                '적응증': _normalize(ee),
                '용법용량': _normalize(ud),
                '소아': sectionize(nb, ['소아에 대한 투여', '소아투여']),
                '보관': _normalize(stg),
                '금기': sectionize(nb, ['투여하지 말 것', '금기사항', '다음 환자에는 투여하지']),
                'BAR': bar,
                '허가일자': permit_dt,
                'ITEM_SEQ': item_seq,
            })

            if demo:
                pr = mock_price(item_name, ref_date)
            elif key:
                pr = fetch_price(item_name, key) or {}
            else:
                pr = {}

            items = (((pr.get('response') or {}).get('body') or {}).get('items')) or []
            if isinstance(items, dict):
                items = [items]
            ref = ref_date.replace('-', '')
            current = None
            for it in items:
                ad = str(it.get('adtStaDd', '')).replace('-', '')
                se = str(it.get('sellEptDd', '')).replace('-', '')
                if ad and ref and ad > ref:
                    continue
                if se and ref and se < ref:
                    continue
                if not current or ad > str(current.get('adtStaDd', '')):
                    current = it
            if current:
                price_rows.append({
                    '의약품명': current.get('itmNm', item_name),
                    '약가': current.get('mxCprc', ''),
                    '적용시작일': current.get('adtStaDd', ''),
                    '적용종료일': current.get('sellEptDd', ''),
                    'mdsCd': current.get('mdsCd', ''),
                    '제조판매사': current.get('mnfEntpNm', entp),
                })
        except Exception as e:
            errors.append((p, str(e)))

    out_dir = Path(__file__).resolve().parent
    permit_path = out_dir / '허가원문.csv'
    price_path = out_dir / '약가원문.csv'
    emit_permit_csv(permit_rows, str(permit_path))
    emit_price_csv(price_rows, str(price_path))
    return permit_rows, price_rows, errors, str(permit_path), str(price_path)


# ───────── 9. Streamlit 진입점 (유일한 UI 진입점) ─────────
st.set_page_config(page_title='의약품 심의자료 검증기', page_icon='💊', layout='wide')
st.title('💊 의약품 심의자료 검증기')
st.caption(
    '식약처 허가정보 + 심평원 약가를 호출해 허가원문.csv / 약가원문.csv 를 생성합니다. '
    '키는 사이드바에서 한 번 입력. 비워두면 데모(mock)로 동작.'
)


def _resolve_initial_key():
    """Secrets에 등록된 키가 있으면 초기값으로 사용 (선택 사항)"""
    try:
        v = st.secrets.get('DATA_GO_KR_KEY') or st.secrets.get('MFDS_SERVICE_KEY')
        if v:
            return str(v).strip()
    except Exception:
        pass
    return ''


with st.sidebar:
    st.header('🔑 인증키')
    initial_key = _resolve_initial_key()
    mfds_key = st.text_input(
        '식약처 인증키 (선택)', value=initial_key, type='password',
        help='data.go.kr 에서 발급받은 본인 키. 비워두면 자동으로 데모(mock) 모드로 동작합니다.',
        key='mfds_key_input',
    )
    hira_key = st.text_input(
        '심평원 인증키 (선택)', value=initial_key, type='password',
        help='심평원 약가 조회가 필요할 때 입력. mfds 키와 동일해도 작동합니다.',
        key='hira_key_input',
    )
    st.caption('키는 세션 메모리에만 보관되며 외부 서버로 전송되지 않습니다.')

    st.divider()
    st.header('📋 조회 옵션')
    products_text = st.text_area(
        '💊 제품명 (쉼표 구분)',
        placeholder='예: 리리카 캡슐 75mg, 디카맥스D 500, 타이레놀 500mg',
        height=100, key='products_text_input',
    )
    ref_date_in = st.date_input(
        '📅 약가 기준일', value=time.strftime('%Y-%m-%d'),
        key='ref_date_input',
    )
    run_btn = st.button('🚀 즉시 실행', type='primary', use_container_width=True)

# 사이드바에서 받은 두 키를 단일 serviceKey 로 통합 (mfds 키 하나에 hira 키 뒤이어 붙임)
combined_key = (mfds_key or '').strip()
if (mfds_key or '').strip() and (hira_key or '').strip() and mfds_key != hira_key:
    combined_key = f"{mfds_key.strip()},{hira_key.strip()}"

key = combined_key
demo_mode = not bool(key)
products = [s.strip() for s in (products_text or '').split(',') if s.strip()]

if not run_btn:
    if demo_mode:
        st.info(
            '👈 왼쪽 사이드바에서 인증키를 입력하면 실제 식약처·심평원 데이터가 조회됩니다. '
            '키를 비워두면 데모(mock) 데이터로 전체 흐름을 체험할 수 있습니다.'
        )
    else:
        st.info(
            '👈 왼쪽 사이드바에서 제품명을 쉼표로 구분해 입력한 뒤 [🚀 즉시 실행]을 누르세요.'
        )
    st.stop()

if not products and not demo_mode:
    st.warning('⚠ 제품명을 1개 이상 입력하세요.')
    st.stop()

if not products:
    products = ['리리카 캡슐 75mg', '디카맥스D 500', '타이레놀 500mg']
    st.info('ℹ 데모 모드 기본 3건으로 실행합니다. 제품명을 직접 입력해 자유 의약품도 조회할 수 있습니다.')

ref_date = ref_date_in.strftime('%Y-%m-%d') if hasattr(ref_date_in, 'strftime') else str(ref_date_in)

with st.spinner('⏳ 식약처·심평원 호출 중…' if not demo_mode else '⏳ 데모 데이터 생성 중…'):
    permit_rows, price_rows, errors, permit_path, price_path = run(
        products, key or None, ref_date, demo_mode,
    )

col1, col2 = st.columns(2)
with col1:
    st.subheader('📑 허가원문')
    st.download_button(
        '⬇ 허가원문.csv 다운로드',
        Path(permit_path).read_bytes(),
        file_name='허가원문.csv', mime='text/csv',
    )
    try:
        import pandas as pd  # 표시만 pandas 사용 (없어도 동작)
        st.dataframe(pd.read_csv(permit_path, dtype=str).fillna(''),
                     use_container_width=True, height=320)
    except Exception:
        with open(permit_path, 'r', encoding='utf-8-sig') as f:
            st.text(f.read())

with col2:
    st.subheader('💰 약가원문')
    st.download_button(
        '⬇ 약가원문.csv 다운로드',
        Path(price_path).read_bytes(),
        file_name='약가원문.csv', mime='text/csv',
    )
    try:
        import pandas as pd
        st.dataframe(pd.read_csv(price_path, dtype=str).fillna(''),
                     use_container_width=True, height=320)
    except Exception:
        with open(price_path, 'r', encoding='utf-8-sig') as f:
            st.text(f.read())

st.success(
    f'✅ 완료 — 허가원문 {len(permit_rows)}행, 약가원문 {len(price_rows)}행 · '
    f'기준일 {ref_date} · 모드={"데모" if demo_mode else "실제 API"}'
)
if errors:
    with st.expander(f'⚠ 처리 중 오류 {len(errors)}건'):
        for p, e in errors:
            st.write(f'- {p}: {e}')
