# Claude Code — `upbit_hybrid_turtle` 진입 명세 (`CLAUDE.md`)

**Claude Code 세션은 본 파일만으로 시작한다.**

**이 프로젝트가 다루는 것:** Upbit Open API 를 이용한 **암호화폐(KRW 마켓) 자동매매 시스템**.
매매 전략: **터틀 트레이딩(자금 관리) + 눌림→재돌파(진입 검증) 하이브리드** 전략.

실행 모델: **원샷(one-shot) 배치 스크립트**. AWS EC2 등의 서버에서
`crontab` 으로 10분마다 `run_all.py` 를 호출한다.
프로세스 내부에서 `while True` 같은 상시 감시 루프는 돌지 않는다.

---

## Claude Code 협업 규칙

- **사용자는 코딩을 전혀 모르는 왕초보**다. 설명할 때는 전문 용어를 피하고, 일상적인 말로 쉽게 풀어서 설명한다.
- **모든 코드에 한글 주석을 달아야 한다.** 함수·변수·로직 단위로 "이 코드가 무엇을 하는지"를 한글로 설명한다.
- 오류 메시지나 결과를 보여줄 때도 한글로 해석해서 전달한다.
- **문서 동기화 의무**: 코드(전략 파라미터, 파일 구성, 실행 절차, 환경변수, 런타임 JSON 스키마, 매매 로직 등)를
  수정할 때는 **반드시 `README.md` 와 `CLAUDE.md` 의 관련 부분도 함께 갱신**한다.
  새 파일을 추가·삭제하거나 진입/청산 조건을 바꾼 경우에도 동일하게 두 문서를 검토해서 반영한다.

---

## 기술 스택

| 항목 | 내용 |
|------|------|
| **Upbit API 라이브러리** | `pyupbit` (공식 SDK) + 저수준 유틸 `myUpbit.py` (지표·주문 래퍼) |
| **알림** | 텔레그램 봇 (`requests` 기반 Webhook) |
| **시간대** | KST (`pytz`) |
| **데이터 저장** | 로컬 JSON + Google Sheets (`gspread`) |
| **실행 환경** | AWS 서버 `crontab` 으로 `python run_all.py` 주기적 호출 |

> `upbit_client.py` 는 `pyupbit` 와 `myUpbit.py` 를 내부적으로 사용해 Upbit API 에 접근한다.
> 전략 파일에서 `pyupbit` 를 직접 호출하지 말고, 반드시 `upbit_client.py` 를 경유한다.

---

## 핵심 아키텍처 (한눈에)

```
(run_all.py — crontab 이 주기적으로 1회 실행)
├── [SA-FOUNDATION]
│   ├── upbit_client.py      — Upbit Open API 래퍼 (pyupbit 직접 호출 금지)
│   ├── myUpbit.py           — 저수준 유틸 (GetMA/GetRSI/BuyCoinMarket 등, 그대로 활용)
│   ├── indicator_calc.py    — ATR(N), 이동평균선(20MA, 5MA), 10일 신저가, N일 신고가
│   ├── trade_ledger.py      — append_trade(record) 단일 진입점 + Google Sheets (SELL 시 포트폴리오 추이·손익차트 즉시 갱신)
│   ├── telegram_alert.py    — SendMessage(msg) 단일 진입점
│   ├── balance_sync.py      — 실행 시작 시 실제 잔고 ↔ held_coin_record.json 동기화 (수동 매수 코인 자동 편입 + 수동 거래 시트 자동 기록)
│   └── config.py            — LOVELY_COIN_LIST (고정 감시 목록)
├── [SA-MODULE-ENTRY]
│   ├── target_manager.py    — 터틀 S1/S2 신호 감지, 눌림→재돌파 상태(peak) 관리, unheld_coin_record.json 갱신
│   └── timer_agent.py       — 눌림→재돌파 조건 확인 + 터틀 S1/S2 진입 신호 산출
└── [SA-MODULE-TRADE]
    ├── turtle_order_logic.py — Unit 수량 계산, 피라미딩 주문
    └── risk_guardian.py      — 2N 손절 + 트레일링 스탑 감시 (호출 시점 1회 판정)
```

## 전체 파일 목록

| 파일 | 역할 |
|------|------|
| `upbit_client.py` | Upbit Open API 래퍼 (로그인·시세·주문·잔고·차트) |
| `myUpbit.py` | pyupbit 기반 저수준 유틸(지표·시장가 매수/매도·소량정리 등) |
| `indicator_calc.py` | ATR(N), 20MA, 5MA, 10일 신저가, N일 신고가 계산 |
| `trade_ledger.py` | 체결 원장 기록 + Google Sheets 동기화; SELL 체결 시 포트폴리오 추이·손익차트 즉시 갱신 (upsert) |
| `pnl_chart.py` | `손익차트` 시트·차트 갱신 모듈. 5개 단위(일/주/월/분기/년) 집계 데이터를 숨김 시트 `차트데이터` 에 저장하고, `손익차트` 시트는 F1 드롭다운 + ARRAYFORMULA + 콤보 차트 1개로 구성. **사용자가 F1 만 바꿔도 시트 함수가 즉시 차트 데이터를 다시 계산해 차트가 자동 전환됨** (Apps Script 불필요, 구글시트 기본 기능). 진입점: `update_pnl_chart()` (호환용 별칭 `run_pnl_chart`). |
| `telegram_alert.py` | 텔레그램 알림 단일 모듈 |
| `config.py` | `get_watchlist()` — `LOVELY_COIN_LIST` 고정 목록 반환 |
| `target_manager.py` | 터틀 S1/S2 신호 감지 + 눌림→재돌파 상태(peak) 관리 + unheld_coin_record.json 갱신 |
| `timer_agent.py` | 눌림→재돌파 조건 확인 + 진입 신호 산출 |
| `balance_sync.py` | 실행 시작 시 실제 잔고 ↔ `held_coin_record.json` 동기화; 수동 매수 코인 발견 시 1회 알림 후 `MANUAL_SYNC` 로 자동 편입. 잔고 불일치 발견 시 그 종목의 Upbit done 주문 중 **최근 15분 이내(crontab 주기 10분 + 여유 5분)** 이고 ledger 에 없는 거래를 `MANUAL_BUY`/`MANUAL_SELL` 로 자동 시트 기록 (코인명은 한글 `config.get_coin_name()` 사용; 수동 매수일 땐 평균가·손절가·피라미딩가도 함께 재계산) |
| `turtle_order_logic.py` | 리스크 기반 Unit 수량 계산, 피라미딩 주문 (`manual: true` 종목은 피라미딩 스킵) |
| `risk_guardian.py` | 2N 하드 손절 및 트레일링 스탑 감시 (5MA 익절은 어제 종가 기준·하루 1회) |
| `run_cache.py` | ATR 캐시 갱신 전담 스크립트 — KST 09:10 1회 실행, 일봉 지표를 `atr_cache.json` 에 저장 |
| `run_all.py` | 통합 배치 실행기 — 로그인 후 모든 모듈을 올바른 순서로 1회 실행 |
| `.env` | API 키·텔레그램·Google 설정 (커밋 금지) |
| `.env.example` | 환경변수 템플릿 |
| `requirements.txt` | 의존성 목록 (`pyupbit`, `gspread` 등) |
| `.gitignore` | 민감 파일·런타임 JSON 제외 규칙 |

**런타임 중 자동 생성되는 JSON 파일 (모두 `.gitignore` 대상, 커밋 금지):**

| 파일 | 내용 |
|------|------|
| `unheld_coin_record.json` | 미보유 코인의 터틀 신호(`turtle_s1/s2_signal`) 및 눌림→재돌파 상태(`peak_price`, `peak_locked`, `entry_ready`) |
| `held_coin_record.json` | 보유 코인의 Unit 수·마지막 매수가·평균단가·손절가(트레일링)·최고가·피라미딩 트리거가·분할익절 완료플래그(`tp_5_done`, `tp_10_done`) |
| `trade_ledger.json` | 누적 체결 원장 (Google Sheets 동기화 대상) |
| `daily_snapshot.json` | `run_daily.py` 의 하루 1회 포트폴리오 스냅샷 중복 방지 (`last_recorded_date` 필드). 매도 즉시 갱신 경로는 이 파일을 건드리지 않는다. |
| `atr_cache.json` | 일봉 기반 지표(ATR·5MA·ma5_prev·20MA·10일 신저가·prev_close) 하루 1회 캐시 — `run_cache.py` (KST 09:10) 가 저장 |
| `ma5_check_record.json` | 5MA 익절 "하루 1회" 가드 (`last_checked_date`). 그날 첫 실행에만 어제 종가 vs 5MA 비교를 수행하고 날짜를 기록 → 이후 실행은 5MA 익절 스킵 |

---

## 감시 코인 리스트 (`LOVELY_COIN_LIST`)

진입·감시·주문 대상은 **`config.LOVELY_COIN_LIST` 에 포함된 티커만**으로 한정한다.
리스트 밖 코인은 주문·상태 변경을 하지 않는다. 코인 식별자는 **`KRW-BTC`** 형태로 전역 통일한다.

| 코인명 | 티커 |
|--------|------|
| 비트코인 | `KRW-BTC` |
| 이더리움 | `KRW-ETH` |
| 리플 | `KRW-XRP` |
| 솔라나 | `KRW-SOL` |
| 도지코인 | `KRW-DOGE` |
| 에이다 | `KRW-ADA` |
| 알고랜드 | `KRW-ALGO` |
| 헤데라 | `KRW-HBAR` |
| 체인링크 | `KRW-LINK` |
| 수이 | `KRW-SUI` |
| 스텔라루멘 | `KRW-XLM` |

> 목록을 바꾸려면 `config.py` 의 `LOVELY_COIN_LIST` 를 직접 편집한다.
> 자동 스크리너(동적 종목 선정) 는 본 프로젝트에서 제공하지 않는다.

---

## 핵심 매매 로직

### 1. 진입 단계 (Entry)

**터틀 S1 / S2 신고가 돌파 + 눌림→재돌파** — 두 조건을 **AND** 로 모두 만족해야 진입한다.

- 직전 20일(S1) 또는 55일(S2) 장중 고가를 현재가가 돌파 (`turtle_s1/s2_signal = True`)
- 돌파 후 **한 번 눌렸다가 그 시점의 최고점을 다시 돌파** (`turtle_s1/s2_entry_ready = True`)

**상태 흐름 (target_manager → unheld_coin_record.json 에 저장):**
- `WATCHING`: 신호 ON 직후. 현재가로 최고값(`peak_price`) 계속 갱신. 현재가 < 최고값 → `PULLBACK` 으로 전환
- `PULLBACK`: 최고값 잠금(`peak_locked = True`). 현재가 > 최고값 → `entry_ready = True` (진입 신호)
- 신호 OFF (현재가 < 돌파 기준선) → 최고값·잠금·진입 신호 전체 초기화
- S1·S2 동시 해당 시 S2 우선 (`TURTLE_S2 > TURTLE_S1`)

### 2. 포지션 사이징 및 피라미딩

계좌의 변동성을 일정하게 관리하는 터틀 원칙을 적용한다.

- **N(ATR) 계산**: 최근 **20일** True Range 평균, 매 실행마다 갱신.
- **1 Unit 수량 결정** (터틀 정석 — 1% 리스크 + 최대 금액 상한):
  ```
  이론 1U 수량  = (총 자본 × RISK_PER_TRADE) / ATR(N)
  이론 1U 금액  = 이론 1U 수량 × 현재가
  1U 최대 금액  = 총 자본 × MAX_UNIT_KRW_RATIO  (기본 10%)
  ```
  - 이론 1U 금액 ≤ 1U 최대 금액 → 이론 수량 그대로 매수, `effective_risk_factor = RISK_PER_TRADE`
  - 이론 1U 금액 > 1U 최대 금액 → 1U 최대 금액으로 축소 매수,
    `effective_risk_factor = MAX_UNIT_KRW_RATIO × ATR / 현재가` (역산, 0.01 미만)
  - 종목마다 다른 `effective_risk_factor` 는 `held_coin_record.json` 에 저장.
  - 관련 상수: `RISK_PER_TRADE` (기본 `0.01`), `MAX_UNIT_KRW_RATIO` (기본 `0.10`)
- **최소 주문 금액 필터**: `수량 * 현재가 < 5,000원` 이면 해당 코인 주문 스킵 (Upbit KRW 마켓 최소 주문금액 제한).
- **피라미딩(불타기)**:
  - 마지막 매수가 대비 **0.5N 상승** 시마다 1 Unit 추가.
  - **코인당 최대 3 Unit**.
  - 전체 포트폴리오 **최대 15 Unit** (`turtle_order_logic.MAX_TOTAL_UNITS`).

### 3. 청산 및 손절 (Risk Management)

수익 보호와 손실 제한을 위해 트레일링 스탑과 하드 스탑을 병행한다.

- **트레일링 2N 손절**: 매 실행마다 `매수 후 최고가 - 2N` 을 손절가로 갱신. 가격이 올라갈수록 손절 기준도 함께 올라가 수익을 보호한다.
  (매수 직후에는 체결가 = 최고가이므로 기존 2N 손절과 동일. 이후 가격이 오르면 최고가 기준으로 자동 트레일링.)
- **분할 익절**:
  - **5% 분할 익절**: 평균 매입가 대비 수익률이 5% 이상 10% 미만일 때 보유량의 25% 시장가 매도. 포지션당 1회 제한 (`tp_5_done` 플래그).
  - **10% 분할 익절**: 평균 매입가 대비 수익률이 10% 이상일 때 보유량의 33% 시장가 매도. 포지션당 1회 제한 (`tp_10_done` 플래그).
  - **동시 충족 (갭상승 급등)**: 5% 익절 전에 바로 10% 이상으로 점프할 경우에도 10% 조건인 33%만 매도 처리하고 두 플래그 모두 `True` 설정하여 완료 처리.
- **트레일링 스탑**:
  - **10일 신저가 경신** → 추세 종료로 판단, 전량 청산. (매 실행 실시간 감시)
  - **어제 일봉 종가 < 5MA + 평균 매입단가 초과** (수익권) → 기술적 익절.
    - 장중 휴쓰(whipsaw) 방지를 위해 **어제 확정 일봉 종가**(`prev_close`)를 **어제까지 5MA**(`ma5_prev`, 오늘 미완성 봉 제외)와 비교한다.
    - 수익권 판단(`현재가 > avg_buy_price`)은 실시간 현재가로 한다.
    - 어제 종가·5MA는 하루 동안 변하지 않으므로, 업비트 일봉이 확정되는 09:00 이후 **그날 첫 실행(09:06)에만 하루 1회** 판단한다 (`ma5_check_record.json` 가드). 나머지 시간엔 5MA 익절을 스킵하고 10일 신저가·2N 손절만 계속 감시한다.

---

## 위험 관리 요약

| 항목 | 규칙 | 비고 |
|:-----|:-----|:-----|
| **최대 손실 제한** | 계좌 자산의 4% 이내 (2N 기준) | 단일 코인 기준 |
| **코인당 노출도** | 최대 3 Unit | 피라미딩 상한 |
| **포트폴리오 노출도** | 최대 15 Unit | 전체 Unit 합 상한 |
| **감시·주문 범위** | `LOVELY_COIN_LIST` 만 | 리스트 외 코인 미처리 |
| **최소 주문 금액** | 5,000 원 | Upbit KRW 마켓 제한 |

---

## Google Sheets 연동

**체결 원장(매매 기록)** 및 포트폴리오 스냅샷을 Google Sheets 에 반영한다.

- **인증**: Google Cloud **서비스 계정** JSON. `service_account.json` 은 **`.gitignore`** 에 포함하고 커밋하지 않는다.
- **환경 변수 (`.env`)**
  - `GOOGLE_SERVICE_ACCOUNT_JSON` — 서비스 계정 키 파일 경로
  - `GOOGLE_SPREADSHEET_TITLE` — 예: `업비트터틀_체결원장`
  - `GOOGLE_DRIVE_FOLDER_ID` — (선택) 특정 드라이브 폴더에 생성할 때
- **원장 진입점**: 모든 체결 기록은 **`trade_ledger.append_trade(record)`** 단일 경로로 남긴다.
- **의존성**: `gspread`, `oauth2client` (`requirements.txt` 참조).
- **포트폴리오 스냅샷 갱신 경로 (2가지)**:
  - **즉시 갱신**: SELL 체결 시 `append_trade()` 가 자동으로 `refresh_sheets_after_sell()` 호출 → 포트폴리오 추이·손익차트 즉시 upsert. 같은 날 여러 번 매도해도 날짜별 1줄 유지.
  - **하루 1회 갱신**: `run_daily.py` 가 `record_portfolio_snapshot()` 호출 → `daily_snapshot.json` 가드로 중복 방지. 이미 즉시 갱신으로 오늘 행이 있으면 그 행을 최신값으로 덮어씀.

---

## 공통 계약 (모든 모듈 준수)

- **코인 키:** `KRW-BTC` 형태의 Upbit 티커로 전역 통일
- **시간:** 사용자-facing 은 **KST** (`pytz`)
- **Upbit 접근:** `upbit_client` 만 경유 — 전략 파일에서 `pyupbit` 직접 호출 금지
- **체결 원장:** `trade_ledger.append_trade(record)` 단일 진입점
  `source` ∈ `ENTRY_30MIN` | `ENTRY_S1` | `ENTRY_S2` | `PYRAMID` | `EXIT_STOP` | `EXIT_10LOW` | `EXIT_5MA` | `EXIT_TP_5` | `EXIT_TP_10` | `EXIT_TP_BOTH` | `MANUAL_SYNC` | `MANUAL_BUY` | `MANUAL_SELL`
- **알림:** 텔레그램 봇 — `telegram_alert.SendMessage(msg)` 단일 모듈 경유
- **보안:** 키·서비스계정 JSON 커밋 금지 (`.env`, `.gitignore`)
- **실행:** 프로세스 내 상시 루프 금지. 스케줄링은 외부 `crontab` 이 담당.

---

## 체결 원장 스키마 (`trade_ledger.append_trade` record 필드)

| 필드명 | 타입 | 설명 |
|--------|------|------|
| `record_id` | string | 중복 방지 고유 ID (자동 생성) |
| `ts_kst` | string | `YYYY-MM-DD HH:MM:SS` (KST, 자동) |
| `account_id` | string | `.env` 의 `UPBIT_ACCOUNT_LABEL` 값 |
| `side` | string | `BUY` / `SELL` |
| `ticker` | string | 예: `KRW-BTC` |
| `coin_name` | string | 예: `비트코인` |
| `order_no` | string | Upbit 주문 uuid |
| `exec_no` | string | (선택) 체결번호 |
| `volume` | number | 코인 수량 (소수 허용) |
| `unit_price` | number | 단가 (원) |
| `gross_amount` | number | `volume × unit_price` (자동) |
| `fee` | number | (선택) 수수료 |
| `net_amount` | number | (선택) 실수령 금액 |
| `order_type` | string | `MARKET` / `LIMIT` |
| `source` | string | 위 매매구분 중 하나 |
| `profit_rate` | number | (SELL 전용) 수익률 % |
| `profit_amount` | number | (SELL 전용) 수익금 (원) |
| `note` | string | (선택) Unit 차수·손절/익절 구분 |

---

## 금지 사항

- 전략 파일에서 `pyupbit` 를 직접 호출 (반드시 `upbit_client` 경유)
- `record_id` 없이 원장 무한 증식
- API 키·시크릿을 로그·텔레그램·커밋에 노출
- `LOVELY_COIN_LIST` 외 티커에 주문·상태 변경 수행
- 프로세스 내부에 상시 감시 루프(`while True` 등) 추가 — 주기 실행은 `crontab` 으로만

---

## 실행 (crontab 기반)

```bash
cd upbit_hybrid_turtle
pip install -r requirements.txt
cp .env.example .env           # 값 채우기 (Upbit 키, 텔레그램, Google 등)

# 1회 전체 파이프라인 실행 (crontab 으로 10분 간격 호출 권장)
python run_all.py
```

**crontab 예시** (10분 간격):

```cron
*/10 * * * * cd /home/ubuntu/upbit_hybrid_turtle && /usr/bin/python3 run_all.py >> /home/ubuntu/upbit_hybrid_turtle/cron.log 2>&1
```

**개별 모듈 실행** (디버깅용):

```bash
python -c "import target_manager; target_manager.run_update()"
python -c "import timer_agent; print(timer_agent.run_timer_check())"
python -c "import turtle_order_logic as t; t.run_orders(__import__('timer_agent').run_timer_check())"
python -c "import risk_guardian; risk_guardian.run_guardian()"
```

> 실계좌 전용 모드로 운영된다. `.env` 에 `UPBIT_ACCESS_KEY` / `UPBIT_SECRET_KEY` 가 올바르게 설정되어 있어야 한다.

---

> 마지막 업데이트: 2026-05-30 (2N 손절을 트레일링 방식으로 교체: `risk_guardian.py` 가 매 실행마다 `매수 후 최고가 - 2N` 으로 `stop_loss_price` 를 갱신. `held_coin_record.json` 에 `high_price_since_entry` 필드 추가. crontab 주기 10분으로 통일.)
>
> 추가 업데이트: 5MA 익절 조건을 **실시간 현재가 < 5MA** → **어제 확정 일봉 종가(`prev_close`) < 어제까지 5MA(`ma5_prev`)** 기준으로 변경. 장중 휴쓰 방지를 위해 해당 조건은 09:00 이후 그날 첫 실행(09:06)에만 하루 1회 평가 (`ma5_check_record.json` 가드). `indicator_calc.py` 에 `ma5_prev`·`prev_close` 지표 추가, `atr_cache.json` 캐싱에도 반영. 수익권 판단(현재가 > 평균매입가)은 실시간 현재가 유지. 10일 신저가·2N 하드손절은 기존처럼 매 실행 실시간 감시.
>
> 신규 기능 추가: 평균 매입가 대비 **5% 상승 시 보유 수량의 25%**, **10% 상승 시 남은 수량의 33% 분할 익절** 조건 추가. 갭상승 등으로 5% 익절을 거치지 않고 바로 10% 이상이 되는 경우에도 10% 조건인 33%만 익절 매도하고 두 조건 모두 `True` 설정(포지션당 최대 1회씩만 동작). `risk_guardian.py`, `turtle_order_logic.py`, `balance_sync.py`, `trade_ledger.py` 에 반영 완료.

