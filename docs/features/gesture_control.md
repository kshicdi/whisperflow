# 제스처 컨트롤 현황

## 실행 방법

```bash
# 테스트 모드 (카메라 시각화 + 맥 제어)
python -m whisperflow.gesture_control --test --mac --camera 0

# 맥 제어 전용 (GUI 없음)
python -m whisperflow.gesture_control --mac --camera 0

# WebSocket 모드 (JARVIS UI 연동)
python -m whisperflow.gesture_control --camera 0
```

---

## 한 손 모드 (포즈 기반)

손가락 모양을 분류하여 즉시 발동. **준비 동작 없음**.

| 제스처 | 손 모양 | 맥 제어 | 문제점 |
|--------|---------|---------|--------|
| PALM_OPEN | 5개 손가락 모두 펴기 | 전체화면 토글 (Ctrl+Cmd+F) | 손을 카메라에 들기만 해도 발동 |
| FIST | 5개 손가락 모두 접기 | 최소화 (Cmd+M) | 손을 내리다가 주먹이 되면 오발동 |
| OK_SIGN | 엄지+검지 원, 나머지 펴기 | (미매핑) | - |
| PEACE | 검지+중지만 펴기 | (미매핑) | - |

### 한 손 발동 조건
- 제스처가 0.5초(GESTURE_HOLD_SECONDS) 유지되면 발동
- 같은 제스처가 연속 발동되지 않음 (손이 사라졌다 다시 나타나야 재발동)

### 문제: 준비 동작이 없다
- 카메라 앞에서 자연스럽게 손을 움직이면 의도치 않게 PALM_OPEN/FIST 발동
- 특히 FIST는 손을 내리거나 물건 잡을 때 오인식 위험
- 전체화면/최소화 같은 큰 동작이 너무 쉽게 발동됨

### 해결 아이디어 (논의 필요)
1. **트리거 제스처 방식**: 특정 제스처(예: OK_SIGN)를 먼저 해야 "제어 모드" 진입, 이후 PALM/FIST가 동작
2. **영역 제한**: 카메라 화면의 특정 영역(상단 등)에 손이 있을 때만 인식
3. **동작 추가 조건**: 포즈 + 모션 결합 (예: 손바닥 편 상태에서 앞으로 밀기)
4. **한 손은 맥 제어에서 제외**: 두 손 모드만 맥 제어, 한 손은 JARVIS UI 전용

---

## 두 손 모드 (모션 기반)

두 손이 감지되면 자동 전환. wrist(손목) 간 거리/위치 변화로 판정.

| 제스처 | 동작 | 맥 제어 | 판정 기준 |
|--------|------|---------|----------|
| SPREAD | 두 손 벌리기 | Mission Control 열기 (Ctrl+Up) | wrist 간 x거리 0.20 이상 증가 |
| GATHER | 두 손 모으기 | Mission Control 닫기 (Ctrl+Down) | wrist 간 x거리 0.20 이상 감소 |
| PUSH_DOWN | 두 손 아래로 | Show Desktop (Cmd+F11) | wrist 평균 y좌표 0.15 이상 증가 |
| PULL_UP | 두 손 위로 | Show Desktop 복귀 (Cmd+F11) | wrist 평균 y좌표 0.15 이상 감소 |

### 두 손 판정 로직
1. 매 프레임 wrist 간 거리(x)와 평균 y좌표를 히스토리에 저장 (최근 10프레임, ~0.33초)
2. 히스토리가 10프레임 쌓이면 첫 프레임 vs 마지막 프레임 변화량 비교
3. x축 변화 > y축 변화 → SPREAD/GATHER 판정
4. y축 변화 > x축 변화 → PUSH_DOWN/PULL_UP 판정
5. 발동 후 1초 쿨다운 (연속 오발동 방지)

### 장점
- "동작"을 해야 발동 → 오인식이 적음
- 손가락 모양 무관 → 손이 겹쳐도 동작
- 쿨다운으로 연속 발동 방지

---

## 상태 전환 다이어그램

```
[한 손 감지]                    [두 손 감지]
    │                              │
    ▼                              ▼
 포즈 분류                    모션 추적 시작
 (손가락 상태)                 (wrist 거리/위치)
    │                              │
    ▼                              ├─ x축 변화 큼 → SPREAD / GATHER
 PALM_OPEN                         ├─ y축 변화 큼 → PUSH_DOWN / PULL_UP
 FIST                              └─ 변화 작음 → None (대기)
 OK_SIGN
 PEACE
```

---

## 미구현 / 향후 논의

- SPREAD 세부 동작: 미션컨트롤식 vs 카드 배열식 정렬
- 한 손 제스처의 오발동 방지 방안 확정
- JARVIS UI에서 SPREAD/GATHER/PUSH_DOWN/PULL_UP 액션 처리
- 추가 제스처: 스와이프(좌/우), 포인팅(검지) 등

## 관련 파일

- `whisperflow/gesture_control.py` — 제스처 인식 + 맥 제어 전체
- `models/hand_landmarker.task` — MediaPipe 모델 (7.5MB)
- `whisperflow/static/jarvis.html` — UI 액션 수신 처리 (ui_action case)
