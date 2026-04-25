// Tesla Model X (2018) 핸들 맥북 트레이 마운트
// OpenSCAD 파일 — 3D 프린트용
//
// 사용법:
//   1. grip_diameter 값을 실제 측정값으로 수정
//   2. OpenSCAD에서 열고 STL 내보내기
//   3. 3D 프린터로 출력 (PLA 또는 PETG 권장)
//
// 구조: 핸들 좌우 그립에 끼우는 클램프 2개 + 상단 트레이 레일

// ========================================
// 파라미터 (실측 후 수정할 것)
// ========================================
grip_diameter = 30;        // 핸들 그립 직경 (mm) — 실측 필요!
grip_circumference = 95;   // 그립 둘레 (mm) — 줄자로 측정
tray_width = 300;          // 트레이 폭 (맥북 16인치 기준)
tray_depth = 200;          // 트레이 깊이
clamp_thickness = 5;       // 클램프 두께
clamp_height = 40;         // 클램프 높이 (핸들 감싸는 부분)
rail_height = 8;           // 레일 높이
wall = 3;                  // 벽 두께

// ========================================
// 모듈: 핸들 클램프 (C자형)
// ========================================
module handle_clamp() {
    inner_r = grip_diameter / 2;
    outer_r = inner_r + wall;

    difference() {
        // 외부 실린더
        cylinder(h=clamp_height, r=outer_r, $fn=64);

        // 내부 공간 (핸들 그립)
        translate([0, 0, -1])
            cylinder(h=clamp_height+2, r=inner_r, $fn=64);

        // C자 개구부 (위쪽 열림 — 핸들에 끼울 수 있게)
        translate([0, 0, -1])
            cube([outer_r*2+2, outer_r+2, clamp_height+2]);
    }

    // 개구부 양쪽 끝에 잠금 돌기 (스냅핏)
    for (angle = [60, 120]) {
        rotate([0, 0, angle])
            translate([inner_r + wall/2, 0, clamp_height/2])
                sphere(r=1.5, $fn=16);
    }

    // 상단 트레이 연결 브래킷
    translate([-outer_r, -outer_r-10, clamp_height])
        cube([outer_r*2, 10, rail_height]);
}

// ========================================
// 모듈: 트레이 레일
// ========================================
module tray_rail() {
    // 좌우 클램프를 연결하는 레일
    translate([0, 0, 0])
        cube([tray_width, 15, rail_height]);

    // 트레이 지지 립 (맥북 미끄럼 방지)
    // 앞쪽 스토퍼
    translate([0, 0, 0])
        cube([tray_width, wall, rail_height + 5]);

    // 뒤쪽 스토퍼
    translate([0, 15 - wall, 0])
        cube([tray_width, wall, rail_height + 5]);
}

// ========================================
// 모듈: 트레이 플랫폼 (옵션)
// ========================================
module tray_platform() {
    // 맥북 받침대 — 너무 크면 2분할 출력
    difference() {
        cube([tray_width, tray_depth, wall]);

        // 무게 절감 + 통풍을 위한 격자 패턴
        for (x = [20 : 30 : tray_width-20]) {
            for (y = [20 : 30 : tray_depth-20]) {
                translate([x, y, -1])
                    cylinder(h=wall+2, r=8, $fn=6);
            }
        }
    }

    // 앞쪽 스토퍼 (맥북 미끄럼 방지)
    translate([0, 0, 0])
        cube([tray_width, wall, 15]);

    // 뒤쪽 스토퍼
    translate([0, tray_depth - wall, 0])
        cube([tray_width, wall, 10]);

    // 좌우 가이드
    cube([wall, tray_depth, 10]);
    translate([tray_width - wall, 0, 0])
        cube([wall, tray_depth, 10]);
}

// ========================================
// 조립 미리보기
// ========================================

// 좌측 클램프
translate([0, 0, 0])
    handle_clamp();

// 우측 클램프
translate([tray_width, 0, 0])
    handle_clamp();

// 연결 레일 (클램프 상단)
translate([0, -(grip_diameter/2 + wall + 10), clamp_height])
    tray_rail();

// 트레이 플랫폼 (레일 위)
color("lightblue", 0.5)
translate([0, -(grip_diameter/2 + wall + 10), clamp_height + rail_height])
    tray_platform();

// ========================================
// 출력 가이드
// ========================================
//
// 1. 클램프 2개: 각각 별도 출력
//    - 인필 40% 이상 (강도 중요)
//    - PETG 권장 (PLA는 차량 내부 열에 변형 가능)
//    - 서포트 필요 (C자 개구부)
//
// 2. 레일: 1개 출력
//    - 인필 30%
//    - 트레이 폭이 300mm 넘으면 2분할
//
// 3. 트레이: 1개 출력 (옵션)
//    - 크기가 크면 2~4분할 출력 후 접착
//    - 또는 기존 트레이/보드를 레일 위에 올려도 됨
//
// 조립:
//    클램프를 핸들 좌우 그립에 끼움
//    → 레일을 클램프 상단 브래킷에 볼트 고정
//    → 트레이를 레일 위에 올림
