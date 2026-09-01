#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""의약품 심의자료 검증기 — app.py v4.0 companion
목적: 식약처 목록·상세 · 심평원 약가 호출 → 11필드 카노니컬 CSV로 emit → HTML이 읽음.

사용:
    python app.py --products "리리카 캡슐 75mg,디카맥스D 500"   # 실제 호출
    python app.py --demo                                        # 외부 호출 없이 mock emit
    (환경변수 DATA_GO_KR_KEY 또는 MFDS_SERVICE_KEY + HIRA_SERVICE_KEY 필요)
"""
from __future__ import annotations
import argparse, csv, hashlib, json, os, re, sys, time, pathlib
try:
    import streamlit as st  # Streamlit Cloud / 로컬 streamlit run 양쪽 호환
    _HAS_ST = True
except Exception:  # CLI 모드에선 streamlit이 없어도 됨
    _HAS_ST = False

# ---------- 1. 상수 (HTML과 문자 단위로 동일) ----------
PERMIT_FIELDS = ['의약품명','성분명','제조판매사','함량','제형','적응증','용법용량','소아','보관','금기','BAR','허가일자','ITEM_SEQ']
PRICE_FIELDS  = ['의약품명','약가','적용시작일','적용종료일','mdsCd','제조판매사']

#.api endpoint (사전에 있던 app.py 원본 값 그대로; env 로 덮어쓰기 가능)
MFDS_LIST   = os.getenv('MFDS_LIST_URL',   'http://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnInq07')
MFDS_DETAIL = os.getenv('MFDS_DETAIL_URL', 'https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnDtlInq06')
HIRA_URL    = os.getenv('HIRA_PRICE_URL',  'https://apis.data.go.kr/B551182/dgamtCrtrInfoService1.2/getDgamtList')

# 체스 검색·동의어
WARN = lambda m: print(f'[app.py] WARN: {m}', file=sys.stderr)

def normalize(t): 
    return re.sub(r'\s+', ' ', str(t or '')).replace('\u3000',' ').strip()

def extract_amount(name: str) -> str:
    m = re.search(r'\d+(?:\.\d+)?\s*(?:mg|g|mcg|μg|㎍|IU|mEq|mL|%)\s*(?:/\s*\d+(?:\.\d+)?\s*(?:mg|mL|mL))?', name or '')
    return m.group(0).strip() if m else ''

FORM_MAP = [
    ('서방정', ['SR','CR','XR','ER','서방']),
    ('캡슐',   ['Cap','Capsule','캡슐']),
    ('주사',   ['Inj','Injection','inj','주사']),
    ('바이알', ['vial','Vial']),
    ('펜',     ['pen','Pen']),
    ('현탁액', ['Susp','susp']),
    ('점안액', ['Ophth','eye']),
    ('정',     ['Tab','Tablet','정']),
]
def extract_form(name: str) -> str:
    for ko, syns in FORM_MAP:
        for s in syns:
            if s.lower() in (name or '').lower(): return ko
    return ''

def sectionize(nbdoc: str, anchor_keys) -> str:
    """NB_DOC_DATA HTML/XML 섹션에서 anchor_keys로 시작하는 섹션 한 단락 추출"""
    if not nbdoc: return ''
    txt = re.sub(r'<[^>]+>',' ', nbdoc)
    txt = re.sub(r'\s+',' ', txt)
    for k in anchor_keys:
        m = re.search(re.escape(k)+r'([\s\S]{0,500}?)(?=\d+\.\s|※|\Z)', txt)
        if m: return m.group(1).strip()
    return ''

# ---------- 2. 캐시 ----------
def cache_path(key: str) -> pathlib.Path:
    p = pathlib.Path(__file__).resolve().parent / 'cache'
    p.mkdir(parents=True, exist_ok=True)
    return p / (hashlib.sha1(key.encode('utf-8')).hexdigest() + '.json')

def cache_get(key: str, ttl_days: int = 7):
    cp = cache_path(key)
    if not cp.exists(): return None
    age = time.time() - cp.stat().st_mtime
    if age > ttl_days*86400: return None
    try: return json.loads(cp.read_text(encoding='utf-8'))
    except Exception: return None
def cache_put(key: str, data):
    cache_path(key).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

# ---------- 3. API 클라이언트 (실제 호출 가능) ----------
def http_get_json(url: str, params: dict, timeout: int = 30):
    import urllib.parse, urllib.request
    qs = urllib.parse.urlencode({k:v for k,v in params.items() if v is not None})
    full = url + ('?' + qs if qs else '')
    try:
        with urllib.request.urlopen(full, timeout=timeout) as r:
            raw = r.read().decode('utf-8', errors='ignore')
        try: return json.loads(raw)
        except Exception:
            # XML 응답 fallback (심평원 등)
            return {'_xml': raw}
    except Exception as e:
        WARN('HTTP 실패: '+str(e)); return None

# ---------- 4. 제품 정규화 ----------
def find_mfds(item_name: str, key: str | None):
    """식약처 목록 → ITEM_SEQ 매칭"""
    if item_name.startswith('MOCK_'): return None
    data = cache_get('mfds_list:'+item_name)
    if not data and key:
        params = {'serviceKey': key, 'itemName': item_name, 'numOfRows': 30, 'pageNo': 1, 'type':'json'}
        data = http_get_json(MFDS_LIST, params) or {}
        cache_put('mfds_list:'+item_name, data)
    if not data: return None
    body = (data.get('body') or {}).get('items') or []
    if isinstance(body, dict): body = [body]
    if not body: 
        # XML try-fallback
        WARN(f'목록 응답 0건: {item_name}'); return None
    return body[0]  # 첫 매칭

def fetch_detail(item_seq: str, key: str | None):
    if str(item_seq).startswith('MOCK_'): return None
    data = cache_get('mfds_detail:'+item_seq)
    if not data and key:
        params = {'serviceKey': key, 'itemSeq': item_seq, 'type':'json'}
        data = http_get_json(MFDS_DETAIL, params) or {}
        cache_put('mfds_detail:'+item_seq, data)
    return data or {}

def fetch_price(itm_nm: str, key: str | None):
    data = cache_get('hira:'+itm_nm)
    if not data and key:
        params = {'serviceKey': key, 'itemName': itm_nm, 'numOfRows': 30, 'pageNo': 1, 'type':'json'}
        data = http_get_json(HIRA_URL, params) or {}
        cache_put('hira:'+itm_nm, data)
    return data or {}

# ---------- 5. MOCK 생성기 --demo ----------
def mock_mfds(p: str):
    if '리리카' in p or 'Lyrica' in p.lower():
        return {'ITEM_SEQ':'MOCK_LYR', 'ITEM_NAME':'리리카캡슐75밀리그램(프레가발린)',
                'ENTP_NAME':'비아트리스코리아(주)', 'ITEM_PERMIT_DATE':'2010-03-15',
                'MAIN_ITEM_INGR':'프레가발린','INGR_NAME':'Pregabalin',
                'BAR_CODE':'8801234500011'}
    if '디카맥스' in p or 'dicamax' in p.lower():
        return {'ITEM_SEQ':'MOCK_DIC', 'ITEM_NAME':'디카맥스D정(Calcium carbonate + Cholecalciferol)',
                'ENTP_NAME':'동아제약(주)', 'ITEM_PERMIT_DATE':'2018-07-10',
                'MAIN_ITEM_INGR':'칼슘카보네이트+콜레칼시페롤','INGR_NAME':'Calcium carbonate + Cholecalciferol',
                'BAR_CODE':'8806462000011'}
    return {'ITEM_SEQ':'MOCK_X', 'ITEM_NAME':p, 'ENTP_NAME':'한독소비(주)',
            'ITEM_PERMIT_DATE':'2020-01-01','MAIN_ITEM_INGR':p,'INGR_NAME':p,'BAR_CODE':'8800000000000'}

def mock_detail(name: str):
    nb = ('1. 다음 환자에게는 투여하지 말 것: 이 약의 성분에 과민증 환자에 대한 금기.'
          ' 2. 소아에 대한 투여: 소아(만 12세 미만)에 대한 안전성·유효성은 확립되어 있지 않다. '
          '3. 임부에 대한 투여: 임부 또는 임신하고 있을 가능성이 있는 여성에는 투여하지 말 것.')
    if '리리카' in name or 'lyrica' in name.lower():
        return {'EE_DOC_DATA':'<p>말초성 신경병증 통증, 섬유근육통, 부분발작 보조요법 (성인)</p>',
                'UD_DOC_DATA':'<p>초기 1일 150mg, 3-7일에 걸쳐 최대 600mg까지 증량. 1일 2회 분할.</p>',
                'NB_DOC_DATA':'<p>' + nb + '</p>',
                'STORAGE_METHOD':'기밀용기, 실온(1~30℃) 보관'}
    if '디카맥스' in name or 'dicamax' in name.lower():
        return {'EE_DOC_DATA':'<p>칼슘 및 비타민 D3 보급 (칼슘·비타민D 결핍 시)</p>',
                'UD_DOC_DATA':'<p>성인 1일 1회, 1정 (식후)</p>',
                'NB_DOC_DATA':'<p>' + nb + '</p>',
                'STORAGE_METHOD':'기밀용기, 실온 보관 (1~30℃)'}
    return {'EE_DOC_DATA':'허가 적응증 본문 (데모)', 'UD_DOC_DATA':'용법·용량 본문 (데모)',
            'NB_DOC_DATA':'<p>' + nb + '</p>', 'STORAGE_METHOD':'기밀용기, 실온 보관'}

def mock_price(name: str, ref_date: str):
    rows = []
    if '리리카 75' in name or '리리카캡슐75' in name:
        rows = [{'itmNm':'리리카캡슐75밀리그램','mnfEntpNm':'비아트리스코리아(주)',
                 'mxCprc':'523','adtStaDd':'2024-01-01','sellEptDd':'', 'mdsCd': 'MOCK_LYR_C75'}]
    elif '리리카' in name:
        rows = [{'itmNm':'리리카CR서방정330밀리그램','mnfEntpNm':'비아트리스코리아(주)',
                 'mxCprc':'1399','adtStaDd':'2024-04-01','sellEptDd':'', 'mdsCd':'MOCK_LYR_C330'}]
    elif '디카맥스' in name:
        rows = [{'itmNm':'디카맥스D정','mnfEntpNm':'동아제약(주)',
                 'mxCprc':'70','adtStaDd':'2024-07-01','sellEptDd':'', 'mdsCd':'MOCK_DIC_D'}]
    else:
        rows = [{'itmNm':name,'mnfEntpNm':'한독소비(주)','mxCprc':'1000',
                 'adtStaDd':'2024-01-01','sellEptDd':'','mdsCd':'MOCK_X'}]
    return {'response':{'body':{'items': rows}}}

# ---------- 6. CSV emit ----------
def emit_permit_csv(rows: list, path: str):
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(PERMIT_FIELDS)
        for r in rows:
            w.writerow([r.get(k,'') for k in PERMIT_FIELDS])

def emit_price_csv(rows: list, path: str):
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(PRICE_FIELDS)
        for r in rows:
            w.writerow([r.get(k,'') for k in PRICE_FIELDS])

# ---------- 7. 메인 ----------
def run(products: list[str], key: str | None, ref_date: str, demo: bool):
    permit_rows, price_rows = [], []

    for p in products:
        p = p.strip()
        if not p: continue
        try:
            # 1) 식약처 목록
            if demo:
                base = mock_mfds(p)
            else:
                base = (find_mfds(p, key) or {})
            item_name = base.get('ITEM_NAME') or p
            item_seq  = base.get('ITEM_SEQ',  '')
            entp      = base.get('ENTP_NAME', '')
            permit_dt = base.get('ITEM_PERMIT_DATE','')
            main_ingr = base.get('MAIN_ITEM_INGR','') or base.get('INGR_NAME','')
            bar       = base.get('BAR_CODE', '')

            # 2) 식약처 상세
            if demo:
                det = mock_detail(item_name)
            else:
                det = fetch_detail(item_seq, key) if key else {}
            ee = det.get('EE_DOC_DATA','')
            ud = det.get('UD_DOC_DATA','')
            nb = det.get('NB_DOC_DATA','')
            st = det.get('STORAGE_METHOD','')

            permit_rows.append({
                '의약품명': item_name,
                '성분명': main_ingr,
                '제조판매사': entp,
                '함량': extract_amount(item_name),
                '제형': extract_form(item_name),
                '적응증': normalize(ee),
                '용법용량': normalize(ud),
                '소아': sectionize(nb, ['소아에 대한 투여','소아투여']),
                '보관': normalize(st),
                '금기': sectionize(nb, ['투여하지 말 것','금기사항','다음 환자에는 투여하지']),
                'BAR': bar,
                '허가일자': permit_dt,
                'ITEM_SEQ': item_seq,
            })

            # 3) 심평원 약가 (기준일 적용)
            if demo:
                pr = mock_price(item_name, ref_date)
            elif key:
                pr = fetch_price(item_name, key) or {}
            else:
                pr = {}

            items = (((pr.get('response') or {}).get('body') or {}).get('items')) or []
            if isinstance(items, dict): items = [items]
            ref = ref_date.replace('-','')
            current = None
            for it in items:
                ad = str(it.get('adtStaDd','')).replace('-','')
                se = str(it.get('sellEptDd','')).replace('-','')
                if ad and ref and ad > ref: continue
                if se and ref and se < ref: continue
                if not current or ad > str(current.get('adtStaDd','')):
                    current = it
            if current:
                price_rows.append({
                    '의약품명': current.get('itmNm', item_name),
                    '약가': current.get('mxCprc',''),
                    '적용시작일': current.get('adtStaDd',''),
                    '적용종료일': current.get('sellEptDd',''),
                    'mdsCd': current.get('mdsCd',''),
                    '제조판매사': current.get('mnfEntpNm', entp),
                })
        except Exception as e:
            WARN(f"처리 실패 ({p}): {e}")

    out_dir = pathlib.Path(__file__).resolve().parent
    permit_path = out_dir / '허가원문.csv'
    price_path  = out_dir / '약가원문.csv'
    emit_permit_csv(permit_rows, str(permit_path))
    emit_price_csv(price_rows, str(price_path))
    print(f'[app.py] 허가원문.csv  → {permit_path}  ({len(permit_rows)}행)')
    print(f'[app.py] 약가원문.csv  → {price_path}   ({len(price_rows)}행)')
    print(f'[app.py] 기준일 = {ref_date}')

def _resolve_key() -> str | None:
    """
    키 로드 우선순위
      1) Streamlit 사이드바 입력이 있으면 그 값
      2) st.secrets["DATA_GO_KR_KEY"]      (Streamlit Cloud Settings → Secrets)
      3) os.environ["DATA_GO_KR_KEY" | "MFDS_SERVICE_KEY"]
    """
    if _HAS_ST and hasattr(st, 'session_state'):
        v = st.session_state.get('USER_KEY')
        if v: return v.strip()
    if _HAS_ST:
        try:
            v = st.secrets.get('DATA_GO_KR_KEY') or st.secrets.get('MFDS_SERVICE_KEY')
            if v: return str(v).strip()
        except Exception:
            pass
    return os.getenv('DATA_GO_KR_KEY') or os.getenv('MFDS_SERVICE_KEY')


def streamlit_app():
    """Streamlit Cloud / 로컬 streamlit run 진입점 — 사이드바에서 키·제품 입력"""
    st.set_page_config(page_title='의약품 심의자료 검증기 v5.0', layout='wide', page_icon='💊')
    st.title('💊 의약품 심의자료 검증기')
    st.caption('식약처·심평원 OpenAPI 직접 호출 · 키는 사이드바에서 한 번만 입력 · 세션 동안만 유지')

    # ----- 사이드바: 키 + 제품 입력 -----
    with st.sidebar:
        st.header('🔑 인증키')
        key_input = st.text_input('data.go.kr 인증키', type='password',
                                  placeholder='예: abc123...  (mfds 키 또는 mfds,hira 합본)',
                                  help='data.go.kr 에서 발급받은 본인 키. 다른 사람과 공유 금지.')
        st.session_state['USER_KEY'] = key_input
        st.caption('키는 세션 메모리에만 보관 · 외부 전송 없음')

        st.divider()
        st.header('📋 조회 옵션')
        demo_only = st.checkbox('🔧 데모 모드 (외부 호출 없이 mock)', value=not bool(key_input))
        ref_date = st.date_input('📅 약가 기준일', value=time.strftime('%Y-%m-%d')).isoformat()
        products_text = st.text_area('💊 제품명 (쉼표 구분)',
                                      placeholder='예: 리리카 캡슐 75mg, 디카맥스D 500',
                                      height=100)
        run_btn = st.button('🚀 즉시 실행', type='primary', use_container_width=True)

    if not run_btn:
        st.info('👈 왼쪽 사이드바에서 키를 입력·저장한 뒤 [🚀 즉시 실행]을 누르세요. '
                '키가 없거나 데모만 체험하고 싶으면 🔧 데모 모드에 체크하면 됩니다.')
        return

    key = (key_input or '').strip() or _resolve_key()
    products = [s.strip() for s in products_text.split(',') if s.strip()]
    if not demo_only and not key:
        st.error('⚠ 키가 없습니다. 사이드바에 키를 입력하거나 🔧 데모 모드에 체크하세요.')
        return
    if not products and not demo_only:
        st.error('⚠ 제품명을 1개 이상 입력하세요.')
        return
    if not products and demo_only:
        products = ['리리카 캡슐 75mg', '디카맥스D 500', '타이레놀 500mg']  # 데모 기본 3건

    with st.spinner('⏳ 식약처·심평원 호출 중…'):
        # run()은 werkzeug에 stdout을 남기므로 Streamlit에는 st.info 로 한번 더 표시
        run(products, key, ref_date, demo_only)

    # 결과 표시
    permit_path = pathlib.Path('허가원문.csv')
    price_path  = pathlib.Path('약가원문.csv')
    col1, col2 = st.columns(2)
    with col1:
        st.subheader('📑 허가원문')
        if permit_path.exists():
            st.download_button('⬇ 허가원문.csv 다운로드', permit_path.read_bytes(),
                               file_name='허가원문.csv', mime='text/csv')
            st.dataframe(_read_csv_safe(permit_path), use_container_width=True, height=320)
        else:
            st.warning('허가원문.csv 가 생성되지 않았습니다')
    with col2:
        st.subheader('💰 약가원문')
        if price_path.exists():
            st.download_button('⬇ 약가원문.csv 다운로드', price_path.read_bytes(),
                               file_name='약가원문.csv', mime='text/csv')
            st.dataframe(_read_csv_safe(price_path), use_container_width=True, height=320)
        else:
            st.warning('약가원문.csv 가 생성되지 않았습니다')

    st.success(f'✅ 완료 — 기준일 {ref_date} · 데모={demo_only} · 키={"있음" if key else "없음(데모)"}')


def _read_csv_safe(p: pathlib.Path):
    import io
    try:
        return list(csv.DictReader(io.StringIO(p.read_text(encoding='utf-8-sig'))))
    except Exception as e:
        return [{'오류': str(e)}]


def main():
    # Streamlit 모드 감지: streamlit run 으로 실행되면 __name__ == 'streamlit.runtime.scriptrunner.script_runner'
    if _HAS_ST and ('streamlit' in sys.argv[0] or os.getenv('STREAMLIT_SERVER_RUNNING')):
        streamlit_app(); return

    ap = argparse.ArgumentParser()
    ap.add_argument('--products', help='쉼표 구분 제품명 목록', default='')
    ap.add_argument('--demo', action='store_true', help='외부 호출 없이 mock 데이터로 CSV emit')
    ap.add_argument('--ref', help='약가 기준일(YYYY-MM-DD, 기본 오늘)', default=time.strftime('%Y-%m-%d'))
    args = ap.parse_args()
    key = _resolve_key()
    products = [s.strip() for s in args.products.split(',') if s.strip()]

    # 키 없고 --demo 도 없으면: 인터랙티브 입력 폴백 (CLI 모드에서도 친절하게)
    if not args.demo and not key:
        print('[app.py] 키 또는 시크릿이 없습니다. 아래에서 입력하세요 (Ctrl+C로 종료).', file=sys.stderr)
        try:
            import getpass
            key = getpass.getpass('data.go.kr 인증키: ').strip()
            if not key:
                WARN('키 입력이 비어있습니다. --demo 로 재시도하거나 키를 다시 입력하세요.')
                sys.exit(2)
        except (KeyboardInterrupt, EOFError):
            WARN('키 입력 취소 — --demo 로 재시도하세요.'); sys.exit(2)

    if not products and not args.demo:
        # CLI 모드 폴백: 제품도 인터랙티브 입력
        try:
            raw = input('제품명 (쉼표 구분, 엔터=리리카 75mg 데모): ').strip()
            products = [s.strip() for s in raw.split(',') if s.strip()] or ['리리카 캡슐 75mg']
        except (KeyboardInterrupt, EOFError):
            WARN('제품명 입력 취소.'); sys.exit(2)

    if not products and args.demo:
        products = ['리리카 캡슐 75mg']  # 데모 기본 1건

    run(products, key, args.ref, args.demo)

if __name__ == '__main__':
    main()
