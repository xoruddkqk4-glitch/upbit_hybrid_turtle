# Upbit 하이브리드 터틀 자동매매

Upbit Open API 기반 **암호화폐(KRW 마켓) 자동매매 시스템**.
고전 터틀 트레이딩의 **S1/S2 신고가 돌파 + 30분 가드** 진입 필터와 **자금관리 원칙**을 결합한 전략을 구현한다.

> AWS EC2 같은 서버에서 `crontab` 으로 5~15분마다 `run_all.py` 를 1회씩 호출하는 **원샷 배치** 방식으로 동작한다.
> 프로세스 내부에 `while True` 같은 상시 감시 루프는 두지 않는다.

Claude Code/Agent 로 작업할 때의 상세 규약은 [`CLAUDE.md`](CLAUDE.md) 를 참고한다.

---

## 핵심 특징

- **터틀 S1/S2 + 30분 가드**: 20일(S1) 또는 55일(S2) 신고가 돌파 후 **30분 연속 유지** 시에만 진입
- **리스크 균등화 수량**: `1 Unit = 총자본 × 2% / ATR(N)` → 코인별 리스크 동일화
- **0.5N 피라미딩**: 마지막 매수가 대비 0.5N 상승 시마다 1 Unit 추가 (코인당 최대 3 Unit)
- **2N 하드 손절**: 마지막 매수가 - 2N 이하 하락 시 전량 즉시 매도
- **트레일링 스탑**: 10일 신저가 경신 또는 (수익권에서의) 5MA 하향 돌파 시 청산
- **포트폴리오 상한**: 전체 12 Unit 이내
- **체결 원장 + 텔레그램 알림** + **Google Sheets** 자동 동기화
- **모의투자 모드** 내장 (`UPBIT_PAPER_TRADING=True`)

---

## 요구 사항

- **Python 3.9+**
- 종속성: `pyupbit`, `cryptography`, `pandas`, `numpy`, `python-dotenv`, `requests`, `pytz`, `gspread`, `oauth2client`
  (상세 버전은 [`requirements.txt`](requirements.txt) 참조)
- (선택) Google 서비스 계정 JSON — 체결 원장·포트폴리오 스냅샷을 Google Sheets 에 기록할 때 사용
- (선택) 텔레그램 봇 토큰 + 채팅 ID — 매매 알림용

---

## 설치

```bash
git clone <repo-url>
cd upbit_hybrid_turtle
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 환경변수 설정

`.env.example` 파일을 복사해서 `.env` 를 만든 뒤 값을 채운다.

```bash
cp .env.example .env
```

| 변수 | 설명 |
|------|------|
| `UPBIT_ACCESS_KEY` / `UPBIT_SECRET_KEY` | Upbit 개발자 센터에서 발급한 API 키 |
| `UPBIT_PAPER_TRADING` | `True` 면 모의투자(실주문 없음), `False` 면 실계좌 주문 |
| `UPBIT_ACCOUNT_LABEL` | 로그·시트 기록용 계좌 별칭 (예: `upbit_main`) |
| `TELEGRAM_BOT_TOKEN` | BotFather 에서 발급한 봇 토큰 (생략 가능) |
| `TELEGRAM_CHAT_ID` | 알림을 받을 채팅 ID (생략 가능) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google 서비스 계정 JSON 파일 경로 (생략 가능) |
| `GOOGLE_SPREADSHEET_TITLE` | Sheets 스프레드시트 제목 |
| `GOOGLE_DRIVE_FOLDER_ID` | (선택) 특정 드라이브 폴더에 생성할 때 |

> `.env`, `service_account.json`, 런타임 JSON(`held_coin_record.json` 등)은 모두 `.gitignore` 대상이다.
> **절대 커밋하지 않는다.**

---

## 감시 코인 설정

진입·감시·주문 대상은 [`config.py`](config.py) 의 `LOVELY_COIN_LIST` 에 포함된 티커로만 한정된다.

```python
LOVELY_COIN_LIST = {
    "KRW-BTC":  {"name": "비트코인", "market": "KRW"},
    "KRW-ETH":  {"name": "이더리움", "market": "KRW"},
    "KRW-XRP":  {"name": "리플",     "market": "KRW"},
    "KRW-SOL":  {"name": "솔라나",   "market": "KRW"},
    "KRW-DOGE": {"name": "도지코인", "market": "KRW"},
    "KRW-ADA":  {"name": "에이다",   "market": "KRW"},
    "KRW-AVAX": {"name": "아발란체", "market": "KRW"},
}
```

감시 코인을 변경하려면 이 딕셔너리를 직접 편집한다. 자동 스크리너는 제공하지 않는다.

---

## 실행

### 1) 수동 실행 (검증·디버깅용)

```bash
python run_all.py
```

로그인 → 손절·익절 감시 → 터틀 신호 갱신 → 30분 가드 체크 → 주문 순서대로 **1회** 실행하고 종료한다.

### 2) AWS EC2 배포 (권장)

운영 경로는 `/var/autobot/upbit_hybrid_turtle` 기준. `ubuntu` 사용자가 해당 디렉토리의 소유권을 갖는다고 가정한다.

#### (a) 프로젝트 이전

```bash
sudo mkdir -p /var/autobot
sudo chown -R $USER:$USER /var/autobot

# 방법 1: git 저장소에서 clone
cd /var/autobot
git clone <repo-url> upbit_hybrid_turtle

# 방법 2: 로컬 폴더를 scp/rsync 로 업로드
#   (로컬 PowerShell) scp -r ./upbit_hybrid_turtle ec2-user:/var/autobot/
```

#### (b) Python 가상환경 + 의존성

```bash
cd /var/autobot/upbit_hybrid_turtle
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt
```

#### (c) 비밀 파일 권한 고정

`.env` 와 `service_account.json` 은 **`/var/autobot/upbit_hybrid_turtle/` 폴더 안에** 함께 두고 절대 git 에 커밋하지 않는다. `.env` 의 `GOOGLE_SERVICE_ACCOUNT_JSON=service_account.json` 은 상대경로로 해석되므로 cwd 가 프로젝트 폴더인 이상 그대로 동작한다.

```bash
# (파일이 아직 없다면 로컬에서 scp 로 업로드)
# scp ./.env ./service_account.json  ec2-user:/var/autobot/upbit_hybrid_turtle/

cd /var/autobot/upbit_hybrid_turtle
chmod 600 .env service_account.json
ls -l .env service_account.json      # 권한이 -rw------- 인지 확인
```

#### (d) 서버 시간대 확인 (권장: KST)

```bash
timedatectl                                   # 현재 TZ 확인
sudo timedatectl set-timezone Asia/Seoul      # KST 로 고정
```

> TZ 를 바꾸지 않고 UTC 그대로 운영하려면 crontab 쪽에서 `TZ=Asia/Seoul` 을 지정한다 (다음 섹션 참조).

#### (e) 최초 1회 수동 검증

```bash
cd /var/autobot/upbit_hybrid_turtle
./.venv/bin/python run_all.py        # 로그인·손절·목표가 갱신·30분 가드·주문 한 사이클
./.venv/bin/python run_cache.py      # ATR 캐시 갱신 (일봉 지표 저장)
./.venv/bin/python run_daily.py      # 포트폴리오 스냅샷 + 실현 손익 차트
```

텔레그램 메시지 수신과 Google Sheets 두 개(체결 원장 / 포트폴리오 추이) 생성 여부를 확인한다.

#### (f) crontab 등록

```bash
crontab -e
```

아래 블록을 그대로 붙여넣는다.

```cron
# ─── Upbit Hybrid Turtle 전역 설정 ───
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
TZ=Asia/Seoul
PYTHONUTF8=1
PYTHONIOENCODING=utf-8
PROJECT=/var/autobot/upbit_hybrid_turtle
PY=/var/autobot/upbit_hybrid_turtle/.venv/bin/python

# ─── ATR 캐시 갱신 — 일봉 지표 미리 저장 (KST 09:10, 1일 1회) ───
10 9 * * * cd $PROJECT && $PY run_cache.py >> $PROJECT/cron_cache.log 2>&1

# ─── 포트폴리오 스냅샷 + 실현 손익 차트 (KST 23:55, 1일 1회) ───
55 23 * * * cd $PROJECT && $PY run_daily.py >> $PROJECT/cron_daily.log 2>&1

# ─── 실시간 손절·익절·목표가 갱신·30분 가드·진입·피라미딩 (10분 간격, 24시간) ───
*/10 * * * * cd $PROJECT && $PY run_all.py >> $PROJECT/cron.log 2>&1
```

등록 후 확인:

```bash
crontab -l                                    # 등록 내용 확인
tail -f /var/autobot/upbit_hybrid_turtle/cron.log   # 다음 정각 즈음 실시간 확인
```

> **동작 설명**  
> - 매 10분 (`run_all.py`): 손절·익절 감시 → 목표가 갱신 → 30분 가드 체크 → 신규 진입/피라미딩. 일봉 지표는 `atr_cache.json` 캐시에서 읽으므로 일봉 API 를 직접 호출하지 않는다.  
> - 매일 09:10 (`run_cache.py`): 감시 코인 전체의 일봉 기반 지표(ATR·5MA·20MA·10일 신저가·S1/S2 신고가)를 계산해 `atr_cache.json` 에 저장한다. 이후 `run_all.py` 는 하루 종일 캐시를 읽기만 한다.  
> - 매일 23:55 (`run_daily.py`): `포트폴리오 추이` 시트에 누적 실현손익 1점 기록 + `손익차트` 시트·차트 갱신. 이 잡은 **주문을 내지 않는다**. 이미 당일 매도로 행이 생성된 경우 해당 행을 최신값으로 덮어쓴다(중복 없음).  
> - 네트워크 일시 장애 시 다음 10분 주기가 자연 재시도 역할을 한다.

#### (g) 로그 파일 위치 (배포 후)

| 파일 | 의미 |
|------|------|
| `/var/autobot/upbit_hybrid_turtle/run_all.log` | `run_all.py` 내부 로그 (5MB × 4개 자동 로테이션) |
| `/var/autobot/upbit_hybrid_turtle/run_cache.log` | `run_cache.py` 내부 로그 (동일 로테이션) |
| `/var/autobot/upbit_hybrid_turtle/run_daily.log` | `run_daily.py` 내부 로그 (동일 로테이션) |
| `/var/autobot/upbit_hybrid_turtle/cron.log` | crontab → `run_all.py` stdout/stderr 리다이렉션 |
| `/var/autobot/upbit_hybrid_turtle/cron_cache.log` | crontab → `run_cache.py` stdout/stderr 리다이렉션 |
| `/var/autobot/upbit_hybrid_turtle/cron_daily.log` | crontab → `run_daily.py` stdout/stderr 리다이렉션 |

> `cron.log` / `cron_cache.log` / `cron_daily.log` 는 자동 로테이션이 없으므로 한 달에 한 번 정도 `truncate -s 0 cron.log` 또는 `logrotate` 로 비우는 것을 권장한다.

### 3) 개별 모듈 실행 (디버깅)

```bash
python -c "import target_manager; target_manager.run_update()"
python -c "import timer_agent; print(timer_agent.run_timer_check())"
python -c "import turtle_order_logic as t; t.run_orders(__import__('timer_agent').run_timer_check())"
python -c "import risk_guardian; risk_guardian.run_guardian()"
```

> **운영 전 체크리스트**: 처음에는 반드시 `UPBIT_PAPER_TRADING=True` 로 며칠간 로그를 관찰해서
> 진입/손절/피라미딩 로직이 의도대로 동작하는지 검증한 뒤에만 `False` 로 전환한다.

---

## 매매 로직 개요

### 진입 (Entry)

**터틀 S1 / S2 신고가 돌파 + 30분 가드 (`TURTLE_S1` / `TURTLE_S2`)**

두 조건을 **AND** 로 모두 만족해야 진입한다.

1. 직전 20일(S1) 또는 55일(S2) 장중 고가를 현재가가 돌파 (`turtle_s1/s2_signal = True`)
2. 해당 신호가 **30분 이상 연속 유지** (`turtle_s1/s2_since` 기준 경과 시간 확인)

- 신호가 꺼지면 타이머(`_since`) 초기화 → 재돌파 시 30분 다시 카운트
- S1·S2 동시 해당 시 S2 우선 (`TURTLE_S2 > TURTLE_S1`)

### 포지션 사이징 — 1 Unit 최대 금액 상한 + 동적 리스크 계수

```
이론 1U 수량  = (총 자본 × RISK_PER_TRADE) / ATR(N)   ← 터틀 정석(1% 리스크)
이론 1U 금액  = 이론 1U 수량 × 현재가
1U 최대 금액  = 총 자본 × MAX_UNIT_KRW_RATIO (기본 10%)
```

| 조건 | 적용 수량 | effective_risk_factor |
|---|---|---|
| 이론 1U 금액 ≤ 1U 최대 금액 | 이론 수량 그대로 | `RISK_PER_TRADE` (0.01) |
| 이론 1U 금액 > 1U 최대 금액 | 최대 금액으로 축소 | `MAX_UNIT_KRW_RATIO × ATR / 현재가` (0.01 미만) |

- **ATR(N)**: 최근 20일 True Range 평균. 코인별 변동성에 반비례해 수량 결정.
- 이론 금액이 상한을 초과하면 해당 종목의 리스크 계수(`effective_risk_factor`)를 낮춰 1U 금액을 상한에 맞춘다. **종목마다 다른 리스크 계수**가 `held_coin_record.json` 에 저장된다.
- 상수는 `turtle_order_logic.py` 에서 조정 가능:
  - `RISK_PER_TRADE = 0.01` (기본 리스크 계수 1%)
  - `MAX_UNIT_KRW_RATIO = 0.10` (1 Unit당 최대 매수 금액 = 총 자본 × 10%)
- `수량 × 현재가 < 5,000원` 이면 Upbit 최소 주문 제한으로 스킵.

### 피라미딩

- **0.5N 상승 시마다 1 Unit 추가**
- **코인당 최대 3 Unit**, **포트폴리오 전체 12 Unit** 상한
- 피라미딩 성공 시 손절가도 `새 평균 매입가 - 2N` 으로 **갱신**

### 청산

| 조건 | 발동 시점 | 비고 |
|------|-----------|------|
| **2N 하드 손절** | 현재가 ≤ `last_buy_price - 2N` | 최우선 판정 |
| **10일 신저가 경신** | 현재가 ≤ 최근 10일 최저 종가 | 무조건 청산 |
| **5MA 하향 돌파** | 현재가 < 5일 이평 **AND** 현재가 > 평균 매입가 | 수익권에서만 작동 |

> 손실권에서는 5MA 이탈로 매도하지 않는다. 2N 하드 손절에만 의존해 추세가 살아날 가능성을 남긴다.

---

## 아키텍처

```
run_all.py (crontab 이 주기적으로 호출)
├── [SA-FOUNDATION] 기반 모듈
│   ├── upbit_client.py      — Upbit API 래퍼 (전략 파일은 pyupbit 직접 호출 금지)
│   ├── myUpbit.py           — pyupbit 저수준 유틸
│   ├── indicator_calc.py    — ATR, 5/20MA, 10일 신저가, N일 신고가, 240분봉 20MA
│   ├── trade_ledger.py      — 체결 원장 (단일 진입점) + Google Sheets
│   ├── telegram_alert.py    — 텔레그램 알림 (단일 진입점)
│   ├── balance_sync.py      — 실행 시작 시 실제 잔고 ↔ held_coin_record.json 동기화
│   └── config.py            — LOVELY_COIN_LIST
├── [SA-MODULE-ENTRY] 진입 판정
│   ├── target_manager.py    — 터틀 S1/S2 신호 감지, unheld_coin_record.json 관리
│   └── timer_agent.py       — 터틀 신호 30분 가드 확인 + 진입 신호 산출
└── [SA-MODULE-TRADE] 주문·리스크
    ├── turtle_order_logic.py — Unit 수량 계산, 진입·피라미딩 주문
    └── risk_guardian.py      — 2N 손절 + 트레일링 스탑 (1회 판정)
```

### 실행 순서 (`run_all.main`)

1. **Upbit 로그인**
2. **잔고 동기화** — 실제 잔고 ↔ `held_coin_record.json` 불일치 자동 정정
3. **손절·익절 감시** — 기존 포지션 보호 (최우선)
4. **터틀 신호 갱신** — 미보유 코인의 S1/S2 신호 및 `_since` 타임스탬프 업데이트
5. **30분 가드 체크** — 신호 유지 30분 이상 코인을 진입 신호 목록으로 생성
6. **주문 실행** — 신규 진입 + 기존 포지션 피라미딩

---

## 파일 구조

### 소스 파일 (커밋 대상)

| 파일 | 역할 |
|------|------|
| `run_all.py` | 통합 배치 실행기 |
| `balance_sync.py` | 실행 시작 시 잔고 동기화; 수동 매수 코인 자동 편입(MANUAL_SYNC) |
| `upbit_client.py` | Upbit Open API 래퍼 |
| `myUpbit.py` | pyupbit 저수준 유틸 |
| `indicator_calc.py` | 기술지표 계산 |
| `config.py` | 감시 코인 목록 |
| `target_manager.py` | 터틀 S1/S2 신호 감지 및 상태 관리 |
| `timer_agent.py` | 진입 신호 통합 |
| `turtle_order_logic.py` | 주문·피라미딩 |
| `risk_guardian.py` | 손절·익절 감시 |
| `trade_ledger.py` | 체결 원장 |
| `telegram_alert.py` | 알림 |
| `requirements.txt` | 의존성 |
| `.env.example` | 환경변수 템플릿 |
| `.gitignore` | 커밋 제외 규칙 |
| `CLAUDE.md` | Claude Code 작업 규약 |
| `README.md` | 본 문서 |

### 런타임 JSON (자동 생성, 커밋 금지)

| 파일 | 내용 |
|------|------|
| `unheld_coin_record.json` | 미보유 코인의 터틀 신호(`turtle_s1/s2_signal`) 및 신호 발생 시각(`turtle_s1/s2_since`) |
| `held_coin_record.json` | 보유 코인의 Unit 수·매수가·손절가·피라미딩 트리거가 |
| `trade_ledger.json` | 누적 체결 원장 |
| `daily_snapshot.json` | `run_daily.py` 의 하루 1회 스냅샷 중복 방지. 매도 즉시 갱신 경로는 건드리지 않음. |
| `atr_cache.json` | 일봉 기반 지표(ATR·5MA·20MA·10일 신저가) 하루 1회 캐시 — `run_cache.py` 가 09:10 에 저장 |

---

## 위험 관리 요약

| 항목 | 규칙 | 근거 |
|------|------|------|
| **단일 트레이드 최대 손실** | 자본의 2% 이하 | `1 Unit = 총자본 × 1% / ATR`, 손절가 = 진입가 - 2N. 상한 조정 시 실제 손실은 2% 보다 작아짐 |
| **코인당 최대 노출** | 3 Unit (≈ 자본의 6%) | `MAX_UNIT_PER_COIN` |
| **포트폴리오 최대 노출** | 12 Unit (≈ 자본의 24%) | `MAX_TOTAL_UNITS` |
| **감시·주문 범위** | `LOVELY_COIN_LIST` 만 | 리스트 외 티커는 무시 (단, 이미 보유 중이면 손절·익절 계속) |
| **최소 주문 금액** | 5,000 원 | Upbit KRW 마켓 제한 |
| **모의투자 플래그** | 검증 전 반드시 `True` | `.env` 의 `UPBIT_PAPER_TRADING` |

---

## 로그 및 관찰

- `run_all.log` — 통합 실행 로그 (5MB × 4개 로테이션)
- Google Sheets — 체결 원장(워크시트 1) + 포트폴리오 추이(워크시트 2) + 손익차트(워크시트 3)
  - 매도 체결 시 포트폴리오 추이·손익차트 **즉시 갱신** (날짜별 1줄 upsert)
  - `run_daily.py` 23:55 에도 동일한 upsert 방식으로 갱신 (중복 없음)
- 텔레그램 — 진입/피라미딩/손절/익절/시트 기록 알림

---

## 면책 조항 (Disclaimer)

- 본 프로젝트는 교육·개인 연구 목적으로 공개된 코드이며, **투자 수익을 보장하지 않는다.**
- 암호화폐 시장은 변동성이 매우 크고, 자동매매는 **원금 손실**을 초래할 수 있다.
- 실계좌 전환 전 반드시 모의투자(`UPBIT_PAPER_TRADING=True`) 로 충분한 기간 검증한다.
- API 키와 서비스 계정 JSON 은 절대로 저장소·공개 채널에 노출하지 않는다.
- 작성자/기여자는 본 코드로 발생한 어떠한 손실에도 책임지지 않는다.

---

## 관련 문서

- [`CLAUDE.md`](CLAUDE.md) — Claude Code / Cursor Agent 작업 규약, 아키텍처, 체결 원장 스키마
- [`.claude/plans/`](.claude/plans/) — 기능별 설계·이식 계획서
