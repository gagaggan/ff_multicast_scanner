# Multicast Scanner for FlaskFarm

FlaskFarm에서 IPTV 멀티캐스트 UDP/RTP 주소를 작은 배치로 검색하고, 발견한 MPEG-TS 스트림을 I-Proxy 호환 JSON 또는 M3U로 내보내는 플러그인입니다.

## 주요 기능

- CIDR, IP 범위, 단일 주소 입력
- 중단 가능한 백그라운드 스캔과 진행률 표시
- MPEG-TS 동기 바이트 감지 후 `ffprobe` 메타데이터 검증
- I-Proxy 기존 채널 주소 읽기 및 중복 제외
- 결과 이름 수정 및 내보내기 포함 여부 관리
- I-Proxy가 가져올 수 있는 JSON/M3U API
- 배치 크기, 수신 시간, 최대 대상 수 안전 제한

## 설치

FlaskFarm 플러그인 설치 화면에서 다음 Git 저장소 URL을 사용합니다.

```text
https://github.com/gagaggan/ff_multicast_scanner
```

플러그인 컨테이너가 멀티캐스트를 수신할 수 있는 네트워크 모드여야 하며 `ffprobe`가 필요합니다. 현재 FlaskFarm 컨테이너가 호스트 네트워크를 사용한다면 별도 포트 매핑은 필요하지 않습니다.

## 기본 사용법

1. `스캔` 메뉴에서 범위와 포트를 입력합니다. 처음에는 `239.192.67.0/24`처럼 작은 범위로 시작하세요.
2. 실제 NIC 주소를 지정해야 한다면 `수신 인터페이스 IPv4`에 입력합니다. 기본값 `0.0.0.0`은 시스템의 기본 멀티캐스트 인터페이스를 사용합니다.
3. 스캔 후 `검색 결과`에서 채널명을 수정하고 사용할 항목을 선택합니다.
4. JSON 또는 M3U API URL을 I-Proxy의 `채널 목록 가져오기`에 입력합니다.

## I-Proxy 연동 원칙

스캐너는 기본 경로 `/data/db/ff_iproxy.db`를 **읽기 전용**으로 열어 기존 엔드포인트를 확인합니다. I-Proxy DB나 설정은 직접 수정하지 않습니다. 최종 반영은 I-Proxy의 정상 가져오기/저장 흐름을 사용합니다.

## 개발 및 검사

외부 Python 패키지는 필요하지 않습니다.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
git diff --check
```

## 주의

넓은 범위와 큰 배치 크기는 IPTV 회선과 VM에 순간적인 멀티캐스트 트래픽을 발생시킵니다. 작은 범위와 배치 크기 4 이하로 시작하고, 필요할 때 범위를 나눠 실행하세요.
